# 生产链路：CloudWatch Alarm → DevOps Agent Investigation

> **文档日期**：2026-05-20
> **维护者**：架构审阅猫（基于实际线上代码 + AWS 配置抓取）
> **范围**：从 CloudWatch Alarm 触发到 DevOps Agent 完成 investigation 并推到 Slack 的端到端链路
> **代码源**：`/home/ubuntu/tech/devops-agent/`（线上 deploy 时间 2026-05-20）

---

## 1. 整体链路

```
┌──────────────────────┐
│ CloudWatch Alarm     │  state=ALARM, name 以 "petsite-" 开头
│ (any AWS service)    │  source: aws.cloudwatch
└──────────┬───────────┘
           │ EventBridge default bus
           │ event source: aws.cloudwatch
           │ detail-type: "CloudWatch Alarm State Change"
           ▼
┌──────────────────────────────────────────────────────────┐
│ EventBridge Rule-1                                       │
│ "DevOps-Agent-Demo-Alarm-To-Lambda"                      │
│ pattern: source=aws.cloudwatch                           │
│          detail-type="CloudWatch Alarm State Change"     │
│          detail.state.value=["ALARM"]                    │
│          detail.alarmName=[{prefix:"petsite-"}]          │
└──────────┬───────────────────────────────────────────────┘
           │ target = Lambda-A (async invoke)
           ▼
┌──────────────────────────────────────────────────────────┐
│ Lambda-A: devops-agent-trigger-investigation             │
│ - 解析 event.detail.configuration.metrics[0]             │
│   → namespace / metric_name / dimensions                 │
│ - 拼结构化 description (starting point + details)        │
│ - boto3 client('devops-agent').create_backlog_task(      │
│     agentSpaceId=..., taskType=INVESTIGATION,            │
│     priority=HIGH, title=..., description=...)           │
└──────────┬───────────────────────────────────────────────┘
           │ AWS SDK call (SigV4 IAM)
           │ NOT a webhook — pure API call
           ▼
┌──────────────────────────────────────────────────────────┐
│ DevOps Agent Service (preview, ap-northeast-1)           │
│ - 异步处理 backlog task                                  │
│ - status 流转：CREATED → IN_PROGRESS → COMPLETED         │
│ - 完成后产出 investigation_summary_md (markdown)         │
└──────────┬───────────────────────────────────────────────┘
           │ EventBridge default bus
           │ source: aws.aidevops
           │ detail-type: "Investigation Completed"
           │ detail.metadata: { agent_space_id, execution_id, task_id }
           │ detail.data:     { status, createdAt, updatedAt, ... }
           ▼
┌──────────────────────────────────────────────────────────┐
│ EventBridge Rule-2                                       │
│ "DevOps-Agent-Investigation-Completed"                   │
│ pattern: source=aws.aidevops                             │
│          detail-type="Investigation Completed"           │
└──────────┬───────────────────────────────────────────────┘
           │ target = Lambda-B (async invoke)
           ▼
┌──────────────────────────────────────────────────────────┐
│ Lambda-B: devops-agent-notify-slack                      │
│ - 跳过 status != COMPLETED 的 event                      │
│ - boto3 list_journal_records() 拉 markdown summary       │
│ - 拼 Slack Block Kit (含 task_id / execution_id /        │
│   Started / Duration 等 metadata)                        │
│ - urllib POST 到 Slack Incoming Webhook                  │
│ - Webhook URL 来自 Secrets Manager (cold-start 缓存)     │
└──────────┬───────────────────────────────────────────────┘
           │ HTTPS POST
           ▼
┌──────────────────────────────────────────────────────────┐
│ Slack #<SLACK_CHANNEL_ID>                                       │
│ 渲染含 :mag: 标题 / metadata fields / markdown 正文      │
└──────────────────────────────────────────────────────────┘
```

**关键澄清**：链路里 *没有用 Agent Space Webhook*。

- Lambda-A 触发 investigation 走的是 `boto3.client("devops-agent").create_backlog_task()` SDK API（IAM 鉴权 / SigV4 签名），不是 console 上看到的"Agent Space Webhook"。
- Investigation 完成的回调走的是 EventBridge `aws.aidevops` source（标准 AWS 事件总线），同样不是 webhook。
- "Agent Space Webhook" 是 console 提供的另一种集成方式（DevOps Agent → 外部 HTTP endpoint 推送），本方案刻意没用，理由见 §6 设计权衡。

---

## 2. 组件清单（线上实际配置）

| 组件 | 名称 / ARN | 配置 |
|------|-----------|------|
| Account | <ACCOUNT_ID> | — |
| Region | ap-northeast-1 (Tokyo) | — |
| Agent Space | `<AGENT_SPACE_UUID>` | name: petsite-devops |
| Slack Channel | `<SLACK_CHANNEL_ID>` | 复用 `petsite-ops-slack-notifier` 的 Incoming Webhook |
| Webhook Secret | `arn:aws:secretsmanager:<region>:<ACCOUNT_ID>:secret:devops-agent/slack-webhook-url-<RANDOM_SUFFIX>` | SecretString 是 webhook URL；2026-05-20 P0-6 落地 |
| EventBridge Rule-1 | `DevOps-Agent-Demo-Alarm-To-Lambda` | pattern: aws.cloudwatch + ALARM + alarmName prefix petsite- |
| EventBridge Rule-2 | `DevOps-Agent-Investigation-Completed` | pattern: aws.aidevops + Investigation Completed |
| Lambda-A | `devops-agent-trigger-investigation` | 128MB / 30s / 无 DLQ ⚠️ |
| Lambda-B | `devops-agent-notify-slack` | 128MB / 60s / DLQ → `devops-agent-notify-dlq` |
| IAM Role (Lambda-A/B 共用) | `DevOpsAgentDemoLambdaRole` | 含 `aidevops:CreateBacklogTask` / `ListJournalRecords` 等 |
| Lambda-B DLQ | `arn:aws:sqs:ap-northeast-1:<ACCOUNT_ID>:devops-agent-notify-dlq` | 14d retention |

---

## 3. EventBridge Rule 详解

### 3.1 Rule-1: Alarm → Lambda-A

**文件**：`eventbridge/rule-1-alarm-to-lambda-pattern.json`

```json
{
  "source": ["aws.cloudwatch"],
  "detail-type": ["CloudWatch Alarm State Change"],
  "detail": {
    "state": { "value": ["ALARM"] },
    "alarmName": [{ "prefix": "petsite-" }]
  }
}
```

**关键设计点**：

- `state.value=ALARM`：只在告警进入 ALARM 时触发；OK / INSUFFICIENT_DATA 状态忽略
- `alarmName.prefix=petsite-`：只触发 PetSite 项目的告警，避免账号里其他人加的告警刷 DevOps Agent preview 配额（2026-05-20 P0-7 落地）
- **没用 `account` 过滤**：单账号部署不需要；多账号部署需补上
- **Target 是 Lambda async invoke**：EventBridge 自动重试（默认 24 小时 / 185 次），但当前 Lambda-A *没配 DLQ*（见 §7 已知 gap）

### 3.2 Rule-2: Investigation Completed → Lambda-B

**文件**：`eventbridge/rule-2-investigation-completed-pattern.json`

```json
{
  "source": ["aws.aidevops"],
  "detail-type": ["Investigation Completed"]
}
```

**关键设计点**：

- pattern 故意宽松：不限定 agent_space_id 或 status；过滤逻辑放在 Lambda-B 里（更灵活，方便后续加 Failed/Cancelled 通知）
- **Lambda-B 顶层会 skip 非 COMPLETED 的 event**（Investigation 触发的 IN_PROGRESS / 中间事件不刷 Slack）

### 3.3 Event 样本（Investigation Completed）

DevOps Agent 完成时投到 EventBridge 的事件结构（从 Lambda-B 代码反推）：

```json
{
  "source": "aws.aidevops",
  "detail-type": "Investigation Completed",
  "account": "<ACCOUNT_ID>",
  "region": "ap-northeast-1",
  "time": "2026-05-20T...",
  "detail": {
    "metadata": {
      "agent_space_id": "<AGENT_SPACE_UUID>",
      "execution_id": "<uuid>",
      "task_id": "<uuid>"
    },
    "data": {
      "status": "COMPLETED",
      "createdAt": "2026-05-20T13:45:00Z",
      "updatedAt": "2026-05-20T13:48:42Z"
    }
  }
}
```

**注**：DevOps Agent preview 期 event schema 文档不全，字段名是从 `list_journal_records` API 和实际 event 里反推的。GA 后字段可能调整，需要重新对齐。

---

## 4. Lambda-A 代码要点

**文件**：`lambda/lambda_a/lambda_function.py`（~145 行）

### 4.1 入口短路

```python
if state_value != "ALARM":
    return {"statusCode": 200, "body": f"Skipped: state={state_value}"}
```

EventBridge Rule-1 已经过滤过 ALARM，但代码里再防一次（防 EventBridge 改 pattern 时漏过滤）。

### 4.2 字段取值（容易踩坑）

```python
# accountId / region 在 event 顶层，不在 detail 里！
account = event.get("account", "")
region  = event.get("region", REGION)
```

**这是 2026-05-18 修过的 bug**：早期版本从 `detail['account']` 取，结果总是空字符串。CloudWatch Alarm State Change event 的 schema 是 `account` / `region` 在顶层，详见：
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html

### 4.3 Starting point 提取（通用化）

```python
def _build_starting_point(detail):
    metrics = detail["configuration"]["metrics"]
    metric  = metrics[0]["metricStat"]["metric"]
    return metric["namespace"], metric["name"], metric.get("dimensions", {})
```

**为什么不写死 `"EC2 Instance: ..."`**：早期博客示例对 EC2 写死了 `EC2 Instance:` 描述，但 PetSite 用 EKS / RDS / Lambda 多种服务。提取 `namespace + dimensions` 让 description 自适应任何 AWS 服务。

### 4.4 Description 拼装（mirror console 体验）

```python
return (
    f"Investigation starting point:\n"
    f"  Source: CloudWatch Alarm '{alarm_name}'\n"
    f"  Account: {account}\n  Region: {region}\n"
    f"  Namespace: {namespace}\n  Metric: {metric_name}\n"
    f"  Dimensions:\n{dim_lines}\n"
    f"  Alarm reason: {reason}\n\n"
    f"Investigation details:\n"
    f"  Identify the root cause ... examine ... correlate ... "
    f"  Recommend concrete remediation actions ranked by impact and risk."
)
```

Agent 的 reasoning 是基于这段 free text 做的——结构化的"starting point + details"格式 mirror 了 DevOps Agent console 上的两个字段（preview 期 API 没分两个 field，所以用 `\n\n` 在 description 里分段）。

### 4.5 调用 SDK（不是 Webhook）

```python
_client = boto3.client("devops-agent", region_name=REGION)

response = _client.create_backlog_task(
    agentSpaceId=DEVOPS_AGENT_SPACE_ID,
    taskType="INVESTIGATION",
    title=title,
    priority="HIGH",
    description=description,
)
task = response["task"]  # 含 taskId / executionId / status
```

- **boto3 ≥ 1.43.0** 才有 `devops-agent` client（endpoint: `aidevops.<region>.amazonaws.com`）。Lambda 必须打 boto3 layer，runtime 自带的不够。
- IAM action 前缀是 `aidevops:`（不是 `devops-agent:`）—— 三套命名（service id / IAM action / event source）一定要用对：
  - service id: `devops-agent` (boto3 client)
  - IAM action: `aidevops:CreateBacklogTask`
  - event source: `aws.aidevops`

---

## 5. Lambda-B 代码要点

**文件**：`lambda/lambda_b/lambda_function.py`（~195 行）

### 5.1 Webhook URL 来自 Secrets Manager（cold-start 缓存）

```python
SLACK_WEBHOOK_SECRET_ARN = os.environ["SLACK_WEBHOOK_SECRET_ARN"]
_secrets_client = boto3.client("secretsmanager", region_name=REGION)
_webhook_url = _secrets_client.get_secret_value(
    SecretId=SLACK_WEBHOOK_SECRET_ARN,
)["SecretString"].strip()
```

- **顶层执行**：cold-start 拉一次，warm 容器复用
- **不 fallback** 到 env var 明文：拉失败直接抛异常 → invocation 进 DLQ
- **轮换语义**：`aws secretsmanager put-secret-value` 后等下一次 cold start 才生效（preview 期接受；要立即生效就 `aws lambda update-function-configuration` 强制刷新）

### 5.2 拉 investigation summary

```python
response = _client.list_journal_records(
    agentSpaceId=agent_space_id,
    executionId=execution_id,
)
for record in response.get("records", []):
    if record.get("recordType") == "investigation_summary_md":
        return record.get("content", "")
```

DevOps Agent 把不同类型的产出（thinking / tool_use / summary）作为不同 `recordType` 的 journal record。Lambda-B 只关心 `investigation_summary_md` 这一种（markdown 格式的最终结论）。

### 5.3 Slack Block Kit 渲染

```python
fields = [
    {"type": "mrkdwn", "text": f"*Task ID*\n`{task_id}`"},
    {"type": "mrkdwn", "text": f"*Execution ID*\n`{execution_id}`"},
    {"type": "mrkdwn", "text": f"*Agent Space*\n`{agent_space_id}`"},
    {"type": "mrkdwn", "text": f"*Region*\n`{REGION}`"},
]
if created_at:
    fields.append({"type": "mrkdwn", "text": f"*Started*\n`{created_at}`"})
if duration:
    fields.append({"type": "mrkdwn", "text": f"*Duration*\n`{duration}`"})
```

`Started` / `Duration` 是 2026-05-20 P0-9 加的（参见 TODO.md）。`Duration` 用 `_format_duration(createdAt, updatedAt)` 算成 `Xm Ys` 字符串。

### 5.4 长内容分块

Slack mrkdwn block 单段上限 3000 字符，代码里设 `SLACK_BLOCK_LIMIT = 2900` 留余量，按 line boundary 拆分：

```python
def _chunk_for_slack(text, limit=2900):
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current: chunks.append(current)
            current = line
        else:
            current += line
    if current: chunks.append(current)
    return chunks
```

每个 chunk 一个 `section` block，渲染成连续的 markdown 段。

### 5.5 POST 到 Webhook（不引依赖）

```python
req = urllib.request.Request(_webhook_url, data=..., method="POST")
with urllib.request.urlopen(req, timeout=10) as resp:
    body = resp.read().decode("utf-8", "ignore")
    if body.strip() != "ok":
        raise RuntimeError(f"Slack webhook returned: {body!r}")
```

故意没引 `slack_sdk` —— 包小冷启动快，需求简单（只 POST 一种 payload）。

---

## 6. 设计权衡：为什么不用 Agent Space Webhook

DevOps Agent console 提供 "Agent Space Webhook" 配置项，可以让 Agent 把 investigation 完成事件主动 POST 到外部 HTTP endpoint。我们的方案 **没用** 这个能力，原因：

| 维度 | 当前方案（EventBridge + Lambda） | Webhook 方案 |
|------|----------------------------------|-------------|
| 鉴权 | IAM SigV4（VPC-internal） | 公网 HTTPS endpoint + 自定义签名校验 |
| 可观测 | EventBridge metrics + Lambda CloudWatch | 自己负责 |
| 重试 | EventBridge 自动重试 + DLQ | 自己实现 |
| 延迟 | EventBridge 通常 <2s | 类似 |
| 触发 investigation | Lambda-A 用 SDK，能完全自定义 description | console 上配的 webhook 是单向（Agent → 外部）的，不能用于触发 |

**结论**：当前架构在安全 + 可观测 + 重试三个维度都比 webhook 方案更好。Webhook 方案只在「外部系统不在 AWS 内」时才更合适（譬如要触发 PagerDuty）。

---

## 7. 已知 gap 与改进路线

> 完整 TODO 见 `TODO.md`，这里只列影响本链路的项。

### 7.1 P0 已完成（2026-05-20）

- ✅ Slack webhook URL 挪到 Secrets Manager（P0-6）
- ✅ EventBridge Rule-1 加 `petsite-` 前缀过滤（P0-7）
- ✅ Lambda-A log 加 account / region（P0-8）
- ✅ Lambda-B Slack 通知加 Started / Duration（P0-9）
- ✅ Lambda-B 加 DLQ（P0-5）

### 7.2 ⚠️ 未修的 gap（重要）

#### **Lambda-A 没有 DLQ** ⚠️

```
$ aws lambda get-function-configuration --function-name devops-agent-trigger-investigation
DLQ: null
```

**风险**：Lambda-A 失败时（如 `aidevops:CreateBacklogTask` 报错、boto3 layer 异常），EventBridge 会重试，但最终失败的 invocation 没地方落，CloudWatch Logs 看不到完整失败链。

**建议**：补一个 SQS DLQ + `dead-letter-config`，参照 P0-5 Lambda-B 做法。属于本批 review 新发现的项，建议加进 TODO 当 *P0-24*（生产前必修）。

#### Failed / Cancelled investigation 没通知（P1-15）

当前 Lambda-B 只处理 `status=COMPLETED`，DevOps Agent 调查失败时 Slack 完全沉默。

**建议**：加一个 Failed/Cancelled 的红色 emoji 消息，附原因。已挂在 TODO P1-15。

#### CloudWatch Dashboard 缺失（P1-16）

Lambda-A invocations / errors / duration、investigation 平均耗时、quota 使用率都没集中展板。已挂 TODO P1-16。

### 7.3 P1 / P2

详见 TODO.md。本链路相关：
- P1-14 单元测试覆盖纯函数（`_format_description` / `_chunk_for_slack` / `_format_duration`）
- P1-17 DevOps Agent Skill / KnowledgeItem（让 Agent 知道 PetSite 拓扑）
- P2-21 Agent IAM tag-based scope（限制 Agent 只看 `Project=petsite` 资源）

---

## 8. 排错速查

| 现象 | 排查路径 |
|------|---------|
| 告警触发但 DevOps Agent 没建 investigation | 1. `aws events describe-rule --name DevOps-Agent-Demo-Alarm-To-Lambda` 看 EventPattern 是否含 `prefix:petsite-` <br> 2. CloudWatch Logs `/aws/lambda/devops-agent-trigger-investigation` 看 Lambda 是否收到 event <br> 3. 看 alarm 名字是否以 `petsite-` 开头 |
| Lambda-A 收到 event 但 create_backlog_task 失败 | 1. 看 CloudWatch Logs 异常 traceback <br> 2. `aws iam get-role-policy --role-name DevOpsAgentDemoLambdaRole` 看权限 <br> 3. 确认 boto3 layer 版本 ≥ 1.43.0（runtime 自带的不够） |
| Investigation 完成但 Slack 没通知 | 1. CloudWatch Logs `/aws/lambda/devops-agent-notify-slack` <br> 2. `aws sqs get-queue-attributes --queue-url .../devops-agent-notify-dlq --attribute-names ApproximateNumberOfMessages` 看 DLQ 是否有积压 <br> 3. 确认 Secret 里的 webhook URL 还有效 |
| Slack 消息 markdown 格式错乱 | Slack mrkdwn 不是标准 markdown：粗体用 `*text*` 不是 `**text**`；链接用 `<url|label>`。检查 `investigation_summary_md` 内容是否符合 Slack 风格 |

---

## 9. 引用

- 代码仓库：`/home/ubuntu/tech/devops-agent/`
- DevOps Agent 用户指南：https://docs.aws.amazon.com/devopsagent/latest/userguide/
- CloudWatch Alarm State Change event schema：https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html
- TODO 跟进：`/home/ubuntu/tech/devops-agent/TODO.md`
- 历次 review 记录：`/home/ubuntu/.openclaw/workspace-doc-reviewer/memory/2026-05-{18,19,20}.md`
