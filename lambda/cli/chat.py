"""
DevOps Agent Chat CLI — interactive and one-shot modes.

Streams responses from devops-agent.SendMessage, parses the EventStream,
and prints text deltas as they arrive. Supports multi-turn dialogs by
reusing the same executionId.

Usage:
    python3 chat.py --agent-space-id <UUID> "List running EC2 instances"
    python3 chat.py --agent-space-id <UUID> -i
    python3 chat.py --agent-space-id <UUID> --resume <executionId> "follow-up"

Reads DEVOPS_AGENT_SPACE_ID and AWS_REGION from env if not on CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import botocore

# Make `lib/` (sibling of `lambda/`) importable when running directly.
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.agent_chat import get_client, stream_message  # noqa: E402


def cmd_one_shot(args, client, user_id):
    chat = client.create_chat(
        agentSpaceId=args.agent_space_id,
        userId=user_id,
        userType="IAM",
    )
    execution_id = chat["executionId"]
    if args.show_ids:
        print(f"[executionId={execution_id}]\n", file=sys.stderr)

    result = stream_message(
        client, args.agent_space_id, execution_id, args.message, user_id,
        show_thinking=args.show_thinking,
    )
    print()  # newline after the streamed text
    if result["failed"]:
        return 1
    if args.show_ids:
        _print_summary(result, execution_id)
    return 0


def cmd_resume(args, client, user_id):
    execution_id = args.resume
    if args.show_ids:
        print(f"[resuming executionId={execution_id}]\n", file=sys.stderr)
    result = stream_message(
        client, args.agent_space_id, execution_id, args.message, user_id,
        show_thinking=args.show_thinking,
    )
    print()
    if result["failed"]:
        return 1
    if args.show_ids:
        _print_summary(result, execution_id)
    return 0


def cmd_interactive(args, client, user_id):
    chat = client.create_chat(
        agentSpaceId=args.agent_space_id,
        userId=user_id,
        userType="IAM",
    )
    execution_id = chat["executionId"]
    print(f"DevOps Agent chat — executionId={execution_id}", file=sys.stderr)
    print("Type your message; empty line / Ctrl-D / 'exit' to quit.\n",
          file=sys.stderr)

    turn = 1
    while True:
        try:
            prompt = input(f"\033[1;36m[{turn}] you ›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not prompt or prompt.lower() in {"exit", "quit", ":q"}:
            break

        sys.stdout.write("\033[1;32magent ›\033[0m ")
        sys.stdout.flush()
        try:
            result = stream_message(
                client, args.agent_space_id, execution_id, prompt, user_id,
                show_thinking=args.show_thinking,
            )
        except botocore.exceptions.ClientError as e:
            print(f"\n[ERROR] {e}", file=sys.stderr)
            continue
        print("\n")
        if result["failed"]:
            print("[turn failed; you may want to start a new session]",
                  file=sys.stderr)
        turn += 1

    print(f"\nLast executionId (use --resume to continue): {execution_id}",
          file=sys.stderr)
    return 0


def _print_summary(result: dict, execution_id: str) -> None:
    print("\n--- meta ---", file=sys.stderr)
    print(f"  executionId : {execution_id}", file=sys.stderr)
    print(f"  responseId  : {result['response_id']}", file=sys.stderr)
    if result.get("chat_title"):
        print(f"  chatTitle   : {result['chat_title']}", file=sys.stderr)
    if result.get("usage"):
        print(f"  usage       : {json.dumps(result['usage'], default=str)}",
              file=sys.stderr)
    if result.get("context_usage"):
        cw = result["context_usage"].get("data", {}).get(
            "context_window", {})
        if cw:
            print(f"  context     : {cw.get('utilization')}% used, "
                  f"{cw.get('compaction_count')} compactions",
                  file=sys.stderr)
    if result.get("tool_calls"):
        print(f"  tool calls  : {len(result['tool_calls'])}", file=sys.stderr)
        for tc in result["tool_calls"]:
            print(f"    - {tc['name']}: {json.dumps(tc['input'])[:200]}",
                  file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="DevOps Agent Chat CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--agent-space-id",
        default=os.environ.get("DEVOPS_AGENT_SPACE_ID", ""),
        help="Agent Space UUID (or set DEVOPS_AGENT_SPACE_ID).",
    )
    p.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "ap-northeast-1"),
        help="AWS region.",
    )
    p.add_argument(
        "--user-id",
        default=os.environ.get("USER", "cli"),
        help="Identifier for this user (logged in chat).",
    )
    p.add_argument(
        "-i", "--interactive", action="store_true",
        help="Interactive REPL mode.",
    )
    p.add_argument(
        "--resume", default="",
        help="Resume an existing executionId for multi-turn follow-ups.",
    )
    p.add_argument(
        "--show-ids", action="store_true",
        help="Print executionId / usage / tool calls to stderr.",
    )
    p.add_argument(
        "--show-thinking", action="store_true",
        help="Stream the agent's intermediate 'thinking' text (dim).",
    )
    p.add_argument(
        "message", nargs="?", default="",
        help="Message to send (omit with -i for interactive mode).",
    )
    args = p.parse_args(argv)

    if not args.agent_space_id:
        p.error("--agent-space-id is required (or set DEVOPS_AGENT_SPACE_ID).")

    client = get_client(args.region)

    if args.interactive:
        return cmd_interactive(args, client, args.user_id)
    if not args.message:
        p.error("Provide a message, or use -i for interactive mode.")
    if args.resume:
        return cmd_resume(args, client, args.user_id)
    return cmd_one_shot(args, client, args.user_id)


if __name__ == "__main__":
    sys.exit(main())
