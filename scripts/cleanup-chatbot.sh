#!/usr/bin/env bash
# Cleanup the Slack Chatbot stack (Lambda + API GW + IAM policy).
# Does NOT touch: the Slack App, secrets, base IAM role, base Lambda-A/B.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
fi

LAMBDA_C_NAME="${LAMBDA_C_NAME:-devops-agent-slack-chatbot}"
API_NAME="${API_NAME:-devops-agent-slack-chatbot-api}"
ROLE_NAME="${ROLE_NAME:-DevOpsAgentDemoLambdaRole}"

echo "==> Removing API Gateway..."
API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION:-ap-northeast-1}" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)
if [ "${API_ID}" != "None" ] && [ -n "${API_ID}" ]; then
  aws apigatewayv2 delete-api --region "${AWS_REGION:-ap-northeast-1}" \
    --api-id "${API_ID}" >/dev/null 2>&1 || true
  echo "    Deleted API ${API_ID}"
fi

echo "==> Removing Lambda function..."
aws lambda delete-function --region "${AWS_REGION:-ap-northeast-1}" \
  --function-name "${LAMBDA_C_NAME}" >/dev/null 2>&1 || true

echo "==> Removing SlackChatbotAccess inline policy from role..."
aws iam delete-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name SlackChatbotAccess >/dev/null 2>&1 || true

echo "==> Done. Slack App, secrets, base role and Lambda-A/B left untouched."
