"""Shared pytest setup: stub AWS env, extend sys.path so each Lambda
module can be imported as a top-level `lambda_function`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Make project root importable so `from lib import agent_chat` works.
sys.path.insert(0, str(ROOT))

# Lambda-C imports `slack_verify` and `agent_chat` at module top, so add its
# package dir too. Lambda-A and Lambda-B don't share top-level module names,
# but their loaders import them by file path inside the tests.
sys.path.insert(0, str(ROOT / "lambda" / "lambda_c"))

# In the deployed Lambda-C zip, `agent_chat.py` is at package root (the
# deploy script copies it there). Locally it lives under `lib/`. Putting
# `lib/` on the path lets Lambda-C's `import agent_chat` succeed in tests.
sys.path.insert(0, str(ROOT / "lib"))

# Required env vars consumed at import time of each Lambda module. Set
# before any test imports them; individual tests can override.
os.environ.setdefault("AWS_REGION", "ap-northeast-1")
os.environ.setdefault("DEVOPS_AGENT_SPACE_ID", "test-space-id")
os.environ.setdefault(
    "SLACK_WEBHOOK_SECRET_ARN",
    "arn:aws:secretsmanager:ap-northeast-1:000000000000:secret:test-webhook",
)
os.environ.setdefault("SLACK_CHANNEL", "#test")
os.environ.setdefault("SLACK_BOT_TOKEN_SECRET_ID", "test-bot-token")
os.environ.setdefault("SLACK_SIGNING_SECRET_ID", "test-signing-secret")
