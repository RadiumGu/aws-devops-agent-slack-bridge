# DevOps Agent 中国区桥接（CFN 通用模板，默认 ReadOnlyAccess）

让 **Global 分区** 的 AWS DevOps Agent（仅在 us-east-1/us-west-2 等可用，不支持 aws-cn）能**只读访问**中国区（cn-northwest-1 / cn-north-1）资源。如需写权限，部署时传 `ManagedPolicyArn=...PowerUserAccess` 或 `...AdministratorAccess` 显式 opt-in。

> 本模板从 workshop 项目 [`operating-china-region-using-devops-agent`](../../operating-china-region-using-devops-agent) 提炼而来，**去除 Workshop Studio 强依赖**（不再从 assets bucket 拉 cert），改成 SSM Parameter Store 输入；ALB SG 接 CloudFront origin-facing prefix list；IAM Role 默认 `ReadOnlyAccess`；MCP server 默认 `@latest`。

## ⚠️ 安全说明

当前是 **只读模式（默认）**：
- 中国 IAM Role 默认挂 `ReadOnlyAccess`（只能列出 / 描述资源，不能创建 / 修改 / 删除）
- MCP server 未设 `READ_OPERATIONS_ONLY`（这是 profile name `china-readonly` + IAM 只读 policy 双重限制）
- 任何拿到 *X-API-Key + CloudFront 域名* 的人只能读中国账号信息（包括资源拓扑、IAM 配置、实例信息等）

**如果你主动 opt-in 扩大权限**（`ManagedPolicyArn=...AdministratorAccess`）：
- 中国 IAM Role 能干账号内任何事（创建 / 删除 / 修改）
- 拿到 *X-API-Key + CloudFront 域名* 的人 = 中国账号完全控制权

无论哪种模式，请确保：
1. SecretsManager 里的 API Key **只发给必要的人**，泄漏后立即 update-stack 旋转
2. client.crt/key 在本地 + SSM SecureString 严妥保管，泄漏后立即删 Trust Anchor 重建
3. CloudWatch / CloudTrail 开启中国账号审计，跟踪 `devops-agent-cn-admin-role` 的所有调用

---

## 拓扑

```
DevOps Agent (Global, e.g. us-east-1)
   │  HTTPS + X-API-Key
   ▼
CloudFront ──► ALB(:80, Header 校验, SG=CloudFront prefix list) ──► EC2 (MCP Server :8000)
                                                                        │ credential_process
                                                                        ▼
                                                          aws_signing_helper ──► IAM Roles Anywhere
                                                          (X.509 client cert)    (cn-northwest-1)
                                                                        │ 1h 临时凭证 (ReadOnly default)
                                                                        ▼
                                                                  aws-cn API
```

---

## 文件

| 文件 | 部署位置 | 作用 |
|------|---------|------|
| `01-cn-roles-anywhere.yaml` | **中国账号 / cn-northwest-1** | Trust Anchor + Profile + Read-Only IAM Role（可 opt-in 到 Admin）|
| `02-global-mcp-bridge.yaml` | **Global 账号 / us-east-1** | VPC + ALB(prefix list) + CloudFront + EC2(MCP server) |

---

## 部署步骤

### Step 0：生成 CA + 客户端证书（本地一次性）

```bash
mkdir -p ~/devops-agent-cn-bridge-certs && cd ~/devops-agent-cn-bridge-certs

# CA（10 年）
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
  -out ca.crt -subj "/CN=devops-agent-cn-bridge-ca"

# Client cert（1 年；到期需重发）
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr \
  -subj "/CN=devops-agent-cn-bridge-client"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt -days 365 -sha256

chmod 600 ca.key client.key
```

### Step 1：在中国账号部署 Roles Anywhere

```bash
aws cloudformation deploy \
  --region cn-northwest-1 \
  --profile <your-china-profile> \
  --stack-name devops-agent-cn-bridge \
  --template-file 01-cn-roles-anywhere.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    CACertificateBody="$(cat ~/devops-agent-cn-bridge-certs/ca.crt)"
```

记下 outputs：
```bash
aws cloudformation describe-stacks \
  --region cn-northwest-1 --profile <your-china-profile> \
  --stack-name devops-agent-cn-bridge \
  --query 'Stacks[0].Outputs'
```

会拿到 `TrustAnchorArn` / `ProfileArn` / `RoleArn`。

> 想收紧权限？默认已是 `ReadOnlyAccess`，无需额外参数。需要写权限才传 `--parameter-overrides ManagedPolicyArn=arn:aws-cn:iam::aws:policy/PowerUserAccess`（或 AdministratorAccess / 你自己的 managed policy ARN）。

### Step 2：把 client cert/key 上传到 Global 账号 SSM Parameter Store

```bash
GLOBAL_REGION=us-east-1

aws ssm put-parameter \
  --region $GLOBAL_REGION \
  --name /devops-agent-cn-bridge/client-cert \
  --type SecureString \
  --value "$(cat ~/devops-agent-cn-bridge-certs/client.crt)" \
  --overwrite

aws ssm put-parameter \
  --region $GLOBAL_REGION \
  --name /devops-agent-cn-bridge/client-key \
  --type SecureString \
  --value "$(cat ~/devops-agent-cn-bridge-certs/client.key)" \
  --overwrite
```

### Step 3：在 Global 账号部署桥接

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name devops-agent-cn-bridge \
  --template-file 02-global-mcp-bridge.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    TrustAnchorArn="arn:aws-cn:rolesanywhere:cn-northwest-1:..." \
    ProfileArn="arn:aws-cn:rolesanywhere:cn-northwest-1:..." \
    CnRoleArn="arn:aws-cn:iam::...:role/devops-agent-cn-admin-role" \
    CnRegion=cn-northwest-1
```

部署完成后取出端点和 API Key：

```bash
aws cloudformation describe-stacks \
  --region us-east-1 --stack-name devops-agent-cn-bridge \
  --query 'Stacks[0].Outputs'

# API Key 明文
aws secretsmanager get-secret-value \
  --region us-east-1 \
  --secret-id devops-agent-cn-bridge/alb-api-key \
  --query SecretString --output text
```

### Step 4：注册到 DevOps Agent

```bash
ENDPOINT="<McpEndpointHttps from outputs>"
API_KEY="<from secretsmanager>"

aws bedrock-agent register-service \
  --region us-east-1 \
  --service-name devops-agent-cn-bridge \
  --service-type mcpserver \
  --service-config "{\"endpoint\":\"$ENDPOINT\",\"apiKey\":\"$API_KEY\"}"

# 然后到 Bedrock Agent Space 把 call_aws / suggest_aws_commands 关联到你的 Agent
```

### 验证

```bash
ENDPOINT="<McpEndpointHttps>"
API_KEY="<from secretsmanager>"

# MCP initialize
curl -sS "$ENDPOINT" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

---

## 设计说明

### ALB SG ingress = CloudFront origin-facing prefix list

`02-global-mcp-bridge.yaml` 在 stack 内置一个小 Lambda custom resource，部署时调用 `ec2:DescribeManagedPrefixLists` 查找当前 region 的 `com.amazonaws.global.cloudfront.origin-facing` 这条 AWS managed prefix list（每个 region 的 `pl-xxx` ID 不同，且没有公共 SSM 参数路径），把 ID 注入到 ALB SG 的 ingress rule。

这样：
- 只有 CloudFront 的回源 IP 段能直连 ALB :80
- 即使有人扫到 ALB DNS，也访问不通（403 / connection refused）
- AWS 维护这个 prefix list，IP 范围变更不需要你管

需要再加管理员/测试 IP 直连？传 `AllowedAdminCidrs=1.2.3.4/32,5.6.7.8/32`（最多 2 个）。

### MCP server 用 `@latest`

`McpServerVersion=latest`（默认）。EC2 重启或重建时拉最新版。
风险：上游 `awslabs.aws-api-mcp-server` 出 breaking change 会让桥服务挂掉。
出问题时把参数改成具体版本（例如 `0.2.5`）做 update-stack 即可回退。

### API Key 旋转的局限

ALB ListenerRule 用 `{{resolve:secretsmanager:...}}` 引用 API Key，是 **CFN 模板期** 解析的静态值。SecretsManager 自动旋转后 ALB 规则不会跟着变，**必须 `update-stack` 才生效**。
解决思路（未实现）：
- 用 Lambda@Edge / CloudFront Function 在边缘做 header 校验，从 SecretsManager 读最新值
- 或换成 ALB + Cognito / OIDC 真正的 auth 层

### EC2 是单点

单 AZ、单 instance、public subnet。Demo 够用，生产建议：
- ASG 起 2 台跨 AZ
- TargetGroup 多 target
- 移到 private subnet + NAT GW

---

## 与原 workshop 的差异

| 项 | Workshop 原版 | 本模板 |
|----|--------------|--------|
| cert/params 来源 | S3 assets bucket | SSM Parameter Store SecureString |
| MCP server 版本 | `awslabs...@latest` | 参数化（默认 `latest`，可钉版） |
| `AWS_API_MCP_ALLOWED_HOSTS` | `*` | ALB DNS（防 Host header 攻击） |
| `READ_OPERATIONS_ONLY` | `true` | 不设置（依赖 IAM `ReadOnlyAccess` 限制） |
| ALB SG ingress | DevOps Agent IP/32 + `0.0.0.0/0` | CloudFront origin-facing prefix list + 可选 admin CIDR |
| China Role policy | `ReadOnlyAccess` | `ReadOnlyAccess`（默认；可参数覆盖为 PowerUser / Admin） |
| CloudFront | 必选 | 可选（`EnableCloudFront`） |
| Roles Anywhere session | 固定 3600s | 参数化 |
| 中国 sample resources | 模板内含 | 不包含 |

---

## 清理

```bash
# Global
aws cloudformation delete-stack --region us-east-1 --stack-name devops-agent-cn-bridge

# China
aws cloudformation delete-stack --region cn-northwest-1 --profile <china> \
  --stack-name devops-agent-cn-bridge

# SSM
aws ssm delete-parameter --region us-east-1 --name /devops-agent-cn-bridge/client-cert
aws ssm delete-parameter --region us-east-1 --name /devops-agent-cn-bridge/client-key
```
