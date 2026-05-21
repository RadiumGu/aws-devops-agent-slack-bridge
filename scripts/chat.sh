#!/usr/bin/env bash
# DevOps Agent Chat CLI — wrapper around lambda/cli/chat.py.
# Loads .env so DEVOPS_AGENT_SPACE_ID / AWS_REGION are available.
#
# Examples:
#   ./scripts/chat.sh "List running EC2 instances"
#   ./scripts/chat.sh -i                              # interactive REPL
#   ./scripts/chat.sh --resume <executionId> "more?"  # continue a session
#   ./scripts/chat.sh --show-ids "ping"               # see executionId/usage

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
fi

# Ensure the host has boto3 >= 1.43.0 (devops-agent client).
if ! python3 - <<'PY'
import sys
try:
    import boto3
except ImportError:
    sys.exit(1)
parts = boto3.__version__.split(".")
major, minor = int(parts[0]), int(parts[1])
if (major, minor) < (1, 43):
    sys.exit(2)
sess = boto3.Session()
if "devops-agent" not in sess.get_available_services():
    sys.exit(3)
PY
then
  echo "[chat.sh] ERROR: need boto3 >= 1.43.0 with devops-agent client." >&2
  echo "          Install:  pip install --user --upgrade 'boto3>=1.43.0'" >&2
  exit 1
fi

exec python3 "${ROOT_DIR}/lambda/cli/chat.py" "$@"
