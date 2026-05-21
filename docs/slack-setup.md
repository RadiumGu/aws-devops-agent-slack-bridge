# Slack App 配置指南

> 一次性配置，给 *DevOps Agent Slack Chatbot*（Lambda-C）用。
> 完成 Step 1-5 后存好两个 secret 的 ARN，运行 `./scripts/deploy-chatbot.sh`。
> 再回来做 Step 6（绑 Request URL）。

## Step 1：创建 Slack App

1. 打开 https://api.slack.com/apps → *Create New App* → *From scratch*
2. App Name：`PetSite DevOps Agent`
3. Workspace：选你自己的 workspace
4. Create

## Step 2：配 Bot Token Scopes

侧边栏 *OAuth & Permissions* → *Scopes* → *Bot Token Scopes*：

| Scope | 用途 |
|---|---|
| `app_mentions:read` | 接收 @bot 消息 |
| `chat:write` | 回复消息 |
| `chat:write.public` | 在没 invite bot 的频道也能回复 |
| `users:read` | 取得发问者用户名 |
| `channels:history` | （P1-18）已开 thread 内免 @ 续问；只读已 invite 的频道历史 |

## Step 3：安装 App，拿 Bot Token

1. *Install to Workspace* → 同意权限
2. 复制 *Bot User OAuth Token*（`xoxb-...`）
3. 把 bot 加进目标频道（`#<SLACK_CHANNEL_ID>`）：发 `/invite @PetSite DevOps Agent`

## Step 4：拿 Signing Secret

侧边栏 *Basic Information* → *App Credentials* → *Signing Secret* → *Show* → 复制

## Step 5：两个 secret 存进 Secrets Manager

先 `source .env`，让 `${AWS_REGION}` 可用：

```bash
source .env

aws secretsmanager create-secret \
  --region "${AWS_REGION}" \
  --name devops-agent/slack-chatbot-token \
  --secret-string 'xoxb-…'

aws secretsmanager create-secret \
  --region "${AWS_REGION}" \
  --name devops-agent/slack-signing-secret \
  --secret-string '32-char-hex…'
```

⚠️ secret 字符串不要带任何空格、换行、引号，*只有 token 本身*。

```bash
# 验 token 能用
TOKEN=$(aws secretsmanager get-secret-value --region "${AWS_REGION}" \
  --secret-id devops-agent/slack-chatbot-token --query SecretString --output text | tr -d '\n')
curl -sS -H "Authorization: Bearer $TOKEN" https://slack.com/api/auth.test | python3 -m json.tool
# 期望 "ok": true
```

## Step 6：配 Event Subscriptions（部署 Lambda-C 后做）

跑 `./scripts/deploy-chatbot.sh` 后会输出 *API GW Invoke URL*，拿那个 URL（也可以从 `.env` 的 `API_GATEWAY_INVOKE_URL` 读）：

1. 侧边栏 *Event Subscriptions* → *Enable Events*: ON
2. *Request URL*: `https://<API_ID>.execute-api.<region>.amazonaws.com/slack/events`
   - Slack 立即 POST `url_verification`，Lambda 应返回 challenge → ✅ Verified
3. *Subscribe to bot events* → *Add Bot User Event* → 选：
   - `app_mention` — 接收显式 @bot
   - `message.channels` — *（P1-18）已开 thread 内可免 @ 续问；Lambda 会先查 DDB 命中 thread_ts 再决定是否回复，noise 由 metric `DevOpsAgent/SlackChatbot/UnhandledMessageEvents` 监控*
4. *Save Changes*
5. 顶部黄条 *Reinstall App* → 确认

## 测试

在你配置的频道（`SLACK_TEST_CHANNEL` in `.env`）：

```
@devops_agent ping
@devops_agent list EC2 instances in <region>
```

同 thread 追问（点上一条 *Reply in thread*）：

```
which one is biggest?       # P1-18: thread 内免 @
```

第二条 Agent 应该记得上一轮的 EC2 列表。

## 故障排查

```bash
source .env

# Lambda 日志
aws logs tail /aws/lambda/devops-agent-slack-chatbot --follow --region "${AWS_REGION}"

# DDB 里 thread 状态
aws dynamodb scan --region "${AWS_REGION}" --table-name devops-agent-slack-threads

# Slack 那边没收到？看 Slack App → Event Subscriptions → 看 "Request URL" 是不是 ✅ Verified
```

常见错：

- *401 invalid signature* → Signing Secret 不对，或 Lambda env var 没指对 secret
- *Lambda log 没动静* → Slack Event Subscriptions Request URL 没绑 / scope 没加 / App 没 reinstall
- *Bot 不响应* → bot 没 `/invite` 进频道
