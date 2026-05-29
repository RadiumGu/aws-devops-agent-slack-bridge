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

Note on rotation: the webhook URL is fetched lazily on first use and
cached in-process for SECRET_CACHE_TTL_S seconds (default 1800 = 30 min).
A rotation propagates to warm containers within that TTL without a
redeploy. To force an immediate refresh you can still trigger a Lambda
config update (re-run deploy.sh).
"""
import json
import logging
import os
import time
import urllib.request
import urllib.error
import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SLACK_WEBHOOK_SECRET_ARN = os.environ["SLACK_WEBHOOK_SECRET_ARN"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

# Slack mrkdwn block size limit is 3000 chars per text block; leave margin
SLACK_BLOCK_LIMIT = 2900

# Lazy-load secret. P0-2 fix: previously this module called
# get_secret_value() at import time, which made cold-starts brittle
# (Secrets Manager throttling / IAM propagation lag would crash init and
# bounce the EventBridge event toward the DLQ after 2 retries). We now
# fetch on first invocation and cache for SECRET_CACHE_TTL_S to also pick
# up rotations without a redeploy.
SECRET_CACHE_TTL_S = int(os.environ.get("SECRET_CACHE_TTL_S", "1800"))

# P1-2: explicit retry/timeout. devops-agent list_journal_records can be
# slow on large investigations; secretsmanager is fast.
_DEVOPS_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=5,
    read_timeout=30,
)
_SECRETS_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=3,
    read_timeout=10,
)

_client = boto3.client("devops-agent", region_name=REGION, config=_DEVOPS_BOTO_CONFIG)
_secrets_client = boto3.client("secretsmanager", region_name=REGION, config=_SECRETS_BOTO_CONFIG)

_webhook_url_cache: dict = {"value": None, "expires_at": 0.0}


def _get_webhook_url() -> str:
    """Return the Slack Incoming Webhook URL, fetching from Secrets Manager
    on first call and refreshing every SECRET_CACHE_TTL_S seconds."""
    now = time.time()
    if _webhook_url_cache["value"] and now < _webhook_url_cache["expires_at"]:
        return _webhook_url_cache["value"]
    resp = _secrets_client.get_secret_value(SecretId=SLACK_WEBHOOK_SECRET_ARN)
    url = resp["SecretString"].strip()
    _webhook_url_cache["value"] = url
    _webhook_url_cache["expires_at"] = now + SECRET_CACHE_TTL_S
    return url


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
    """Split markdown content into <=limit-char chunks at line boundaries.

    P1-3: hard-split single lines that exceed the limit. Slack rejects an
    entire block when its text field is >3000 chars, so an oversized line
    (e.g. a long URL, a stack trace dumped on one line) used to drop the
    whole investigation summary. We now slice the offending line in fixed
    `limit`-sized pieces before placing them into chunks.
    """
    if len(text) <= limit:
        return [text]

    def _split_oversized(line: str) -> list[str]:
        if len(line) <= limit:
            return [line]
        parts = []
        for i in range(0, len(line), limit):
            parts.append(line[i:i + limit])
        return parts

    chunks, current = [], ""
    for raw_line in text.splitlines(keepends=True):
        for line in _split_oversized(raw_line):
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
        _get_webhook_url(),
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
        logger.warning(json.dumps({
            "msg": "missing_metadata",
            "agent_space_id": agent_space_id,
            "execution_id": execution_id,
        }))
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
        logger.info(json.dumps({
            "msg": "skipped_non_terminal_status",
            "status": status,
        }))
        return {"statusCode": 200, "body": f"Skipped: status={status}"}

    post_to_slack(blocks, fallback)
    logger.info(json.dumps({
        "msg": "slack_posted",
        "task_id": task_id,
        "execution_id": execution_id,
        "status": status or "COMPLETED",
    }))

    return {"statusCode": 200, "body": "ok"}
