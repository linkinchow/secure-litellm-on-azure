# LiteLLM 1.104.0升级验证

> 核对日期：2026-10-07。本文记录公共镜像、隔离数据库和本地协议验证，不代表客户Azure环境或生产发布已经验收。

> 现状补充（2026-10-09）：只读 Azure 管理核查确认 West US3 新建 dev/test 的 private AKS Running/Succeeded、Azure CNI overlay、Azure RBAC/禁用本地账户、WI/OIDC；System/User 池各 2 台 `Standard_D4s_v4`。双 API/Admin Front Door Premium、独立 PLS/私有入口与 WAF Prevention 已部署；Admin 否定 SocketAddr IPMatch 白名单同时含三个 IPv4 `/32` 和一个 IPv6 `/128` 精确出口。模型 WI、私网 Entra-only PG/Managed Redis、私有 ACR、分离的后端/证书 Vault 和 Firewall 出站构成当前基线。默认原生 LiteLLM 网关；用户已验证 Admin 密码 fallback 登录及 vkey Codex Responses 推理，**不是 Entra 用户 SSO 或生产就绪**。以下 2026-10-07 隔离测试结论不扩展为 Stage0–9 云端全验收。

> 后续（2026-10-28）：源镜像已改为同分支补丁版`1.104.8`，离线核对结果及剩余实测项见[1.104.8升级验证](litellm-1.104.8-upgrade-validation-2026-10-28.md)。下文仍是`1.104.0`的原始验证记录。

## 1. 选择结论

当前受审运行基线从LiteLLM `1.98.0`升级到稳定版`1.104.0`，固定镜像为：

```text
docker.litellm.ai/berriai/litellm@sha256:625981c83410a3ea68eb0697590a57ec1d764d634514d54fa5db0591077ee839
```

- PyPI稳定版：`1.104.0`，2026-10-03发布。
- Git标签：`v1.104.0`，commit `79645770fedc7ec2627e6468d31062f20f82aecc`。
- `docker.litellm.ai/berriai/litellm:v1.104.0`与`ghcr.io/berriai/litellm:v1.104.0`解析到同一digest。
- 使用LiteLLM在commit `0112e53046018d726492c814b3644b7d376029d0`固定的Cosign公钥验证GHCR digest成功；签名claims、公钥签名及透明日志记录均通过。

不使用`latest`、dev或RC标签。后续升级仍须重新固定digest并重跑本文全部门禁。

## 2. 漏洞门禁

Trivy `0.67.2`使用项目Stage3同等策略：`CRITICAL`、`--ignore-unfixed`、发现项退出1。

| 镜像 | 结果 |
| --- | --- |
| 旧`1.98.0`上游层 | `CVE-2026-102268`：PyJWT `2.13.0`，修复于`2.14.0`；`CVE-2026-63374`：AnyIO `4.13.0`，修复于`4.14.2` |
| 旧仓库派生层 | AnyIO覆盖已生效；仍有PyJWT `CVE-2026-102268`，因此Stage3正确阻断 |
| 新`1.104.0`上游及仓库派生层 | 可修复CRITICAL为0 |

新运行时实际包版本为LiteLLM `1.104.0`、AnyIO `4.14.2`、PyJWT `2.15.0`。仓库继续用hash固定AnyIO `4.14.2`作为防回退约束。

“可修复CRITICAL为0”不等于零漏洞；HIGH/MEDIUM及尚无修复的项目不在本门禁结论内。

## 3. GPT-6与Azure模型合同

`1.104.0`具备以下已验证能力：

- `gpt-6.1`和`azure/gpt-6.1`会进入GPT-6推理系列转换路径；
- 自定义Azure deployment名称配合`base_model=azure/gpt-6.1`会选择`AzureOpenAIGPT5Config`；
- 模型目录包含`azure/gpt-6-astra`、`azure/gpt-6-sol`、`azure/gpt-6-luna`；
- 同时包含Azure AI Foundry、区域Azure及带日期的GPT-6 Astra/Sol/Luna条目；
- Azure AI转换层对GPT-6推理与function tools使用新的Foundry Responses兼容规则。

Stage3的`runtime`检查已将非root UID、精确依赖版本、GPT-6.1识别及三项Azure目录条目变成机器门禁。真实可用性仍取决于客户Foundry账号中已成功部署的deployment、API版本、区域配额和实际Responses调用；Stage6必须使用真实deployment名称做端到端验证。

## 4. 数据库与运行时兼容

`1.104.0`包含189个Prisma迁移，目标schema和视图哈希已重新固定。隔离PostgreSQL/TLS测试已通过：

- greenfield空库执行全部迁移、八个必需视图及只读应用启动；
- native Admin登录、virtual key、JSON/SSE调用和Spend Logs写入/受限读取；
- Azure数据库Token并发刷新、失败回滚、旧事务排空及TLS参数保持；
- LiteLLM `1.95.0`合成库dump/restore后升级到`1.104.0`，预算、密文及旧源库保持不变。

上游`1.104.0`有意把以下两个大表索引迁移改为no-op，避免升级时锁住`LiteLLM_SpendLogs`：

```text
LiteLLM_SpendLogs_litellm_call_id_idx
LiteLLM_SpendLogs_api_key_startTime_idx
```

项目schema验证仅豁免“索引名、表名和列顺序完全匹配”的这两条operator-managed差异；任何其他DDL drift继续失败。两个索引须按数据库维护窗口和上游Spend Logs索引runbook在线创建，不由自动迁移偷偷补建。

## 5. 尚未覆盖

- 未在客户Azure OpenAI/Foundry deployment上实际调用GPT-6系列模型；
- 截至2026-10-07本文测试未覆盖目标AKS、WI、Managed Redis和Key Vault CSI云端验收；2026-10-09管理核查确认资源配置，但长期令牌刷新、CSI轮换、故障恢复与负向权限仍需运行证据；
- 未完成客户规模负载、索引在线创建耗时、HA/PITR及生产回退演练；
- 未批准生产发布或旧环境退役。

因此本次隔离升级证据可用于 Stage3 供应链与后续部署门禁，但不能替代 Stage4–9 的真实验收或生产批准。当前路径和执行要求见[部署指南](customer-deployment-workflows-zh.md)及[迁移指南](customer-migration-guide-zh.md)；运行时阶段编号不变。