# DevOps Agent — TODO List

> **维护规则**：完成的项打 `[x]` 不要删，留下做过什么的痕迹。新增的事项加在对应优先级下面。In-progress 用 `[~]` 标，并注明谁在做。
> **更新时间**：2026-05-21
> **范围**：DevOps Agent 主链路（Lambda-A/B）+ Slack chatbot（Lambda-C/API GW/DDB）+ 工程化收尾

---

## 当前状态（已完成）

- [x] Backlog Task 主链路：CloudWatch Alarm → Lambda-A → DevOps Agent → Lambda-B → Slack
- [x] Slack 复用 `petsite-ops-slack-notifier` 的 Incoming Webhook（webhook URL 已挪到 Secrets Manager，P0-6 完成 2026-05-20）
- [x] Chat CLI（one-shot / REPL / `--resume` / `--show-thinking`）
- [x] FIS 端到端验证（删 2 个 EKS node → 收到 investigation summary）
- [x] 4 个 EKS/ASG 告警（替代失效的 ContainerInsights node 告警）
- [x] Lambda-A bug fix：从 `event['account']` / `event['region']` 读字段（不是 `detail`）
- [x] README 与线上实现完全对齐
- [x] Slack 多轮对话集成 Phase 1+2+3（Lambda-C + API GW HTTP API + DDB `devops-agent-slack-threads`）
  - Invoke URL: `https://<API_ID>.execute-api.ap-northeast-1.amazonaws.com/slack/events`
  - 5 个测试 prompt 验证通过（含多轮 thread 复用）
  - Slack App Console 配 Event Subscriptions Request URL（5 分钟手动操作）
- [x] Lambda-C 双视角架构审阅（2026-05-19，参见 Slack #C0AJQ1TELTY 历史）→ 形成下方 P0/P1/P2 清单

---

## P0 — 生产前必做

按"先 Lambda-C 再 Lambda-A/B 再 EventBridge"顺序推。Lambda-C 已派编程猫，session `tide-bison` 跑中。

### Lambda-C（chatbot）— 已派编程猫 in-progress

- [x] **P0-1 调换 `_get_or_create_chat` 内顺序，避免孤儿 executionId**（完成 2026-05-20）
  - 先 DDB conditional `put_item` placeholder（execution_id="PENDING"）
  - put 失败 → 轮询 placeholder 翻成真值（25s 上限），拿 peer 的 executionId
  - put 成功 → create_chat → update_item 写真值（conditional `execution_id = :pending`）
  - create_chat 失败 → conditional delete placeholder 回滚
- [x] **P0-2 加 `ReservedConcurrentExecutions=5` 给 Lambda-C**（完成 2026-05-20）
  - `scripts/deploy-chatbot.sh` 步骤 [6/8]：`aws lambda put-function-concurrency`
  - 实测 `aws lambda get-function-concurrency` 返回 `ReservedConcurrentExecutions=5`
- [x] **P0-3 bare `except Exception:` 不外泄 raw `str(e)` 到 Slack**（完成 2026-05-20）
  - `logger.exception(json.dumps({...request_id...}))` 内部记完整 traceback
  - Slack 固定文案常量 `SLACK_ERROR_TEMPLATE`：`:warning: 调查失败，请稍后重试 (request_id: <id>)`
  - `_worker_path` 收敛 ClientError + Exception 到同一分支；`_fast_path` self_invoke + handler 入口同样模式
- [x] **P0-4 Lambda async invoke 配 OnFailure → SQS DLQ**（完成 2026-05-20）
  - `scripts/deploy-chatbot.sh` 步骤 [7/8] 创建队列 + put-role-policy `SlackChatbotDLQAccess` + put-function-event-invoke-config OnFailure
  - 实测 `OnFailure → arn:aws:sqs:ap-northeast-1:<ACCOUNT_ID>:devops-agent-slack-chatbot-dlq`，`MaximumRetryAttempts=2`

### Lambda-B / Lambda-A / EventBridge

- [x] **P0-5 Lambda-B 加 DLQ**（完成 2026-05-20）  - `scripts/deploy.sh` 步骤 [2b] 创建 SQS `devops-agent-notify-dlq` (`MessageRetentionPeriod=1209600`)
  - `put-role-policy NotifySlackDLQAccess` 限定 `sqs:SendMessage` 到该 DLQ ARN
  - 步骤 [5/8] 末尾 `update-function-configuration --dead-letter-config TargetArn=...`（IAM 传播 retry × 5）
  - 实测：`aws lambda get-function-configuration --function-name devops-agent-notify-slack` → `DeadLetterConfig.TargetArn = arn:aws:sqs:ap-northeast-1:<ACCOUNT_ID>:devops-agent-notify-dlq`
- [x] **P0-6 Slack webhook URL 挪到 Secrets Manager**（完成 2026-05-20）
  - 创建 secret `devops-agent/slack-webhook-url`（idempotent；存在则 `put-secret-value` 更新）
  - `lambda_b/lambda_function.py`：从 env 读 `SLACK_WEBHOOK_SECRET_ARN`，模块导入时一次性拉 secret 并缓存（cold start 一次，warm 复用）；拉失败 raise 让 invocation 触发 DLQ
  - `put-role-policy NotifySlackSecretAccess` 限定 `secretsmanager:GetSecretValue` 到该 secret ARN
  - Lambda env 改 `Variables={SLACK_WEBHOOK_SECRET_ARN=<arn>,SLACK_CHANNEL=...}`，删 `SLACK_WEBHOOK_URL`
  - `.env.example` 改成 redacted 占位 + 文档化 secret 轮换流程
  - 实测：`aws lambda get-function-configuration` → `Environment.Variables` 含 `SLACK_WEBHOOK_SECRET_ARN`，无 `SLACK_WEBHOOK_URL` 明文；smoke invoke cold start 583ms（secret 加载成功，没 401/AccessDenied）
- [x] **P0-7 Rule-1 加 alarm 名 prefix filter**（完成 2026-05-20）
  - `eventbridge/rule-1-alarm-to-lambda-pattern.json` 加 `alarmName: [{prefix: "petsite-"}]`
  - `scripts/deploy.sh` 步骤 [6/8] `aws events put-rule --event-pattern file://...` 已重新应用
  - 实测：`aws events describe-rule --name DevOps-Agent-Demo-Alarm-To-Lambda` → `EventPattern` 含 `"alarmName": [{"prefix": "petsite-"}]`
- [x] **P0-8 Lambda-A `_print_summary` log 加 account / region**（完成 2026-05-20）
  - `lambda/lambda_a/lambda_function.py` `investigation_created` print 字典加 `account` / `region`（取自 `event['account']` / `event['region']`）
  - 仅加字段，不改造 logger 体系（避免 scope creep）
- [x] **P0-9 Slack 通知加 timeline 字段**（完成 2026-05-20）

### 新增 P0（Wave 4 发现）

- [x] **P0-24 Lambda-A 加 DLQ**（完成 2026-05-20）
  - 现状：`aws lambda get-function-configuration --function-name devops-agent-trigger-investigation` 返回 `DLQ: null`
  - 风险：Lambda-A 失败时 EventBridge 重试后最终丢弃 invocation 没地方落，可观测性 gap
  - 修法：参照 P0-5 做法：创建 SQS `devops-agent-trigger-dlq` + IAM policy `TriggerInvestigationDLQAccess` + `aws lambda update-function-configuration --dead-letter-config`
  - 实测：`DeadLetterConfig.TargetArn = arn:aws:sqs:ap-northeast-1:<ACCOUNT_ID>:devops-agent-trigger-dlq`
  - `lambda/lambda_b/lambda_function.py`：`_build_blocks` 加 `Started` (`detail.data.createdAt`) + `Duration` (`updatedAt - createdAt` 格式 `Xm Ys`)；时间戳缺失或不可解析则不渲染该字段
  - 实测：本地 unit test 用 mock secret 客户端跑 `_format_duration('2026-05-20T10:00:00Z','2026-05-20T10:03:42Z')` → `'3m 42s'`；`_build_blocks` 渲染含 `*Started*` + `*Duration*`

---

## P1 — 应该做（生产质量加固）

### Lambda-C 收尾

- [x] **P1-10 抽 `stream_message` 到共享 `lib/agent_chat.py`**（完成 2026-05-20）
  - 新建 `lib/agent_chat.py`（166 行，纯函数 `get_client` + `stream_message`，无 CLI 代码）
  - `lambda/cli/chat.py` 改为 `from lib.agent_chat import get_client, stream_message`（顶部加 `sys.path` 一行 hack 让脚本直跑能用）
  - `lambda/lambda_c/lambda_function.py` `import chat_lib` → `import agent_chat`
  - 删 `lambda/lambda_c/chat_lib.py`（339 行 dead code 退出 zip）
  - `scripts/deploy-chatbot.sh` 打包步骤改为 zip `lambda_function.py + slack_verify.py + lib/agent_chat.py`，`agent_chat.py` 落 zip 根
  - 实测：CLI `python3 lambda/cli/chat.py --help` 正常输出；Lambda 合成 worker payload invoke 不报 `ImportError: agent_chat`
- [x] **P1-11 修 `userType="IAM"` 与传 Slack user_id 不一致问题**（完成 2026-05-20）
  - `_get_or_create_chat`：`userType="STATIC"` + `userId=f"slack_{user}"`
  - 验证 1：`UNAUTHENTICATED` 被 enum 拒（valid=`GAIA|MIDWAY|STATIC|IAM|IDC|IDP`），改 `STATIC` 通过
  - 验证 2：`userId` 正则 `^[a-zA-Z0-9_.-]+$` 拒冒号，前缀分隔符从 `:` 改 `_`
  - 实测：合成 `slack_event{user="U_SMOKE"}` invoke → `create_chat` 成功，executionId=4249b843-... 写入 DDB
- [x] **P1-12 Lambda-C 内存 256MB → 512MB**（完成 2026-05-20）
  - `scripts/deploy-chatbot.sh` 步骤 [4/9] update/create 两处都改 `--memory-size 512`
  - 实测：`aws lambda get-function-configuration` → `MemorySize=512`，REPORT 行确认 `Memory Size: 512 MB`，cold-start init 463ms
- [x] **P1-13 CloudWatch alarm 加监控**（完成 2026-05-20）
  - 新增 `scripts/deploy-chatbot.sh` 步骤 [8/9]，3 个 idempotent `put-metric-alarm`：
    - `devops-agent-slack-chatbot-errors`：AWS/Lambda Errors ≥ 1 in 5min
    - `devops-agent-slack-chatbot-apigw-5xx`：AWS/ApiGateway 5xx ≥ 1 in 5min（dim=ApiId=<API_ID>）
    - `devops-agent-slack-chatbot-ddb-throttle`：AWS/DynamoDB ThrottledRequests ≥ 1 in 5min（dim=TableName=devops-agent-slack-threads）
  - 三个 alarm `treat-missing-data=notBreaching`，目前无 SNS topic 所以未配 AlarmActions（fire 仅在 console 可见，wiring 留 follow-up）
  - 实测：`aws cloudwatch describe-alarms --alarm-name-prefix devops-agent-slack-chatbot` → 3 个 alarm 存在，state=INSUFFICIENT_DATA

### Lambda-A/B 收尾

- [x] **P1-14 补单元测试**（完成 2026-05-20）
  - `tests/test_lambda_a.py`（23 tests）+ `tests/test_lambda_b.py`（20 tests）+ `tests/conftest.py`
  - Coverage: `lambda/lambda_a/lambda_function.py` 100%, `lambda/lambda_b/lambda_function.py` 85%（剩余 16 行是 `get_investigation_summary` + `post_to_slack` 网络/AWS I/O，不在纯函数 scope 内）
  - 全套 `pytest tests/` 63 测试通过
- [x] **P1-15 Failed / Cancelled investigation 也通知**（完成 2026-05-20）
  - `_build_failure_blocks(metadata, data, status)` 渲染 `:x: Failed` / `:warning: Cancelled` header + 复用 metadata fields + reason 字段
  - `lambda_handler` 三分支：`COMPLETED` 走原路径；`FAILED` / `CANCELLED` 走 failure blocks（不调 `list_journal_records`）；`IN_PROGRESS`/其他仍 skip
  - 实测：合成 FAILED 事件（`failureReason="SMOKE TEST"`）→ 拉到 `post_to_slack`（验证：把 webhook 临时改成 `https://example.invalid/` 看到 URLError 出在 post_to_slack 这一步，说明 `_build_failure_blocks` 已成功执行，没在 status 检查时被 skip）；IN_PROGRESS 事件返回 `Skipped: status=IN_PROGRESS`
  - 已知风险：`failureReason` / `cancellationReason` 字段名是猜的（DevOps Agent preview 期事件 schema 未公开）；fallback 字符串 `"Reason not provided by Agent"` 兜底
- [x] **P1-16 加 CloudWatch Dashboard**（完成 2026-05-20）
  - 新建 `scripts/setup-dashboard.sh`（idempotent `put-dashboard`）
  - Dashboard `DevOpsAgent-PetSite` 7 个 widget：Lambda invocations/errors（Sum）、duration p50/p99、DLQ depth（3 队列 Max）、EventBridge rule 调用、API Gateway 4xx/5xx（ApiId=<API_ID>）、DynamoDB R/W capacity（`devops-agent-slack-threads`）
  - `scripts/deploy.sh` 末尾追加自动调用，可用 `SKIP_DASHBOARD=1` 跳过
  - 实测：`aws cloudwatch list-dashboards --dashboard-name-prefix DevOpsAgent-PetSite` → 1 entry；URL 直链可访问
  - Follow-up：未配 alarm；Investigation 平均耗时 metric filter 太复杂、留 follow-up；项目级 SNS topic 缺位，alarm action 仍未挂

### Agent 配置

- [ ] **P1-17 DevOps Agent Skill / KnowledgeItem** — 在 console 配 custom skill 让 Agent 知道 PetSite 的拓扑/常见故障模式（preview 期 console-only，没 API，纯手工）

### Lambda-C UX 改进

- [x] **P1-18 thread 内免 @ 自动响应**（完成 2026-05-21）

---

## P2 — 锦上添花（工程化）

### Lambda-C 健壮性

- [x] **P2-18 Lambda-C handler 三个分支补单元测试**（完成 2026-05-20）
  - `tests/test_lambda_c.py` 8 个测试：`url_verification` 回 challenge / `X-Slack-Retry-Num` header 直接 ack 不调 worker / `app_mention` 异步 invoke worker；plus subtype=bot_message / 错签拒绝；plus P2-19 长度三档（within / at / over limit）
  - mock 全外部依赖（boto3 lambda / DDB / secretsmanager）；不打真 AWS
- [x] **P2-19 Slack 输入长度截断**（完成 2026-05-20）
  - `lambda/lambda_c/lambda_function.py`：`MAX_USER_PROMPT_CHARS = 4000` + `PROMPT_TOO_LONG_TEMPLATE`；`_worker_path` 在 `text.strip()` 后立即检查，超长直接 post warning + return `prompt-too-long`
  - 单测：≤ limit 走原路径；> limit 收到 `:warning: Your prompt is too long` 文案，且 `_get_or_create_chat` 未被调用

### 飞书适配（Lark）

- [ ] **P2-FS1 抽象 chat 平台层**
  - 状态：未开始
  - 目标：让 Lambda-C 的核心逻辑（thread 状态机、event_id 幂等、prompt 长度校验、chat session 管理）与 Slack 解耦
  - 范围：定义 `ChatAdapter` 接口（`verify_signature`、`parse_event`、`post_message`、`update_message`、`render_summary_blocks`），把 Slack 实现独立成 `chat_adapter_slack.py`
  - 收益：飞书适配只需新增一个 `chat_adapter_lark.py`，主流程零改动

- [ ] **P2-FS2 飞书 chatbot adapter 实现**
  - 状态：未开始
  - 范围：
    - 飞书机器人验签（X-Lark-Signature + body）
    - 飞书 event subscription URL_VERIFICATION challenge
    - `@_user_<open_id>` mention 解析（飞书的 mention 格式与 Slack 不同）
    - 飞书富文本 / 卡片消息渲染（替代 Slack Block Kit）
    - thread 概念映射：飞书的「话题/话题回复」≈ Slack thread；用 `chat_id + root_message_id` 当 thread_ts
  - 飞书侧配置 checklist（Lark Open Platform → 自建应用 → 事件订阅 + 权限：`im:message`、`im:message.group_at_msg`、`im:message:send_as_bot`）

- [ ] **P2-FS3 多平台路由**
  - 状态：未开始
  - 目标：单 Lambda-C 同时处理 Slack 和飞书（按 API GW 路径区分 `/slack/events` vs `/lark/events`）
  - 或：拆双 Lambda（Lambda-C-slack / Lambda-C-lark）共享 thread state DDB 表（PK 加 `slack:` / `lark:` 前缀）

- [ ] **P2-FS4 Lambda-B 推送通道扩展**
  - 状态：未开始
  - 把 investigation summary 同时推 Slack（已实现）+ 飞书群（新增），按 alarm 严重度路由

---

### IaC + 治理

- [ ] **P2-20 改 IaC（CDK / Terraform）替代 `deploy.sh`** — 当前 bash 适合 demo，进生产前改成 IaC，享受 drift detection 和 PR-based change review。约 15 个 resource
- [ ] **P2-21 Agent 服务角色 tag-based scope** — 用 `aws:ResourceTag/Project=petsite` 替代之前删掉的 `RestrictToTokyoRegion` deny，让 Agent 只看 PetSite 资源（详见 README §5.3 提到的官方文档）。需要先批量给资源打 Project tag
- [x] **P2-22 按 namespace 差异化 prompt**（完成 2026-05-20）
  - `lambda/lambda_a/lambda_function.py`：`NAMESPACE_HINTS` dict 覆盖 6 个 namespace（AWS/EKS / ContainerInsights / AWS/RDS / AWS/Lambda / AWS/ApplicationELB / AWS/AutoScaling）+ `DEFAULT_NAMESPACE_HINT` fallback
  - `_format_description(namespace=...)` 根据 namespace 选 hint 拼到 "Investigation details:" 段
  - 单测：每个 namespace 命中各自 hint；未知 namespace fallback 到 default
- [ ] **P2-23 Cost tracking**：DevOps Agent preview 期定价不公开，等 GA 后建立成本监控（`get_account_usage` API）

---

## 派发与协作记录

### 当前活跃任务

| 任务 | Owner | 派发时间 | 派发方式 | Status |
|------|-------|---------|---------|--------|
| P0-1 ~ P0-4 (Lambda-C 4 项) | 编程猫 (Claude Code headless) | 2026-05-20 | exec session `tide-bison` | done 2026-05-20 — `tasks/doc-reviewer/lambda-c-p0-fix-20260520/result.md` |
| P0-5 ~ P0-9 (Lambda-B/A/EventBridge 5 项) | 编程猫 (Claude Code) | 2026-05-20 | wave2 batch | done 2026-05-20 — `tasks/doc-reviewer/wave2-p0-fix-20260520/result.md` |
| P1-10 ~ P1-13 (Lambda-C 收尾 4 项) | 编程猫 (Claude Code) | 2026-05-20 | wave3 batch | done 2026-05-20 — `tasks/doc-reviewer/wave3-p1-fix-20260520/result.md` |
| P0-24 + P1-14/15/16 + P2-18/19/22 (7 项清扫) | 编程猫 (Claude Code) | 2026-05-20 | wave4 batch | done 2026-05-20 — `tasks/doc-reviewer/wave4-cleanup-20260520/result.md` |

工作目录 `/home/ubuntu/tech/devops-agent`，prompt 存档 `/home/ubuntu/tech/tasks/doc-reviewer/lambda-c-p0-fix-20260520/prompt.md`。
完成后产出 `result.md` 由架构审阅猫做 post-execution gate（git diff scope + 验收逐条对）。

### 推进顺序（建议）

1. **Wave 1 - Lambda-C P0-1~4** ✅ done 2026-05-20
2. **Wave 2 - Lambda-B/A/EventBridge P0-5~9** ✅ done 2026-05-20
3. **Wave 3 - P1 Lambda-C 收尾 P1-10~13** ✅ done 2026-05-20
4. **Wave 4 - 全清扫 P0-24 / P1-14/15/16 / P2-18/19/22** ✅ done 2026-05-20
5. **剩余项**（不在编程猫 scope）— P1-17（手工 console）/ P2-20（IaC 重构）/ P2-21（tag-based scope）/ P2-23（cost tracking 等 GA）

---

## 截止 2026-05-21 未完成项

按优先级列出当前仍 open 的 todo，对应上面 P1/P2 章节里的原条目。每条标了为什么没做 + 解锁条件。

### P1 — 应该做

- [ ] **P1-17 DevOps Agent Skill / KnowledgeItem**
  - 状态：未开始
  - 卡点：preview 期 console-only，没 boto3 API
  - 下一步：手工进 AWS console 配 PetSite 拓扑 + 常见故障模式

### P2 — 锦上添花

- [ ] **P2-20 改 IaC（CDK / Terraform）替代 `deploy.sh`**
  - 状态：未开始
  - 卡点：当前 bash 适合 demo，进生产前再做
  - 下一步：约 15 个 resource，建议 CDK Python（与现有 Lambda 同语言栈）

- [ ] **P2-21 Agent 服务角色 tag-based scope**
  - 状态：未开始
  - 卡点：需要先批量给 PetSite 资源打 `Project=petsite` tag
  - 下一步：先做 tag 普查脚本（之前 2026-05-18 决定“先不费劲”），再恢复带 tag condition 的 IAM 限制（替代删掉的 `RestrictToTokyoRegion`）

- [ ] **P2-23 Cost tracking**
  - 状态：未开始
  - 卡点：DevOps Agent preview 期定价不公开
  - 下一步：等 GA 后用 `get_account_usage` API 建监控

### 观察项（非 todo）

- ⚠️ **Lambda-C 本身 `DeadLetterConfig` 为 null**
  - 现状：P0-4 的 `OnFailure → SQS DLQ` 已在 async event-invoke-config 那层兜住，功能上不漏
  - 风险：与 Lambda-A/B 的 `DeadLetterConfig` 字段配置不一致，可观测性不统一
  - 决策：是否补齐由大乖乖定，不阻塞生产

---

## 历史决策 / 已搁置

- [ ] **region 限制** —— 删了 `RestrictToTokyoRegion` inline policy，备份在 `iam/backups/`。以后做 tag-based 替代时一起恢复保护
- [x] ~~给 Agent description 加 system prompt 限制只查 Tokyo~~ — *验证过 Agent 不读 description*，无效，已撤
- [x] ~~Slack 用 Bot Token + chat.postMessage~~ — Lambda-B 复用现成 webhook（更省事）；Lambda-C 用 Bot Token（多轮需要 chat.update）
