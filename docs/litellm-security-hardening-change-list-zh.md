# LiteLLM 网关安全增强改造项审查清单

> 文档状态：历史风险工作包与生产门禁清单；不是当前资源验收报告
> 原始编制日期：2026-08-31；现状对齐：2026-10-09
> 输入方案：`docs/litellm-azure-security-hardening-zh.md`
> 历史实现：`LiteLLM/deploy_mi_aks_litellm.py`、`LiteLLM/litellm.config.yaml`；当前托管路径见部署/迁移指南
> 目标：将现有 PoC 形态改造成可安全运行、可审计、可扩展、可恢复的企业 LiteLLM 网关
> 明确边界：本方案不引入 Azure API Management（APIM）

## 1. 文档目的

本文不是详细设计或执行 Runbook，而是进入实施前的完整改造项清单，用于确认：

1. 新架构需要改哪些 Azure 资源、Kubernetes 工作负载、LiteLLM 配置和仓库代码；
2. 哪些改造可以在现有集群原地完成，哪些建议通过新生产环境迁移完成；
3. 各改造项的依赖、优先级、验收标准和回滚要求；
4. 原安全方案中的风险 R01-R15 是否全部有对应治理措施；
5. 哪些架构决策仍需客户安全、网络、身份、平台和数据团队确认。

本文中的 `P0` 表示生产上线前必须完成，`P1` 表示首个生产阶段完成，`P2` 表示持续增强。

## 2. 当前实现基线

2026-10-09只读 Azure 管理面核查：West US3 新建 dev/test，Private AKS Running/Succeeded，Azure CNI overlay、Azure RBAC、禁用本地账户、WI/OIDC；System/User 池各 2 台 `Standard_D4s_v4`。API/Admin Front Door Premium、PLS/内部 LB/私有 Traefik 分开，两侧 WAF Prevention，Admin 以否定 SocketAddr IPMatch 限制批准的三个 IPv4 `/32`、一个 IPv6 `/128` 出口。模型使用 WI，PG/Managed Redis 私网 Entra-only，后端 Vault 与证书 Vault 分离，ACR 私有，Firewall 路由出站。

默认原生 LiteLLM `1.104.0` 网关不经过 Entra 代理；用户已验证 Admin 密码 fallback 登录及 vkey Codex Responses 推理，不是 Entra 用户 SSO 或生产就绪。当前无 APIM、App Service 或共享认证代理。Entra 企业准入、协议负向测试、HA/恢复/容量及企业治理仍须独立验收。实际执行和证据见[部署指南](customer-deployment-workflows-zh.md)及[迁移指南](customer-migration-guide-zh.md)；SEC/D 编号保留，不代表完成勾选。

以下表格是 2026-08-31 的旧 PoC 与生产目标对照，不是当前 West US3 资源清单。

| 范围 | 历史实现 | 生产目标变化（仍须验收） |
| --- | --- | --- |
| Azure 部署 | 单个 Python 脚本直接创建/修改 Azure 与 Kubernetes 资源 | Azure 基础设施转为可审查、可重复的 IaC；应用部署与平台部署解耦 |
| AKS | 默认单节点，节点级 UAMI，公网控制面/网络安全能力未完整声明 | Private AKS、Entra/Azure RBAC、Workload Identity、分离节点池、跨可用区 |
| 入口 | 公网 ingress-nginx LoadBalancer；管理面和推理面共用 Host | WAF 前置、私有源站、管理面与推理数据面分离、禁止源站绕过 |
| LiteLLM | 单副本、无 Kubernetes probes、无 PDB/HPA/topology spread、无资源限制 | 至少双副本、健康探针、资源治理、优雅终止、弹性与跨节点分布 |
| 身份 | 上游使用节点级 Managed Identity；客户端主要使用 Master/Virtual Key | Pod 级 Workload Identity；人员与应用使用 Entra；Virtual Key 降为授权与计量载体 |
| Secret | Master Key、PG 密码、`DATABASE_URL` 写入 Kubernetes Secret 并通过 `env_from` 注入 | Key Vault 管理、固定 Salt、最小化 Secret 暴露、轮换和审计 |
| PostgreSQL | AKS 内单副本 PostgreSQL + PVC | Flexible Server HA、TLS、私网、备份/PITR、容量告警和迁移演练 |
| Redis | 未部署共享 Redis | Azure Managed Redis 保存多副本共享限流、冷却、亲和与认证缓存状态 |
| 网络 | 未定义 NetworkPolicy；模型端点未全面私网化；出站未收敛 | Private Link、Private DNS、Default Deny、受控 egress、Firewall 策略 |
| 模型配置 | 文件配置与 UI 数据库配置可并存 | 明确单一事实源、变更审批、漂移检测和回滚 |
| Guardrail | 已验证本地英文关键词过滤；Presidio 未部署依赖服务 | Azure AI Content Safety/Prompt Shields、PII/Secret 检测、多语言策略与失败模式 |
| 日志 | Spend Logs 可写 PG；缺少完整日志分层与集中安全监控 | 元数据默认、正文默认关闭、统一 Trace ID、Monitor/Log Analytics/Sentinel 闭环 |
| 供应链 | 镜像 Tag + `Always` 拉取，未声明签名/SBOM/准入 | ACR、digest、扫描、SBOM、签名、Admission/Policy 门禁 |

## 3. 总体实施策略

本轮改造不建议继续把所有能力堆入一个部署脚本。建议拆成三个交付层：

```text
Azure 平台层（IaC）
  VNet / Private DNS / Firewall / Private Endpoints / AKS / ACR
  Key Vault / PostgreSQL / Redis / Monitor / Defender / Front Door 或 App Gateway

Kubernetes 平台与应用层（Helm/Kustomize 或受控 manifests）
  Namespace / ServiceAccount / Workload Identity / CSI / NetworkPolicy
  LiteLLM Deployment / Service / Ingress / PDB / HPA / ConfigMap

LiteLLM 逻辑配置层
  Models / Router / OSS Guardrails / SSO / Team / Budget / Spend retention

入口与身份层
  默认：双平面私有 Traefik -> 原生 LiteLLM（vkey / 原生 Admin 登录）
  延期企业增强：独立 Entra 准入 / App Roles / Header反伪造
```

由于目标变化同时涉及 Private Cluster、网络插件/策略、节点身份、数据库和入口，建议优先采用“新建安全生产环境 -> 数据迁移 -> 灰度切流”，避免在现有 PoC 集群上一次性原地改造。现有集群可保留为回归和迁移源。

## 4. 改造工作包总览

| ID | 工作包 | 优先级 | 主要依赖 | 建议方式 |
| --- | --- | --- | --- | --- |
| SEC-01 | 架构决策与现场盘点 | P0 | 无 | 先完成 |
| SEC-02 | IaC 与环境分层 | P0 | SEC-01 | 新建 |
| SEC-03 | 边缘 WAF 与私有源站 | P0 | 网络与入口决策 | 新建后切流 |
| SEC-04 | 管理面与推理面分离 | P0 | SEC-03、SEC-05 | 新建路由/域名 |
| SEC-05 | Entra 用户和应用身份 | P0 | 身份决策、LiteLLM 能力核验 | 分阶段 |
| SEC-06 | Workload Identity | P0 | AKS OIDC、UAMI | 新环境优先 |
| SEC-07 | Key Vault 与 Secret 生命周期 | P0 | SEC-06 | 新建并迁移 |
| SEC-08 | PostgreSQL Flexible Server | P0 | 网络、身份、RTO/RPO | 新建并迁移 |
| SEC-09 | Azure Managed Redis | P0/P1 | 多副本、私网 | 新建 |
| SEC-10 | AKS 与 Pod 安全基线 | P0/P1 | SEC-02、SEC-06 | 新环境优先 |
| SEC-11 | NetworkPolicy 与出站治理 | P1 | 私网/DNS/Firewall | 分阶段收敛 |
| SEC-12 | Azure OpenAI/Foundry 私网化 | P0/P1 | Private DNS、RBAC | 分资源迁移 |
| SEC-13 | LiteLLM 高可用与容量治理 | P0 | PG、Redis | 应用改造 |
| SEC-14 | LiteLLM 配置与密钥治理 | P0 | Key Vault、固定 Salt | 应用改造 |
| SEC-15 | AI Guardrail 与数据防泄漏 | P1 | 产品/语言/失败模式决策 | 试点后强制 |
| SEC-16 | 日志、指标和隐私分层 | P0/P1 | Monitor、数据分类 | 分阶段 |
| SEC-17 | Defender、Policy 与供应链 | P1 | ACR、AKS、CI/CD | 平台与流水线 |
| SEC-18 | Sentinel 检测与响应 | P1/P2 | SEC-16、Defender | SOC 联调 |
| SEC-19 | 备份、恢复与业务连续性 | P0/P1 | PG、配置、Key Vault | 演练后上线 |
| SEC-20 | 测试、迁移、灰度和回退 | P0 | 全部核心工作包 | 上线门禁 |
| SEC-21 | 员工与 Agent 上下文审计 | P1 | SEC-05、SEC-15、SEC-16 | 治理批准后试点 |

## 5. 具体改造项

### SEC-01 架构决策与现场盘点

**目标**：在创建资源前固定边界，避免实施中反复改变网络、身份和数据方案。

**改造项**：

- 盘点现有订阅、区域、配额、Policy、VNet、DNS、ExpressRoute/VPN、Azure OpenAI/Foundry 资源和数据驻留要求；
- 确认公网模式（Front Door Premium）或纯内网模式（Application Gateway WAF v2），不同时建设两套主入口；
- 明确 API 数据面和 Admin 管理面的域名、访问人群和网络来源；
- 明确原生 vkey 与延期企业 Entra 双凭据模式的边界；企业准入不复制模型 ACL，客户端 vkey 仍由 LiteLLM 执行模型、Team 和预算权限；
- 核验LiteLLM OSS能力边界，并将所有Enterprise付费能力排除或替换为Azure原生、客户自有组件或OSS方案；
- 明确 Prompt/Response 是否允许落盘、日志留存和数据出境限制；
- 明确 RTO、RPO、可用区、跨区域恢复和维护窗口；
- 形成数据流图、信任边界图、端口/FQDN 清单和责任矩阵。

**验收**：关键决策项均有书面 Owner 和结论；未决事项不能进入对应资源实施。

### SEC-02 IaC 与环境分层

**目标**：将 Azure 资源部署从单个 Python 脚本中拆出，形成可审查和可重复的部署。

**改造项**：

- 选择 Bicep 或 Terraform 作为 Azure 资源唯一 IaC；
- 按 `dev/test/prod` 分离参数、状态、订阅或资源组；
- IaC 覆盖网络、AKS、UAMI、Key Vault、ACR、PostgreSQL、Redis、入口、Private Endpoint、Private DNS、Monitor、Defender 和 Policy；
- 应用部署改用 Helm/Kustomize 或模块化 Kubernetes manifests；
- 将当前 Python 脚本收敛为开发/迁移工具，或仅保留模型同步与验证能力；
- 明确仓库治理边界：生产 Azure 资源只能由批准的 IaC创建或修改，Python 脚本不得创建/修改 APIM，也不得作为生产平台资源的长期事实源；
- 所有生产变更经 Pull Request、计划预览、双人审批和审计；
- 引入 drift detection，禁止 Portal 临时变更长期漂移。

**仓库预期交付物**：

```text
infra/
  modules/
  environments/dev/
  environments/prod/
deploy/
  base/
  overlays/dev/
  overlays/prod/
scripts/
  migration/
  validation/
```

**验收**：空环境可通过批准流水线重复部署；IaC plan 无未解释漂移；仓库不包含 Secret；生产执行记录证明未创建或修改任何 APIM 资源。

### SEC-03 边缘 WAF 与私有源站

**目标**：公网流量只能经过 WAF，不能直达 AKS ingress。

**互联网模式改造项**：

- 部署 Azure Front Door Premium；
- 配置 WAF Managed Rules、Bot Protection、自定义限速、请求大小和方法限制；
- 托管路径使用分离的 API/Admin Traefik 与 Internal Load Balancer；旧 ingress-nginx 仅作迁移源；
- 创建 Private Link Service，并将 Front Door origin 通过 Private Link 接入；
- 删除或封禁现有 ingress 公网 IP，验证源站不可绕过；
- 单独配置 WebSocket、SSE、长请求和文件上传路由参数；
- API WAF先Detection观测，再逐规则切换Prevention；Admin WAF固定Prevention并按批准公网出口CIDR阻断未批准来源；
- Front Door、WAF 和源站访问日志发送到 Log Analytics。

**内网模式替代项**：

- 使用内部 Application Gateway WAF v2；
- 通过企业网络、Private DNS、ExpressRoute/VPN 访问；
- 不部署公网 Front Door，也不保留公网 AKS ingress。

**生产切流门槛**：任何生产部署不得为 LiteLLM Service 或 ingress controller 创建/保留公网 `LoadBalancer`、NodePort 或其他绕过 WAF 的入口。

**验收**：直接访问源站失败；批准域名成功；旧公网 IP 已移除；资源清单中不存在 LiteLLM/ingress 公网入口；WebSocket 101、SSE、长任务、大请求和证书轮换回归通过。

### SEC-04 管理面与推理面分离

**目标**：普通推理调用无法访问 UI 和管理 API。

**改造项**：

- 定义独立 API/Admin 域名；互联网模式分别绑定 Front Door、WAF、PLS 和内部 LB；
- 数据面仅开放六条固定 Chat/Responses/Embeddings 路径（带 `/v1` 与不带 `/v1`），不开放 `/v1/models` 或通配 `/v1/*`；扩展协议须另行审批、配置与验收；
- 管理面承载 `/ui`、SSO callback、用户/Key/Team/模型/配置管理 API；
- 公网数据面入口对管理路径明确返回 403；
- 当前 Admin 边缘允许批准公网出口，私有回源后执行原生登录；生产企业增强另验批准设备和 Entra 管理员组；
- Master Key 不分发给普通调用者，仅保留受控自动化和 Break Glass；
- 为两个入口设置不同 WAF、速率、日志和告警策略；
- 验证路径匹配不存在 URL 编码、大小写、尾斜杠或备用路由绕过。

**验收**：API 数据面无法访问管理功能；Admin IPv4/IPv6 未批准来源被 WAF 拒绝，批准来源仍须内层登录。生产企业增强另须证明 Entra MFA/Conditional Access 和 Break Glass 告警。

### SEC-05 Entra 用户和应用身份

**目标**：每次访问可以归因到唯一人员或工作负载。

**改造项**：

- Microsoft SSO 是延期企业准入门禁，当前原生 Admin 密码登录不构成 SSO 验证；
- 为 Admin UI 建立独立 Entra App Registration/Enterprise Application；
- 配置精确 Redirect URI、固定 Proxy Base URL 和客户端凭据轮换；
- 建立 `proxy_admin`、`proxy_admin_viewer`、`internal_user` 等 App Roles；
- 通过受控安全组分配角色，管理员权限结合 PIM；
- 配置 Conditional Access、MFA、合规设备、登录风险和会话策略；
- 可选企业模式使用客户自有独立认证代理，不启用LiteLLM原生JWT；不得将其写成当前原生默认路径；
- JWT 必须校验 issuer、audience、tenant、signature、expiry、roles/scopes；
- 后台应用使用 Client Credentials、Managed Identity 或 Workload Identity，不共享用户 Virtual Key；
- 设计 Entra identity 到 LiteLLM Team、模型 ACL、预算和 Virtual Key 的映射；
- 定义离职、应用停用、组变更后的访问回收 SLA；
- 当前原生 Admin 通过受限来源的密码 fallback 登录；采用企业模式时再将其约束为受控 Break Glass，并验证告警。

**需先验证**：独立认证代理能否在Responses、WebSocket、Chat、SSE、Embeddings、Files和MCP/Tools等全部目标路由统一执行JWT和授权检查，并正确删除伪造Header、保护后端凭据和保持流式传输。

**验收**：禁用 Entra 用户/应用后访问失效；跨 Team/模型访问被拒绝；Token 中角色变化重新登录后生效。

### SEC-06 AKS Workload Identity

**目标**：只有 LiteLLM Pod 能取得 Azure OpenAI/Foundry 数据平面 Token。

**改造项**：

- AKS 启用 OIDC issuer 和 Workload Identity；
- 为 LiteLLM 创建专用 UAMI 和 Kubernetes ServiceAccount；
- 创建 Federated Identity Credential，绑定 namespace、ServiceAccount 和 issuer；
- Pod 添加 `azure.workload.identity/use: "true"` 标签；
- ServiceAccount 添加 UAMI Client ID 注解；
- UAMI 仅在目标 Azure OpenAI/Foundry 资源获得数据平面最小角色；
- 运维、备份、监控和迁移 Job 使用不同身份；
- 验证 Workload Identity 后，移除 VMSS 上的业务 UAMI；
- 限制业务 Pod 访问 IMDS，防止继续使用节点身份；
- 为角色分配、Federated Credential 和 Token 获取失败建立审计与告警。

**迁移顺序**：先双轨验证 Pod 身份，再移除节点身份，不能反向执行。

**生产切流门槛**：VMSS 上不得继续附加 LiteLLM 业务 UAMI；LiteLLM 必须绑定专用 ServiceAccount 和 Federated Identity Credential。

**验收**：LiteLLM Pod 可调用模型；同 namespace 未授权 Pod和其他 namespace Pod 无法取得该身份 Token；VMSS identity 清单中不存在 LiteLLM 业务 UAMI；节点身份不能调用目标模型资源。

### SEC-07 Key Vault 与 Secret 生命周期

**目标**：Secret 有集中保管、最小读取、稳定加密和可验证轮换。

**改造项**：

- 部署 Key Vault Premium，启用 RBAC、Soft Delete、Purge Protection、Private Endpoint 和诊断日志；
- 管理 `LITELLM_MASTER_KEY`、永久 `LITELLM_SALT_KEY`、数据库凭据、SSO Secret、第三方 Guardrail Secret 和证书；
- `LITELLM_SALT_KEY` 必须稳定且与 Master Key轮换解耦，防止 UI 模型/SSO 等数据库密文失效；
- 评估 LiteLLM Key Vault Secret Manager、CSI 文件挂载或同步到 Kubernetes Secret 的兼容方式；
- 若应用只能读取环境变量，应明确 CSI 同步 Secret仍会落入 Kubernetes Secret，并配置 etcd 加密与严格 RBAC作为补偿控制；
- 禁止 `env_from` 注入整份 Secret，改为逐项引用或受控文件读取；
- Secret 不写日志、不进入 Pod describe 输出、不进入 Git/流水线变量明文；
- 建立轮换流程、双 Key/灰度窗口、Pod 安全滚动和旧凭据失效验证；
- 为 Secret 读取异常、批量导出、Purge/权限变更建立告警。

**强制 Salt 迁移步骤**：

1. 盘点并备份使用旧 Master Key/Salt 加密的 UI 模型、SSO、Guardrail、Credential 等数据库对象；
2. 使用现有有效密钥执行脱敏的可读性基线测试，不输出解密内容；
3. 选择保留现有加密 key 作为永久 Salt，或实现并验证旧 key -> 新 Salt 的解密/重加密工具；
4. 在隔离数据库副本完成重加密、重启和对象可读性回归；
5. 只有全部密文通过验证后，才允许生产设置或轮换 `LITELLM_SALT_KEY`；
6. Salt轮换 Runbook 必须包含备份、双人审批、回退密钥和恢复验证。

不能直接设置新 Salt 后期望旧密文自动可读。

**必需交付物**：Salt/密文迁移 Runbook、脱敏可读性测试脚本、隔离环境迁移报告和回退验证记录。

**验收**：轮换后业务不中断且旧凭据失效；非 LiteLLM 身份无法读取 Secret；数据库配置重启后仍可解密。

### SEC-08 PostgreSQL Flexible Server

**目标**：消除 AKS 内单副本 PG 的容量、恢复和单点风险。

**改造项**：

- 部署 Azure Database for PostgreSQL Flexible Server；
- 按 RTO/RPO 选择 Zone-Redundant HA、计算层和存储容量；
- 使用 Private Endpoint/Private Access、Private DNS，禁用公网访问；
- 强制 TLS，目标为 `sslmode=verify-full`；
- 优先评估 Entra authentication；无法使用时，密码进入 Key Vault；
- 配置自动备份、PITR、可选 Geo Backup、维护窗口和版本策略；
- 配置连接池、连接数、statement/lock timeout 和慢查询监控；
- 设置存储增长、连接、CPU、IOPS、延迟、锁、失败率和剩余容量告警；
- 设置 Spend Logs retention、Prompt/Response 默认不落库；
- 评估 Spend Logs 分区、归档或输出到专用分析存储；
- 通过 `pg_dump/pg_restore` 或批准迁移工具迁移现有 Virtual Key、Team、预算、SSO、模型、Guardrail 和 Spend Logs；
- 执行行数、关键对象、预算、密文解密和登录验证；
- 保留旧 PG 只读回退窗口，确认后再退役 PVC。

**本次事故纳入设计**：必须对 PG 容量 >70%/>85%、checkpoint/WAL 写入失败、recovery loop 和 LiteLLM DB 401/503 告警。

**迁移兼容性门槛**：在建库前先用目标 LiteLLM/Prisma 版本验证 Flexible Server 的 TLS CA、`sslmode=verify-full`、连接池、schema migration、密码或 Entra token刷新方式。若 Entra authentication 不兼容，不得临时降级为散落密码，应使用 Key Vault托管密码并记录例外期限。

**迁移演练清单**：执行 `pg_dump/pg_restore` dry run；核对 schema、表行数和关键对象；验证 Virtual Key、SSO、模型、Guardrail、预算及数据库密文可读；验证连接切换和旧库只读回退。

**验收**：主库故障切换、PITR 到隔离环境、凭据轮换和 LiteLLM 重连均通过演练。

### SEC-09 Azure Managed Redis

**目标**：为 LiteLLM 多副本提供共享的短期状态和限流一致性。

**改造项**：

- 部署 Azure Managed Redis，启用 Private Endpoint、Private DNS 和 TLS；
- 优先使用 Entra authentication；否则凭据进入 Key Vault；
- LiteLLM Router 使用 Redis 共享 RPM/TPM、冷却、失败计数和亲和状态；
- 启用适用的 Virtual Key auth cache，降低 PG 认证热点；
- 验证 Responses API `previous_response_id`、session/deployment affinity 在跨 Pod 后仍一致；
- 明确 TTL、容量、淘汰策略、持久化和维护窗口；
- Redis 不保存 Prompt/Response 正文、完整 Key 或 PII；
- 建立连接、内存、eviction、命中率、延迟和故障告警；
- 验证 Redis 不可用时的 fail-open/fail-closed 行为及预算一致性。

**兼容性门槛**：在生产使用前验证目标 LiteLLM Redis客户端支持 Azure Managed Redis 的 TLS、证书、端口、Entra token刷新或 Key Vault凭据，以及 planned failover/maintenance 行为。未经验证不得依赖 Redis 承担硬预算或跨 Pod认证一致性的唯一状态。

**验收**：双 Pod随机分流下限流、缓存和亲和一致；Redis 重启不会导致跨租户或预算绕过。

### SEC-10 AKS 与 Pod 安全基线

**目标**：建立私有、最小权限、可升级的 Kubernetes 运行环境。

**AKS 改造项**：

- Private Cluster；
- Entra integration + Azure RBAC for Kubernetes；
- 禁用 local accounts 和共享 admin kubeconfig；
- Azure CNI powered by Cilium 或批准的网络策略能力；
- System/User Node Pool 分离，至少两个节点并使用可用区；
- 启用 Cluster Autoscaler、Node Image 自动升级和受支持版本升级策略；
- 启用 Azure Policy add-on、Defender for Containers、Container Insights、Managed Prometheus；
- 限制 API Server、kubelet、Dashboard 和 admission webhook 暴露；
- Namespace 应用 Pod Security Admission `restricted`、ResourceQuota 和 LimitRange。

**LiteLLM Pod 改造项**：

- 专用 ServiceAccount；
- `runAsNonRoot`、`seccompProfile: RuntimeDefault`；
- `allowPrivilegeEscalation: false`、drop `ALL` capabilities；
- 评估并实现 `readOnlyRootFilesystem`，为必要写路径提供受限 `emptyDir`；
- 禁止 privileged、hostNetwork、hostPID、hostIPC 和 hostPath；
- `automountServiceAccountToken: false`，仅 Workload Identity场景挂载必要令牌；
- 配置 CPU/内存 requests 与 limits；
- 配置 startup、readiness、liveness probes；
- 配置 termination grace、preStop/drain 和优雅关闭；
- 配置 PodDisruptionBudget、topology spread/反亲和；
- 配置 HPA 或 KEDA，并验证扩缩容不会破坏 WebSocket/会话亲和。

**验收**：Pod 通过 restricted 基线；故障 Pod不接收流量；节点维护时至少一个副本可用。

### SEC-11 NetworkPolicy 与出站治理

**目标**：限制横向移动和数据外泄路径。

**改造项**：

- 对业务 namespace 建立 ingress/egress Default Deny；
- 仅允许 ingress controller 到 LiteLLM 4000；
- 仅允许 LiteLLM 到 DNS、PostgreSQL、Redis、Key Vault、模型 Private Endpoint 和批准的可观测端点；
- PG/Redis 仅允许 LiteLLM 和批准的运维/迁移 Job；
- 禁止无需求的 Kubernetes API 与 IMDS 访问；
- 使用 UDR 将批准的公网出站强制经过 Azure Firewall Premium；
- 建立 FQDN、Service Tag、端口和协议 allowlist；
- 第三方模型、MCP、OIDC、CRL/OCSP、镜像和包源逐项纳入出站清单；
- TLS Inspection 仅在专项评审后启用；
- 先以审计/观察模式收集实际依赖，再切换 Default Deny。

**验收**：允许路径全部成功；未批准 Pod、端口和公网目标均失败；DNS 与证书验证不被误阻断。

### SEC-12 Azure OpenAI / Foundry 私网化

**目标**：模型端点只接受批准身份和私网来源。

**改造项**：

- 每个 Azure OpenAI/Foundry 资源创建 Private Endpoint 与 Private DNS；
- 验证 AKS 内解析为私有地址；
- 禁用 Public Network Access；
- 在支持的资源上禁用 Local Auth；
- LiteLLM UAMI 仅授予 `Cognitive Services OpenAI User` 等最小数据平面角色；
- 模型部署管理身份与调用身份分离；
- 配置 Diagnostic Settings；
- 明确 Global/Data Zone/Regional deployment 的数据驻留和故障转移策略；
- 禁止将内容策略拒绝当作可用性故障跨隔离组重试；
- 对 Preview 模型设置独立准入与回滚。

**验收**：公网和 API Key调用失败；AKS Workload Identity 私网调用成功；跨资源最小权限测试通过。

### SEC-13 LiteLLM 高可用与容量治理

**目标**：消除单副本和数据库恢复窗口造成的整体不可用。

**改造项**：

- LiteLLM 至少两个副本，分布在不同节点/可用区；
- readiness 必须真实验证应用可接收请求，而非仅容器进程存在；
- startup probe 覆盖 Prisma migration 和配置加载时间；
- liveness 只检测不可恢复卡死，避免数据库短暂故障引发重启风暴；
- 评估数据库不可用时 Virtual Key 认证的 fail-open/fail-closed 策略；
- 配置 Redis auth cache 和数据库连接重建；
- 配置每 Pod worker 数、连接池、并发、队列、请求超时和流式连接上限；
- 为 WebSocket/SSE 配置 drain，避免滚动升级中断长会话；
- HPA 指标至少覆盖 CPU、内存和请求/并发；
- 配置 PDB、滚动更新参数、最小可用副本和升级维护窗口；
- 路由采用“Affinity 优先、容量路由负责新会话初选”的分层策略；目标态为 `usage-based-routing-v2` + 按模型组 affinity + Redis，过渡期保留已验证的 `simple-shuffle` + affinity；
- 对每个 deployment 配置真实 RPM/TPM、最大并发、冷却和故障阈值。

**验收**：单 Pod删除、节点排空、滚动升级、PG/Redis短暂故障和上游限流测试满足 SLO。

#### 历史 LiteLLM 1.98.0 路由评估（2026-09-03）

以下源码判断保留为设计依据，不是 `1.104.0` 云端 affinity、缓存或故障切换证明；精确版本和隔离验证见[升级记录](litellm-1.104.0-upgrade-validation-2026-10-07.md)，生产仍须按实际 deployment 重跑容量与协议门禁。

LiteLLM `1.98.0` 官方仍将 `simple-shuffle` 作为低开销生产默认。`usage-based-routing-v2` 并不是所有场景下更优：它按 1 分钟窗口中的 deployment RPM/TPM 使用量选择剩余容量较多的后端，适合多 Foundry Resource 的吞吐与配额均衡，但如果不叠加 affinity，会让相同前缀更容易分散到不同 deployment，降低 Prompt Cache复用。

本方案的推荐顺序是：

1. `encrypted_content_affinity`：优先处理 Codex/Responses encrypted content，避免 `invalid_encrypted_content`；
2. `responses_api_deployment_check`：有 `previous_response_id` 时回到生成原响应的 deployment；
3. `session_affinity`：同一稳定 session 在空闲 TTL 内固定 deployment；
4. `deployment_affinity`：Virtual Key过渡期按 Key hash固定 deployment；
5. 以上均未命中时，才由 `usage-based-routing-v2` 为新会话选择当前有容量的 deployment。

在 `1.98.0` 源码中，Responses、session 和 API Key affinity 已统一到 `DeploymentAffinityCheck`，其内部优先级是 `previous_response_id -> session_id -> API Key hash`；`encrypted_content_affinity` 会被放在该检查之前。affinity 使用 Redis原子 first-writer-wins pin，并在 Redis故障时退化为 Pod本地 pin。TTL是滑动空闲窗口，不是会话总时长。

**目标配置示例**：

```yaml
model_list:
  - model_name: gpt-5.6-sol
    litellm_params:
      model: azure/gpt-5.6-sol
      api_base: os.environ/AZURE_OPENAI_ENDPOINT_1
      api_version: v1
      rpm: 1000                 # 使用 Foundry 实际配额，不使用示例值上线
      tpm: 1000000
      max_parallel_requests: 50
    model_info:
      id: gpt-5-6-sol-resource-1 # 固定且跨重启稳定
      base_model: azure/gpt-5.6-sol

  - model_name: gpt-5.6-sol
    litellm_params:
      model: azure/gpt-5.6-sol
      api_base: os.environ/AZURE_OPENAI_ENDPOINT_2
      api_version: v1
      rpm: 2000
      tpm: 2000000
      max_parallel_requests: 100
    model_info:
      id: gpt-5-6-sol-resource-2
      base_model: azure/gpt-5.6-sol

router_settings:
  routing_strategy: usage-based-routing-v2
  routing_strategy_args:
    ttl: 60

  # 多 LiteLLM Pod必须共享同一 Redis；TLS/Entra认证按 SEC-09 实施。
  redis_host: os.environ/REDIS_HOST
  redis_port: os.environ/REDIS_PORT
  cache_kwargs:
    username: os.environ/REDIS_USERNAME
    azure_redis_ad_token: true
    ssl: true
    ssl_cert_reqs: CERT_REQUIRED
    ssl_check_hostname: true
    max_connections: 100

  # 容量限制对所有模型生效；affinity 只对明确列出的长会话模型生效。
  optional_pre_call_checks:
    - enforce_model_rate_limits
  model_group_affinity_config:
    gpt-5.6-sol:
      - encrypted_content_affinity
      - responses_api_deployment_check
      - session_affinity
      - deployment_affinity

  deployment_affinity_ttl_seconds: 3600
  num_retries: 2
  allowed_fails: 3
  cooldown_time: 30

general_settings:
  enable_health_check_routing: true
  health_check_staleness_threshold: 600
  health_check_ignore_transient_errors: true
```

配置约束：

- `rpm`、`tpm` 和 `max_parallel_requests` 必须来自每个 Foundry deployment的实际配额与压测结果；缺失时 `usage-based-routing-v2` 无法做可靠容量决策；
- `model_info.id` 必须显式、唯一且稳定，否则重启或配置重建后，已有 affinity 和 Responses ID不能可靠定位 deployment；
- `model_group_affinity_config` 仅对长会话/Codex/Agent模型开启。批处理和无状态模型不启用 `deployment_affinity`，避免把大流量 Key长期压在单一后端；
- `deployment_affinity`使用内部Virtual Key hash，不使用OpenAI `user`字段。接入客户自有Entra认证代理后，要验证代理映射是否仍产生稳定归因键；否则以`session_affinity`为主，或增加受信身份到路由键的映射；
- `REDIS_USERNAME` 使用 Azure Managed Redis数据访问策略对应身份的 Object ID；`azure_redis_ad_token: true` 需要 `azure-identity`，LiteLLM `1.98.0` 会通过 `AzureADCredentialProvider` 刷新 token。AKS Workload Identity必须验证初次认证、token过期刷新、连接池重连和 Redis故障转移；未通过前按 SEC-09 使用 Key Vault托管凭据，不得关闭 TLS或证书校验；
- 多 LiteLLM Pod不配置共享 Redis时，只能保证单 Pod内 stickiness，不能作为生产缓存命中保证；
- affinity 命中的 deployment如果处于 cooldown，必须按错误类型明确重试/失败策略，不能为追求缓存命中而持续请求不健康后端；
- 内容策略拒绝、认证错误和无效请求不应跨模型或隔离组重试。

#### Foundry Prompt Cache 配套要求

路由只能提高“请求回到相同 deployment”的概率，不能弥补 Prompt前缀变化。客户端和 Agent还必须满足：

- 请求至少 `1,024` input tokens，且前 `1,024` tokens完全一致；
- 稳定 system/developer instructions、工具定义、Schema和参考内容放在最前，动态用户输入放在末尾；
- 对话保持 append-only，不在中间插入时间戳、随机 ID、动态工具顺序或变化的系统内容；
- GPT-5.6及更新模型对相同前缀复用稳定 `prompt_cache_key`；同一 key + prefix约超过 15 RPM时，按稳定规则分片多个 key，而不是每次随机生成；
- Standard pay-as-you-go的 GPT-5.6可在稳定内容末尾使用 `prompt_cache_breakpoint`，并按业务决定 implicit/explicit模式；该参数需要在 LiteLLM `1.98.0` 到目标 Foundry deployment完成透传回归后才能上线；
- `prompt_cache_options.ttl` 在 GPT-5.6当前只支持 `30m`；更早模型按其支持范围评估 `prompt_cache_retention`；
- 工具定义、图片 detail、结构化输出 Schema和前缀序列化必须稳定。

不要将 LiteLLM `optional_pre_call_checks: [prompt_caching]` 作为 Foundry Responses缓存主方案。`1.98.0` 中该检查仅记录 completion/acompletion/Anthropic Messages，依赖消息中的 `cache_control: ephemeral`，deployment pin只有 5 分钟，而且当前不把 tools纳入记录；它不等同于 Azure Foundry的 `prompt_cache_key` / `prompt_cache_breakpoint`，也不覆盖本方案核心 Responses路径。

#### 分阶段选择

| 阶段 | 路由策略 | 使用条件 |
| --- | --- | --- |
| 当前/过渡 | `simple-shuffle` + Responses/session/deployment affinity | Redis和真实 RPM/TPM尚未完成验证；维持已验证缓存基线 |
| 目标生产 | `usage-based-routing-v2` + 模型组 affinity + Redis | 多 Pod、共享 Redis、真实配额、稳定 deployment ID和故障演练全部通过 |
| 批处理 | `usage-based-routing-v2`，不启用 Key affinity | 优先吞吐和配额均衡；与交互模型组、Key和预算隔离 |
| 无状态低延迟 | `simple-shuffle` 或独立 latency routing group | 不依赖多轮上下文；必须以 p95/p99和错误率实测决定 |

#### 核心 LLM API 指标

上线前后的比较至少覆盖：

| 指标 | 计算/观察方式 | 目标方向 |
| --- | --- | --- |
| Prompt Cache读取率 | `sum(cached_tokens) / sum(prompt_tokens)` | 提升；长会话稳态单独统计 |
| Cache写读比 | `sum(cache_write_tokens) / sum(cached_tokens)`（GPT-5.6 Standard） | 避免大量写入而无后续读取 |
| Cache eligible命中率 | 在 input >=1,024且前缀稳定的请求中，`cached_tokens > 0` 的比例 | 提升 |
| 会话后端切换率 | 同一 session/cache key的 `model_id` transition次数 | 接近 0 |
| Prefix continuity | 连续轮次是否保持相同前缀和工具/Schema顺序 | 接近 100% |
| 429率与重试放大 | 429数、平均 upstream attempts/成功请求 | 降低 |
| TTFT与端到端延迟 | p50/p95/p99 TTFT、总延迟，区分 cache hit/miss | cache hit明显优于 miss |
| 可用性 | 成功率、5xx、timeout、无健康 deployment次数 | 满足 SLO |
| 容量分布 | 各 deployment TPM/RPM使用率与峰值 | 与配额容量近似成比例且无热点 |
| 单位成本 | 每成功请求、每百万有效 input tokens、cached/uncached成本 | 降低 |

**路由验收门槛**：必须用同一批真实 Codex/Agent多轮任务对 `simple-shuffle + affinity` 与 `usage-based-routing-v2 + affinity` 做 A/B 基准。只有当目标配置在缓存读取率不下降的同时改善 429、吞吐或尾延迟，才允许替换当前策略；不能因为策略名称更“智能”就直接切换。

### SEC-14 LiteLLM 配置与密钥治理

**目标**：避免配置漂移、密文失效和未经审批的生产变更。

**改造项**：

- 固定 `LITELLM_SALT_KEY`，Master Key 可独立轮换；
- 轮换前验证数据库中 UI 模型、SSO、Guardrail 和凭据可重新解密；
- 明确模型和 Router 配置的单一事实源：Git/IaC 或 UI/数据库；
- 如保留 UI 管理，定义审批、Audit Log、定期导出、备份和漂移检测；
- 限制 `STORE_MODEL_IN_DB` 可加载的对象范围，评估 `supported_db_objects`；
- 禁止静态文件和数据库长期维护同一 deployment；
- 使用 schema 校验输入 JSON/YAML，避免无效 deployment/resource 笛卡尔积；
- 生产镜像、API version、模型 deployment 和 Router 参数显式固定；
- 设置 Spend Logs retention、禁用 Prompt/Response 正文存储；
- 设置最大请求/响应尺寸、超时、重试和内容策略 fallback；
- SSO App Roles、Team、模型 ACL、预算、RPM/TPM 和 Key 生命周期纳入配置基线；
- 为配置变更生成前后差异、操作者、工单号和回滚点。

**验收**：Master Key轮换不导致 DB 配置消失；重复部署无未解释漂移；未审批配置不能进入生产。

### SEC-15 AI Guardrail 与数据防泄漏

**目标**：在请求出企业边界前识别 Prompt Injection、PII、Secret 和内容风险。

**改造项**：

- 不将当前 `litellm_content_filter` 英文关键词命中等同于多语言语义内容安全；
- 集成 Azure AI Content Safety，覆盖 Prompt Shields/Jailbreak 和目标内容类别；
- 集成 Azure AI Language PII 或经批准的 PII 服务；
- 增加 Secret/高熵凭据、客户标识和机密代码规则；
- 定义 pre-call、post-call、tool-call 和 logging-only 的适用范围；
- 定义 block、mask、review、monitor 的动作矩阵；
- 定义 Guardrail 超时/服务不可用时的 fail-open/fail-closed 策略；
- 高风险工具写操作保留人工确认；
- Guardrail 结果写入脱敏审计元数据，不保存完整命中原文；
- 验证 Chat、Responses HTTP、Responses WebSocket、Embeddings、Files 和 MCP/Tools 的支持边界；
- 建立中英文及业务语言测试集，包含正常、边界、规避、编码和长上下文场景；
- 评估误报/漏报、延迟、吞吐和成本后再设置强制策略；
- 保留 Azure OpenAI/Foundry 原生内容过滤，不以网关规则替代上游控制。

**验收**：批准测试集达到目标召回率/误报率；Guardrail 服务故障行为符合风险分级；不能通过切换模型绕过内容策略。

**协议证明矩阵**：以下每条生产路径都必须分别记录认证结果、Guardrail 结果、失败动作、协议状态和日志脱敏结果，不能用 Playground 或单次握手代替：

| 路径 | 必测行为 |
| --- | --- |
| `/v1/chat/completions` | 非流式与流式请求的允许、阻断、脱敏和超时 |
| `/v1/responses` HTTP | 多轮上下文、tool-call、内容阻断和错误响应 |
| Responses WebSocket | 101 后首帧与后续帧、tool-call、多轮、断线和 Guardrail 失败 |
| SSE | 首包、长时间静默、阻断前后是否泄露 partial output |
| Embeddings/Files | PII/Secret/文件大小与不支持 Guardrail时的显式策略 |
| MCP/Tools | 工具 allowlist、参数校验、用户身份传递和高影响操作审批 |

**生产切流门槛**：任何无法执行目标 Guardrail 的路由必须被显式禁用、限制到低风险数据，或有书面补偿控制；不得默认放行。

### SEC-16 日志、指标和隐私分层

**目标**：可观测、可审计，同时不把日志变成新的敏感数据池。

2026-09-10确定基础版原生 Spend Logs；2026-10-09原生发布/受控读取已有代码与隔离验证。正文获批后只进入私有 PG，运维日志仅元数据；云端字段权限、容量/留存/备份/故障仍须验收，见[部署指南](customer-deployment-workflows-zh.md)。

**改造项**：

- 定义 L1 元数据、L2 脱敏摘要、L3 原文三级日志；
- 未批准采集前默认仅元数据，`store_prompts_in_spend_logs=false`；基础版获批后通过受控发布流程启用原生正文，同时落实读取者、PG清理/容量和备份策略；
- Authorization、Cookie、Token、Virtual Key、连接串和 Secret 永不记录；
- 使用统一 Call/Trace ID 贯穿 WAF、ingress、LiteLLM、Guardrail、模型和数据库；
- LiteLLM 接入 OpenTelemetry；
- AKS 接入 Container Insights 与 Managed Prometheus；
- Azure OpenAI、Key Vault、PostgreSQL、Redis、Front Door/WAF、Firewall 和 Entra 配置诊断日志；
- 日志进入 Log Analytics，长期归档按政策进入 ADLS/Storage；
- 配置 Private Link、RBAC、PIM、CMK 和访问审计；
- Spend Logs 设置保留、分区/清理和容量监控；
- 明确日志时钟、区域、数据驻留、留存、删除与 Legal Hold；
- 建立 SLO Dashboard：成功率、延迟、TTFT、WebSocket、上游错误、DB/Redis、Guardrail、预算和缓存命中。

**验收**：抽样日志无敏感凭据；Call ID 可关联完整链路；日志留存与删除策略按计划生效。

### SEC-17 Defender、Policy 与供应链

**目标**：阻止不可信镜像和高风险 Kubernetes 配置进入生产。

**改造项**：

- LiteLLM 和依赖镜像同步到 ACR Premium；
- ACR 禁用匿名/公网访问并配置 Private Endpoint；
- 生产按 digest 部署，不仅使用 Tag；
- 生成并保存 SBOM；
- 启用镜像漏洞扫描，定义 Critical/High 阻断与修复 SLA；
- 对镜像签名并在 admission 阶段验证；
- 固定基础镜像、Helm Chart、GitHub Action 和依赖版本；
- CI/CD 使用 OIDC/Workload Identity Federation，不保存 Azure Client Secret；
- 启用 SAST、Secret Scanning、Dependency Review 和 IaC 扫描；
- Azure Policy/Admission 拒绝 privileged、hostPath、root、无资源限制、未批准 Registry 和未签名镜像；
- Defender for Containers 检测异常进程、Shell、挖矿、IMDS/Kubernetes API 和异常 egress。

**验收**：故意部署不合规 Pod/镜像被拒绝；批准镜像可追溯到源码、构建、SBOM、签名和扫描报告。

### SEC-18 Sentinel 检测与响应

**目标**：形成从信号到调查、遏制和恢复的闭环。

**改造项**：

- 将 Entra、WAF、Firewall、AKS、Defender、Key Vault、PG、Redis、模型和 LiteLLM 日志接入 Sentinel；
- 建立至少以下检测：异常 Key 使用、Master Key 使用、认证失败突增、预算异常、Prompt Injection 集中命中、PII/Secret 外发、Pod异常、模型端点错误、PG 容量/recovery、配置变更；
- 统一实体：用户、应用、Team、Key hash/alias、Call ID、Pod、模型和 deployment；
- 编写禁用 Virtual Key、隔离 Team、阻断来源、缩小模型 ACL和创建工单的可逆 Runbook；
- 高影响操作如删资源、全量轮换、隔离节点必须人工批准；
- 为 P0/P1 事件定义 MTTD、MTTC、Owner、升级和证据保留；
- 定期执行 Tabletop 和自动化响应演练。

**验收**：至少 5 个核心检测场景和 3 个可逆响应通过端到端演练。

### SEC-19 备份、恢复与业务连续性

**目标**：数据库、配置和关键 Secret 均有可验证恢复路径。

**改造项**：

- PostgreSQL 自动备份、PITR、可选 Geo Backup；
- 定期恢复到隔离环境并执行 LiteLLM 登录、Key、Team、模型和预算验证；
- 备份 LiteLLM 数据库配置、IaC 状态、Git 配置和批准的 UI 导出；
- Key Vault 开启 Soft Delete、Purge Protection 和 Resource Lock；
- 备份删除权限与生产管理权限分离；
- 保存固定 Salt和证书恢复流程，但不得在普通备份中明文导出；
- 明确区域级灾难下 DNS、WAF、AKS、PG、Redis、模型端点和 Secret 的恢复顺序；
- 定义旧环境保留期、数据冻结和回退窗口。

**验收**：季度恢复演练达到 RTO/RPO；恢复环境中的数据库密文可以使用受控 Salt解密。

### SEC-20 测试、迁移、灰度和回退

**目标**：安全增强不能破坏已验证的 LiteLLM 功能和 Codex 长会话。

**测试范围**：

- 功能：Chat、Responses、Embeddings、Image、Files、MCP/Tools、SSO、Virtual Key；
- 协议：HTTPS、SSE、WebSocket 101、多轮 tool-call、长连接 drain；
- 路由：多资源负载均衡、重试、冷却、亲和、缓存、故障切换；
- 权限：用户/应用/Team/模型 ACL、预算、RPM/TPM、App Role；
- 安全：WAF、JWT、NetworkPolicy、Private Link、Workload Identity、Secret、Guardrail；
- 性能：并发、p50/p95/p99、TTFT、数据库连接、Redis、Guardrail 额外延迟；
- 故障：Pod删除、节点排空、PG failover、Redis重启、模型 429/5xx、Key Vault暂时不可用；
- 恢复：PITR、配置恢复、Salt/Secret恢复、回退旧环境；
- 隐私：日志和错误响应无 Key、Token、连接串和未批准原文。

**迁移顺序**：

1. 冻结目标架构与基线；
2. 部署新 Azure 平台层；
3. 部署 PG、Redis、Key Vault、AKS 与私有模型连接；
4. 迁移数据库并做一致性验证；
5. 部署 LiteLLM 双副本并完成内部回归；
6. WAF Detection 模式灰度；
7. 小比例/测试域名切流；
8. WebSocket、缓存和 Guardrail 专项验证；
9. WAF Prevention 与正式 DNS 切流；
10. 保留旧环境只读回退窗口；
11. 达到退出标准后退役旧公网入口和集群内 PG。

**回退要求**：数据库 schema、Secret/Salt、DNS TTL、旧入口、旧模型配置和客户端兼容性必须在切流前准备回退方案。

**生产边界门槛**：切流使用的 Azure 平台资源必须来自批准 IaC；迁移/验证 Python 脚本不得在生产创建公网入口、重新附加 VMSS 业务 UAMI或创建/修改 APIM 资源。

### SEC-21 员工与 Agent 上下文审计

**目标**：在合法、透明、最小化和可追责的前提下，审计经过 LiteLLM 网关的员工与 Agent 模型交互，用于安全调查、数据泄漏检测、合规审计和 Agent 风险治理，而不是默认用于个人绩效评价。

> 2026-09-10范围更新，替代2026-09-07默认L3要求：第一阶段采用原生Spend Logs，完成批准范围、受控查询、PG容量/清理/备份与故障验收；自建独立存储、原文双审批/保全和可靠交付作为可选增强。原生发布/受控读取已有代码与隔离验证，真实云端验收仍待完成，本条不启用正文。分支范围见[实施路线图12.3节](litellm-security-hardening-implementation-roadmap-zh.md#123-l2l3-上下文审计)。

**可审计范围**：

- LiteLLM 实际接收到的 system、user、assistant messages；
- LiteLLM 实际接收到或返回的 Responses API input/output items；
- 发往模型的工具定义、tool call，以及经网关返回的 tool result；
- 模型别名、实际 deployment、时间、Token、费用、延迟、状态码和 Guardrail 结果；
- Entra 用户/应用身份、LiteLLM Team、Virtual Key alias/hash、session ID 和统一 Call/Trace ID；
- 客户端显式提交的业务元数据，例如项目、任务、Agent、工作流和环境标签。

**明确能力边界**：

- 只能审计实际经过 LiteLLM 的流量；绕过网关直连模型的调用不在范围内；
- 不能自动看到员工终端、本地文件、浏览器、剪贴板、Shell 或 Agent 内存，除非这些内容被客户端发送到网关；
- 不能采集模型未返回的隐藏 Chain-of-Thought、内部推理状态或供应商不可见数据；
- 网关之外执行的工具动作需要由 Agent Runtime、MCP Server、终端防护或业务系统提供补充审计；
- Responses WebSocket、流式输出和多轮 Agent 会话是否完整记录，必须按协议逐项验证，不能由 HTTP Chat 测试结果推断；
- LiteLLM 日志是请求级证据，不天然等于完整会话。会话重建需要稳定的 user/app、session、trace、parent response 和 tool-call 关联标识。

**治理前置条件**：

- 由客户 Legal、HR、Security、Privacy、Data Governance 和 Employee Relations 书面批准目的、范围、访问者、留存期和员工告知方式；
- 明确允许的使用目的和禁止用途，默认禁止以全文内容自动生成个人绩效排名或纪律结论；
- 按国家/地区、员工类型、数据分类和业务场景评估劳动法、隐私法、跨境和工会要求；
- 基础版明确读取授权、调查工单及误用问责，核验原生查询权限；双人逐次审批、Legal Hold等有明确要求时选择能满足要求的增强方案，不能假定原生开关自带；
- 未完成治理批准前，仅保留 L1 安全元数据，不启用 L3 全文审计。

**身份与归因改造**：

- 人员调用使用 Entra 唯一身份，后台 Agent 使用独立应用身份或 Managed Identity；
- 禁止多人或多个 Agent 共用无法归因的 Virtual Key；
- 服务端生成可信 user/app/team/session 标识，不能直接信任客户端可伪造的 `user`、标签或身份 Header；
- 对 Virtual Key调用评估 `overwrite_user_with_key_hash`，对 JWT调用从已验证 claim 建立归因；
- Agent 必须提交稳定的 `agent_id`、workflow/run ID、session ID 和 parent trace；
- 身份映射变化、Key 转移、共享账号和未知调用者触发告警。

**采集与存储改造**：

- L1 元数据继续进入 Spend Logs/Log Analytics，用于全量统计和检索；
- L2 保存分类、风险标签、哈希、摘要和脱敏片段；
- 基础版用原生Spend Logs保存获批Prompt/Response及实际记录中的工具内容；覆盖、截断和多模态字段须实测，不承诺完整文件/会话归档；
- 基础版正文在私有PG，须评估与Key/预算控制数据共库的负载、在线保留、清理与备份，不再要求正文一律放独立存储；
- 仅增强方案评估受控异步管线，例如LiteLLM callback -> 受控接收服务 -> 经验证的脱敏 -> 独立Blob；后续独立存储/索引要求只适用于该分支；
- 如果采用 LiteLLM Azure Storage、Generic Logger 或其他 callback，必须先核验版本、许可证、Responses/WebSocket 覆盖和失败行为；
- L3 存储使用 Private Endpoint、CMK、独立容器、不可公开访问、最小 RBAC、PIM 和访问日志；
- 按用户、应用、Team、Agent、Call ID、session 和时间建立可控索引，不在普通日志平台复制全文；
- 二进制、图片、音频、文件和超长上下文采用单独大小上限、内容类型策略和存储规则；
- 存储写入失败不得阻塞普通低风险请求，除非该数据分类明确要求 fail-closed；失败必须告警并记录审计缺口。

**LiteLLM 配置改造**：

- 当前安全默认保持`store_prompts_in_spend_logs=false`；基础版通过受控发布在获批试点启用，验收后再按批准范围放行；留存、是否扩容及监控由实测决定，不直接套用旧容量；
- 若客户要求强制审计，设置 `global_disable_no_log_param: true`，防止调用方使用 `no-log` 跳过日志；
- 拒绝或清除未经授权的 `x-litellm-disable-callbacks`、`LiteLLM-Disable-Message-Redaction`、`log_raw_request` 等客户端日志控制参数；
- 生产日志禁止包含 Authorization、Cookie、Token、完整 Virtual Key、数据库连接串和 Secret；
- 原始请求 Header 采用 allowlist，不将全部 `proxy_server_request.headers` 写入审计存储；
- 配置 Prompt/Response 单字段长度、请求/响应大小和 base64 截断限制；
- 全文采集与 `turn_off_message_logging`、callback 的 `message_logging`、Spend Logs正文开关必须形成一份无冲突配置矩阵；
- 审计配置变更本身进入 Audit Log 和 Sentinel，并要求审批单号。

**Agent 专项改造**：

- 区分模型上下文、工具计划、工具参数、工具结果和最终答复；
- 工具审计记录调用者、目标工具、资源、动作、参数摘要、审批结果、状态和返回摘要；
- 高影响写操作由执行层要求人工确认，不能只依赖模型文本或 Prompt审计；
- Agent Runtime、MCP Server 和 LiteLLM 使用同一 Trace ID，支持从模型决策追踪到实际业务动作；
- 对无限循环、重复调用、异常长上下文、大量文件读取、异常外部域名和费用突增建立检测；
- 对包含 Secret、PII、源代码和受限数据的工具结果在进入模型前执行 Guardrail/DLP。

**协议与完整性验证矩阵**：

| 场景 | 必须证明的内容 |
| --- | --- |
| Chat 非流式/流式 | 输入、最终输出、身份、模型、Token 和 Trace 可关联 |
| Responses HTTP | input/output items、`previous_response_id`、tool call/result 和多轮关系可关联 |
| Responses WebSocket | 101 后请求帧、响应帧、断线、重连和多轮记录无静默缺口 |
| SSE | 流式 chunk 与最终聚合结果的保存策略明确，避免重复和 partial output 泄漏 |
| MCP/Tools | 模型请求、工具选择、参数、审批、执行结果和业务系统日志可串联 |
| Files/Images/Audio | 内容是否保存、仅保存引用还是保存文件本体有明确策略 |
| 失败/超时/阻断 | 上游未返回、Guardrail 阻断和客户端断连仍生成必要审计事件 |

**审计访问控制**：

- 普通 LiteLLM 管理员不自动获得 L3 原文读取权限；
- 平台运维、安全调查、HR/Legal 和数据治理使用不同角色；
- L3 查询要求 PIM、工单、理由、时间限制和双人审批；
- 查询、预览、导出、共享、删除和留存变更全部记录不可抵赖审计日志；
- 批量导出默认禁止，Break Glass访问立即告警；
- 看板默认展示团队/项目聚合趋势，不展示个人全文或个人排名。

**验收标准**：

- 对批准协议执行端到端测试，网关请求、模型响应、工具动作和审计事件可按 Trace ID关联；
- 直接调用上游模型被网络策略阻断，避免绕过审计；
- `no-log` 和客户端禁用 callback/脱敏的绕过测试失败；
- 抽样审计记录不包含 Key、Token、连接串和未批准数据；
- 未授权管理员无法读取 L3，批准调查员只能在授权窗口访问；
- retention 到期删除、Legal Hold、导出审批和访问日志均通过演练；
- 审计管线故障能够告警，并可量化缺失事件数量和影响范围；
- WebSocket、Responses 和 Agent 工具链不存在未经批准的静默审计缺口。

## 6. 仓库代码具体修改面

以下是实施阶段预计会触及的代码和配置边界，供审查是否遗漏。

| 模块 | 具体改造 |
| --- | --- |
| `deploy_mi_aks_litellm.py` | 拆分 Azure IaC职责；移除 VMSS UAMI；支持 Workload Identity、外部 PG/Redis/Key Vault、双副本、probes、resources、PDB、HPA、安全上下文、内部 Service |
| LiteLLM config 生成 | JWT/SSO、固定 Salt引用、Redis、PG TLS、Spend retention、审计模式、日志脱敏、反 `no-log` 绕过、请求大小、timeout、Router 容量、Guardrail、内容策略 fallback |
| Kubernetes manifests | ServiceAccount、SecretProviderClass、NetworkPolicy、Deployment、Service、Ingress、PDB、HPA、ResourceQuota、LimitRange、Pod Security labels |
| Azure IaC | Front Door/App Gateway、WAF、PLS、VNet、Firewall、Private DNS/Endpoint、Private AKS、UAMI、KV、ACR、PG、Redis、Monitor、Defender、Policy |
| CI/CD | lint/test/security scan、IaC plan、SBOM、签名、digest promotion、OIDC、审批、drift detection |
| 测试 | 单元、schema、权限、网络、协议、审计完整性、反绕过、性能、故障、恢复和安全回归 |
| 文档 | 部署、迁移、密钥轮换、PG恢复、Redis故障、WAF调优、SSO、Guardrail、上下文审计治理、事件响应和回退 Runbook |

## 7. 安全风险追踪矩阵

| 原风险 | 对应工作包 | 预期关闭条件 |
| --- | --- | --- |
| R01 公网入口缺少边缘防护 | SEC-03、SEC-11 | WAF 为唯一入口，源站不可绕过 |
| R02 管理面与数据面共用入口 | SEC-04、SEC-05 | API 拒绝管理路径；独立 Admin WAF 来源白名单和私有源站，内层登录；企业身份另验 |
| R03 长期 Bearer Key | SEC-05、SEC-14 | Entra 身份可归因，Key 独立、短周期、受限 |
| R04 节点级 Managed Identity | SEC-06 | VMSS 业务 UAMI移除，Pod federation生效 |
| R05 Secret 注入不完善 | SEC-07、SEC-14 | Key Vault、最小读取、固定 Salt、轮换验证 |
| R06 PostgreSQL 单点 | SEC-08、SEC-19 | Flexible Server HA、TLS、PITR演练 |
| R07 AKS 网络边界不完整 | SEC-10、SEC-11、SEC-12 | Private AKS、Default Deny、受控 egress |
| R08 Pod 安全基线缺失 | SEC-10、SEC-17 | restricted 基线和准入策略通过 |
| R09 软件供应链风险 | SEC-02、SEC-17 | digest、扫描、SBOM、签名和门禁 |
| R10 缺少威胁检测 | SEC-16、SEC-17、SEC-18 | Defender/Sentinel 信号与响应演练通过 |
| R11 AI 内容与数据泄漏 | SEC-15、SEC-16、SEC-21 | 多语言 Guardrail、DLP和上下文审计测试达标 |
| R12 日志敏感数据池 | SEC-08、SEC-16、SEC-21 | 正文默认关闭、分层留存、独立原文存储和访问审批 |
| R13 安全事件未闭环 | SEC-18、SEC-19 | 检测、Runbook、恢复演练上线 |
| R14 配置漂移与越权变更 | SEC-02、SEC-14 | 单一事实源、PR审批、Audit和 drift detection |
| R15 公网 ACME 依赖 | SEC-03、SEC-04、SEC-07 | 证书方案与入口模式匹配并完成续期演练 |

## 8. 建议实施阶段

### 阶段 A：现有环境紧急加固

- 固定并安全保存 `LITELLM_SALT_KEY`；
- 固定 Master Key，不再由脚本重复生成；
- PG 扩容、retention、备份和容量告警；
- 完成 SSO、App Role、MFA 和最小管理员组；
- 限制公网管理面和源站访问；
- 锁定现有镜像 digest；
- 增加 LiteLLM/PG probes、资源限制和基础告警；
- 默认关闭 Prompt/Response 正文日志。

### 阶段 B：新安全基础设施

- IaC、VNet、Private DNS、Firewall；
- Private AKS、ACR、Workload Identity、Key Vault；
- Flexible Server HA、Managed Redis；
- Azure OpenAI/Foundry Private Endpoint；
- NetworkPolicy、Defender、Policy、Monitor。

### 阶段 C：入口与应用迁移

- LiteLLM 双副本与 Redis 共享状态；
- 数据库迁移和配置一致性验证；
- Front Door/App Gateway WAF 与管理/数据面分离；
- JWT、Team、模型 ACL、预算和 Guardrail；
- 在治理批准后试点员工与 Agent 上下文审计，先 L1/L2 后受控 L3；
- 灰度、负载、故障和协议回归。

### 阶段 D：SOC 与持续治理

- Sentinel 检测与 SOAR；
- Purview/日志分层；
- 供应链签名与准入；
- 恢复、红队、权限审查和季度演练。

## 9. 实施前必须确认的决策

请在审查时重点确认以下项目：

| 决策 ID | 待确认问题 | 推荐默认 |
| --- | --- | --- |
| D01 | 网关互联网可达还是仅企业内网 | 按真实客户端位置选择，不双建主入口 |
| D02 | 公网入口使用 Front Door 还是内网 App Gateway | 互联网：Front Door Premium；需要首字节超过240秒或检查大请求体时可选公网 App Gateway WAF_v2（`edge.type=applicationGateway`）；内网：App Gateway WAF v2 |
| D03 | Admin UI 是否独立入口 | 是，独立 Admin Front Door/WAF、PLS 和私有源站；内网模式另选 |
| D04 | 数据面是否启用Entra身份 | 当前原生 vkey；企业准入延期，启用时企业 Token + 客户端 vkey，不复制模型 ACL |
| D05 | 是否使用LiteLLM原生JWT | 否，已确认属于Enterprise付费能力，方案中禁止启用 |
| D06 | 模型配置事实源使用 Git 还是 UI/DB | 生产优先 Git；若保留 UI则强制审批和导出 |
| D07 | Prompt/Response 是否允许落盘 | 默认不允许，例外按业务审批 |
| D08 | PG 认证使用 Entra 还是 Key Vault密码 | 优先 Entra，先验证 LiteLLM/Prisma兼容性 |
| D09 | Redis 认证使用 Entra 还是访问密钥 | 优先 Entra，先验证 LiteLLM客户端兼容性 |
| D10 | Guardrail 失败时 fail-open 还是 fail-closed | 按数据分类；高敏请求 fail-closed |
| D11 | Guardrail 覆盖哪些语言和 API | 至少中文/英文及所有生产调用协议 |
| D12 | RTO/RPO、可用区和跨区域要求 | 客户业务 Owner批准 |
| D13 | 是否采用新集群迁移而非原地改造 | 推荐新集群迁移 |
| D14 | 日志平台、留存、区域和 CMK要求 | 客户安全/数据治理批准 |
| D15 | 可自动执行哪些安全响应 | 仅低风险可逆动作默认自动化 |
| D16 | LiteLLM目标版本和产品边界 | 固定精确OSS版本与digest，不采购或依赖LiteLLM Enterprise能力 |
| D17 | 上下文审计用于哪些目的和人群 | 仅安全、合规、数据保护和 Agent 风险治理；禁止默认用于绩效排名 |
| D18 | 审计哪些内容和协议 | 明确 messages、Responses items、tool call/result、文件及 WebSocket覆盖范围 |
| D19 | 正文保存位置和期限 | 基础版原生Spend Logs进入私有PG；发布/读取已有代码与隔离验证，云端采集授权/容量/留存仍待验收；增强L3才采用独立存储/保全 |
| D20 | 谁能查看、搜索和导出原文 | 独立角色、PIM、工单、双人审批和全量访问审计 |
| D21 | 是否强制审计并禁止 `no-log` | 受监管范围内强制，其他场景按数据分类；先验证反绕过 |
| D22 | 员工告知、Legal Hold 和数据主体流程 | Legal、HR、Privacy 和 Data Governance批准 |

## 10. 本轮建议的审查输出

审查本文后，建议形成以下结论：

1. 确认工作包是否完整，标记新增、删除或降级项；
2. 确认 D01-D22 的 Owner 和目标决策日期；
3. 确认先做现有环境紧急加固，还是直接建设新生产环境；
4. 选择 IaC 技术栈；
5. 选择第一批实施工作包及其验收测试；
6. 确认LiteLLM OSS能力边界；仅Enterprise提供的能力必须从方案中删除或使用Azure原生、客户自有组件或OSS实现替代；
7. 将批准后的工作包拆成详细设计、实施任务和变更窗口。

## 11. 当前已识别但不在首批实施范围的增强项

- 多区域 Active/Passive 或 Active/Active；
- CMK 覆盖所有支持的 Azure 服务；
- 原文日志 Immutable Blob 和 Legal Hold；
- 全量 Purview 标签驱动策略；
- 第三方模型和跨云统一 egress；
- MCP/Agent 全链路用户委托身份；
- 高级行为分析和模型滥用检测；
- 自动化红队与持续 Prompt Injection 评估。

这些项目不应从总体架构中删除，但可在核心生产基线稳定后单独立项。