#!/usr/bin/env bash
# Trigger a CPU spike on a target EC2 via SSM, which will fire the demo
# alarm if you've created one named DevOps-Agent-Demo-CPU-High (or any
# other alarm matched by Rule-1).
#
# Usage:
#   ./scripts/test-trigger.sh i-0123456789abcdef0

set -euo pipefail

INSTANCE_ID="${1:?Usage: $0 <ec2-instance-id>}"
export AWS_REGION="${AWS_REGION:-ap-northeast-1}"

echo "==> Sending CPU stress command to ${INSTANCE_ID} (2 minutes, 2 cores)..."
aws ssm send-command \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["which stress-ng >/dev/null 2>&1 || sudo dnf install -y stress-ng || sudo apt-get install -y stress-ng","nohup stress-ng --cpu 2 --timeout 120 > /tmp/stress.log 2>&1 &"]' \
  --query 'Command.CommandId' --output text

cat <<EOF

  CPU stress kicked off. CloudWatch alarm should fire within ~2 minutes.
  Then DevOps Agent will start investigating; expect a Slack message in
  the channel configured via SLACK_CHANNEL within 5–15 minutes when it completes.

  Tail logs:
    aws logs tail /aws/lambda/devops-agent-trigger-investigation --follow --region ${AWS_REGION}
    aws logs tail /aws/lambda/devops-agent-notify-slack --follow --region ${AWS_REGION}
EOF
