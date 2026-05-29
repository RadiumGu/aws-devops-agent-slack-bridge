#!/usr/bin/env bash
# Deploy Slack Chatbot Lambda + API Gateway HTTP API.
# Reuses existing IAM role DevOpsAgentDemoLambdaRole and boto3 layer.
#
# Usage:
#   ./scripts/deploy-chatbot.sh
#
# Outputs the API Gateway Invoke URL for Slack Event Subscriptions config.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
BUILD_DIR="${ROOT_DIR}/.build"
mkdir -p "${BUILD_DIR}"

if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found"
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

# Required base config (already in .env from earlier deploy)
: "${AWS_ACCOUNT_ID:?}"
: "${AWS_REGION:?}"
: "${DEVOPS_AGENT_SPACE_ID:?}"
: "${ROLE_NAME:?}"
: "${LAYER_NAME:?}"

# Chatbot-specific config
LAMBDA_C_NAME="${LAMBDA_C_NAME:-devops-agent-slack-chatbot}"
SLACK_BOT_TOKEN_SECRET_ID="${SLACK_BOT_TOKEN_SECRET_ID:-devops-agent/slack-chatbot-token}"
SLACK_SIGNING_SECRET_ID="${SLACK_SIGNING_SECRET_ID:-devops-agent/slack-signing-secret}"
SLACK_TEST_CHANNEL="${SLACK_TEST_CHANNEL:?SLACK_TEST_CHANNEL must be set in .env}"
API_NAME="${API_NAME:-devops-agent-slack-chatbot-api}"
RESERVED_CONCURRENCY="${RESERVED_CONCURRENCY:-5}"
DLQ_NAME="${DLQ_NAME:-devops-agent-slack-chatbot-dlq}"

echo "==> Account=${AWS_ACCOUNT_ID}  Region=${AWS_REGION}  Lambda=${LAMBDA_C_NAME}"

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_C_NAME}"

# --------------------------------------------------------------------------
# 1. Verify caller
# --------------------------------------------------------------------------
echo "==> [1/10] Verifying caller identity..."
ACTUAL=$(aws sts get-caller-identity --query Account --output text)
[ "${ACTUAL}" = "${AWS_ACCOUNT_ID}" ] || {
  echo "ERROR: caller is in account ${ACTUAL}, expected ${AWS_ACCOUNT_ID}"
  exit 1
}

# --------------------------------------------------------------------------
# 2. Attach SlackChatbotAccess policy to existing role
# --------------------------------------------------------------------------
echo "==> [2/10] Updating IAM role with SlackChatbotAccess policy..."
# Render template (account/region come from .env, kept out of git)
mkdir -p "${BUILD_DIR}/iam"
RENDERED_POLICY="${BUILD_DIR}/iam/chatbot-policy.json"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID}" AWS_REGION="${AWS_REGION}" \
  envsubst '${AWS_ACCOUNT_ID} ${AWS_REGION}' \
  < "${ROOT_DIR}/iam/chatbot-policy.json.template" \
  > "${RENDERED_POLICY}"
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name SlackChatbotAccess \
  --policy-document "file://${RENDERED_POLICY}"

# --------------------------------------------------------------------------
# 3. Look up boto3 layer ARN
# --------------------------------------------------------------------------
echo "==> [3/10] Resolving boto3 layer..."
LAYER_ARN=$(aws lambda list-layer-versions --region "${AWS_REGION}" \
  --layer-name "${LAYER_NAME}" \
  --query 'LayerVersions[0].LayerVersionArn' --output text)
echo "    Layer: ${LAYER_ARN}"

# --------------------------------------------------------------------------
# 4. Package & deploy Lambda-C
# --------------------------------------------------------------------------
echo "==> [4/10] Packaging Lambda-C..."
ZIP="${BUILD_DIR}/lambda_c.zip"
rm -f "${ZIP}"
# zip -j flattens paths so agent_chat.py lands at the zip root next to
# lambda_function.py — required for `import agent_chat` to resolve.
( cd "${ROOT_DIR}/lambda/lambda_c" && \
  zip -qj "${ZIP}" lambda_function.py slack_verify.py )
zip -qj "${ZIP}" "${ROOT_DIR}/lib/agent_chat.py"

ENV_VARS="DEVOPS_AGENT_SPACE_ID=${DEVOPS_AGENT_SPACE_ID}"
ENV_VARS+=",SLACK_BOT_TOKEN_SECRET_ID=${SLACK_BOT_TOKEN_SECRET_ID}"
ENV_VARS+=",SLACK_SIGNING_SECRET_ID=${SLACK_SIGNING_SECRET_ID}"

if aws lambda get-function --region "${AWS_REGION}" --function-name "${LAMBDA_C_NAME}" >/dev/null 2>&1; then
  aws lambda update-function-code --region "${AWS_REGION}" \
    --function-name "${LAMBDA_C_NAME}" \
    --zip-file "fileb://${ZIP}" >/dev/null
  aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_C_NAME}"
  aws lambda update-function-configuration --region "${AWS_REGION}" \
    --function-name "${LAMBDA_C_NAME}" \
    --layers "${LAYER_ARN}" \
    --timeout 300 \
    --memory-size 512 \
    --environment "Variables={${ENV_VARS}}" >/dev/null
  echo "    Updated existing function"
else
  aws lambda create-function --region "${AWS_REGION}" \
    --function-name "${LAMBDA_C_NAME}" \
    --runtime python3.12 \
    --handler lambda_function.lambda_handler \
    --role "${ROLE_ARN}" \
    --zip-file "fileb://${ZIP}" \
    --timeout 300 --memory-size 512 \
    --layers "${LAYER_ARN}" \
    --environment "Variables={${ENV_VARS}}" >/dev/null
  echo "    Created new function"
fi

# --------------------------------------------------------------------------
# 5. Ensure DynamoDB threads table (P0-1)
# --------------------------------------------------------------------------
# Lambda-C reads/writes thread state via this table; the IAM policy + the
# CloudWatch DDB-throttle alarm below already reference its name. Create it
# here so a fresh deploy is fully self-contained (no manual aws CLI step).
echo "==> [5/10] Ensuring DynamoDB threads table..."
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
# Enable TTL on the `ttl` attribute (idempotent; describe + skip if already on).
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
# 6. Create / reuse API Gateway HTTP API (P0-3: no --target shortcut)
# --------------------------------------------------------------------------
echo "==> [6/10] Setting up API Gateway HTTP API..."

API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)

if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
  # Avoid `--target`: it auto-creates an ANY catch-all route + $default
  # integration that will accept any HTTP method/path. We want exactly one
  # explicit route (POST /slack/events) to minimise unauthenticated
  # invocation surface (Slack signing check still runs, but unauthenticated
  # traffic would still consume Lambda invocations).
  API_ID=$(aws apigatewayv2 create-api --region "${AWS_REGION}" \
    --name "${API_NAME}" \
    --protocol-type HTTP \
    --query 'ApiId' --output text)
  echo "    Created API: ${API_ID}"
else
  echo "    Reusing API: ${API_ID}"
  # If a previous deploy used --target, an ANY catch-all route from that run
  # may still exist. Remove any non-POST-/slack/events routes (idempotent).
  for stale in $(aws apigatewayv2 get-routes --region "${AWS_REGION}" \
      --api-id "${API_ID}" \
      --query "Items[?RouteKey!='POST /slack/events'].RouteId" \
      --output text); do
    [ -n "${stale}" ] && [ "${stale}" != "None" ] || continue
    aws apigatewayv2 delete-route --region "${AWS_REGION}" \
      --api-id "${API_ID}" --route-id "${stale}" >/dev/null
    echo "    Removed stale route: ${stale}"
  done
fi

# Ensure an explicit POST /slack/events route. Without --target there is no
# default integration/route, so we always create both.
INTEG_ID=$(aws apigatewayv2 get-integrations --region "${AWS_REGION}" \
  --api-id "${API_ID}" \
  --query "Items[?IntegrationUri=='${LAMBDA_ARN}'].IntegrationId | [0]" \
  --output text)

if [ "${INTEG_ID}" = "None" ] || [ -z "${INTEG_ID}" ]; then
  INTEG_ID=$(aws apigatewayv2 create-integration --region "${AWS_REGION}" \
    --api-id "${API_ID}" \
    --integration-type AWS_PROXY \
    --integration-uri "${LAMBDA_ARN}" \
    --payload-format-version "2.0" \
    --query 'IntegrationId' --output text)
fi

# Idempotent route create (ignore "already exists")
aws apigatewayv2 create-route --region "${AWS_REGION}" \
  --api-id "${API_ID}" \
  --route-key "POST /slack/events" \
  --target "integrations/${INTEG_ID}" >/dev/null 2>&1 || \
  echo "    POST /slack/events route already exists"

# Default stage with auto-deploy (no longer comes for free without --target)
aws apigatewayv2 get-stage --region "${AWS_REGION}" \
  --api-id "${API_ID}" --stage-name '$default' >/dev/null 2>&1 || \
  aws apigatewayv2 create-stage --region "${AWS_REGION}" \
    --api-id "${API_ID}" --stage-name '$default' --auto-deploy >/dev/null
aws apigatewayv2 update-stage --region "${AWS_REGION}" \
  --api-id "${API_ID}" --stage-name '$default' \
  --auto-deploy >/dev/null 2>&1 || true

# Lambda permission to allow API GW invocation
aws lambda add-permission --region "${AWS_REGION}" \
  --function-name "${LAMBDA_C_NAME}" \
  --statement-id "allow-apigw-${API_ID}" \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*/*" \
  >/dev/null 2>&1 || echo "    Lambda permission already exists"

INVOKE_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/slack/events"

# --------------------------------------------------------------------------
# 7. Reserved concurrency (P0-2): cap blast radius if URL leaks
# --------------------------------------------------------------------------
echo "==> [7/10] Setting reserved concurrency to ${RESERVED_CONCURRENCY}..."
aws lambda put-function-concurrency --region "${AWS_REGION}" \
  --function-name "${LAMBDA_C_NAME}" \
  --reserved-concurrent-executions "${RESERVED_CONCURRENCY}" >/dev/null
echo "    Reserved concurrency: ${RESERVED_CONCURRENCY}"

# --------------------------------------------------------------------------
# 8. SQS DLQ + OnFailure destination (P0-4): catch async-invoke drops
# --------------------------------------------------------------------------
echo "==> [8/10] Configuring SQS DLQ for async invoke failures..."

# Create or reuse DLQ (idempotent — create-queue is no-op if attrs match)
DLQ_URL=$(aws sqs create-queue --region "${AWS_REGION}" \
  --queue-name "${DLQ_NAME}" \
  --attributes '{"MessageRetentionPeriod":"1209600"}' \
  --query 'QueueUrl' --output text)
DLQ_ARN=$(aws sqs get-queue-attributes --region "${AWS_REGION}" \
  --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)
echo "    DLQ ARN: ${DLQ_ARN}"

# Grant Lambda role permission to send to the DLQ (idempotent put-role-policy)
DLQ_POLICY_DOC=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendToChatbotDLQ",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${DLQ_ARN}"
    }
  ]
}
JSON
)
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name SlackChatbotDLQAccess \
  --policy-document "${DLQ_POLICY_DOC}" >/dev/null
echo "    IAM policy SlackChatbotDLQAccess attached to ${ROLE_NAME}"

# IAM propagation can lag a few seconds before put-function-event-invoke-config
# accepts the destination. Retry briefly.
for attempt in 1 2 3 4 5; do
  if aws lambda put-function-event-invoke-config --region "${AWS_REGION}" \
       --function-name "${LAMBDA_C_NAME}" \
       --maximum-retry-attempts 2 \
       --destination-config "{\"OnFailure\":{\"Destination\":\"${DLQ_ARN}\"}}" \
       >/dev/null 2>&1; then
    echo "    OnFailure → ${DLQ_ARN}"
    break
  fi
  if [ "${attempt}" = "5" ]; then
    echo "ERROR: put-function-event-invoke-config failed after 5 attempts"
    aws lambda put-function-event-invoke-config --region "${AWS_REGION}" \
      --function-name "${LAMBDA_C_NAME}" \
      --maximum-retry-attempts 2 \
      --destination-config "{\"OnFailure\":{\"Destination\":\"${DLQ_ARN}\"}}"
    exit 1
  fi
  sleep 3
done

# --------------------------------------------------------------------------
# 9. CloudWatch alarms (P1-13): Lambda-C errors / API GW 5xx / DDB throttle
# --------------------------------------------------------------------------
echo "==> [9/10] Setting up CloudWatch alarms..."

# DDB_TABLE_NAME already set in step 5; reuse here for the throttle alarm.

# AlarmActions intentionally unset: project has no SNS topic dedicated to
# this stack. Alarms will fire to the CloudWatch console only — wiring an
# action is a follow-up (TODO P1/P2 once an ops SNS topic exists).
COMMON_ALARM_ARGS=(
  --region "${AWS_REGION}"
  --comparison-operator GreaterThanOrEqualToThreshold
  --evaluation-periods 1
  --threshold 1
  --period 300
  --statistic Sum
  --treat-missing-data notBreaching
)

# Alarm 1 — Lambda-C invocation errors
aws cloudwatch put-metric-alarm \
  --alarm-name "${LAMBDA_C_NAME}-errors" \
  --alarm-description "Lambda-C (${LAMBDA_C_NAME}) Errors >= 1 in 5min" \
  --namespace AWS/Lambda \
  --metric-name Errors \
  --dimensions "Name=FunctionName,Value=${LAMBDA_C_NAME}" \
  "${COMMON_ALARM_ARGS[@]}"
echo "    Alarm: ${LAMBDA_C_NAME}-errors"

# Alarm 2 — API Gateway 5xx for the chatbot HTTP API
aws cloudwatch put-metric-alarm \
  --alarm-name "${LAMBDA_C_NAME}-apigw-5xx" \
  --alarm-description "API Gateway HTTP API ${API_ID} 5xx >= 1 in 5min" \
  --namespace AWS/ApiGateway \
  --metric-name 5xx \
  --dimensions "Name=ApiId,Value=${API_ID}" \
  "${COMMON_ALARM_ARGS[@]}"
echo "    Alarm: ${LAMBDA_C_NAME}-apigw-5xx"

# Alarm 3 — DDB throttling on the threads table
aws cloudwatch put-metric-alarm \
  --alarm-name "${LAMBDA_C_NAME}-ddb-throttle" \
  --alarm-description "DDB ${DDB_TABLE_NAME} ThrottledRequests >= 1 in 5min" \
  --namespace AWS/DynamoDB \
  --metric-name ThrottledRequests \
  --dimensions "Name=TableName,Value=${DDB_TABLE_NAME}" \
  "${COMMON_ALARM_ARGS[@]}"
echo "    Alarm: ${LAMBDA_C_NAME}-ddb-throttle"

# --------------------------------------------------------------------------
# 10. Summary
# --------------------------------------------------------------------------
echo "==> [10/10] Deployment complete."
cat <<EOF

  Account:        ${AWS_ACCOUNT_ID}
  Region:         ${AWS_REGION}
  Lambda:         ${LAMBDA_ARN}
  API Gateway:    ${API_ID}
  Concurrency:    ${RESERVED_CONCURRENCY} (reserved)
  Memory:         512 MB
  DLQ:            ${DLQ_ARN}
  Alarms:         ${LAMBDA_C_NAME}-{errors,apigw-5xx,ddb-throttle}
  Test channel:   ${SLACK_TEST_CHANNEL}

  ▶ NEXT: Configure Slack App Event Subscriptions
    Go to https://api.slack.com/apps → your app → Event Subscriptions
    Set Request URL to:

        ${INVOKE_URL}

    Then subscribe to bot event: app_mention
    Click Save → Reinstall app to workspace.

  ▶ Watch Lambda logs:
    aws logs tail /aws/lambda/${LAMBDA_C_NAME} --follow --region ${AWS_REGION}

  ▶ Test from Slack: in #${SLACK_TEST_CHANNEL}, type:
    @devops_agent ping
EOF
