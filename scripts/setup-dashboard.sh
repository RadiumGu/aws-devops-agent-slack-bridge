#!/usr/bin/env bash
# Idempotent setup of the DevOpsAgent-PetSite CloudWatch dashboard.
#
# Aggregates Lambda-A/B/C invocation/error/duration, DLQ depth across all
# three queues, EventBridge Rule-1/Rule-2 invocations, API Gateway 4xx/5xx,
# and DynamoDB consumed capacity for the slack-thread table into a single
# pane. Re-runs are safe — `put-dashboard` overwrites by name.
#
# Usage:
#   ./scripts/setup-dashboard.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
fi

AWS_REGION="${AWS_REGION:-ap-northeast-1}"
DASHBOARD_NAME="${DASHBOARD_NAME:-DevOpsAgent-PetSite}"

LAMBDA_A_NAME="${LAMBDA_A_NAME:-devops-agent-trigger-investigation}"
LAMBDA_B_NAME="${LAMBDA_B_NAME:-devops-agent-notify-slack}"
LAMBDA_C_NAME="${LAMBDA_C_NAME:-devops-agent-slack-chatbot}"

TRIGGER_DLQ_NAME="${TRIGGER_DLQ_NAME:-devops-agent-trigger-dlq}"
NOTIFY_DLQ_NAME="${NOTIFY_DLQ_NAME:-devops-agent-notify-dlq}"
CHATBOT_DLQ_NAME="${CHATBOT_DLQ_NAME:-devops-agent-slack-chatbot-dlq}"

RULE_1_NAME="${RULE_1_NAME:-DevOps-Agent-Demo-Alarm-To-Lambda}"
RULE_2_NAME="${RULE_2_NAME:-DevOps-Agent-Demo-Investigation-Completed}"

API_NAME="${API_NAME:-devops-agent-slack-chatbot-api}"
DDB_TABLE_NAME="${DDB_TABLE_NAME:-devops-agent-slack-threads}"

echo "==> Region=${AWS_REGION}  Dashboard=${DASHBOARD_NAME}"

# Resolve API GW HTTP API ID by name (dashboard widgets need ApiId, not Name)
API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text 2>/dev/null || echo "None")
if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
  echo "    WARN: API Gateway '${API_NAME}' not found — API GW widget will be empty"
  API_ID=""
else
  echo "    API Gateway: ${API_NAME} (ApiId=${API_ID})"
fi

DASHBOARD_BODY=$(cat <<JSON
{
  "widgets": [
    {
      "type": "metric", "x": 0, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "Lambda invocations (Sum / 5min)",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300, "stat": "Sum",
        "metrics": [
          ["AWS/Lambda", "Invocations", "FunctionName", "${LAMBDA_A_NAME}"],
          [".", ".", ".", "${LAMBDA_B_NAME}"],
          [".", ".", ".", "${LAMBDA_C_NAME}"]
        ]
      }
    },
    {
      "type": "metric", "x": 12, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "Lambda errors (Sum / 5min)",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300, "stat": "Sum",
        "metrics": [
          ["AWS/Lambda", "Errors", "FunctionName", "${LAMBDA_A_NAME}"],
          [".", ".", ".", "${LAMBDA_B_NAME}"],
          [".", ".", ".", "${LAMBDA_C_NAME}"]
        ]
      }
    },
    {
      "type": "metric", "x": 0, "y": 6, "width": 12, "height": 6,
      "properties": {
        "title": "Lambda duration p50 / p99 (ms)",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300,
        "metrics": [
          ["AWS/Lambda", "Duration", "FunctionName", "${LAMBDA_A_NAME}", {"stat": "p50"}],
          ["...", "${LAMBDA_B_NAME}", {"stat": "p50"}],
          ["...", "${LAMBDA_C_NAME}", {"stat": "p50"}],
          ["AWS/Lambda", "Duration", "FunctionName", "${LAMBDA_A_NAME}", {"stat": "p99"}],
          ["...", "${LAMBDA_B_NAME}", {"stat": "p99"}],
          ["...", "${LAMBDA_C_NAME}", {"stat": "p99"}]
        ]
      }
    },
    {
      "type": "metric", "x": 12, "y": 6, "width": 12, "height": 6,
      "properties": {
        "title": "DLQ depth (Max / 5min)",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300, "stat": "Maximum",
        "metrics": [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${TRIGGER_DLQ_NAME}"],
          [".", ".", ".", "${NOTIFY_DLQ_NAME}"],
          [".", ".", ".", "${CHATBOT_DLQ_NAME}"]
        ]
      }
    },
    {
      "type": "metric", "x": 0, "y": 12, "width": 12, "height": 6,
      "properties": {
        "title": "EventBridge rule invocations (Sum / 5min)",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300, "stat": "Sum",
        "metrics": [
          ["AWS/Events", "Invocations", "RuleName", "${RULE_1_NAME}"],
          [".", ".", ".", "${RULE_2_NAME}"]
        ]
      }
    },
    {
      "type": "metric", "x": 12, "y": 12, "width": 12, "height": 6,
      "properties": {
        "title": "API Gateway 4xx / 5xx (Sum / 5min)",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300, "stat": "Sum",
        "metrics": [
          ["AWS/ApiGateway", "4xx", "ApiId", "${API_ID}"],
          [".", "5xx", ".", "."]
        ]
      }
    },
    {
      "type": "metric", "x": 0, "y": 18, "width": 24, "height": 6,
      "properties": {
        "title": "DynamoDB consumed capacity — ${DDB_TABLE_NAME}",
        "view": "timeSeries", "stacked": false, "region": "${AWS_REGION}",
        "period": 300, "stat": "Sum",
        "metrics": [
          ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "${DDB_TABLE_NAME}"],
          [".", "ConsumedWriteCapacityUnits", ".", "."]
        ]
      }
    }
  ]
}
JSON
)

echo "==> Putting dashboard ${DASHBOARD_NAME}..."
aws cloudwatch put-dashboard --region "${AWS_REGION}" \
  --dashboard-name "${DASHBOARD_NAME}" \
  --dashboard-body "${DASHBOARD_BODY}" >/dev/null

echo "==> Dashboard created/updated."
echo "    https://${AWS_REGION}.console.aws.amazon.com/cloudwatch/home?region=${AWS_REGION}#dashboards:name=${DASHBOARD_NAME}"
