# AWS DevOps Agent — 部署文档

> **目标账号**：`<ACCOUNT_ID>`（你自己的 12 位 AWS 账号）
> **Region**：`<region>`（PetSite 实测在 `ap-northeast-1`）
> **Agent Space**：`<agent-space-name>` (`<AGENT_SPACE_UUID>`)
> **通知渠道**：Slack 频道 `<SLACK_CHANNEL_ID>` + Incoming Webhook
> **状态**：✅ 已端到端验证通过（FIS 删 2 个 EKS node → DevOps Agent 自动调查 → Slack 推摘要）

> 本仓里所有 `<UPPERCASE_PLACEHOLDER>` 都靠 `.env` 注入，文件不入库。具体每个变量的取值方式见 [§2.5 Configuration](#25-configuration-env)。

## Quick Start

```bash
git clone <repo-url> && cd devops-agent
cp .env.example .env
# edit .env with your account/region/Slack/Agent Space values
bash scripts/deploy.sh           # main alarm → investigation → Slack pipeline
bash scripts/deploy-chatbot.sh   # optional: Slack chatbot for interactive chat
```

---

## 0. 架构总览

```
CloudWatch Alarm (任意 namespace)
        │  state = ALARM
        ▼
EventBridge Rule-1 ── aws.cloudwatch / CloudWatch Alarm State Change
        │
        ▼
Lambda-A (devops-agent-trigger-investigation)
        │  create_backlog_task(taskType=INVESTIGATION)
        │  把 namespace + dimensions + alarm reason 结构化进 description
        ▼
DevOps Agent  ── 自主调查 5–15 min
        │     - 调 use_aws describe_xxx 收集资源数据
        │     - 拉 CloudWatch metrics / CloudTrail events
        │  发布 aws.aidevops / Investigation Completed
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

---

---

## 2. 前置条件

### 2.1 账号侧

- AWS CLI v2 已配置 `<ACCOUNT_ID>` 凭证
- AWS DevOps Agent 在 `ap-northeast-1` 已可用（preview 阶段）
- *已确认*：你已经在 console 创建好 Agent Space（如果没有，aidevops console → *Create Agent Space*），把 ID 填进 `.env` 的 `DEVOPS_AGENT_SPACE_ID`

> 控制台拿 ID：`aidevops` → `Agent Spaces` → 点详情 → 右上角 *Copy ARN* → 取 `agentspace/` 后的 UUID。

### 2.2 boto3 版本（重要）

DevOps Agent 客户端在 *boto3 1.43.0* 才发布到公开 PyPI。Lambda Python 3.12 内置的 boto3 是更老的版本，*不带 `devops-agent` 客户端*，所以*必须打 layer*。部署脚本已自动处理（`pip install boto3>=1.43.0 -t layer/python`）。

> 服务名映射（要全部记住）：
> - boto3 client 名：`devops-agent`
> - IAM action 前缀：`aidevops:`
> - EventBridge source：`aws.aidevops`
> - 服务 endpoint：`aidevops.<region>.amazonaws.com`

### 2.3 Slack 集成（已就绪）

频道 `<SLACK_CHANNEL_ID>` 已有 Incoming Webhook（被 `petsite-ops-slack-notifier` 用着，*本方案直接复用*——不用新建 Slack App，不用 Bot Token）。

Webhook URL 已挪到 Secrets Manager（2026-05-20，P0-6）：

```
arn:aws:secretsmanager:<region>:<ACCOUNT_ID>:secret:devops-agent/slack-webhook-url-<RANDOM_SUFFIX>
```

Lambda-B 通过 `SLACK_WEBHOOK_SECRET_ARN` env var 拿到 ARN，cold-start 时 `GetSecretValue` 一次后缓存到 module global。

*Webhook 轮换流程*：`aws secretsmanager put-secret-value --secret-id devops-agent/slack-webhook-url --secret-string <new-url>` → 等下一次 cold start（≤15 分钟空闲后）生效。

### 2.4 本机工具

- `aws` v2.x、`python3.12`、`pip`、`zip`、`unzip`、`jq`、`envsubst`（`gettext` 包带）
- `kubectl`（验证 EKS node 时用）

### 2.5 Configuration (`.env`)

仓库里没有任何账号 / 资源 ID 真值；全部走 `.env`：

```bash
cp .env.example .env
$EDITOR .env       # 填进真值
```

`.env` 已在 `.gitignore`，*真值永远不进 git*。`.env.example` 全是占位符 + 注释说明，可以放心 push。

| 变量 | 怎么拿 |
|---|---|
| `AWS_ACCOUNT_ID` | `aws sts get-caller-identity --query Account --output text` |
| `AWS_REGION` | 你计划部署的 region（本项目所有资源同区） |
| `DEVOPS_AGENT_SPACE_ID` | Console → `aidevops` → Agent Spaces → 详情 → *Copy ARN* → 取 `agentspace/` 后的 UUID |
| `DEVOPS_AGENT_SPACE_ARN` | 同上，整个 ARN |
| `SLACK_CHANNEL` / `SLACK_TEST_CHANNEL` | Slack 客户端：右键频道 → View channel details → 底部 *Channel ID* (`Cxxxxxx`) |
| `SLACK_WEBHOOK_URL` | Slack App → Incoming Webhooks → Add New Webhook（`deploy.sh` 写进 Secrets Manager 后 Lambda 从那读） |
| `SLACK_BOT_TOKEN_SECRET_ID` / `SLACK_SIGNING_SECRET_ID` | chatbot 才用，建 Slack App 流程见 `docs/slack-setup.md` |
| `API_GATEWAY_INVOKE_URL` | 第一次跑 `deploy-chatbot.sh` 后自动打印，复制回 `.env` |

> 安全提示：千万不要 `git add .env`。脚本和 docs 都从 `.env` 读，`<...>` 占位符只用于文档展示，不会走进 AWS API。

---

## 3. 部署

### 3.1 准备 .env

```bash
cd /home/ubuntu/tech/devops-agent
cp .env.example .env
# 编辑 .env 填真值后再继续。详见 §2.5 Configuration。
```

### 3.2 一键部署核心组件

```bash
./scripts/deploy.sh
```

脚本步骤：

1. 校验当前 caller 是 `<ACCOUNT_ID>`
2. 创建 IAM Role `DevOpsAgentDemoLambdaRole`（含 `aidevops:*` 投放任务 / 读 chat / 拉 journal 权限）
3. 构建并发布 `boto3-latest` layer（boto3 ≥ 1.43.0，含 devops-agent 客户端）
4. 部署 Lambda-A `devops-agent-trigger-investigation`
5. 部署 Lambda-B `devops-agent-notify-slack`
6. 创建 EventBridge Rule-1（任意 CloudWatch Alarm → Lambda-A）
7. 创建 EventBridge Rule-2（Investigation Completed → Lambda-B）
8. 打印资源 ARN

幂等：可重复执行，已存在资源走 update 路径。

### 3.3 加靠谱的告警（针对 EKS node 故障）

PetSite 账号默认的 ContainerInsights node 告警*已经全废*（NodeName 写死了旧节点 IP）。要让 *删 node* 类故障真的能触发告警，跑：

```bash
./scripts/add-node-loss-alarms.sh
```

会创建 4 个告警（period 60s, eval 1）：

| 告警 | 触发条件 | 真实命中率（FIS 实测） |
|---|---|---|
| `petsite-asg-instances-below-desired-workers1a60` | `GroupInServiceInstances / GroupDesiredCapacity < 1.0`（metric math） | ❌ 故障窗口 < 60s 时不触发 |
| `petsite-asg-instances-below-desired-workers1cFE` | 同上 | 同上 |
| `petsite-eks-pods-unschedulable` | `AWS/EKS scheduler_pending_pods_UNSCHEDULABLE > 0` | ✅ 命中（实测 60s 故障窗口能捕到） |
| `petsite-eks-apiserver-5xx` | `AWS/EKS apiserver_request_total_5XX > 5/min` | ⚠️ 删 node 通常不影响 control plane |

> *关键经验*：本账号 `ContainerInsights` namespace *完全没 metric*（CW agent / fluent-bit 没安装或停了），所以最初尝试用 `cluster_node_count` / `cluster_failed_node_count` 都失败。最后落到 `AWS/EKS scheduler_pending_pods_UNSCHEDULABLE`，这是 EKS 内置的 control-plane metric，*不依赖任何 add-on*，并且能真实反映"node 不够导致 pod 调度失败"。

---

## 4. 文件结构

```
devops-agent/
├── README.md                                    ← 本文件
├── .env.example                                 ← 配置模板（占位符，可入库）
├── .env                                         ← 真实配置（.gitignore 排除，永远不入库）
├── .gitignore
├── iam/
│   ├── lambda-role-trust.json                   Lambda 信任策略
│   ├── devops-agent-policy.json                 aidevops:* 权限（含 chat）
│   ├── chatbot-policy.json.template             chatbot 用 IAM policy 模板（账号 ID 用 ${AWS_ACCOUNT_ID} 占位，deploy 时 envsubst 渲染到 .build/iam/）
│   └── backups/
│       └── RestrictToTokyoRegion-*.json         旧 region 限制 policy 备份（已删除）
├── lib/
│   └── agent_chat.py                            DevOps Agent EventStream 解析与多轮对话（CLI + Lambda-C 共用）
├── lambda/
│   ├── cli/chat.py                              Chat CLI 入口
│   ├── lambda_a/lambda_function.py              触发调查（通用化 description）
│   ├── lambda_b/lambda_function.py              Slack webhook 通知
│   └── lambda_c/                                Slack Chatbot（交互式多轮）
│       ├── lambda_function.py                   entry + worker dispatcher
│       └── slack_verify.py                      Slack 签名验证
├── tests/                                       71 测试覆盖 Lambda-A/B/C + 签名验证
├── eventbridge/
│   ├── rule-1-alarm-to-lambda-pattern.json
│   └── rule-2-investigation-completed-pattern.json
└── scripts/
    ├── deploy.sh                                一键部署主链路（读 .env）
    ├── deploy-chatbot.sh                        部署 Lambda-C + API GW + DLQ + alarms
    ├── setup-dashboard.sh                       provision CloudWatch Dashboard（deploy.sh 默认会调）
    ├── cleanup.sh                               一键清理主链路
    ├── cleanup-chatbot.sh                       清理 chatbot 资源
    ├── add-node-loss-alarms.sh                  PetSite 专用：加 EKS/ASG node-loss 告警
    ├── chat.sh                                  Chat CLI wrapper
    ├── run-fis-2node-termination.sh             PetSite 专用：FIS 删 2 个 node 验证脚本
    └── test-trigger.sh                          （旧）stress-ng EC2 触发测试
```

---

## 5. 核心代码（关键改动点）

### 5.0 Chat CLI（交互式实时对话）

除了异步的 Backlog Task 调查，本项目还提供一个 CLI 工具，调 `create_chat` + `send_message` 同步跟 Agent 聊天。

```bash
# 单轮问答
./scripts/chat.sh "List running EC2 instances"

# 交互式 REPL（多轮上下文）
./scripts/chat.sh -i

# 恢复之前的会话（多轮追问）
./scripts/chat.sh --resume <executionId> "follow-up question"

# 看到 executionId / token usage / tool calls / context utilization
./scripts/chat.sh --show-ids "..."

# 看到 Agent 的 thinking 过程
./scripts/chat.sh --show-thinking "..."
```

*实现要点*（`lambda/cli/chat.py`）：

- `send_message` 返回一个 EventStream，需要按 *内容块类型* 分流：

  | block type | 含义 | 默认是否展示 |
  |---|---|---|
  | `final_response` | **最终回答** | ✅ stdout |
  | `text` | Agent 思考过程 | ❌（`--show-thinking` 才展） |
  | `chat_title` | 自动生成的会话标题 | ❌（`--show-ids` 才展） |
  | `context_usage` | 上下文窗口利用率 | ❌ metadata |
  | `tool_use` / `tool_summary` | Agent 调用的 AWS API 详情 | ❌（`--show-ids` 才展） |

- 多轮对话靠复用同一个 `executionId`。`create_chat` 只需一次。
- `usage.inputTokens` / `outputTokens` 可以跟踪成本。
- `context_usage.context_window.utilization` 超过 70% 考虑开新会话。

### 5.1 Lambda-A：通用化的 description（含 bug fix）

```python
# 把所有 dimensions 都摊出来，不假设是 EC2
metric = metrics[0].get("metricStat", {}).get("metric", {})
namespace   = metric.get("namespace", "")
metric_name = metric.get("name", "")
dimensions  = metric.get("dimensions", {}) or {}

# ⚠️ accountId / region 在 EventBridge event 的 TOP LEVEL，不在 detail 里！
# 之前曾从 detail.get("accountId") 读，永远是空。现在从 event.get("account") 读。
account = event.get("account", "")
region  = event.get("region", REGION)

description = (
    # ===== Investigation starting point（与控制台字段对齐）=====
    f"Investigation starting point:\n"
    f"  Source: CloudWatch Alarm '{alarm_name}'\n"
    f"  Account: {account}  Region: {region}\n"
    f"  Namespace: {namespace}\n"
    f"  Metric: {metric_name}\n"
    f"  Dimensions:\n" + "\n".join(f"  - {k}: {v}" for k, v in dimensions.items()) + "\n"
    f"  Alarm reason: {reason}\n\n"
    # ===== Investigation details =====
    f"Investigation details:\n"
    f"  Identify the root cause of this alarm. ..."
)
```

| 告警类型 | Namespace | 关键 dimensions | 是否需改代码 |
|---|---|---|---|
| EC2 CPU 飙升 | `AWS/EC2` | `InstanceId` | ❌ 自适配 |
| RDS 连接耗尽 | `AWS/RDS` | `DBInstanceIdentifier` | ❌ 自适配 |
| EKS pods unschedulable | `AWS/EKS` | `ClusterName` | ❌ 自适配 |
| Lambda 错误率 | `AWS/Lambda` | `FunctionName` | ❌ 自适配 |
| ALB 5xx | `AWS/ApplicationELB` | `LoadBalancer`, `TargetGroup` | ❌ 自适配 |

### 5.2 Lambda-B：Slack Incoming Webhook

- 复用 `petsite-ops-slack-notifier` 用的 webhook
- 标准库 `urllib.request`，无第三方依赖
- Block Kit 三段式：header / metadata fields / divider / summary chunks
- summary > 2900 字符自动按行分块（Slack mrkdwn block 单块 3000 字符上限）
- 仅在 `data.status == "COMPLETED"` 才发，避免中间状态噪音

### 5.3 IAM 权限

`iam/devops-agent-policy.json`：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DevOpsAgentBacklog",
      "Effect": "Allow",
      "Action": [
        "aidevops:CreateBacklogTask",
        "aidevops:GetBacklogTask",
        "aidevops:ListJournalRecords",
        "aidevops:ListBacklogTasks"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DevOpsAgentChat",
      "Effect": "Allow",
      "Action": [
        "aidevops:CreateChat",
        "aidevops:SendMessage",
        "aidevops:ListChats",
        "aidevops:ListPendingMessages"
      ],
      "Resource": "*"
    }
  ]
}
```

> ⚠️ IAM action 前缀是 `aidevops:`（**不是** `devops-agent:`）。

> *Agent Space 服务角色另外说一下*：`DevOpsAgentRole-AgentSpace-<RANDOM_SUFFIX>` 是 *Agent 自己*用来调 AWS API（EC2/RDS/EKS Describe）的角色，不归本项目管。它原本挂了一个 `RestrictToTokyoRegion` inline policy 限制只能查 Tokyo，但*副作用是 Agent 第一步 DescribeRegions 在 us-east-1 endpoint 就被 deny，导致整个调查链路失效*。该 policy 已经在调试时删除（备份在 `iam/backups/`），允许 Agent 跨 region 扫描（每次问答 ~30 个 tool call 是这个原因）。如需恢复 region 限制，参考[官方文档](https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-limiting-agent-access-in-an-aws-account.html) 用 *tag-based scoping* 替代纯 region deny。

### 5.4 EventBridge

**Rule-1**（任意 ALARM → Lambda-A）：
```json
{
  "source": ["aws.cloudwatch"],
  "detail-type": ["CloudWatch Alarm State Change"],
  "detail": { "state": { "value": ["ALARM"] } }
}
```

**Rule-2**（Investigation Completed → Lambda-B）：
```json
{
  "source": ["aws.aidevops"],
  "detail-type": ["Investigation Completed"]
}
```

### 5.5 Slack Chatbot（交互式多轮对话）

除了 *告警 → 调查 → 推送 Slack* 这条异步主线，还部署了 *Slack Chatbot*：用户在 `#<SLACK_CHANNEL_ID>` 里 `@devops_agent <问题>` 即可调 DevOps Agent chat，结果推回同一 thread，*同 thread 内多轮追问会复用同一个 chat session*。

*架构*：

```
Slack @mention → Slack Events API → API Gateway HTTP API → Lambda-C
                                                              │ (fast path: ack 200 in <3s)
                                                              ├→ lambda.invoke(self, _internal=chat)  异步自调用
                                                              │
                                                              └→ (worker path)
                                                                  1. DDB get_item(thread_ts) 查/建 chat
                                                                  2. devops-agent.send_message  调用
                                                                  3. Slack chat.postMessage 占位占 + chat.update 替换
```

*环境变量*（都在 `.env`）：

```bash
SLACK_BOT_TOKEN_SECRET_ID=devops-agent/slack-chatbot-token
SLACK_SIGNING_SECRET_ID=devops-agent/slack-signing-secret
SLACK_TEST_CHANNEL=<SLACK_CHANNEL_ID>
THREAD_TABLE_NAME=devops-agent-slack-threads
LAMBDA_C_NAME=devops-agent-slack-chatbot
```

*部署*：

```bash
# 1. 创建 Slack App（手动一次）、拿 Bot Token + Signing Secret、存进 Secrets Manager（详见 docs/slack-setup.md）
# 2. 创建 DDB 表
Lambda-C 部署前必须先建表：

aws dynamodb create-table --region "${AWS_REGION}" \
  --table-name devops-agent-slack-threads \
  --attribute-definitions AttributeName=thread_ts,AttributeType=S \
  --key-schema AttributeName=thread_ts,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
aws dynamodb wait table-exists --region "${AWS_REGION}" --table-name devops-agent-slack-threads
aws dynamodb update-time-to-live --region "${AWS_REGION}" \
  --table-name devops-agent-slack-threads \
  --time-to-live-specification "Enabled=true,AttributeName=ttl"

# 3. 部署
export ENV_FILE=.env
./scripts/deploy-chatbot.sh
# 输出里会包含 API GW Invoke URL，用于下一步

# 4. 在 Slack App 控制台配 Event Subscriptions（手动一次）：
#    Request URL = 上一步 Invoke URL
#    Subscribe to bot events: app_mention
#    Save → Reinstall App
```

*验证*：

```bash
# 在 #<SLACK_CHANNEL_ID> 里：
@devops_agent ping                                  # 得到 "Hey there!" 之类
@devops_agent list EC2 instances in <region>        # 得到 EC2 列表
# 同 thread 追问（点上一条的 “Reply in thread”）：
which one is biggest?                                # 已开 thread 内可免 @ 直接对话（见下方 P1-18）
```

#### P1-18：thread 内免 @mention 续问（2026-05-21 起）

每次都打 `@devops_agent` 太啰嗦。从 P1-18 开始：*只要 thread 已被 bot 回过（DDB 命中 thread_ts 记录）*，同 thread 内的 `message.channels` event 也会被 chatbot 接住，无需再 @。

启用步骤（一次性）：

1. Slack App Console → **OAuth & Permissions** → 给 Bot 加 `channels:history` scope（读取频道消息历史，仅在已 invite 的频道生效）
2. **Event Subscriptions** → *Subscribe to bot events* → 加 `message.channels`
3. 顶部黄条 *Reinstall App*

设计要点：

- 进 Lambda-C 的 *所有* `message.channels` event 先查 DDB（thread_ts → executionId）；*没命中就直接丢*，不走 send_message。这是为什么 noise 不会爆量
- 防 bot 自言自语：`event.user == bot_user_id` 直接 skip
- 收到陌生 thread 消息（DDB 没记录）→ 计数到 CloudWatch metric `DevOpsAgent/SlackChatbot/UnhandledMessageEvents`，可在 Dashboard 看 noise 量；持续 > 10/min 表示要么 scope 太宽要么 DDB TTL 过期了

*考获与设计要点*：

- *Slack 3 秒响应规则* — fast path 必须在 3s 内 200 ack。实现为同一 Lambda 自异步调用（`InvocationType=Event`）走 worker 路径，实测 fast path *热启动 ~258ms / 冷启动 ~1019ms*
- *多轮对话* — thread_ts 作为 DDB PK。同 thread 复用同一 `executionId`。并发顶双 mention 靠 *DDB conditional put* 避免重复 create_chat
- *安全* — Slack signing 验签（v0:timestamp:body HMAC-SHA256）+ 5-min replay protection + `hmac.compare_digest`
- *去重* — Slack 重试（`X-Slack-Retry-Num`）直接返 200 不二次调 chat
- *TTL* — thread 表 7 天后自动过期，超过一周没人提的 thread 下次会重启会话

*未取 webhook 路线*：不复用 Lambda-B 的 Incoming Webhook，因为 webhook *不能接收 events*，只能单向发 —— 双向必须上 Bot Token。这也是为什么新建了 Slack App `PetSite DevOps Agent`。

*单元测试*：tests/test_slack_verify.py 覆盖 12 个场景（正常签名 / 大写 header / 过期时间戳 / 未来时间戳 / 篡改 body / 错 secret / 缺 header / 空 secret / 非 bytes body / timing-safe 比对 等）。

---

## 6. 端到端验证（FIS 实战）

### 6.1 实战脚本

```bash
./scripts/run-fis-2node-termination.sh
```

会克隆账号里现成的 EKS node termination 模板（`EXT25AmEAp21foyA`），改 `instanceTerminationPercentage=100`，删 PetSite/workers1a60 nodegroup 的*全部 2 个 node*。

### 6.2 流量来源

复用账号里现有的 `openclaw-health-canary` Synthetic（rate 30 min，频率较低）。如需更密集流量验证 ALB 5xx 告警：

```bash
# 临时压一会儿
hey -z 5m -c 5 http://<your-alb-dns-name>/
```

### 6.3 实测时间线（参考）

| 时间点 | 事件 | 备注 |
|---|---|---|
| T+0s | FIS `start-experiment` | 状态 `initiating` |
| T+13s | FIS `completed` | 2 个 1a node 已被 terminate |
| T+1min | ASG 补上 1 个新 node | minSize=2 强制 |
| T+2min | `petsite-eks-pods-unschedulable` *OK → ALARM*（3 pods unschedulable） | ✅ 告警捕到 |
| T+2min | EventBridge → Lambda-A → `create_backlog_task` | DevOps Agent 接到 |
| T+3min | 告警 *ALARM → OK*（pods 已重新调度） | 故障窗口约 60s |
| T+5–15min | DevOps Agent 调查完成，发 `Investigation Completed` 事件 | |
| T+5–15min | Lambda-B 拉 journal → Slack `<SLACK_CHANNEL_ID>` | 收到 markdown 摘要 |

### 6.4 观察日志

```bash
source .env  # so AWS_REGION / DEVOPS_AGENT_SPACE_ID are loaded into shell

# Lambda-A 实时日志
aws logs tail /aws/lambda/devops-agent-trigger-investigation --follow --region "${AWS_REGION}"

# Lambda-B 实时日志
aws logs tail /aws/lambda/devops-agent-notify-slack --follow --region "${AWS_REGION}"

# 当前 Investigation 任务
python3 -c "
import os, boto3, json
c = boto3.client('devops-agent', region_name=os.environ['AWS_REGION'])
r = c.list_backlog_tasks(agentSpaceId=os.environ['DEVOPS_AGENT_SPACE_ID'], filter='taskType=INVESTIGATION')
print(json.dumps(r['tasks'][:5], default=str, indent=2))
"
```

### 6.5 关键发现（这次实战学到的）

1. *ASG `GroupInServiceInstances < Desired` 告警没触发* — 故障窗口 < 60s，1-min period 的告警没采到 < 1.0 的 datapoint。*删 node 类故障 ASG 告警不可靠*
2. *`AWS/EKS scheduler_pending_pods_UNSCHEDULABLE` 是关键检测点* — 这是 EKS 内置 metric，反映 *实际症状*（pod 找不到 node）。建议作为 EKS 集群的 baseline alarm
3. *ContainerInsights metric 在本账号不存在* — 任何依赖 `cluster_node_count` / `cluster_failed_node_count` 的告警都会 INSUFFICIENT_DATA
4. *Bug found & fixed*：原 Lambda-A 从 `event['detail'].get('accountId')` 读 account ID，但 EventBridge 事件结构里 `account` 和 `region` 是 *top-level 字段*。已改成 `event.get('account')` / `event.get('region')`

---
---

## 8. 清理

```bash
./scripts/cleanup.sh           # 主链路（IAM role / Lambda-A&B / 两个 EventBridge 规则）
./scripts/cleanup-chatbot.sh   # chatbot（Lambda-C / API Gateway / SlackChatbotAccess inline policy）
```

会删：IAM role、3 个 Lambda、2 个 EventBridge 规则、API Gateway HTTP API。
**不会删**（需手动）：
- boto3 layer 版本（layer 是按账号共享，cleanup 留下避免误删别的项目）
- Agent Space
- 3 个 SQS DLQ（`devops-agent-trigger-dlq` / `devops-agent-notify-dlq` / `devops-agent-slack-chatbot-dlq`）
- CloudWatch Alarms（`devops-agent-slack-chatbot-{errors,apigw-5xx,ddb-throttle}` + 4 个 node-loss 告警）
- CloudWatch Dashboard `DevOpsAgent-PetSite`
- DynamoDB `devops-agent-slack-threads`
- Secrets Manager 三个 secret（webhook URL / bot token / signing secret）
- Slack webhook + Slack App

手动清理示例：
```bash
# DLQ
for q in devops-agent-trigger-dlq devops-agent-notify-dlq devops-agent-slack-chatbot-dlq; do
  url=$(aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "$q" --query QueueUrl --output text 2>/dev/null) \
    && aws sqs delete-queue --region "${AWS_REGION}" --queue-url "$url"
done

# Dashboard
aws cloudwatch delete-dashboards --region "${AWS_REGION}" --dashboard-names DevOpsAgent-PetSite

# Chatbot alarms
aws cloudwatch delete-alarms --region "${AWS_REGION}" --alarm-names \
  devops-agent-slack-chatbot-errors \
  devops-agent-slack-chatbot-apigw-5xx \
  devops-agent-slack-chatbot-ddb-throttle

# Node-loss alarms (PetSite-specific)
aws cloudwatch delete-alarms --region "${AWS_REGION}" --alarm-names \
  petsite-asg-instances-below-desired-workers1a60 \
  petsite-asg-instances-below-desired-workers1cFE \
  petsite-eks-pods-unschedulable \
  petsite-eks-apiserver-5xx
```

---

## 附录 A：Investigation Completed 事件示例

```json
{
  "source": "aws.aidevops",
  "detail-type": "Investigation Completed",
  "detail": {
    "version": "1.0.0",
    "metadata": {
      "agent_space_id": "<AGENT_SPACE_UUID>",
      "task_id": "<UUID>",
      "execution_id": "exe-ops1-<UUID>"
    },
    "data": {
      "task_type": "INVESTIGATION",
      "priority": "HIGH",
      "status": "COMPLETED",
      "created_at": "...",
      "updated_at": "...",
      "summary_record_id": "<UUID>"
    }
  }
}
```

## 附录 B：Journal record 类型

| recordType | 说明 |
|---|---|
| `investigation_summary_md` | **完整调查摘要（Markdown）—— Lambda-B 用这个发 Slack** |
| `investigation_summary` | 结构化摘要（JSON） |
| `symptom` | 发现的症状 |
| `finding` | 调查发现（含根因） |
| `observation` | 观测数据 |
| `investigation_gap` | 调查信息缺口 |
| `message` | Agent 对话消息 |

## 附录 C：关键 ARN 速查（模板）

> 全部用占位符；运行 `bash scripts/print-config.sh` 或自己看 .env / `aws sts get-caller-identity` 拿到真值。

```
Account:            <ACCOUNT_ID>
Region:             <region>
Agent Space:        <agent-space-name>
Agent Space ID:     <AGENT_SPACE_UUID>
Agent Space ARN:    arn:aws:aidevops:<region>:<ACCOUNT_ID>:agentspace/<AGENT_SPACE_UUID>
Slack Channel:      <SLACK_CHANNEL_ID>
Slack Webhook:      <secret> arn:aws:secretsmanager:<region>:<ACCOUNT_ID>:secret:devops-agent/slack-webhook-url-<RANDOM_SUFFIX>
Slack App (bot):    <slack-app-id> (PetSite DevOps Agent)
Slack Bot User:     <slack-bot-user-id> (devops_agent)
Slack Bot ID:       <slack-bot-id>
Bot Token Secret:   arn:aws:secretsmanager:<region>:<ACCOUNT_ID>:secret:devops-agent/slack-chatbot-token-<RANDOM_SUFFIX>
Sign Secret:        arn:aws:secretsmanager:<region>:<ACCOUNT_ID>:secret:devops-agent/slack-signing-secret-<RANDOM_SUFFIX>
IAM Role (Lambda):  arn:aws:iam::<ACCOUNT_ID>:role/DevOpsAgentDemoLambdaRole
IAM Role (Agent):   arn:aws:iam::<ACCOUNT_ID>:role/service-role/DevOpsAgentRole-AgentSpace-<RANDOM_SUFFIX>
Lambda-A:           arn:aws:lambda:<region>:<ACCOUNT_ID>:function:devops-agent-trigger-investigation
Lambda-B:           arn:aws:lambda:<region>:<ACCOUNT_ID>:function:devops-agent-notify-slack
Lambda-C:           arn:aws:lambda:<region>:<ACCOUNT_ID>:function:devops-agent-slack-chatbot
API Gateway:        <API_ID>
API Invoke URL:     https://<API_ID>.execute-api.<region>.amazonaws.com/slack/events
DDB Thread Table:   arn:aws:dynamodb:<region>:<ACCOUNT_ID>:table/devops-agent-slack-threads
Layer (boto3):      arn:aws:lambda:<region>:<ACCOUNT_ID>:layer:boto3-latest:1
Rule-1:             DevOps-Agent-Demo-Alarm-To-Lambda
Rule-2:             DevOps-Agent-Investigation-Completed
EKS Cluster:        <eks-cluster-name>
PetSite ALB:        <alb-dns-name>
```
