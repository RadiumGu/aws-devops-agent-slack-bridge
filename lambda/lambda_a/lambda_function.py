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
import logging
import os
import time
import boto3
import botocore
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEVOPS_AGENT_SPACE_ID = os.environ["DEVOPS_AGENT_SPACE_ID"]
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

# P1-1: alarm-event idempotency. Reuses the chatbot's threads DDB table
# (single-table design, distinct partition-key prefixes per record class).
# When the env var is unset (e.g. older deploys that haven't run the
# updated deploy.sh), idempotency is silently skipped and we behave like
# the original implementation.
THREAD_TABLE_NAME = os.environ.get("THREAD_TABLE_NAME", "")
ALARM_KEY_PREFIX = "alarm:"
# Alarms have ms-resolution timestamps; 1-day TTL is more than enough to
# cover EventBridge re-delivery (which happens within minutes at most).
ALARM_IDEMPOTENCY_TTL_S = 24 * 3600

# P1-2: explicit retry/timeout. create_backlog_task is a control-plane
# call (small payload, fast). Adaptive retries cope with throttling on
# DevOps Agent's preview-period quotas without hammering it.
_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=5,
    read_timeout=30,
)
_DDB_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=3,
    read_timeout=10,
)

_client = boto3.client("devops-agent", region_name=REGION, config=_BOTO_CONFIG)

_ddb_table = None


def _get_thread_table():
    """Lazy-init DDB Table resource. Returns None if THREAD_TABLE_NAME is
    not configured (idempotency disabled)."""
    global _ddb_table
    if not THREAD_TABLE_NAME:
        return None
    if _ddb_table is None:
        _ddb_table = boto3.resource(
            "dynamodb", region_name=REGION, config=_DDB_BOTO_CONFIG,
        ).Table(THREAD_TABLE_NAME)
    return _ddb_table


def _claim_alarm_idempotent(alarm_name: str, state_timestamp: str) -> bool:
    """Atomically claim an (alarmName, state.timestamp) tuple as already
    handled. Same pattern as Lambda-C's `evt:` rows: shared threads table,
    distinct key prefix.

    Returns:
        True  -- first time we've seen this alarm transition; proceed.
        False -- duplicate (EventBridge redelivery); caller should drop.

    Fail-open: any DDB error (throttling / network / table missing) logs
    a warning and returns True. We'd rather risk creating a duplicate
    backlog task than silently drop a real first-arrival alarm.
    """
    table = _get_thread_table()
    if table is None:
        return True  # idempotency disabled
    if not (alarm_name and state_timestamp):
        return True  # missing fields; can't dedup safely
    now = int(time.time())
    try:
        table.put_item(
            Item={
                "thread_ts": f"{ALARM_KEY_PREFIX}{alarm_name}:{state_timestamp}",
                "alarm_name": alarm_name,
                "state_timestamp": state_timestamp,
                "processed_at": now,
                "ttl": now + ALARM_IDEMPOTENCY_TTL_S,
            },
            ConditionExpression="attribute_not_exists(thread_ts)",
        )
        return True
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return False
        logger.warning(json.dumps({
            "msg": "alarm_idempotency_check_failed",
            "alarm": alarm_name,
            "state_timestamp": state_timestamp,
            "error": str(e),
        }))
        return True  # fail-open

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

    # P1-1: drop EventBridge re-deliveries of the same alarm transition.
    # state.timestamp is the alarm's state-change time; identical retries
    # share it, while real ALARM->OK->ALARM flips have different values.
    state_timestamp = detail.get("state", {}).get("timestamp", "")
    if not _claim_alarm_idempotent(alarm_name, state_timestamp):
        logger.info(json.dumps({
            "msg": "skipped_duplicate_alarm",
            "alarm": alarm_name,
            "state_timestamp": state_timestamp,
        }))
        return {"statusCode": 200,
                "body": f"Skipped: duplicate alarm {alarm_name}"}

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
    logger.info(json.dumps({
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
