#!/usr/bin/env bash
# Add CloudWatch alarms that will reliably fire when EKS nodes are deleted.
#
# Three alarms:
#  1. ASG GroupInServiceInstances < DesiredCapacity (per nodegroup)
#  2. ContainerInsights cluster_node_count drops (cluster-level)
#  3. ContainerInsights cluster_failed_node_count > 0 (cluster-level)
#
# All alarms fire on a 60s period for fast detection (FIS scenarios are short).
# All alarms publish to petsite-ops-alerts SNS, which fans out to:
#   - petsite-ops-slack-notifier Lambda (existing)
#   - DevOps Agent's EventBridge rule (will route to investigation)

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
SNS_TOPIC="arn:aws:sns:${REGION}:${ACCOUNT}:petsite-ops-alerts"

# ----------------------------------------------------------------------------
# PetSite-specific fixtures — edit these if reusing this script in another
# project. They identify the EKS cluster + ASGs that this alarm set targets.
# ----------------------------------------------------------------------------
CLUSTER=PetSite
NG1_ASG="eks-petsiteNodegroupworkers1a60-ZJElxYDbKT8H-8ccea529-d21b-f8e5-1c82-1f0367d29249"
NG2_ASG="eks-petsiteNodegroupworkers1cFE-rBSMHnU8raBy-d0cea529-d4f8-d859-812a-f69b5c6f5a0b"

echo "==> Creating ASG GroupInServiceInstances alarms..."

for asg in "$NG1_ASG" "$NG2_ASG"; do
  short_name=$(echo "$asg" | grep -oE 'workers1[ac][^-]+' | head -1)
  alarm_name="petsite-asg-instances-below-desired-${short_name}"
  echo "  - ${alarm_name}"

  # Use a metric math expression: in-service / desired ratio < 1.0 means short.
  aws cloudwatch put-metric-alarm \
    --region "${REGION}" \
    --alarm-name "${alarm_name}" \
    --alarm-description "ASG ${asg} has fewer in-service instances than desired (node loss in EKS nodegroup)" \
    --comparison-operator LessThanThreshold \
    --threshold 1.0 \
    --evaluation-periods 1 \
    --datapoints-to-alarm 1 \
    --treat-missing-data notBreaching \
    --alarm-actions "${SNS_TOPIC}" \
    --metrics "[
      {
        \"Id\": \"e1\",
        \"Expression\": \"IF(m_desired==0, 1, m_inservice / m_desired)\",
        \"Label\": \"InService/Desired ratio\",
        \"ReturnData\": true
      },
      {
        \"Id\": \"m_inservice\",
        \"MetricStat\": {
          \"Metric\": {
            \"Namespace\": \"AWS/AutoScaling\",
            \"MetricName\": \"GroupInServiceInstances\",
            \"Dimensions\": [
              {\"Name\": \"AutoScalingGroupName\", \"Value\": \"${asg}\"}
            ]
          },
          \"Period\": 60,
          \"Stat\": \"Average\"
        },
        \"ReturnData\": false
      },
      {
        \"Id\": \"m_desired\",
        \"MetricStat\": {
          \"Metric\": {
            \"Namespace\": \"AWS/AutoScaling\",
            \"MetricName\": \"GroupDesiredCapacity\",
            \"Dimensions\": [
              {\"Name\": \"AutoScalingGroupName\", \"Value\": \"${asg}\"}
            ]
          },
          \"Period\": 60,
          \"Stat\": \"Average\"
        },
        \"ReturnData\": false
      }
    ]"
done

echo
echo "==> Creating EKS cluster node-count alarms..."

# Alarm: failed_node_count > 0 (any node in NotReady state)
aws cloudwatch put-metric-alarm \
  --region "${REGION}" \
  --alarm-name "petsite-eks-failed-nodes" \
  --alarm-description "EKS cluster ${CLUSTER} has nodes in NotReady state" \
  --metric-name cluster_failed_node_count \
  --namespace ContainerInsights \
  --statistic Maximum \
  --dimensions "Name=ClusterName,Value=${CLUSTER}" \
  --period 60 \
  --evaluation-periods 1 \
  --datapoints-to-alarm 1 \
  --threshold 0 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "${SNS_TOPIC}"

echo "  - petsite-eks-failed-nodes"

# Alarm: node_count below floor (< 4 means we lost at least 1 node)
# Total desired = 4 (2 per nodegroup × 2 nodegroups). Alert if below 4 sustained 1 minute.
aws cloudwatch put-metric-alarm \
  --region "${REGION}" \
  --alarm-name "petsite-eks-node-count-low" \
  --alarm-description "EKS cluster ${CLUSTER} has fewer than 4 healthy nodes (lost at least 1 node)" \
  --metric-name cluster_node_count \
  --namespace ContainerInsights \
  --statistic Minimum \
  --dimensions "Name=ClusterName,Value=${CLUSTER}" \
  --period 60 \
  --evaluation-periods 1 \
  --datapoints-to-alarm 1 \
  --threshold 4 \
  --comparison-operator LessThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "${SNS_TOPIC}"

echo "  - petsite-eks-node-count-low"

echo
echo "==> Done. Verifying state..."
aws cloudwatch describe-alarms --region "${REGION}" \
  --alarm-name-prefix "petsite-asg-" \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason}' --output table
aws cloudwatch describe-alarms --region "${REGION}" \
  --alarm-name-prefix "petsite-eks-" \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason}' --output table
