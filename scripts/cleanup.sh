#!/usr/bin/env bash
# Cleanup script — removes everything deploy.sh created.
# Reads .env for resource names. Will NOT delete: layer versions or your
# Agent Space.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found."
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

echo "==> Removing EventBridge targets and rules..."
aws events remove-targets --region "${AWS_REGION}" --rule "${RULE_1_NAME}" --ids trigger-investigation >/dev/null 2>&1 || true
aws events delete-rule --region "${AWS_REGION}" --name "${RULE_1_NAME}" >/dev/null 2>&1 || true
aws events remove-targets --region "${AWS_REGION}" --rule "${RULE_2_NAME}" --ids notify-slack >/dev/null 2>&1 || true
aws events delete-rule --region "${AWS_REGION}" --name "${RULE_2_NAME}" >/dev/null 2>&1 || true

echo "==> Deleting Lambda functions..."
aws lambda delete-function --region "${AWS_REGION}" --function-name "${LAMBDA_A_NAME}" >/dev/null 2>&1 || true
aws lambda delete-function --region "${AWS_REGION}" --function-name "${LAMBDA_B_NAME}" >/dev/null 2>&1 || true

echo "==> Detaching role policies..."
aws iam delete-role-policy --role-name "${ROLE_NAME}" --policy-name DevOpsAgentAccess >/dev/null 2>&1 || true
aws iam detach-role-policy --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null 2>&1 || true
aws iam delete-role --role-name "${ROLE_NAME}" >/dev/null 2>&1 || true

echo "==> Done."
