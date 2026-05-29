"""Unit tests for lambda_c handler dispatch.

Three branches in `_fast_path` are exercised:
  1. url_verification challenge → echoes challenge back
  2. X-Slack-Retry-Num header → ack-retry without invoking worker
  3. app_mention event → async self-invoke

Plus a worker_path test for the P2-19 prompt-length truncation.

Module-level secrets manager / boto3 / DDB calls are mocked at import.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_C_PATH = ROOT / "lambda" / "lambda_c" / "lambda_function.py"
SIGNING_SECRET = "test-signing-secret-value"


def _load_lambda_c():
    if "lambda_c_module" in sys.modules:
        return sys.modules["lambda_c_module"]
    # boto3 clients are lazy in lambda_c, so a top-level import does not
    # require boto3 to actually call AWS. But add a defensive patch so
    # the import is hermetic.
    with patch("boto3.client", return_value=MagicMock()), \
         patch("boto3.resource", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location(
            "lambda_c_module", LAMBDA_C_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["lambda_c_module"] = module
        spec.loader.exec_module(module)
    return module


lambda_c = _load_lambda_c()


def _sign(body: bytes, ts: int, secret: str = SIGNING_SECRET) -> dict:
    base = f"v0:{ts}:".encode("utf-8") + body
    sig = hmac.new(
        key=secret.encode(), msg=base, digestmod=hashlib.sha256,
    ).hexdigest()
    return {
        "x-slack-request-timestamp": str(ts),
        "x-slack-signature": f"v0={sig}",
    }


def _signed_event(body: dict, *, headers_extra: dict | None = None) -> dict:
    body_bytes = json.dumps(body).encode("utf-8")
    ts = int(time.time())
    headers = _sign(body_bytes, ts)
    if headers_extra:
        headers.update(headers_extra)
    return {
        "headers": headers,
        "body": body_bytes.decode("utf-8"),
        "isBase64Encoded": False,
    }


def _ctx():
    return SimpleNamespace(
        aws_request_id="req-test",
        invoked_function_arn=(
            "arn:aws:lambda:ap-northeast-1:000000000000:"
            "function:devops-agent-slack-chatbot"
        ),
    )


class FastPathTests(unittest.TestCase):

    def setUp(self):
        # Pin the signing secret used by _fast_path
        self.patches = [
            patch.object(lambda_c, "_get_signing_secret",
                         return_value=SIGNING_SECRET),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_url_verification_returns_challenge(self):
        challenge = "abc123challenge"
        event = _signed_event({
            "type": "url_verification",
            "challenge": challenge,
        })
        result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], challenge)

    def test_retry_header_acks_without_invoking(self):
        # Slack retry events are dropped without scheduling a worker
        event = _signed_event(
            {
                "type": "event_callback",
                "event": {"type": "app_mention", "channel": "C", "ts": "1.0"},
            },
            headers_extra={"x-slack-retry-num": "1"},
        )
        with patch.object(lambda_c, "_get_lambda_client") as mock_lambda:
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ack-retry")
        mock_lambda.assert_not_called()

    def test_app_mention_async_invokes_worker(self):
        event = _signed_event({
            "type": "event_callback",
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": "<@U0BOT123> hello",
                "ts": "1.0",
            },
        })
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ack")
        mock_lambda.invoke.assert_called_once()
        kwargs = mock_lambda.invoke.call_args.kwargs
        self.assertEqual(kwargs["InvocationType"], "Event")
        payload = json.loads(kwargs["Payload"].decode("utf-8"))
        self.assertEqual(payload["_internal"], "chat")
        self.assertEqual(payload["slack_event"]["channel"], "C1")
        self.assertEqual(payload["team_id"], "T1")

    def test_bot_message_subtype_acked(self):
        event = _signed_event({
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "subtype": "bot_message",
                "channel": "C", "ts": "1.0",
            },
        })
        result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ack-bot")

    def test_invalid_signature_rejected(self):
        body_bytes = b'{"type":"url_verification","challenge":"x"}'
        ts = int(time.time())
        headers = _sign(body_bytes, ts, secret="wrong-secret")
        event = {
            "headers": headers,
            "body": body_bytes.decode("utf-8"),
            "isBase64Encoded": False,
        }
        result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["statusCode"], 401)


class WorkerPathPromptLengthTests(unittest.TestCase):
    """P2-19: prompt longer than MAX_USER_PROMPT_CHARS rejected with friendly
    Slack message before any DevOps Agent call."""

    def _worker_event(self, text: str) -> dict:
        return {
            "_internal": "chat",
            "slack_event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": text,
                "ts": "1.0",
            },
        }

    def test_within_limit_proceeds(self):
        text = f"<@U0BOT123> {'x' * 100}"  # well under 4000
        with patch.object(lambda_c, "post_message") as mock_post, \
             patch.object(lambda_c, "_get_or_create_chat",
                          return_value=("exec-1", True)), \
             patch.object(lambda_c.agent_chat, "stream_message",
                          return_value={"text": "ok", "usage": {},
                                         "context_usage": {"data": {}}}), \
             patch.object(lambda_c, "update_message"), \
             patch.object(lambda_c, "_get_devops_client",
                          return_value=MagicMock()):
            mock_post.return_value = {"ts": "2.0"}
            result = lambda_c.lambda_handler(
                self._worker_event(text), _ctx(),
            )
        self.assertEqual(result["statusCode"], 200)
        # Should NOT receive prompt-too-long message
        sent_messages = [c.args[1] for c in mock_post.call_args_list]
        for msg in sent_messages:
            self.assertNotIn("prompt is too long", msg)

    def test_at_limit_proceeds(self):
        # Strip mention prefix is fixed length; build payload of exactly 4000
        prompt = "y" * lambda_c.MAX_USER_PROMPT_CHARS
        text = f"<@U0BOT123> {prompt}"
        with patch.object(lambda_c, "post_message") as mock_post, \
             patch.object(lambda_c, "_get_or_create_chat",
                          return_value=("exec-1", True)), \
             patch.object(lambda_c.agent_chat, "stream_message",
                          return_value={"text": "ok", "usage": {},
                                         "context_usage": {"data": {}}}), \
             patch.object(lambda_c, "update_message"), \
             patch.object(lambda_c, "_get_devops_client",
                          return_value=MagicMock()):
            mock_post.return_value = {"ts": "2.0"}
            result = lambda_c.lambda_handler(
                self._worker_event(text), _ctx(),
            )
        self.assertEqual(result["statusCode"], 200)
        sent_messages = [c.args[1] for c in mock_post.call_args_list]
        for msg in sent_messages:
            self.assertNotIn("prompt is too long", msg)

    def test_over_limit_rejected_with_friendly_message(self):
        prompt = "z" * (lambda_c.MAX_USER_PROMPT_CHARS + 1)
        text = f"<@U0BOT123> {prompt}"
        with patch.object(lambda_c, "post_message") as mock_post, \
             patch.object(lambda_c, "_get_or_create_chat") as mock_create:
            result = lambda_c.lambda_handler(
                self._worker_event(text), _ctx(),
            )
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "prompt-too-long")
        # No chat creation attempted
        mock_create.assert_not_called()
        # Slack got the friendly warning
        msgs = [c.args[1] for c in mock_post.call_args_list]
        self.assertTrue(any("prompt is too long" in m for m in msgs))


class ThreadReplyAutoRespondTests(unittest.TestCase):
    """P1-18: message events should auto-trigger worker when DDB hits."""

    def setUp(self):
        self.patches = [
            patch.object(lambda_c, "_get_signing_secret",
                         return_value=SIGNING_SECRET),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _msg_event(self, *, thread_ts=None, ts="100.5", subtype=None,
                   bot_id=None):
        inner = {
            "type": "message",
            "channel": "C1",
            "user": "U1",
            "text": "follow-up question",
            "ts": ts,
        }
        if thread_ts is not None:
            inner["thread_ts"] = thread_ts
        if subtype is not None:
            inner["subtype"] = subtype
        if bot_id is not None:
            inner["bot_id"] = bot_id
        return _signed_event({
            "type": "event_callback",
            "team_id": "T1",
            "event": inner,
        })

    def test_thread_reply_with_ddb_hit_invokes_worker(self):
        event = self._msg_event(thread_ts="99.0", ts="100.5")
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"execution_id": "exec-real-123"},
        }
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ack")
        mock_lambda.invoke.assert_called_once()
        # DDB lookup should use ProjectionExpression to avoid pulling body
        get_kwargs = mock_table.get_item.call_args.kwargs
        self.assertEqual(get_kwargs["Key"], {"thread_ts": "99.0"})
        self.assertIn("ProjectionExpression", get_kwargs)

    def test_top_level_message_dropped(self):
        # No thread_ts -> top-level channel message, never respond
        event = self._msg_event(thread_ts=None, ts="100.5")
        mock_lambda = MagicMock()
        mock_table = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-msg-ignored")
        mock_lambda.invoke.assert_not_called()
        # Top-level: short-circuits before DDB lookup
        mock_table.get_item.assert_not_called()

    def test_thread_parent_self_dropped(self):
        # ts == thread_ts is the parent, not a reply
        event = self._msg_event(thread_ts="100.5", ts="100.5")
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-msg-ignored")
        mock_lambda.invoke.assert_not_called()

    def test_thread_reply_ddb_miss_dropped(self):
        # Thread the bot has never seen — drop
        event = self._msg_event(thread_ts="99.0", ts="100.5")
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no Item
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-msg-ignored")
        mock_lambda.invoke.assert_not_called()

    def test_thread_reply_pending_execution_dropped(self):
        # PENDING placeholder must not count as a hit
        event = self._msg_event(thread_ts="99.0", ts="100.5")
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"execution_id": lambda_c.PENDING_EXECUTION_ID},
        }
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-msg-ignored")
        mock_lambda.invoke.assert_not_called()

    def test_message_with_subtype_dropped(self):
        # message_changed / file_share / etc — drop without DDB lookup
        event = self._msg_event(
            thread_ts="99.0", ts="100.5", subtype="message_changed",
        )
        mock_lambda = MagicMock()
        mock_table = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-msg-ignored")
        mock_lambda.invoke.assert_not_called()
        mock_table.get_item.assert_not_called()

    def test_message_from_bot_acked_early(self):
        # bot_id set: hit the early bot_message guard before our logic
        event = self._msg_event(
            thread_ts="99.0", ts="100.5", bot_id="B0BOT",
        )
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-bot")
        mock_lambda.invoke.assert_not_called()

    def test_ddb_error_fails_closed(self):
        # If DDB get_item throws, we must NOT respond
        from botocore.exceptions import ClientError
        event = self._msg_event(thread_ts="99.0", ts="100.5")
        mock_table = MagicMock()
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "x"}},
            "GetItem",
        )
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(event, _ctx())
        self.assertEqual(result["body"], "ack-msg-ignored")
        mock_lambda.invoke.assert_not_called()


class EventIdempotencyTests(unittest.TestCase):
    """P0-4: Slack event_id duplicate suppression. Same event_id arriving
    twice (e.g. app_mention + message.channels double-subscribe, or Slack
    replay) must invoke the worker exactly once."""

    def setUp(self):
        self.patches = [
            patch.object(lambda_c, "_get_signing_secret",
                         return_value=SIGNING_SECRET),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _mention_event(self, event_id: str) -> dict:
        return _signed_event({
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": "<@U0BOT123> hello",
                "ts": "1.0",
            },
        })

    def test_first_event_proceeds_and_claims_idempotency_row(self):
        mock_table = MagicMock()  # put_item succeeds (no ConditionalCheck)
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(
                self._mention_event("Ev_first"), _ctx(),
            )
        self.assertEqual(result["body"], "ack")
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["thread_ts"], "evt:Ev_first")
        self.assertEqual(item["event_id"], "Ev_first")
        self.assertIn("ttl", item)
        mock_lambda.invoke.assert_called_once()

    def test_duplicate_event_dropped_without_invoking_worker(self):
        from botocore.exceptions import ClientError
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException",
                       "Message": "already claimed"}},
            "PutItem",
        )
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(
                self._mention_event("Ev_dup"), _ctx(),
            )
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ack-duplicate")
        mock_lambda.invoke.assert_not_called()

    def test_ddb_error_fails_open_and_proceeds(self):
        from botocore.exceptions import ClientError
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "throttle"}},
            "PutItem",
        )
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(
                self._mention_event("Ev_throttle"), _ctx(),
            )
        self.assertEqual(result["body"], "ack")
        mock_lambda.invoke.assert_called_once()

    def test_missing_event_id_skips_idempotency_check(self):
        body = {
            "type": "event_callback",
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": "<@U0BOT123> hi",
                "ts": "1.0",
            },
        }
        mock_table = MagicMock()
        mock_lambda = MagicMock()
        with patch.object(lambda_c, "_get_thread_table",
                          return_value=mock_table), \
             patch.object(lambda_c, "_get_lambda_client",
                          return_value=mock_lambda):
            result = lambda_c.lambda_handler(_signed_event(body), _ctx())
        self.assertEqual(result["body"], "ack")
        mock_table.put_item.assert_not_called()
        mock_lambda.invoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
