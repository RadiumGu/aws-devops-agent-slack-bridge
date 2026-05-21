"""
Lambda-A: CloudWatch Alarm → DevOps Agent Investigation

Triggered by EventBridge when any matching CloudWatch alarm enters ALARM state.
Creates a DevOps Agent INVESTIGATION backlog task with a generic description
that adapts to any AWS service (EC2/EKS/RDS/Lambda/ALB/...).

Environment variables:
    DEVOPS_AGENT_SPACE_ID  - Required. The Agent Space UUID.
    AWS_REGION             - Auto-injected by Lambda runtime.
"""
import json
import os
import boto3

DEVOPS_AGENT_SPACE_ID = os.environ["DEVOPS_AGENT_SPACE_ID"]
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

_client = boto3.client("devops-agent", region_name=REGION)

# Per-namespace reasoning hints injected into the Investigation details
# section. The Agent reasons over free-form text, so a tighter hint cuts
# down on irrelevant tool calls.
NAMESPACE_HINTS: dict[str, str] = {
    "AWS/EKS": (
        "Examine pod status, node health, recent deployments, HPA scaling "
        "events, and cluster autoscaler logs."
    ),
    "ContainerInsights": (
        "Examine pod status, node health, recent deployments, HPA scaling "
        "events, and cluster autoscaler logs. Also check container-level "
        "CPU/memory throttling."
    ),
    "AWS/RDS": (
        "Check connection pool exhaustion, slow queries, replica lag, IOPS "
        "saturation, and CloudWatch enhanced monitoring metrics."
    ),
    "AWS/Lambda": (
        "Check throttle/concurrency limits, error logs in CloudWatch, "
        "downstream service errors (DDB/SQS/etc), and recent code "
        "deployments."
    ),
    "AWS/ApplicationELB": (
        "Check target health, 5xx rates per target, recent deployments, "
        "and target group connection draining settings."
    ),
    "AWS/AutoScaling": (
        "Check launch failures, scaling history, instance health checks, "
        "and EC2 quota."
    ),
}

DEFAULT_NAMESPACE_HINT = (
    "Identify the root cause of this alarm. Examine the affected "
    "resource(s) listed in Dimensions above; correlate with recent "
    "deployments, configuration changes, autoscaling events, and "
    "upstream/downstream dependencies (network, storage, identity, "
    "managed services). Inspect CloudWatch metrics and logs in the "
    "alarm's namespace, plus CloudTrail events for the affected "
    "resources within the last 30 minutes. Recommend concrete "
    "remediation actions ranked by impact and risk."
)


def _build_starting_point(detail: dict) -> tuple[str, str, dict]:
    """Extract namespace, metric name, and dimensions from a CloudWatch
    Alarm State Change event. Works for any AWS service, not just EC2."""
    metrics = detail.get("configuration", {}).get("metrics", [])
    if not metrics:
        return "", "", {}
    metric = metrics[0].get("metricStat", {}).get("metric", {})
    namespace = metric.get("namespace", "")
    metric_name = metric.get("name", "")
    dimensions = metric.get("dimensions", {}) or {}
    return namespace, metric_name, dimensions


def _format_description(alarm_name: str, account: str, region: str,
                        namespace: str, metric_name: str,
                        dimensions: dict, reason: str) -> str:
    """Build a structured description that mirrors the DevOps Agent console's
    'Investigation starting point' + 'Investigation details' fields.

    The Agent reasons over this free-form text — we give it explicit
    structured context instead of hard-coding 'EC2 Instance: ...'. The
    'Investigation details' hint is selected per CloudWatch namespace so
    EKS/RDS/Lambda alarms each get reasoning hints tuned to their failure
    modes; unknown namespaces fall back to a generic prompt.
    """
    dim_lines = "\n".join(f"  - {k}: {v}" for k, v in dimensions.items()) \
        if dimensions else "  (no dimensions)"

    hint = NAMESPACE_HINTS.get(namespace, DEFAULT_NAMESPACE_HINT)

    return (
        # ===== Investigation starting point =====
        f"Investigation starting point:\n"
        f"  Source: CloudWatch Alarm '{alarm_name}'\n"
        f"  Account: {account}\n"
        f"  Region: {region}\n"
        f"  Namespace: {namespace or '(unknown)'}\n"
        f"  Metric: {metric_name or '(unknown)'}\n"
        f"  Dimensions:\n{dim_lines}\n"
        f"  Alarm reason: {reason}\n\n"
        # ===== Investigation details =====
        f"Investigation details:\n"
        f"  {hint}"
    )


def _build_title(alarm_name: str, dimensions: dict) -> str:
    """Build a concise title. Avoids trailing empty 'Instance: ' artifacts
    when dimensions don't contain InstanceId."""
    primary = (
        dimensions.get("InstanceId")
        or dimensions.get("DBInstanceIdentifier")
        or dimensions.get("ClusterName")
        or dimensions.get("FunctionName")
        or dimensions.get("LoadBalancer")
        or dimensions.get("TargetGroup")
        or ""
    )
    if primary:
        return f"Investigate: {alarm_name} ({primary})"
    return f"Investigate: {alarm_name}"


def lambda_handler(event, context):
    detail = event.get("detail", {})
    state_value = detail.get("state", {}).get("value", "")

    # Only act on ALARM state transitions
    if state_value != "ALARM":
        return {"statusCode": 200,
                "body": f"Skipped: state={state_value}"}

    alarm_name = detail.get("alarmName", "Unknown")
    # accountId and region live at event top-level for CloudWatch Alarm
    # State Change events, NOT inside `detail`. See:
    # https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html
    account = event.get("account", "")
    region = event.get("region", REGION)
    reason = detail.get("state", {}).get("reason", "N/A")

    namespace, metric_name, dimensions = _build_starting_point(detail)

    description = _format_description(
        alarm_name=alarm_name, account=account, region=region,
        namespace=namespace, metric_name=metric_name,
        dimensions=dimensions, reason=reason,
    )
    title = _build_title(alarm_name, dimensions)

    response = _client.create_backlog_task(
        agentSpaceId=DEVOPS_AGENT_SPACE_ID,
        taskType="INVESTIGATION",
        title=title,
        priority="HIGH",
        description=description,
    )

    task = response["task"]
    print(json.dumps({
        "msg": "investigation_created",
        "alarm": alarm_name,
        "account": account,
        "region": region,
        "namespace": namespace,
        "dimensions": dimensions,
        "task_id": task["taskId"],
        "execution_id": task.get("executionId"),
    }))

    return {
        "statusCode": 200,
        "body": json.dumps({
            "taskId": task["taskId"],
            "executionId": task.get("executionId"),
            "status": task["status"],
        }),
    }
