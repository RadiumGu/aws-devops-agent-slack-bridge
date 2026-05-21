"""
Slack request signature verification.

Implements https://api.slack.com/authentication/verifying-requests-from-slack
"""
import hashlib
import hmac
import time
from typing import Mapping


# Slack rejects requests older than 5 minutes (replay protection)
MAX_TIMESTAMP_AGE_SECONDS = 5 * 60


class SignatureError(ValueError):
    """Raised when a Slack request signature does not validate."""


def verify_slack_signature(
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secret: str,
    *,
    now: float | None = None,
) -> None:
    """Verify a Slack signed request.

    Raises SignatureError on any failure. Returns None on success.

    Args:
        headers:        Lowercased header map (API GW HTTP API v2 already
                        delivers lowercased keys, but be defensive).
        raw_body:       Raw request body as bytes (NOT a JSON-decoded dict —
                        any normalization breaks the HMAC).
        signing_secret: From Slack App's "Signing Secret" field.
        now:            Optional override for the current epoch seconds
                        (used in tests).
    """
    if not isinstance(raw_body, (bytes, bytearray)):
        raise SignatureError(
            f"raw_body must be bytes, got {type(raw_body).__name__}"
        )
    if not signing_secret:
        raise SignatureError("signing_secret is empty")

    # API Gateway HTTP API normalizes keys to lowercase, but
    # headers from raw httpd would be capitalized. Build a
    # lowercased view to be safe.
    lower = {k.lower(): v for k, v in headers.items()}

    timestamp = lower.get("x-slack-request-timestamp")
    signature = lower.get("x-slack-signature")
    if not timestamp or not signature:
        raise SignatureError(
            "missing X-Slack-Request-Timestamp or X-Slack-Signature header"
        )

    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError) as e:
        raise SignatureError(f"timestamp is not an integer: {timestamp!r}") from e

    current = time.time() if now is None else now
    if abs(current - ts_int) > MAX_TIMESTAMP_AGE_SECONDS:
        raise SignatureError(
            f"timestamp {ts_int} too far from current time {int(current)} "
            f"(max age {MAX_TIMESTAMP_AGE_SECONDS}s)"
        )

    base = f"v0:{timestamp}:".encode("utf-8") + raw_body
    digest = hmac.new(
        key=signing_secret.encode("utf-8"),
        msg=base,
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"

    # Use timing-safe comparison to prevent timing attacks
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("signature does not match")
