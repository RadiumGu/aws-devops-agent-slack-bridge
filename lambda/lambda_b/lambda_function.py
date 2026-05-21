"""
Lambda-B: DevOps Agent Investigation Completed → Slack notification

Triggered by EventBridge when DevOps Agent emits aws.aidevops /
'Investigation Completed'. Fetches the markdown investigation summary via
list_journal_records() and posts it to Slack via the existing Incoming
Webhook (the same one used by petsite-ops-slack-notifier).

Environment variables:
    SLACK_WEBHOOK_SECRET_ARN  - Required. Secrets Manager ARN holding the
                                Slack Incoming Webhook URL (SecretString).
    SLACK_CHANNEL             - Required. Channel name or ID (informational
                                only; webhooks are pre-bound to a channel).
    AWS_REGION                - Auto-injected by Lambda runtime.

Note on rotation: the webhook URL is cached at module import (cold start).
After rotating the secret, the new value takes effect on the next cold
start; warm containers continue using the old value until they cycle.
For preview-period this is acceptable; rotate by triggering a Lambda config
update (e.g. re-run deploy.sh) to force a refresh if needed.
"""
import json
import os
import urllib.request
import urllib.error
import boto3

SLACK_WEBHOOK_SECRET_ARN = os.environ["SLACK_WEBHOOK_SECRET_ARN"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

# Slack mrkdwn block size limit is 3000 chars per text block; leave margin
SLACK_BLOCK_LIMIT = 2900

_client = boto3.client("devops-agent", region_name=REGION)
_secrets_client = boto3.client("secretsmanager", region_name=REGION)

# Cached at cold start. Raises on failure so the invocation hits the DLQ
# instead of silently using a stale value (there is no stale value yet —
# but be explicit: never fallback to env-var plaintext).
_webhook_url: str = _secrets_client.get_secret_value(
    SecretId=SLACK_WEBHOOK_SECRET_ARN,
)["SecretString"].strip()


def get_investigation_summary(agent_space_id: str, execution_id: str) -> str:
    """Fetch the investigation_summary_md record from DevOps Agent journal."""
    response = _client.list_journal_records(
        agentSpaceId=agent_space_id,
        executionId=execution_id,
    )
    for record in response.get("records", []):
        if record.get("recordType") == "investigation_summary_md":
            return record.get("content", "")
    return "_No investigation summary available._"


def _chunk_for_slack(text: str, limit: int = SLACK_BLOCK_LIMIT) -> list[str]:
    """Split markdown content into <=limit-char chunks at line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def _format_duration(created_at: str, updated_at: str) -> str:
    """Format ISO8601 timestamps into 'Xm Ys' duration string. Returns
    empty string if either timestamp is missing or unparseable."""
    if not (created_at and updated_at):
        return ""
    from datetime import datetime
    try:
        # Python 3.11+ fromisoformat handles 'Z' suffix; older versions don't.
        c = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        u = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((u - c).total_seconds())
    if seconds < 0:
        return ""
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s"


def _build_metadata_fields(metadata: dict, data: dict) -> list[dict]:
    """Common metadata fields shared by completed/failed/cancelled blocks."""
    task_id = metadata.get("task_id", "unknown")
    agent_space_id = metadata.get("agent_space_id", "")
    execution_id = metadata.get("execution_id", "")

    created_at = data.get("createdAt", "") or data.get("created_at", "")
    updated_at = data.get("updatedAt", "") or data.get("updated_at", "")
    duration = _format_duration(created_at, updated_at)

    fields = [
        {"type": "mrkdwn", "text": f"*Task ID*\n`{task_id}`"},
        {"type": "mrkdwn", "text": f"*Execution ID*\n`{execution_id}`"},
        {"type": "mrkdwn", "text": f"*Agent Space*\n`{agent_space_id}`"},
        {"type": "mrkdwn", "text": f"*Region*\n`{REGION}`"},
    ]
    if created_at:
        fields.append({"type": "mrkdwn", "text": f"*Started*\n`{created_at}`"})
    if duration:
        fields.append({"type": "mrkdwn", "text": f"*Duration*\n`{duration}`"})
    return fields


def _build_blocks(metadata: dict, data: dict, summary_md: str) -> list[dict]:
    """Build Slack Block Kit payload for the investigation summary."""
    fields = _build_metadata_fields(metadata, data)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔍 DevOps Agent Investigation Completed",
            },
        },
        {"type": "section", "fields": fields},
        {"type": "divider"},
    ]

    for chunk in _chunk_for_slack(summary_md):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk},
        })

    return blocks


def _build_failure_blocks(metadata: dict, data: dict, status: str) -> list[dict]:
    """Build Slack Block Kit payload for FAILED/CANCELLED investigations.

    Failed investigations don't have a markdown summary, so we render only
    metadata + a Reason field. The DevOps Agent preview-period event schema
    field name for the failure reason is best-effort: we try a few
    candidates and fall back to a generic message.
    """
    if status == "FAILED":
        title = ":x: DevOps Agent Investigation Failed"
        reason = (
            data.get("failureReason")
            or data.get("failure_reason")
            or data.get("errorMessage")
            or "Reason not provided by Agent"
        )
        reason_label = "Failure reason"
    else:  # CANCELLED
        title = ":warning: DevOps Agent Investigation Cancelled"
        reason = (
            data.get("cancellationReason")
            or data.get("cancellation_reason")
            or "Reason not provided by Agent"
        )
        reason_label = "Cancellation reason"

    fields = _build_metadata_fields(metadata, data)
    fields.append({"type": "mrkdwn", "text": f"*{reason_label}*\n{reason}"})

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title},
        },
        {"type": "section", "fields": fields},
    ]


def post_to_slack(blocks: list[dict], fallback_text: str) -> None:
    """POST to Slack Incoming Webhook URL. Webhook is pre-bound to channel."""
    payload = {
        "text": fallback_text,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if SLACK_CHANNEL:
        # Optional override; most Webhooks ignore this and use their bound channel.
        payload["channel"] = SLACK_CHANNEL

    req = urllib.request.Request(
        _webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
            if body.strip() != "ok":
                raise RuntimeError(f"Slack webhook returned: {body!r}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Slack webhook HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
        ) from e


def lambda_handler(event, context):
    detail = event.get("detail", {})
    metadata = detail.get("metadata", {})
    data = detail.get("data", {})

    agent_space_id = metadata.get("agent_space_id")
    execution_id = metadata.get("execution_id")
    task_id = metadata.get("task_id", "unknown")

    if not (agent_space_id and execution_id):
        print(f"WARN: missing metadata, event={json.dumps(event)}")
        return {"statusCode": 400, "body": "missing metadata"}

    status = data.get("status", "")

    if status == "COMPLETED" or not status:
        # Empty status preserves backward compatibility with old test fixtures
        # that omit `status`; production events always include it.
        summary_md = get_investigation_summary(agent_space_id, execution_id)
        blocks = _build_blocks(metadata, data, summary_md)
        fallback = f"DevOps Agent investigation {task_id} completed."
    elif status in ("FAILED", "CANCELLED"):
        blocks = _build_failure_blocks(metadata, data, status)
        fallback = (
            f"DevOps Agent investigation {task_id} {status.lower()}."
        )
    else:
        # IN_PROGRESS or any other transitional status — stay silent so we
        # don't ping Slack on every state flip.
        print(f"Skipping non-terminal event: status={status}")
        return {"statusCode": 200, "body": f"Skipped: status={status}"}

    post_to_slack(blocks, fallback)
    print(json.dumps({
        "msg": "slack_posted",
        "task_id": task_id,
        "execution_id": execution_id,
        "status": status or "COMPLETED",
    }))

    return {"statusCode": 200, "body": "ok"}
