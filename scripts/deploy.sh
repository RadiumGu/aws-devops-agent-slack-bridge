#!/usr/bin/env bash
# DevOps Agent demo — deploy script.
# Reads configuration from .env (copy from .env.example).
#
# Prerequisites:
#   - AWS CLI v2 with credentials for the account in .env
#   - Python 3.12 + pip on the local machine
#   - jq, zip, unzip
#   - boto3 >= 1.43.0 (DevOps Agent client landed in 1.43.0). Lambda's
#     built-in boto3 is older, so the script always bundles a fresh boto3
#     in the layer.
#
# Usage:
#   cp .env.example .env  &&  edit .env
#   ./scripts/deploy.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
BUILD_DIR="${ROOT_DIR}/.build"
mkdir -p "${BUILD_DIR}"

# --------------------------------------------------------------------------
# 0. Load .env
# --------------------------------------------------------------------------
if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found. Copy .env.example to .env and fill it in."
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

# Required vars
for var in AWS_ACCOUNT_ID AWS_REGION DEVOPS_AGENT_SPACE_ID \
           SLACK_WEBHOOK_URL SLACK_CHANNEL ROLE_NAME LAMBDA_A_NAME \
           LAMBDA_B_NAME LAYER_NAME RULE_1_NAME RULE_2_NAME; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: ${var} is not set in ${ENV_FILE}"
    exit 1
  fi
done

BOTO3_MIN_VERSION="${BOTO3_MIN_VERSION:-1.43.0}"
NOTIFY_DLQ_NAME="${NOTIFY_DLQ_NAME:-devops-agent-notify-dlq}"
TRIGGER_DLQ_NAME="${TRIGGER_DLQ_NAME:-devops-agent-trigger-dlq}"
SLACK_WEBHOOK_SECRET_NAME="${SLACK_WEBHOOK_SECRET_NAME:-devops-agent/slack-webhook-url}"

echo "==> Account=${AWS_ACCOUNT_ID}  Region=${AWS_REGION}  AgentSpace=${DEVOPS_AGENT_SPACE_ID}"

# --------------------------------------------------------------------------
# 1. Verify caller identity
# --------------------------------------------------------------------------
echo "==> [1/9] Verifying caller identity..."
ACTUAL_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [ "${ACTUAL_ACCOUNT}" != "${AWS_ACCOUNT_ID}" ]; then
  echo "ERROR: caller is in account ${ACTUAL_ACCOUNT}, expected ${AWS_ACCOUNT_ID}"
  exit 1
fi

# --------------------------------------------------------------------------
# 2. IAM role + policies
# --------------------------------------------------------------------------
echo "==> [2/9] Creating IAM role ${ROLE_NAME}..."
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${ROOT_DIR}/iam/lambda-role-trust.json" >/dev/null
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "    Sleeping 10s for IAM propagation..."
  sleep 10
else
  echo "    Role exists — skipping create."
fi

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name DevOpsAgentAccess \
  --policy-document "file://${ROOT_DIR}/iam/devops-agent-policy.json"

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"

# --------------------------------------------------------------------------
# 2a. Slack webhook secret (P0-6) — store webhook URL in Secrets Manager
# --------------------------------------------------------------------------
echo "==> [2a] Ensuring Slack webhook secret ${SLACK_WEBHOOK_SECRET_NAME}..."
if SECRET_ARN=$(aws secretsmanager describe-secret --region "${AWS_REGION}" \
    --secret-id "${SLACK_WEBHOOK_SECRET_NAME}" \
    --query 'ARN' --output text 2>/dev/null); then
  echo "    Secret exists — updating value (idempotent put-secret-value)"
  aws secretsmanager put-secret-value --region "${AWS_REGION}" \
    --secret-id "${SLACK_WEBHOOK_SECRET_NAME}" \
    --secret-string "${SLACK_WEBHOOK_URL}" >/dev/null
else
  SECRET_ARN=$(aws secretsmanager create-secret --region "${AWS_REGION}" \
    --name "${SLACK_WEBHOOK_SECRET_NAME}" \
    --description "Slack Incoming Webhook URL for devops-agent-notify-slack (P0-6)" \
    --secret-string "${SLACK_WEBHOOK_URL}" \
    --query 'ARN' --output text)
  echo "    Created secret"
fi
echo "    Secret ARN: ${SECRET_ARN}"

# IAM: allow Lambda role to read this secret
NOTIFY_SECRET_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadSlackWebhookSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "${SECRET_ARN}"
    }
  ]
}
JSON
)
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name NotifySlackSecretAccess \
  --policy-document "${NOTIFY_SECRET_POLICY}" >/dev/null
echo "    IAM policy NotifySlackSecretAccess attached to ${ROLE_NAME}"

# --------------------------------------------------------------------------
# 2b. Lambda-B DLQ (P0-5) — SQS queue for async-invoke failures
# --------------------------------------------------------------------------
echo "==> [2b] Ensuring Lambda-B DLQ ${NOTIFY_DLQ_NAME}..."
NOTIFY_DLQ_URL=$(aws sqs create-queue --region "${AWS_REGION}" \
  --queue-name "${NOTIFY_DLQ_NAME}" \
  --attributes '{"MessageRetentionPeriod":"1209600"}' \
  --query 'QueueUrl' --output text)
NOTIFY_DLQ_ARN=$(aws sqs get-queue-attributes --region "${AWS_REGION}" \
  --queue-url "${NOTIFY_DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)
echo "    DLQ ARN: ${NOTIFY_DLQ_ARN}"

NOTIFY_DLQ_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendToNotifyDLQ",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${NOTIFY_DLQ_ARN}"
    }
  ]
}
JSON
)
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name NotifySlackDLQAccess \
  --policy-document "${NOTIFY_DLQ_POLICY}" >/dev/null
echo "    IAM policy NotifySlackDLQAccess attached to ${ROLE_NAME}"

# --------------------------------------------------------------------------
# 2c. Lambda-A DLQ (P0-24) — SQS queue for async-invoke failures
# --------------------------------------------------------------------------
echo "==> [2c] Ensuring Lambda-A DLQ ${TRIGGER_DLQ_NAME}..."
TRIGGER_DLQ_URL=$(aws sqs create-queue --region "${AWS_REGION}" \
  --queue-name "${TRIGGER_DLQ_NAME}" \
  --attributes '{"MessageRetentionPeriod":"1209600"}' \
  --query 'QueueUrl' --output text)
TRIGGER_DLQ_ARN=$(aws sqs get-queue-attributes --region "${AWS_REGION}" \
  --queue-url "${TRIGGER_DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)
echo "    DLQ ARN: ${TRIGGER_DLQ_ARN}"

TRIGGER_DLQ_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendToTriggerDLQ",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${TRIGGER_DLQ_ARN}"
    }
  ]
}
JSON
)
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name TriggerInvestigationDLQAccess \
  --policy-document "${TRIGGER_DLQ_POLICY}" >/dev/null
echo "    IAM policy TriggerInvestigationDLQAccess attached to ${ROLE_NAME}"

# --------------------------------------------------------------------------
# 3. Build & publish boto3 layer
# --------------------------------------------------------------------------
# Lambda's built-in boto3 is older than 1.43.0 and does not include the
# devops-agent client. Bundle a fresh boto3 in a layer.
echo "==> [3/9] Building boto3 layer (>=${BOTO3_MIN_VERSION})..."
LAYER_BUILD="${BUILD_DIR}/boto3-layer"
rm -rf "${LAYER_BUILD}"
mkdir -p "${LAYER_BUILD}/python"

pip install --quiet --upgrade "boto3>=${BOTO3_MIN_VERSION}" \
  -t "${LAYER_BUILD}/python"

# Verify the model is present in the freshly-installed boto3
if [ ! -d "${LAYER_BUILD}/python/botocore/data/devops-agent" ]; then
  echo "    ❌ boto3 in layer does not contain devops-agent model."
  echo "       This means pip resolved an older version of boto3."
  echo "       Check 'pip --version' and your index, then retry."
  exit 1
fi
echo "    boto3 OK with devops-agent model:"
ls "${LAYER_BUILD}/python/botocore/data/devops-agent/" | sed 's/^/      /'

( cd "${LAYER_BUILD}" && zip -qr "${BUILD_DIR}/boto3-layer.zip" python/ )

LAYER_ARN=$(aws lambda publish-layer-version \
  --region "${AWS_REGION}" \
  --layer-name "${LAYER_NAME}" \
  --zip-file "fileb://${BUILD_DIR}/boto3-layer.zip" \
  --compatible-runtimes python3.12 \
  --query 'LayerVersionArn' --output text)
echo "    Layer ARN: ${LAYER_ARN}"

# --------------------------------------------------------------------------
# 4. Ensure DynamoDB threads table (P1-1 alarm idempotency)
# --------------------------------------------------------------------------
# Same single-table that the chatbot uses (different partition-key prefix
# per record class). We create it here too so the alarm pipeline doesn't
# depend on the chatbot deploy having run.
echo "==> [4/9] Ensuring DynamoDB threads table for alarm idempotency..."
DDB_TABLE_NAME="${THREAD_TABLE_NAME:-devops-agent-slack-threads}"
if aws dynamodb describe-table --region "${AWS_REGION}" \
     --table-name "${DDB_TABLE_NAME}" >/dev/null 2>&1; then
  echo "    Table exists: ${DDB_TABLE_NAME}"
else
  aws dynamodb create-table --region "${AWS_REGION}" \
    --table-name "${DDB_TABLE_NAME}" \
    --attribute-definitions AttributeName=thread_ts,AttributeType=S \
    --key-schema AttributeName=thread_ts,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --region "${AWS_REGION}" \
    --table-name "${DDB_TABLE_NAME}"
  echo "    Created table: ${DDB_TABLE_NAME}"
fi
TTL_STATUS=$(aws dynamodb describe-time-to-live --region "${AWS_REGION}" \
  --table-name "${DDB_TABLE_NAME}" \
  --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null || echo "DISABLED")
if [ "${TTL_STATUS}" != "ENABLED" ] && [ "${TTL_STATUS}" != "ENABLING" ]; then
  aws dynamodb update-time-to-live --region "${AWS_REGION}" \
    --table-name "${DDB_TABLE_NAME}" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" >/dev/null
  echo "    Enabled TTL on ttl attribute"
else
  echo "    TTL already ${TTL_STATUS}"
fi

# --------------------------------------------------------------------------
# 5. Package & deploy Lambda-A (trigger investigation)
# --------------------------------------------------------------------------
echo "==> [5/9] Deploying ${LAMBDA_A_NAME}..."
( cd "${ROOT_DIR}/lambda/lambda_a" && zip -qj "${BUILD_DIR}/lambda_a.zip" lambda_function.py )

if aws lambda get-function --region "${AWS_REGION}" --function-name "${LAMBDA_A_NAME}" >/dev/null 2>&1; then
  aws lambda update-function-code --region "${AWS_REGION}" \
    --function-name "${LAMBDA_A_NAME}" \
    --zip-file "fileb://${BUILD_DIR}/lambda_a.zip" >/dev/null
  aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_A_NAME}"
  aws lambda update-function-configuration --region "${AWS_REGION}" \
    --function-name "${LAMBDA_A_NAME}" \
    --layers "${LAYER_ARN}" \
    --environment "Variables={DEVOPS_AGENT_SPACE_ID=${DEVOPS_AGENT_SPACE_ID},THREAD_TABLE_NAME=${DDB_TABLE_NAME}}" >/dev/null
else
  aws lambda create-function --region "${AWS_REGION}" \
    --function-name "${LAMBDA_A_NAME}" \
    --runtime python3.12 \
    --handler lambda_function.lambda_handler \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${BUILD_DIR}/lambda_a.zip" \
    --timeout 30 --memory-size 128 \
    --layers "${LAYER_ARN}" \
    --environment "Variables={DEVOPS_AGENT_SPACE_ID=${DEVOPS_AGENT_SPACE_ID},THREAD_TABLE_NAME=${DDB_TABLE_NAME}}" >/dev/null
fi

# P0-24: attach SQS DLQ for async-invoke failures (EventBridge → Lambda is async).
# IAM propagation can lag a few seconds; retry briefly.
echo "    Attaching DLQ ${TRIGGER_DLQ_ARN} to ${LAMBDA_A_NAME}..."
aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_A_NAME}"
for attempt in 1 2 3 4 5; do
  if aws lambda update-function-configuration --region "${AWS_REGION}" \
       --function-name "${LAMBDA_A_NAME}" \
       --dead-letter-config "TargetArn=${TRIGGER_DLQ_ARN}" >/dev/null 2>&1; then
    echo "    DLQ → ${TRIGGER_DLQ_ARN}"
    break
  fi
  if [ "${attempt}" = "5" ]; then
    echo "ERROR: update-function-configuration --dead-letter-config failed after 5 attempts"
    aws lambda update-function-configuration --region "${AWS_REGION}" \
      --function-name "${LAMBDA_A_NAME}" \
      --dead-letter-config "TargetArn=${TRIGGER_DLQ_ARN}"
    exit 1
  fi
  sleep 3
done
aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_A_NAME}"

# --------------------------------------------------------------------------
# 5. Package & deploy Lambda-B (Slack notifier via webhook)
# --------------------------------------------------------------------------
echo "==> [6/9] Deploying ${LAMBDA_B_NAME}..."
( cd "${ROOT_DIR}/lambda/lambda_b" && zip -qj "${BUILD_DIR}/lambda_b.zip" lambda_function.py )

# P0-6: webhook URL is now in Secrets Manager. Lambda gets only the secret ARN.
# (No SLACK_WEBHOOK_URL plaintext in env vars.)
LAMBDA_B_ENV="Variables={SLACK_WEBHOOK_SECRET_ARN=${SECRET_ARN},SLACK_CHANNEL=${SLACK_CHANNEL}}"

if aws lambda get-function --region "${AWS_REGION}" --function-name "${LAMBDA_B_NAME}" >/dev/null 2>&1; then
  aws lambda update-function-code --region "${AWS_REGION}" \
    --function-name "${LAMBDA_B_NAME}" \
    --zip-file "fileb://${BUILD_DIR}/lambda_b.zip" >/dev/null
  aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_B_NAME}"
  aws lambda update-function-configuration --region "${AWS_REGION}" \
    --function-name "${LAMBDA_B_NAME}" \
    --layers "${LAYER_ARN}" \
    --environment "${LAMBDA_B_ENV}" >/dev/null
else
  aws lambda create-function --region "${AWS_REGION}" \
    --function-name "${LAMBDA_B_NAME}" \
    --runtime python3.12 \
    --handler lambda_function.lambda_handler \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${BUILD_DIR}/lambda_b.zip" \
    --timeout 60 --memory-size 128 \
    --layers "${LAYER_ARN}" \
    --environment "${LAMBDA_B_ENV}" >/dev/null
fi

# P0-5: attach SQS DLQ for async-invoke failures (EventBridge → Lambda is async).
# IAM propagation can lag a few seconds; retry briefly.
echo "    Attaching DLQ ${NOTIFY_DLQ_ARN} to ${LAMBDA_B_NAME}..."
aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_B_NAME}"
for attempt in 1 2 3 4 5; do
  if aws lambda update-function-configuration --region "${AWS_REGION}" \
       --function-name "${LAMBDA_B_NAME}" \
       --dead-letter-config "TargetArn=${NOTIFY_DLQ_ARN}" >/dev/null 2>&1; then
    echo "    DLQ → ${NOTIFY_DLQ_ARN}"
    break
  fi
  if [ "${attempt}" = "5" ]; then
    echo "ERROR: update-function-configuration --dead-letter-config failed after 5 attempts"
    aws lambda update-function-configuration --region "${AWS_REGION}" \
      --function-name "${LAMBDA_B_NAME}" \
      --dead-letter-config "TargetArn=${NOTIFY_DLQ_ARN}"
    exit 1
  fi
  sleep 3
done
aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_B_NAME}"

# --------------------------------------------------------------------------
# 6. EventBridge Rule-1: any CloudWatch Alarm → Lambda-A
# --------------------------------------------------------------------------
echo "==> [7/9] Creating EventBridge rule ${RULE_1_NAME}..."
aws events put-rule --region "${AWS_REGION}" \
  --name "${RULE_1_NAME}" \
  --event-pattern "file://${ROOT_DIR}/eventbridge/rule-1-alarm-to-lambda-pattern.json" >/dev/null

LAMBDA_A_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_A_NAME}"
aws lambda add-permission --region "${AWS_REGION}" \
  --function-name "${LAMBDA_A_NAME}" \
  --statement-id allow-eventbridge-rule-1 \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${AWS_REGION}:${AWS_ACCOUNT_ID}:rule/${RULE_1_NAME}" \
  >/dev/null 2>&1 || true

aws events put-targets --region "${AWS_REGION}" \
  --rule "${RULE_1_NAME}" \
  --targets "Id=trigger-investigation,Arn=${LAMBDA_A_ARN}" >/dev/null

# --------------------------------------------------------------------------
# 7. EventBridge Rule-2: Investigation Completed → Lambda-B
# --------------------------------------------------------------------------
echo "==> [8/9] Creating EventBridge rule ${RULE_2_NAME}..."
aws events put-rule --region "${AWS_REGION}" \
  --name "${RULE_2_NAME}" \
  --event-pattern "file://${ROOT_DIR}/eventbridge/rule-2-investigation-completed-pattern.json" >/dev/null

LAMBDA_B_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_B_NAME}"
aws lambda add-permission --region "${AWS_REGION}" \
  --function-name "${LAMBDA_B_NAME}" \
  --statement-id allow-eventbridge-rule-2 \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${AWS_REGION}:${AWS_ACCOUNT_ID}:rule/${RULE_2_NAME}" \
  >/dev/null 2>&1 || true

aws events put-targets --region "${AWS_REGION}" \
  --rule "${RULE_2_NAME}" \
  --targets "Id=notify-slack,Arn=${LAMBDA_B_ARN}" >/dev/null

# --------------------------------------------------------------------------
# 8. Summary
# --------------------------------------------------------------------------
echo "==> [9/9] Deployment complete."
cat <<EOF

  Account:        ${AWS_ACCOUNT_ID}
  Region:         ${AWS_REGION}
  Agent Space:    ${DEVOPS_AGENT_SPACE_ID}
  Slack channel:  ${SLACK_CHANNEL}
  Webhook secret: ${SECRET_ARN}

  Lambda-A:       ${LAMBDA_A_ARN}
  Lambda-A DLQ:   ${TRIGGER_DLQ_ARN}
  Lambda-B:       ${LAMBDA_B_ARN}
  Lambda-B DLQ:   ${NOTIFY_DLQ_ARN}
  Rule-1:         ${RULE_1_NAME} (alarmName prefix=petsite-)
  Rule-2:         ${RULE_2_NAME}

  Next:
    1. Trigger a test alarm: ./scripts/test-trigger.sh <ec2-instance-id>
    2. Watch CloudWatch Logs:
       aws logs tail /aws/lambda/${LAMBDA_A_NAME} --follow --region ${AWS_REGION}
       aws logs tail /aws/lambda/${LAMBDA_B_NAME} --follow --region ${AWS_REGION}
    3. (Optional) Provision dashboard: ./scripts/setup-dashboard.sh
EOF

# --------------------------------------------------------------------------
# Optional: provision CloudWatch dashboard (P1-16). Skip via SKIP_DASHBOARD=1
# --------------------------------------------------------------------------
if [ "${SKIP_DASHBOARD:-0}" != "1" ] && [ -x "${ROOT_DIR}/scripts/setup-dashboard.sh" ]; then
  echo "==> Provisioning CloudWatch dashboard (set SKIP_DASHBOARD=1 to skip)..."
  "${ROOT_DIR}/scripts/setup-dashboard.sh" || \
    echo "    WARN: setup-dashboard.sh failed; continuing"
fi
