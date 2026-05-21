"""Tests for slack_verify."""
import hashlib
import hmac
import sys
import time
import unittest

# Ensure lambda_c module is importable when running pytest from repo root
sys.path.insert(0, "lambda/lambda_c")

from slack_verify import (  # noqa: E402
    SignatureError,
    verify_slack_signature,
)

SECRET = "8f742231b10e8888abcd99baccd14e5d"  # synthetic, 32 hex chars


def _sign(body: bytes, ts: int, secret: str = SECRET) -> dict:
    base = f"v0:{ts}:".encode("utf-8") + body
    sig = hmac.new(
        key=secret.encode(),
        msg=base,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return {
        "x-slack-request-timestamp": str(ts),
        "x-slack-signature": f"v0={sig}",
    }


class VerifySignatureTests(unittest.TestCase):

    def test_valid_signature_passes(self):
        body = b'{"type":"event_callback"}'
        ts = int(time.time())
        headers = _sign(body, ts)
        # Should not raise
        verify_slack_signature(headers, body, SECRET, now=ts)

    def test_uppercase_headers_normalized(self):
        body = b'{"x":1}'
        ts = int(time.time())
        signed = _sign(body, ts)
        # Slack normally sends lowercase, but be tolerant of upper
        headers = {
            "X-Slack-Request-Timestamp": signed["x-slack-request-timestamp"],
            "X-Slack-Signature": signed["x-slack-signature"],
        }
        verify_slack_signature(headers, body, SECRET, now=ts)

    def test_old_timestamp_rejected(self):
        body = b"{}"
        ts = int(time.time()) - 600  # 10 minutes ago
        headers = _sign(body, ts)
        with self.assertRaisesRegex(SignatureError, "timestamp"):
            verify_slack_signature(headers, body, SECRET, now=time.time())

    def test_future_timestamp_rejected(self):
        body = b"{}"
        ts = int(time.time()) + 600  # 10 minutes ahead
        headers = _sign(body, ts)
        with self.assertRaisesRegex(SignatureError, "timestamp"):
            verify_slack_signature(headers, body, SECRET, now=time.time())

    def test_modified_body_rejected(self):
        body = b'{"type":"event_callback"}'
        ts = int(time.time())
        headers = _sign(body, ts)
        # Tamper with body: signature was for original
        with self.assertRaisesRegex(SignatureError, "signature does not match"):
            verify_slack_signature(headers, b'{"type":"tampered"}',
                                    SECRET, now=ts)

    def test_wrong_secret_rejected(self):
        body = b"{}"
        ts = int(time.time())
        headers = _sign(body, ts, secret="wrong-secret-aaaa")
        with self.assertRaisesRegex(SignatureError, "signature does not match"):
            verify_slack_signature(headers, body, SECRET, now=ts)

    def test_missing_headers_rejected(self):
        with self.assertRaisesRegex(SignatureError, "missing"):
            verify_slack_signature({}, b"{}", SECRET)

    def test_missing_signature_rejected(self):
        with self.assertRaisesRegex(SignatureError, "missing"):
            verify_slack_signature(
                {"x-slack-request-timestamp": str(int(time.time()))},
                b"{}", SECRET,
            )

    def test_invalid_timestamp_format(self):
        with self.assertRaisesRegex(SignatureError, "not an integer"):
            verify_slack_signature(
                {
                    "x-slack-request-timestamp": "not-a-number",
                    "x-slack-signature": "v0=abc",
                },
                b"{}", SECRET, now=time.time(),
            )

    def test_empty_secret_rejected(self):
        with self.assertRaisesRegex(SignatureError, "signing_secret is empty"):
            verify_slack_signature(
                {"x-slack-request-timestamp": str(int(time.time())),
                 "x-slack-signature": "v0=abc"},
                b"{}", "",
            )

    def test_non_bytes_body_rejected(self):
        with self.assertRaisesRegex(SignatureError, "must be bytes"):
            verify_slack_signature({}, "not bytes", SECRET)  # type: ignore[arg-type]

    def test_timing_safe_comparison(self):
        # Just sanity: hmac.compare_digest is used. We can't easily
        # test the timing property, but ensure the signature differs in
        # the middle and is still rejected (not short-circuit prefix match).
        body = b"{}"
        ts = int(time.time())
        headers = _sign(body, ts)
        # Mutate one char in the middle of the sig
        sig = headers["x-slack-signature"]
        bad = sig[:30] + ("1" if sig[30] != "1" else "2") + sig[31:]
        headers["x-slack-signature"] = bad
        with self.assertRaisesRegex(SignatureError, "signature does not match"):
            verify_slack_signature(headers, body, SECRET, now=ts)


if __name__ == "__main__":
    unittest.main()
