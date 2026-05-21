"""Shared agent chat helpers — used by Lambda-C and the CLI.

Contains only pure (non-CLI) helpers needed to drive a DevOps Agent
conversation: client construction and EventStream parsing for SendMessage.
CLI shell (argparse / REPL / one-shot dispatch) lives in lambda/cli/chat.py.
"""
from __future__ import annotations

import json
import sys

import boto3


def get_client(region: str):
    return boto3.client("devops-agent", region_name=region)


# Block types observed from devops-agent SendMessage stream:
#   text           — Agent's intermediate thinking / preamble text
#   final_response — the final user-facing answer (PRINT THIS)
#   chat_title     — auto-generated short title for the conversation
#   context_usage  — context window utilization metadata (jsonDelta)
#   tool_use       — tool invocations (jsonDelta carries args)
USER_VISIBLE_TYPES = {"final_response"}
THINKING_TYPES = {"text"}


def stream_message(
    client,
    agent_space_id: str,
    execution_id: str,
    content: str,
    user_id: str,
    on_final_text=None,
    on_thinking=None,
    on_meta=None,
    show_thinking: bool = False,
) -> dict:
    """Send a message and stream the response.

    Callbacks:
      on_final_text(chunk)  — chunks of the final user-facing answer
      on_thinking(chunk)    — chunks of intermediate 'text' blocks (thinking)
      on_meta(kind, payload) — non-text blocks (context_usage / chat_title /
                               tool_use). `kind` is the block type string.

    Returns:
      text          — assembled final_response text
      thinking      — assembled intermediate text
      chat_title    — auto-generated title (or empty)
      tool_calls    — list of {id, name, input}
      context_usage — last context_usage payload (or {})
      response_id, usage, failed
    """
    if on_final_text is None:
        on_final_text = lambda c: print(c, end="", flush=True)  # noqa: E731
    if on_thinking is None and show_thinking:
        on_thinking = lambda c: print(
            f"\033[2m{c}\033[0m", end="", flush=True)  # noqa: E731
    on_thinking = on_thinking or (lambda chunk: None)
    on_meta = on_meta or (lambda kind, payload: None)

    resp = client.send_message(
        agentSpaceId=agent_space_id,
        executionId=execution_id,
        content=content,
        userId=user_id,
    )

    final_parts: list[str] = []
    thinking_parts: list[str] = []
    chat_title_parts: list[str] = []
    tool_calls: list[dict] = []
    context_usage: dict = {}
    blocks: dict[int, dict] = {}
    response_id = ""
    usage: dict = {}
    failed = False

    for event in resp["events"]:
        if "responseCreated" in event:
            response_id = event["responseCreated"].get("responseId", "")

        elif "contentBlockStart" in event:
            cbs = event["contentBlockStart"]
            idx = int(cbs.get("index", 0))
            blocks[idx] = {
                "type": cbs.get("type", "text"),
                "id": cbs.get("id", ""),
                "json_buf": "",
            }

        elif "contentBlockDelta" in event:
            cbd = event["contentBlockDelta"]
            idx = int(cbd.get("index", 0))
            delta = cbd.get("delta", {}) or {}
            text_delta = delta.get("textDelta") or {}
            json_delta = delta.get("jsonDelta") or {}
            block = blocks.get(idx, {"type": "text"})
            btype = block.get("type", "text")

            if text_delta.get("text"):
                t = text_delta["text"]
                if btype == "final_response":
                    final_parts.append(t)
                    on_final_text(t)
                elif btype == "chat_title":
                    chat_title_parts.append(t)
                else:
                    # 'text' (thinking) and any other text-bearing block
                    thinking_parts.append(t)
                    on_thinking(t)

            if json_delta.get("partialJson"):
                # jsonDelta carries tool-call args / metadata as streaming JSON
                block["json_buf"] += json_delta["partialJson"]
                blocks[idx] = block

        elif "contentBlockStop" in event:
            cbs = event["contentBlockStop"]
            idx = int(cbs.get("index", 0))
            blk = blocks.get(idx)
            if not blk:
                continue
            if blk.get("json_buf"):
                try:
                    parsed = json.loads(blk["json_buf"])
                except json.JSONDecodeError:
                    parsed = {"_raw": blk["json_buf"]}
                btype = blk.get("type", "")
                if btype == "context_usage":
                    context_usage = parsed
                    on_meta(btype, parsed)
                else:
                    tool = {
                        "id": blk.get("id", ""),
                        "name": btype or "tool_use",
                        "input": parsed,
                    }
                    tool_calls.append(tool)
                    on_meta(btype, tool)

        elif "responseCompleted" in event:
            usage = event["responseCompleted"].get("usage", {}) or {}
            break

        elif "responseFailed" in event:
            failed = True
            err = event["responseFailed"]
            print(f"\n[ERROR] response failed: {err}", file=sys.stderr)
            break

    return {
        "text": "".join(final_parts),
        "thinking": "".join(thinking_parts),
        "chat_title": "".join(chat_title_parts),
        "tool_calls": tool_calls,
        "context_usage": context_usage,
        "response_id": response_id,
        "usage": usage,
        "failed": failed,
    }
