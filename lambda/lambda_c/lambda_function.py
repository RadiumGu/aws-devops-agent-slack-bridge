"""
Lambda-C: Slack Chatbot bridge for AWS DevOps Agent.

Receives @mentions in Slack, calls DevOps Agent chat API, posts results back
to the same Slack thread.

Architecture (single Lambda, two paths):
  1. Fast path (entry from API GW HTTP API):
       - Verify Slack signature
       - Handle URL verification challenge
       - Drop Slack retries (X-Slack-Retry-Num set)
       - For app_mention events, async-invoke self with payload
       - Return 200 within 3 seconds (Slack requirement)
  2. Worker path (entry via async lambda.invoke with `_internal=chat`):
       - create_chat + send_message
       - Parse EventStream via chat_lib.stream_message
       - chat.postMessage to Slack (in original thread)

Environment variables:
  DEVOPS_AGENT_SPACE_ID         - Required. Agent Space UUID
  SLACK_BOT_TOKEN_SECRET_ID     - Secrets Manager secret name for xoxb token
  SLACK_SIGNING_SECRET_ID       - Secrets Manager secret name for signing secret
  AWS_REGION                    - Auto-injected
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

import boto3
import botocore
from botocore.config import Config

# Local module on the deployment package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slack_verify import SignatureError, verify_slack_signature  # noqa: E402
import agent_chat  # noqa: E402

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
DEVOPS_AGENT_SPACE_ID = os.environ["DEVOPS_AGENT_SPACE_ID"]
SLACK_BOT_TOKEN_SECRET_ID = os.environ["SLACK_BOT_TOKEN_SECRET_ID"]
SLACK_SIGNING_SECRET_ID = os.environ["SLACK_SIGNING_SECRET_ID"]
THREAD_TABLE_NAME = os.environ.get(
    "THREAD_TABLE_NAME", "devops-agent-slack-threads"
)
# Threads expire after 7 days of inactivity
THREAD_TTL_SECONDS = 7 * 24 * 3600

# P0-4: Slack event_id idempotency. We share the threads table by writing
# a separate row class with a `evt:` prefix on the partition key. The TTL
# here only needs to outlive Slack's retry window (3x within ~3min) plus
# any double-subscribe race (app_mention vs message.channels firing for
# the same user message). 24h is comfortable margin.
EVENT_IDEMPOTENCY_TTL_S = 24 * 3600
EVENT_KEY_PREFIX = "evt:"

# Placeholder kept only for backward-compat reads of rows written by older
# deploys (pre-P1-6). New rows never use PENDING — see _get_or_create_chat.
PENDING_EXECUTION_ID = "PENDING"

# Fixed user-facing error so we never leak ARNs / account IDs / endpoints.
SLACK_ERROR_TEMPLATE = (
    ":warning: 调查失败，请稍后重试 (request_id: {request_id})"
)

# Slack mention prefix pattern: "<@U0B4H6TBBM4>"
MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")

# Soft cap on user prompt length (after stripping the mention). Anything
# beyond this is unlikely to fit DevOps Agent's chat context cleanly, so we
# refuse early with a friendly Slack message instead of letting the Agent
# silently truncate or error out.
MAX_USER_PROMPT_CHARS = 4000
PROMPT_TOO_LONG_TEMPLATE = (
    ":warning: Your prompt is too long ({length} chars). "
    "Max {limit} chars. "
    "Try a shorter question or break it into smaller steps."
)

# Lazy-init clients to keep cold start small
_secrets_client = None
_devops_client = None
_lambda_client = None
_ddb_table = None
_slack_token: str | None = None
_signing_secret: str | None = None

# P1-2: explicit retry/timeout per service. devops-agent send_message is a
# streaming API that can run for minutes, so its read_timeout is sized to
# the Lambda function's own timeout (300s) minus a small safety margin.
# secretsmanager / lambda / ddb are control-plane fast calls.
_DEVOPS_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=5,
    read_timeout=280,
)
_SECRETS_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=3,
    read_timeout=10,
)
_LAMBDA_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 3},
    connect_timeout=3,
    read_timeout=10,
)
_DDB_BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=3,
    read_timeout=10,
)


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client(
            "secretsmanager", region_name=REGION, config=_SECRETS_BOTO_CONFIG,
        )
    return _secrets_client


def _get_devops_client():
    global _devops_client
    if _devops_client is None:
        _devops_client = boto3.client(
            "devops-agent", region_name=REGION, config=_DEVOPS_BOTO_CONFIG,
        )
    return _devops_client


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client(
            "lambda", region_name=REGION, config=_LAMBDA_BOTO_CONFIG,
        )
    return _lambda_client


def _get_thread_table():
    """Lazy-init DDB Table resource for thread_ts → executionId mapping."""
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource(
            "dynamodb", region_name=REGION, config=_DDB_BOTO_CONFIG,
        ).Table(THREAD_TABLE_NAME)
    return _ddb_table


def _get_slack_token() -> str:
    global _slack_token
    if _slack_token is None:
        resp = _get_secrets_client().get_secret_value(
            SecretId=SLACK_BOT_TOKEN_SECRET_ID,
        )
        _slack_token = resp["SecretString"].strip()
    return _slack_token


def _get_signing_secret() -> str:
    global _signing_secret
    if _signing_secret is None:
        resp = _get_secrets_client().get_secret_value(
            SecretId=SLACK_SIGNING_SECRET_ID,
        )
        _signing_secret = resp["SecretString"].strip()
    return _signing_secret


# ----- Idempotency (DDB) -------------------------------------------------

def _claim_event_idempotent(event_id: str) -> bool:
    """Atomically claim a Slack event_id via conditional put on the threads
    table (key prefixed with `evt:` so it can't collide with real thread_ts
    values, which Slack always serializes as numeric strings like '1.0').

    Returns:
        True  -- first time we've seen this event_id; caller should proceed.
        False -- duplicate; caller should ack 200 and drop.

    Failure mode is fail-open: if DDB itself errors (throttle / network),
    we log and return True. Re-processing a duplicate event is preferable
    to silently dropping a first-arrival event because the idempotency
    table was unreachable.
    """
    table = _get_thread_table()
    now = int(time.time())
    try:
        table.put_item(
            Item={
                "thread_ts": f"{EVENT_KEY_PREFIX}{event_id}",
                "event_id": event_id,
                "processed_at": now,
                "ttl": now + EVENT_IDEMPOTENCY_TTL_S,
            },
            ConditionExpression="attribute_not_exists(thread_ts)",
        )
        return True
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return False
        logger.warning(json.dumps({
            "msg": "idempotency_check_failed",
            "event_id": event_id,
            "error": str(e),
        }))
        return True  # fail-open


# ----- Thread state (DDB) ------------------------------------------------

def _get_or_create_chat(
    thread_ts: str,
    channel: str,
    user: str,
    agent_client,
) -> tuple[str, bool]:
    """Look up the executionId for a Slack thread; create a new chat if absent.

    P1-6 simplification: dropped the placeholder + 25s busy-wait race
    arbitration. When two @mentions hit the same thread within ms, both
    workers may now create a chat — the second `put_item` (without a
    ConditionExpression) overwrites the first, so the loser's chat
    becomes orphaned in DevOps Agent. We accept this tradeoff because:

      * a thread’s very first request is unlikely to fan out concurrently
      * orphaned chats incur no extra cost during the preview
      * the original busy-wait was burning Lambda time + reserved
        concurrency (5) on every race-loser, which capped real throughput

    Backward-compat: rows written by older deploys may still carry
    `execution_id == PENDING`; we treat that as "no chat yet" and create
    a new one (overwriting the stale placeholder).

    Returns (execution_id, is_new_chat).
    """
    table = _get_thread_table()
    now = int(time.time())

    # Fast path: read existing finalized row.
    try:
        resp = table.get_item(Key={"thread_ts": thread_ts})
        item = resp.get("Item")
    except botocore.exceptions.ClientError as e:
        logger.warning(json.dumps({"msg": "ddb_get_failed", "error": str(e)}))
        item = None

    if (
        item
        and item.get("execution_id")
        and item["execution_id"] != PENDING_EXECUTION_ID
    ):
        try:
            table.update_item(
                Key={"thread_ts": thread_ts},
                UpdateExpression="SET last_active_at = :t, #ttl = :exp",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":t": now,
                    ":exp": now + THREAD_TTL_SECONDS,
                },
            )
        except botocore.exceptions.ClientError as e:
            logger.warning(json.dumps({"msg": "ddb_update_failed", "error": str(e)}))
        return item["execution_id"], False

    # No usable row. Create the chat first (the only failure mode that
    # truly blocks us is DevOps Agent rejecting create_chat — if that
    # happens we don't want to leave any DDB row behind anyway), then
    # write the row Last-Write-Wins.
    try:
        chat = agent_client.create_chat(
            agentSpaceId=DEVOPS_AGENT_SPACE_ID,
            userId=f"slack_{user}",
            userType="STATIC",
        )
        execution_id = chat["executionId"]
    except Exception:
        logger.exception(json.dumps({
            "msg": "create_chat_failed",
            "thread_ts": thread_ts,
        }))
        raise

    try:
        table.put_item(Item={
            "thread_ts": thread_ts,
            "execution_id": execution_id,
            "channel_id": channel,
            "agent_space_id": DEVOPS_AGENT_SPACE_ID,
            "user_id": user,
            "created_at": now,
            "last_active_at": now,
            "ttl": now + THREAD_TTL_SECONDS,
        })
    except botocore.exceptions.ClientError as e:
        # If the row write fails, the chat itself is fine — we just won’t
        # remember it. The current request still uses execution_id
        # locally; the next mention in the same thread will create a new
        # chat (orphaning this one).
        logger.warning(json.dumps({
            "msg": "ddb_put_failed",
            "error": str(e),
            "thread_ts": thread_ts,
        }))
    return execution_id, True


# ----- Slack helpers ------------------------------------------------------

def slack_post(method: str, payload: dict) -> dict:
    """POST JSON to a Slack Web API method. Returns parsed body."""
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_get_slack_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Slack {method} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
        ) from e
    if not body.get("ok"):
        raise RuntimeError(f"Slack {method} error: {body.get('error')}")
    return body


def post_message(channel: str, text: str, *, thread_ts: str | None = None) -> dict:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_post("chat.postMessage", payload)


def update_message(channel: str, ts: str, text: str) -> dict:
    return slack_post("chat.update", {"channel": channel, "ts": ts, "text": text})


# ----- Path 1: API GW handler (fast path) --------------------------------

def _fast_path(event: dict, context) -> dict:
    """Handle incoming HTTP from API Gateway HTTP API v2.0."""
    headers = event.get("headers") or {}

    # Body may be base64-encoded if API GW says so
    raw_body_str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body_str)
    else:
        raw_body = raw_body_str.encode("utf-8")

    # Detect URL verification BEFORE signature check.
    # Slack docs say signing is still applied, so we still verify, but if
    # we can't parse the body we still want to fail clean. Verify first.
    try:
        verify_slack_signature(headers, raw_body, _get_signing_secret())
    except SignatureError as e:
        logger.warning(json.dumps({"msg": "signature_rejected", "error": str(e)}))
        return {"statusCode": 401, "body": "invalid signature"}

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as e:
        logger.warning(json.dumps({"msg": "json_error", "error": str(e)}))
        return {"statusCode": 400, "body": "bad json"}

    # 1. URL verification challenge
    if payload.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": payload.get("challenge", ""),
        }

    # 2. Slack retry detection
    retry_num = headers.get("x-slack-retry-num")
    if retry_num:
        logger.info(json.dumps({"msg": "skipped_retry", "retry_num": retry_num}))
        return {"statusCode": 200, "body": "ack-retry"}

    # 3. Event callbacks
    if payload.get("type") == "event_callback":
        # P0-4: Slack event_id-based idempotency. Slack guarantees event_id
        # uniqueness, but the same user message can hit us twice when both
        # `app_mention` and `message.channels` are subscribed (their event
        # objects have different `event.ts` rounding but the same wrapper
        # event_id). Without this, every @mention triggers TWO worker
        # invocations -> 2x prompt cost. event_id absent (e.g. legacy
        # fixtures, malformed payloads): skip the check rather than reject.
        event_id = payload.get("event_id")
        if event_id and not _claim_event_idempotent(event_id):
            logger.info(json.dumps({
                "msg": "skipped_duplicate_event",
                "event_id": event_id,
            }))
            return {"statusCode": 200, "body": "ack-duplicate"}

        inner = payload.get("event") or {}
        # Bots replying to themselves: ignore (defensive; subscription
        # uses bot events, not user events, but cover both)
        if inner.get("bot_id") or inner.get("subtype") == "bot_message":
            return {"statusCode": 200, "body": "ack-bot"}

        event_type = inner.get("type")

        # P1-18: Auto-respond to thread replies without requiring @mention.
        # We subscribe to `message.channels` (and optionally `message.groups`)
        # so the bot receives every message in channels it's in. To avoid
        # responding to unrelated chatter, only treat a `message` event as a
        # mention when ALL of the following hold:
        #   - it's a thread reply (has thread_ts and thread_ts != ts)
        #   - the thread_ts hits the DDB threads table (i.e. the bot has
        #     previously created a chat in this thread)
        #   - subtype is plain (no message_changed / message_deleted / etc)
        # Anything else: ack 200 and drop. We do NOT log message bodies here
        # to avoid leaking unrelated channel content into CloudWatch.
        if event_type == "message" and not _is_thread_reply_for_us(inner):
            # Best-effort EMF metric for noise visibility — no message body.
            _emit_unhandled_message_metric()
            return {"statusCode": 200, "body": "ack-msg-ignored"}

        if event_type in ("app_mention", "message"):
            # Async invoke self for the heavy work
            try:
                _get_lambda_client().invoke(
                    FunctionName=context.invoked_function_arn,
                    InvocationType="Event",
                    Payload=json.dumps({
                        "_internal": "chat",
                        "slack_event": inner,
                        "team_id": payload.get("team_id"),
                    }).encode("utf-8"),
                )
            except botocore.exceptions.ClientError:
                request_id = getattr(context, "aws_request_id", "unknown")
                logger.exception(json.dumps({
                    "msg": "self_invoke_failed",
                    "request_id": request_id,
                }))
                # Don't 500 — Slack would retry. Acknowledge and surface
                # a sanitized error to channel best-effort.
                try:
                    post_message(
                        inner["channel"],
                        SLACK_ERROR_TEMPLATE.format(request_id=request_id),
                        thread_ts=inner.get("thread_ts") or inner["ts"],
                    )
                except Exception:
                    logger.exception("post_message failed during dispatch error")
            return {"statusCode": 200, "body": "ack"}

    # Unknown event type — ack and move on
    return {"statusCode": 200, "body": "ack-unknown"}


def _is_thread_reply_for_us(inner: dict) -> bool:
    """Return True iff this `message` event is a thread reply in a thread
    the bot has previously seen (DDB hit). Used by P1-18 auto-respond.

    Drop conditions (return False):
      - subtype set (message_changed / message_deleted / file_share / etc)
      - no thread_ts, OR thread_ts == ts (top-level message, not a reply)
      - DDB lookup miss
      - DDB lookup error (fail-closed: don't respond to random channel chatter)
    """
    if inner.get("subtype"):
        return False
    thread_ts = inner.get("thread_ts")
    ts = inner.get("ts")
    if not thread_ts or thread_ts == ts:
        return False
    # DDB existence check — no body logged.
    try:
        table = _get_thread_table()
        resp = table.get_item(
            Key={"thread_ts": thread_ts},
            ProjectionExpression="execution_id",
        )
    except botocore.exceptions.ClientError as e:
        logger.warning(json.dumps({
            "msg": "thread_lookup_failed",
            "error": str(e),
        }))
        return False
    item = resp.get("Item") or {}
    eid = item.get("execution_id")
    return bool(eid) and eid != PENDING_EXECUTION_ID


def _emit_unhandled_message_metric() -> None:
    """Emit a CloudWatch EMF count for unhandled message events.

    Helps gauge noise from `message.channels` subscription without ever
    logging the message body. One log line per drop is acceptable —
    CloudWatch ingest cost is dominated by message bodies, not counts.
    """
    try:
        # CloudWatch Embedded Metric Format requires a raw JSON line on
        # stdout. Lambda's logger wraps lines with [LEVEL]\t<request_id>
        # which breaks EMF parsing, so this stays as `print` deliberately.
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "DevOpsAgent/SlackChatbot",
                    "Dimensions": [[]],
                    "Metrics": [
                        {"Name": "UnhandledMessageEvents", "Unit": "Count"},
                    ],
                }],
            },
            "UnhandledMessageEvents": 1,
        }))
    except Exception:
        # Metric emission must never break the request.
        pass


# ----- Path 2: Worker (async invoked by self) ----------------------------

def _worker_path(event: dict, context) -> dict:
    """Process a Slack app_mention asynchronously."""
    request_id = getattr(context, "aws_request_id", "unknown")
    slack_event = event["slack_event"]
    channel = slack_event["channel"]
    user = slack_event.get("user", "anonymous")
    raw_text = slack_event.get("text", "")
    parent_ts = slack_event["ts"]
    thread_ts = slack_event.get("thread_ts") or parent_ts

    # Strip the "<@U0...>" mention prefix
    text = MENTION_RE.sub("", raw_text).strip()
    if not text:
        post_message(
            channel,
            ":wave: Mention me with a question, e.g. `@devops_agent list EC2`.",
            thread_ts=thread_ts,
        )
        return {"statusCode": 200, "body": "empty"}

    if len(text) > MAX_USER_PROMPT_CHARS:
        post_message(
            channel,
            PROMPT_TOO_LONG_TEMPLATE.format(
                length=len(text), limit=MAX_USER_PROMPT_CHARS,
            ),
            thread_ts=thread_ts,
        )
        return {"statusCode": 200, "body": "prompt-too-long"}

    # Post a placeholder so the user sees something within seconds
    placeholder_ts: str | None = None
    try:
        placeholder = post_message(
            channel,
            ":hourglass_flowing_sand: investigating...",
            thread_ts=thread_ts,
        )
        placeholder_ts = placeholder["ts"]
    except Exception:
        logger.exception(json.dumps({
            "msg": "placeholder_failed",
            "request_id": request_id,
        }))

    # Multi-turn: reuse executionId per Slack thread (DDB-backed)
    started_at = time.time()
    execution_id: str | None = None
    is_new_chat = False
    try:
        agent = _get_devops_client()
        execution_id, is_new_chat = _get_or_create_chat(
            thread_ts=thread_ts,
            channel=channel,
            user=user,
            agent_client=agent,
        )

        result = agent_chat.stream_message(
            client=agent,
            agent_space_id=DEVOPS_AGENT_SPACE_ID,
            execution_id=execution_id,
            content=text,
            user_id=user,
            on_final_text=lambda chunk: None,  # accumulate, don't stream
            on_thinking=lambda chunk: None,
            on_meta=lambda kind, payload: None,
        )
        final = result.get("text") or "(empty response)"
        if result.get("failed"):
            final = f":warning: investigation failed\n{final}"

        # Append a small footer with token usage / context util
        usage = result.get("usage") or {}
        ctx = (result.get("context_usage") or {}).get("data", {}).get(
            "context_window", {})
        elapsed = int(time.time() - started_at)
        footer_bits = [f"_{elapsed}s_"]
        if usage:
            footer_bits.append(
                f"_in={usage.get('inputTokens', '?')} "
                f"out={usage.get('outputTokens', '?')}_"
            )
        if ctx:
            footer_bits.append(f"_ctx {ctx.get('utilization', '?')}%_")
        final_with_footer = f"{final}\n\n{' · '.join(footer_bits)}"

    except Exception:
        # Never surface raw error strings to Slack — they often contain ARNs,
        # account IDs, or internal endpoints. Log full traceback for ops,
        # send a sanitized fixed-format message to the user.
        logger.exception(json.dumps({
            "msg": "worker_failed",
            "request_id": request_id,
            "thread_ts": thread_ts,
            "channel": channel,
            "user": user,
            "execution_id": execution_id,
        }))
        final_with_footer = SLACK_ERROR_TEMPLATE.format(request_id=request_id)

    # Replace placeholder with the answer (or post fresh if placeholder failed)
    try:
        if placeholder_ts:
            update_message(channel, placeholder_ts, final_with_footer)
        else:
            post_message(channel, final_with_footer, thread_ts=thread_ts)
    except Exception:
        # Last-resort: post a fresh message so the user sees the answer
        logger.exception(json.dumps({
            "msg": "update_failed",
            "request_id": request_id,
        }))
        try:
            post_message(channel, final_with_footer, thread_ts=thread_ts)
        except Exception:
            logger.exception("fallback post_message also failed")

    logger.info(json.dumps({
        "msg": "chat_complete",
        "elapsed_s": int(time.time() - started_at),
        "user": user,
        "channel": channel,
        "thread_ts": thread_ts,
        "execution_id": execution_id,
        "is_new_chat": is_new_chat,
        "region": REGION,
        "request_id": request_id,
    }))
    return {"statusCode": 200, "body": "done"}


# ----- Lambda entry --------------------------------------------------------

def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    try:
        # Routing: async self-invoke marks itself with _internal
        if isinstance(event, dict) and event.get("_internal") == "chat":
            return _worker_path(event, context)
        return _fast_path(event, context)
    except Exception:
        logger.exception(json.dumps({
            "msg": "handler_unhandled",
            "request_id": request_id,
        }))
        # Re-raise on the worker path so async-invoke retries / DLQ kicks in.
        # On the fast path we'd rather 500 than hang Slack — but since fast
        # path already swallows known errors, an unhandled here is genuinely
        # unexpected and should surface.
        raise
