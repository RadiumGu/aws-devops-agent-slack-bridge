"""Unit tests for lambda_b pure helpers + handler dispatch.

Lambda-B calls Secrets Manager at module-import time to cache the webhook
URL. We patch boto3.client before loading so the import succeeds without
real AWS credentials.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_B_PATH = ROOT / "lambda" / "lambda_b" / "lambda_function.py"


def _load_lambda_b():
    if "lambda_b_module" in sys.modules:
        return sys.modules["lambda_b_module"]

    devops_stub = MagicMock(name="devops-agent")
    secrets_stub = MagicMock(name="secretsmanager")
    secrets_stub.get_secret_value.return_value = {
        "SecretString": "https://hooks.slack.com/services/T/B/redacted",
    }

    def fake_boto3_client(service, **_kwargs):
        if service == "devops-agent":
            return devops_stub
        if service == "secretsmanager":
            return secrets_stub
        raise AssertionError(f"unexpected service {service}")

    with patch("boto3.client", side_effect=fake_boto3_client):
        spec = importlib.util.spec_from_file_location(
            "lambda_b_module", LAMBDA_B_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["lambda_b_module"] = module
        spec.loader.exec_module(module)
    return module


lambda_b = _load_lambda_b()


class ChunkForSlackTests(unittest.TestCase):

    def test_short_text_unchanged(self):
        chunks = lambda_b._chunk_for_slack("hello")
        self.assertEqual(chunks, ["hello"])

    def test_text_at_limit_unchanged(self):
        text = "a" * lambda_b.SLACK_BLOCK_LIMIT
        self.assertEqual(lambda_b._chunk_for_slack(text), [text])

    def test_long_text_split_at_line_boundary(self):
        # Build text just over the limit composed of newline-terminated lines.
        line = "x" * 500 + "\n"
        text = line * 8  # 4000 > 2900 limit
        chunks = lambda_b._chunk_for_slack(text, limit=1500)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1500)
        # Reassembled text matches original
        self.assertEqual("".join(chunks), text)

    def test_single_long_line_falls_through(self):
        # When a single line exceeds the limit, the function still emits it
        # (Slack will reject — we accept the limitation; behaviour is to
        # not silently drop content).
        big = "y" * 5000
        chunks = lambda_b._chunk_for_slack(big, limit=2000)
        self.assertEqual("".join(chunks), big)


class FormatDurationTests(unittest.TestCase):

    def test_basic_minute_second(self):
        out = lambda_b._format_duration(
            "2026-05-20T10:00:00Z", "2026-05-20T10:03:42Z",
        )
        self.assertEqual(out, "3m 42s")

    def test_zero_duration(self):
        ts = "2026-05-20T10:00:00Z"
        self.assertEqual(lambda_b._format_duration(ts, ts), "0m 0s")

    def test_under_minute(self):
        out = lambda_b._format_duration(
            "2026-05-20T10:00:00Z", "2026-05-20T10:00:42Z",
        )
        self.assertEqual(out, "0m 42s")

    def test_negative_duration_returns_empty(self):
        out = lambda_b._format_duration(
            "2026-05-20T10:01:00Z", "2026-05-20T10:00:00Z",
        )
        self.assertEqual(out, "")

    def test_unparseable_returns_empty(self):
        self.assertEqual(lambda_b._format_duration("garbage", "more"), "")

    def test_missing_one_returns_empty(self):
        self.assertEqual(lambda_b._format_duration("", "2026-05-20T10:00:00Z"), "")
        self.assertEqual(lambda_b._format_duration("2026-05-20T10:00:00Z", ""), "")

    def test_offset_timezone_iso(self):
        out = lambda_b._format_duration(
            "2026-05-20T10:00:00+00:00", "2026-05-20T10:01:30+00:00",
        )
        self.assertEqual(out, "1m 30s")


class BuildBlocksTests(unittest.TestCase):

    METADATA = {
        "task_id": "task-1",
        "agent_space_id": "space-1",
        "execution_id": "exec-1",
    }

    def test_completed_blocks_have_header_and_metadata(self):
        data = {
            "createdAt": "2026-05-20T10:00:00Z",
            "updatedAt": "2026-05-20T10:03:42Z",
        }
        blocks = lambda_b._build_blocks(self.METADATA, data, "summary body")
        self.assertEqual(blocks[0]["type"], "header")
        self.assertIn("Completed", blocks[0]["text"]["text"])
        # fields section
        fields_text = json.dumps(blocks[1]["fields"])
        self.assertIn("task-1", fields_text)
        self.assertIn("exec-1", fields_text)
        self.assertIn("space-1", fields_text)
        self.assertIn("Started", fields_text)
        self.assertIn("Duration", fields_text)
        # Summary is appended after divider
        body_text = blocks[-1]["text"]["text"]
        self.assertEqual(body_text, "summary body")

    def test_failure_blocks_for_failed_status(self):
        data = {
            "status": "FAILED",
            "failureReason": "Agent timeout after 600s",
        }
        blocks = lambda_b._build_failure_blocks(self.METADATA, data, "FAILED")
        self.assertIn("Failed", blocks[0]["text"]["text"])
        self.assertIn(":x:", blocks[0]["text"]["text"])
        fields_text = json.dumps(blocks[1]["fields"])
        self.assertIn("Failure reason", fields_text)
        self.assertIn("Agent timeout after 600s", fields_text)

    def test_failure_blocks_with_missing_reason(self):
        blocks = lambda_b._build_failure_blocks(self.METADATA, {}, "FAILED")
        self.assertIn(
            "Reason not provided by Agent",
            json.dumps(blocks[1]["fields"]),
        )

    def test_cancelled_blocks(self):
        data = {"cancellationReason": "user cancelled"}
        blocks = lambda_b._build_failure_blocks(self.METADATA, data, "CANCELLED")
        self.assertIn("Cancelled", blocks[0]["text"]["text"])
        self.assertIn(":warning:", blocks[0]["text"]["text"])
        fields_text = json.dumps(blocks[1]["fields"])
        self.assertIn("Cancellation reason", fields_text)
        self.assertIn("user cancelled", fields_text)


class HandlerTests(unittest.TestCase):

    METADATA = {
        "task_id": "task-1",
        "agent_space_id": "space-1",
        "execution_id": "exec-1",
    }

    def _event(self, status="COMPLETED", extra_data=None):
        data = {"status": status} if status else {}
        if extra_data:
            data.update(extra_data)
        return {
            "detail": {
                "metadata": self.METADATA,
                "data": data,
            }
        }

    def test_missing_metadata_returns_400(self):
        result = lambda_b.lambda_handler(
            {"detail": {"metadata": {}, "data": {}}}, None,
        )
        self.assertEqual(result["statusCode"], 400)

    def test_completed_posts_to_slack(self):
        with patch.object(lambda_b, "get_investigation_summary",
                          return_value="summary md") as mock_summary, \
             patch.object(lambda_b, "post_to_slack") as mock_post:
            result = lambda_b.lambda_handler(self._event("COMPLETED"), None)
        self.assertEqual(result["statusCode"], 200)
        mock_summary.assert_called_once()
        mock_post.assert_called_once()
        blocks_arg, fallback = mock_post.call_args.args
        self.assertIn("Completed", blocks_arg[0]["text"]["text"])
        self.assertIn("completed", fallback)

    def test_failed_skips_summary_uses_failure_blocks(self):
        with patch.object(lambda_b, "get_investigation_summary") as mock_summary, \
             patch.object(lambda_b, "post_to_slack") as mock_post:
            result = lambda_b.lambda_handler(
                self._event("FAILED",
                            extra_data={"failureReason": "boom"}),
                None,
            )
        self.assertEqual(result["statusCode"], 200)
        mock_summary.assert_not_called()
        blocks_arg, fallback = mock_post.call_args.args
        self.assertIn("Failed", blocks_arg[0]["text"]["text"])
        self.assertIn("failed", fallback)

    def test_cancelled_uses_failure_blocks(self):
        with patch.object(lambda_b, "get_investigation_summary") as mock_summary, \
             patch.object(lambda_b, "post_to_slack") as mock_post:
            result = lambda_b.lambda_handler(
                self._event("CANCELLED",
                            extra_data={"cancellationReason": "user cancel"}),
                None,
            )
        self.assertEqual(result["statusCode"], 200)
        mock_summary.assert_not_called()
        blocks_arg, _ = mock_post.call_args.args
        self.assertIn("Cancelled", blocks_arg[0]["text"]["text"])

    def test_in_progress_skipped_without_post(self):
        with patch.object(lambda_b, "get_investigation_summary") as mock_summary, \
             patch.object(lambda_b, "post_to_slack") as mock_post:
            result = lambda_b.lambda_handler(self._event("IN_PROGRESS"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("Skipped", result["body"])
        mock_summary.assert_not_called()
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
