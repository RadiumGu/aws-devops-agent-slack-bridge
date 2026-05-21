#!/usr/bin/env bash
# Run a one-off FIS experiment to terminate BOTH nodes in petsite nodegroup
# workers1a60 (AZ-1a). Triggers DevOps Agent investigation via the new alarms.
#
# This script:
#  1. Clones EXT25AmEAp21foyA (EXP-002 AZ-a, 50%) into a new template with 100%
#  2. Starts the experiment
#  3. Streams status until completion
#  4. Prints links to look for: alarms / lambda logs / Slack
#
# Pre-conditions (already verified):
#  - 4 new alarms in place (asg/eks-pods-unschedulable/eks-apiserver-5xx)
#  - DevOps Agent Lambda-A subscribed to EventBridge cloudwatch alarms
#  - PetSite canary running every 5 minutes against ALB

set -euo pipefail

# ----------------------------------------------------------------------------
# Load AWS account / region from .env so we don't hard-code secrets in git.
# ----------------------------------------------------------------------------
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
fi
: "${AWS_REGION:?AWS_REGION must be set in .env}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID must be set in .env}"

REGION="${AWS_REGION}"
ACCOUNT="${AWS_ACCOUNT_ID}"

# ----------------------------------------------------------------------------
# PetSite-specific fixtures — edit these if reusing this script in another
# project / on different FIS resources. None of these are secrets, but they
# only make sense in the PetSite account; calibrate them before running.
#   SOURCE_TEMPLATE:        an existing FIS template you want to clone+tweak
#   TARGET_NODEGROUP_ARN:   the EKS nodegroup the cloned template will target
#   ROLE_ARN:               the FIS execution role that already has the
#                           required eks:TerminateNodegroupInstances perms
# ----------------------------------------------------------------------------
SOURCE_TEMPLATE="${SOURCE_TEMPLATE:-EXT25AmEAp21foyA}"
TARGET_NODEGROUP_ARN="${TARGET_NODEGROUP_ARN:-arn:aws:eks:${REGION}:${ACCOUNT}:nodegroup/PetSite/petsiteNodegroupworkers1a60-ZJElxYDbKT8H/8ccea529-d21b-f8e5-1c82-1f0367d29249}"
ROLE_ARN="${FIS_ROLE_ARN:-arn:aws:iam::${ACCOUNT}:role/FISExperimentRole}"

echo "==> [1/3] Creating one-off FIS template (100% termination)..."

# Build template JSON
cat > /tmp/fis-experiment-100pct.json <<EOF
{
  "description": "Chaos validation: terminate ALL nodes in PetSite/workers1a60 to validate DevOps Agent investigation flow",
  "roleArn": "${ROLE_ARN}",
  "stopConditions": [
    { "source": "none" }
  ],
  "targets": {
    "eks-nodegroup": {
      "resourceType": "aws:eks:nodegroup",
      "resourceArns": ["${TARGET_NODEGROUP_ARN}"],
      "selectionMode": "ALL"
    }
  },
  "actions": {
    "terminate-eks-node": {
      "actionId": "aws:eks:terminate-nodegroup-instances",
      "parameters": {
        "instanceTerminationPercentage": "100"
      },
      "targets": { "Nodegroups": "eks-nodegroup" }
    }
  },
  "tags": {
    "Name": "petsite-chaos-2nodes-devops-agent-validation",
    "Purpose": "validate-devops-agent-investigation-flow"
  }
}
EOF

# IMPORTANT: stopConditions=[{source:none}] = no auto-stop. We want the alarm
# to fire so DevOps Agent picks it up. ASG will recover automatically anyway.

TEMPLATE_ID=$(aws fis create-experiment-template \
  --region "${REGION}" \
  --cli-input-json file:///tmp/fis-experiment-100pct.json \
  --query 'experimentTemplate.id' --output text)

echo "    New template: ${TEMPLATE_ID}"
echo

echo "==> [2/3] Starting experiment..."
EXP_ID=$(aws fis start-experiment \
  --region "${REGION}" \
  --experiment-template-id "${TEMPLATE_ID}" \
  --tags "Name=petsite-chaos-2nodes-$(date +%Y%m%d-%H%M%S)" \
  --query 'experiment.id' --output text)

START_TS=$(date +%s)
echo "    Experiment ID: ${EXP_ID}"
echo "    Start time:    $(date -u -Iseconds)"
echo

echo "==> [3/3] Streaming status..."
echo
echo "  In another terminal you can watch:"
echo "    1. Nodes:         kubectl get nodes -w"
echo "    2. ALB / alarms:  aws cloudwatch describe-alarms --region ${REGION} \\"
echo "                       --alarm-name-prefix petsite-asg --output table"
echo "    3. Lambda-A:      aws logs tail /aws/lambda/devops-agent-trigger-investigation \\"
echo "                       --follow --region ${REGION}"
echo "    4. Slack channel: #${SLACK_CHANNEL:-<your-slack-channel-id>}"
echo

PREV_STATUS=""
for i in $(seq 1 60); do
  STATUS=$(aws fis get-experiment --region "${REGION}" --id "${EXP_ID}" \
    --query 'experiment.state.status' --output text 2>/dev/null || echo "ERROR")
  ELAPSED=$(($(date +%s) - START_TS))
  if [ "$STATUS" != "$PREV_STATUS" ]; then
    echo "  T+${ELAPSED}s  ${STATUS}"
    PREV_STATUS="$STATUS"
  fi
  case "$STATUS" in
    completed|failed|stopped) break ;;
  esac
  sleep 5
done

echo
echo "==> Final state:"
aws fis get-experiment --region "${REGION}" --id "${EXP_ID}" \
  --query 'experiment.{State:state.status,Reason:state.reason,StartTime:startTime,EndTime:endTime}' --output json

echo
echo "==> Experiment ID kept for reference: ${EXP_ID}"
echo "==> Template ID kept for cleanup:    ${TEMPLATE_ID}"
echo "    Cleanup template later with:"
echo "    aws fis delete-experiment-template --region ${REGION} --id ${TEMPLATE_ID}"
