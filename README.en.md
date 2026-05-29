# AWS DevOps Agent ↔ Slack Bridge

> 🌐 *Languages*: [中文](README.md) · English (current)
>
> *Related*: [aws-devops-agent-cn-bridge](https://github.com/RadiumGu/aws-devops-agent-cn-bridge) — cross-partition bridge that lets a DevOps Agent in the Global partition reach aws-cn resources (CFN + IAM Roles Anywhere).

## What it is

A lightweight *bridge layer* that connects [AWS DevOps Agent](https://aws.amazon.com/blogs/aws/aws-devops-agent-helps-you-accelerate-incident-response-and-improve-system-reliability-preview/) (preview) to Slack in *both directions*:

- *Alarm → investigation → Slack summary* fully automated SRE pipeline (CloudWatch Alarm → Lambda → DevOps Agent → Slack Block Kit)
- *Multi-turn chat inside Slack*: `@devops_agent <question>` triggers an investigation; follow-ups in the same thread reuse the chat session without re-mentioning the bot

The native AWS Slack integration is one-way ("post a finding to a channel"); this repo fills in everything else (see §0.0 in the [Chinese README](README.md#00-为什么要做这个-bridge) for the long form).

## Key features

- 🔌 *Configurable alarm intake* — Lambda-A serializes any-namespace CloudWatch alarms (EKS / RDS / Lambda / custom metrics / ContainerInsights) into a structured investigation description; not EC2-specific
- 🔄 *EventBridge event idempotency* — same alarm transition is never investigated twice (DDB atomic claim by `alarmName + state.timestamp`)
- 💬 *Slack-side initiation · multi-turn threads* — Lambda-C handles the Slack Events API with a 3-second fast-path ack and async self-invoke into a worker; DDB maps `thread_ts → chat_id` so follow-ups in the same thread skip the `@mention`
- 🛡️ *Slack signature verification + 5-min replay protection + event_id idempotency* — dual subscriptions (`app_mention` + `message.channels`) won't double-fire prompts
- 🌍 *Cross-partition extensible* — the event pipeline is pure EventBridge + Lambda, so the cn-bridge repo can be layered in to investigate aws-cn resources from a Global Agent Space
- 📦 *One-command deploy* — IAM / Lambda / EventBridge / API Gateway / DDB / DLQ / CloudWatch alarms are fully scripted and idempotent; the only manual step is creating the Slack App once (see `docs/slack-setup.md`)
- 🔁 *Easily portable to Feishu (Lark)* — the chat-platform abstraction is contained inside Lambda-C (signature verification, Block Kit rendering, `@mention` parsing, `chat.update` placeholders). Swapping in Feishu requires rewriting `slack_verify.py`, the Block Kit renderer (→ Feishu interactive cards), and the webhook URL format. The Lambda-A/B/EventBridge backbone stays untouched.

## Quick Start

```bash
git clone <repo-url> && cd aws-devops-agent-slack-bridge
cp .env.example .env
# edit .env with your account/region/Slack/Agent Space values
bash scripts/deploy.sh           # main alarm → investigation → Slack pipeline
bash scripts/deploy-chatbot.sh   # optional: Slack chatbot for interactive chat
```

Required `.env` keys: `AWS_ACCOUNT_ID` / `AWS_REGION` / `DEVOPS_AGENT_SPACE_ID` / `SLACK_WEBHOOK_URL` / `SLACK_TEST_CHANNEL`.

> *Status*: ✅ end-to-end verified (FIS terminating 2 EKS nodes → DevOps Agent investigates automatically → Slack receives the summary + multi-turn follow-ups).

## Architecture

```
CloudWatch Alarm (any namespace)
        │  state = ALARM
        ▼
EventBridge Rule-1 ── aws.cloudwatch / CloudWatch Alarm State Change
        │
        ▼
Lambda-A (devops-agent-trigger-investigation)
        │  alarm idempotency (DDB)
        │  create_backlog_task with namespace + dimensions + reason
        ▼
DevOps Agent  ── autonomous investigation (5–15 min)
        │     emits aws.aidevops / Investigation Completed
        ▼
EventBridge Rule-2 ── aws.aidevops / Investigation Completed
        │
        ▼
Lambda-B (devops-agent-notify-slack)
        │  list_journal_records → investigation_summary_md
        │  Slack Incoming Webhook → Block Kit
        ▼
Slack #<SLACK_CHANNEL_ID>
```

Plus the interactive chatbot path:

```
Slack @mention → Slack Events API → API Gateway HTTP API → Lambda-C
                                                              │ (fast-path: 200 ack in <3s)
                                                              ├─ event_id idempotency (DDB evt: prefix)
                                                              ├─ lambda.invoke(self, _internal=chat) async
                                                              │
                                                              └─ (worker path)
                                                                  1. DDB get_item(thread_ts) → chat
                                                                  2. devops-agent.send_message
                                                                  3. Slack chat.postMessage placeholder + chat.update
```

## Slack App setup (one-time)

| Step | Where | What |
|---|---|---|
| 1 | https://api.slack.com/apps → Create | New App, any name (e.g. `DevOps Agent`) |
| 2 | OAuth & Permissions → Bot Token Scopes | Add `app_mentions:read`, `chat:write`, `channels:history`, `groups:history` (for private channels) |
| 3 | Event Subscriptions → Enable | Request URL = the API Gateway URL printed by `deploy-chatbot.sh`; subscribe to bot events: `app_mention` + `message.channels` |
| 4 | Install to Workspace | Get the Bot Token (`xoxb-...`) and Signing Secret; store both in Secrets Manager (the script reads them by ID) |
| 5 | In each Slack channel | `/invite @devops_agent` (mandatory for private channels, recommended for public ones) |

Detailed checklist: `docs/slack-setup.md`.

## How users interact

| Scenario | How to send | Bot behaviour |
|---|---|---|
| *New question (top-level)* | `@devops_agent <question>` at channel top-level | Bot creates a new chat session and replies in a thread |
| *Follow-up in the same thread* | Plain message in the thread (*no `@`* needed) | Bot reuses the thread's existing chat session (DDB hit) — full multi-turn context |
| *Re-engage an older thread (≤7d)* | Continue posting in the same thread | Bot reuses the original chat (DDB TTL is 7 days) |
| *Older thread (>7d)* | Same thread, after TTL | DDB row expired → treated as a new question (unavoidable) |
| *New topic / new thread* | Must start a fresh top-level `@devops_agent <question>` | Bot opens a new chat session, isolated from earlier threads |
| *Unrelated chatter in a channel* | Don't mention the bot | Bot stays silent (DDB miss; EMF metric `UnhandledMessageEvents` increments) |

⚠️ *Notes*:

- *Don't `@mention` in private channels until the bot is `/invite`d.*
- *Don't add the bot to high-noise channels like #general* — even filtered events still consume Lambda invocations.
- *Each new message in an active thread costs one Lambda invocation* (fast-path ack + async worker); default reserved concurrency is 5.
- *The bot keys off `thread_ts`, not user identity* — anyone in the thread can talk to the bot. Add a user allow-list in the worker if you don't want that.

## Key design points

- *Slack 3-second rule* — fast-path must 200-ack within 3s. Implemented as same-Lambda async self-invoke (`InvocationType=Event`) routing to the worker. Measured fast-path: *warm ~258ms / cold ~1019ms*.
- *Multi-turn conversation* — `thread_ts` is the DDB partition key; each thread has one persistent `executionId` (chat session).
- *Dual-subscription idempotency* — `app_mention` and `message.channels` deliver the same user message twice; the DDB `evt:` prefix row uses ConditionalCheck for atomic deduplication.
- *Security* — Slack signing verification (HMAC-SHA256 over `v0:timestamp:body`) + 5-min replay protection + `hmac.compare_digest` for timing-safe comparison.
- *TTL* — the threads table auto-expires entries after 7 days; idle threads start a new chat session on reactivation.

## Why not just use Incoming Webhook?

Webhooks are one-way (send only) and can't receive Slack events. Lambda-B (push-only summary) uses a webhook fine; Lambda-C (interactive multi-turn) needs a Bot Token plus `chat.postMessage` / `chat.update`.

## Repo layout

```
lambda/
  lambda_a/   alarm → backlog task              ~290 lines
  lambda_b/   investigation completed → Slack   ~280 lines
  lambda_c/   Slack chatbot fast/worker         ~620 lines
  cli/        Chat CLI (one-shot / REPL)        ~190 lines
lib/
  agent_chat.py  shared DevOps Agent client wrapper
iam/           IAM policies + role trust
scripts/
  deploy.sh             main pipeline (idempotent)
  deploy-chatbot.sh     Slack chatbot (idempotent, creates DDB table + TTL)
  cleanup*.sh           teardown
docs/
  slack-setup.md
  cloudwatch-to-investigation-pipeline.md
tests/         85 unit tests covering Lambda-A/B/C + slack_verify
```

## Tests

```bash
python3 -m pytest tests/ -v
# 85 passed
```

## Roadmap

See [TODO.md](TODO.md) — includes a *Feishu (Lark) adaptation* track under P2.

## License

See [LICENSE](LICENSE).
