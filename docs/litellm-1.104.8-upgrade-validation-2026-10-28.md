# LiteLLM 1.104.8升级验证

> 核对日期：2026-10-28。本文只记录离线只读核对（公共镜像清单、签名、镜像层内文件哈希与上游发布说明）及仓库本地门禁，**未在Runner上构建/扫描/签名派生镜像，也未在任何Azure环境部署或调用**。前一版本的隔离数据库与协议验证见[1.104.0升级验证](litellm-1.104.0-upgrade-validation-2026-10-07.md)。

## 1. 选择结论

受审运行基线从LiteLLM `1.104.0`升级到同一稳定分支的补丁版`1.104.8`，固定镜像为：

```text
docker.litellm.ai/berriai/litellm@sha256:6cc08e60be3e4ebf0cfb41ef23aed97e9ba4f61702d1e95e533f47ee6d67865f
```

- PyPI稳定版：`1.104.8`，2026-10-27发布；同日上游同时发布`1.106.5`、`1.105.6`、`1.103.10`。
- Git标签：`v1.104.8`，commit `b78a6341b7270d2a659725f0b48f83af4698cd47`（`stable/1.104.x`）。
- 匿名registry API只读解析：`docker.litellm.ai/berriai/litellm:v1.104.8`与`ghcr.io/berriai/litellm:v1.104.8`返回同一OCI index digest；下载的index内容SHA256与该digest一致。index含`linux/amd64`（`sha256:eb45c77fa55f5df57e9f5b175f82340376e03a8070861fcd2870d4ae019d2e78`）和`linux/arm64`两个平台及各自attestation。

不使用`latest`、dev或RC标签。

## 2. 上游Cosign签名密钥轮换

上游自`1.104.6`起改用commit `e82a3c646908ba0a6ee0beddc4310d7a6cac4065`中的`cosign.pub`签名镜像，原commit `0112e53046018d726492c814b3644b7d376029d0`的公钥不再能验证新镜像。

| 公钥来源commit | 文件SHA256 |
| --- | --- |
| `0112e53`（旧，1.104.0验证所用） | `ff8869bf14ba9d10af7b64b9d479543b44daec0165e715753c43ff8a998f6dd3` |
| `e82a3c6`（新） | `e3fe3e08c44b16407fe0b37c43b4f8b603b3a0f4ee74c29e647bc022d762c2c0` |

仓库代码不固定上游公钥：promote workflow和本地执行器只验证本仓库自己的签名（GitHub OIDC keyless或客户自管密钥），因此本次无需修改代码中的公钥。上游签名只作为源镜像入选证据。

本次离线核对（未安装cosign，使用OpenSSL）：

- `ghcr.io/berriai/litellm:sha256-6cc08e60….sig`的simple-signing payload绑定`docker-reference: ghcr.io/berriai/litellm`及上述index digest；
- 该payload的ECDSA签名用`e82a3c6`公钥验证通过，用`0112e53`公钥验证失败，与上游说明一致；
- 签名附带的Rekor bundle记录同一新公钥，`integratedTime`为2026-10-27 19:41:58 UTC，logIndex `3524171098`。

**未完成：** Rekor SET与透明日志包含证明未做离线校验。Runner上应执行：

```bash
cosign verify \
  --key https://raw.githubusercontent.com/BerriAI/litellm/e82a3c646908ba0a6ee0beddc4310d7a6cac4065/cosign.pub \
  ghcr.io/berriai/litellm@sha256:6cc08e60be3e4ebf0cfb41ef23aed97e9ba4f61702d1e95e533f47ee6d67865f
```

## 3. 镜像内容核对

从GHCR只读下载`linux/amd64`各层并逐层校验digest，在不解包到磁盘的情况下读取以下文件，与`1.104.0`镜像同路径对比：

| 项目 | `1.104.0` | `1.104.8` |
| --- | --- | --- |
| LiteLLM / proxy-extras | `1.104.0` / `0.4.102.post1` | `1.104.8` / `0.4.102.post1` |
| AnyIO / PyJWT / Prisma | `4.14.2` / `2.15.0` / `0.11.0` | 相同 |
| certifi | `2026.4.22` | `2026.10.19` |
| Prisma迁移 | 189个 | 189个，名称与逐文件SHA256完全相同 |
| `schema.prisma`、`create_views.py` | 与仓库固定哈希一致 | 相同，无需改`SCHEMA_SHA256`/`VIEWS_SHA256` |
| `prisma_client.py`、`db_url_settings.py` | 与仓库固定哈希一致 | 相同，Azure数据库适配器哈希不变 |
| `proxy/utils.py`、`proxy/proxy_server.py` | 与仓库固定哈希一致 | **已变化**，见下文 |
| Azure GPT-6 Astra/Sol/Luna目录项 | 存在 | 存在 |

`proxy/utils.py`的变化集中在失败请求计费与被拒请求的失败日志字段；本仓库替换的`PrismaClient.check_view_exists`所在类在两版间逐行相同。`proxy_server.py`的变化为token计数路由和流式chunk模型名复制；应用依赖的`app.router.lifespan_context`及`prisma_client`全局未改。因此`LiteLLM/runtime/application.py`中的`UTILS_SHA256`/`SERVER_SHA256`已按新镜像重新固定。

上游镜像已自带certifi `2026.10.19`，其`cacert.pem`与仓库按hash固定的同版本wheel完全相同；派生层的certifi覆盖保留为防回退约束，与AnyIO相同。

`scripts/runtime_configuration.py`允许的运行时字段在新版中仍存在：`GeneralSettings`未变化，`Router.__init__`签名未变化，路由策略集合相同。

## 4. 1.104.1–1.104.8变更影响

依据上游各补丁发布说明及PR描述：

- **无新数据库迁移**（1.104.6、1.104.8说明均明确）；
- **1.104.6**：签名密钥轮换；证书包移除eMudhra根CA；失败、超时、取消请求按输入估算计费，`openai/responses/*`大请求不再重复计费；gpt-5.4+工具调用回到`choices[0]`；
- **1.104.7**：镜像基础层更新；远程成本表不可达时偶发`ImportError`的修复（本仓库设`LITELLM_LOCAL_MODEL_COST_MAP=True`，不依赖远程成本表）；
- **1.104.8**：被拒请求（无效key、限流、预算、Guardrail）的失败日志只保留`model`、`messages`、`input`、`prompt`、`user`、`stream`、`litellm_call_id`及`tags`/Guardrail信息，丢弃调用方自带的`trace_id`、`session_id`等metadata；GCS logger只使用代理侧配置的bucket；
- 1.104.2–1.104.5新增Decisions/System One路由、Bedrock token计数等，不在本仓库native ingress放行的路径内。

结论：`scripts/runtime_configuration.py`与后端清单无须修改。需要在真实环境确认的行为变化：

- 被拒请求的Spend Logs失败行不再包含调用方自定义metadata；按原生Spend Logs审计时，被拒请求的归因以认证key/team为准；
- 超时或断开的已派发请求现在会按输入价格计费，并可能为此前超时的重试各写一行Spend Log，预算消耗和审计行数可能高于`1.104.0`；
- Responses/Chat的reasoning effort门禁统一为按模型表开/关（`xhigh`仍需模型表显式支持，`max`不在网关拦截）。

## 5. 仓库门禁

- 源镜像digest已同步到Dockerfile、静态Base、`validate_manifests`、`validate_stage6`、promote默认值、CI Prisma合同测试及隔离schema测试；
- Stage3 `runtime`合同改为LiteLLM `1.104.8`、AnyIO `4.14.2`、PyJWT `2.15.0`；`gpt-6.1`系列识别、`AzureOpenAIGPT5Config`选择及三项Azure目录条目要求不变（`is_model_gpt_6_plus_model`在新版仍存在）；
- CI仍以同一测试运行`1.95.0`合成库dump/restore后升级到新版，因189个迁移及两条operator-managed SpendLogs索引豁免（`LiteLLM_SpendLogs_litellm_call_id_idx`、`LiteLLM_SpendLogs_api_key_startTime_idx`）不变，`SUPPORTED_SOURCE_HISTORIES`不需调整；
- `CVE-2026-102268`（PyJWT）和AnyIO修复版本与`1.104.0`相同，继续由精确版本合同防回退。

## 6. 尚未覆盖

- Runner上构建派生镜像、Trivy CRITICAL门禁、SBOM、推送私有ACR及本仓库签名；
- Runner上`cosign verify`上游签名（含Rekor证明）；
- CI中的隔离PostgreSQL/TLS、Prisma合同及1.95.0→1.104.8合成升级重跑；
- Stage6在目标AKS发布新digest、`schema-migrate`只读核对（应无待执行迁移）及应用启动哈希门禁；
- 真实Azure Responses调用（JSON/SSE/WSS、Codex多轮工具、reasoning effort）、Admin登录及Spend Logs失败行抽查；
- 客户规模负载、HA/PITR与生产回退演练。

因此本文只能作为Stage3入选及离线兼容证据，不能替代Stage3–9实际执行或生产批准。
