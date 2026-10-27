# LiteLLM 网关历史版与安全增强版 BOM 成本对比

> 文档状态：方案审查估算，不是 Azure 报价单
> 估算日期：2026-08-31
> SKU 必要性复核：2026-09-10；本次补充选型依据，未重新获取价格，原金额仍为历史估算
> 审计方案更新：2026-09-10；第一阶段采用原生 Spend Logs 正文留痕，自建 L3 为可选增强项；配置与发布门禁尚待适配，本文不启用日志
> 价格来源：Azure Pricing MCP / Azure Retail Prices API
> 币种：USD，公开零售价，Pay-As-You-Go
> 估算区域：`westus`；Front Door、Private Link、DNS 等全球服务按对应全球/Zone 1 meter
> 月度换算：`730 小时/月`、`30 天/月`
> 目标口径：小型生产基线 + Azure Front Door Premium + 用量项只列单价
> 明确边界：不包含 APIM

## 0. 当前环境与历史估算的区别（2026-10-09）

本文“当前版”是 2026-08-31 的旧 PoC 对照，“安全增强版”也是同日设计估算；`westus` 价格不能作为 West US3 账单或当前报价。本文未重新询价。

只读管理面核查的当前参考环境是 West US3 新建 dev/test：Private AKS Running/Succeeded，Azure CNI overlay、Azure RBAC、禁用本地账户、WI/OIDC；System/User 池各 2 台 `Standard_D4s_v4`，不是下面的 2 台 D2s_v3 或 3 台 D2s_v5。独立 API/Admin Front Door Premium 经各自 PLS/内部 LB/私有 Traefik 回源，两侧 WAF Prevention；Admin 否定 SocketAddr IPMatch 白名单包含三个 IPv4 `/32` 和一个 IPv6 `/128` 精确出口。PG/Managed Redis 私网 Entra-only，后端 Vault 与证书 Vault 分离，ACR 私有，Firewall 路由出站。当前无 APIM、App Service 或单一共享代理。

默认原生 LiteLLM `1.104.0` 网关已由用户验证 Admin 密码 fallback 登录及 vkey Codex Responses 推理，不是 Entra 用户 SSO 或生产验收。企业准入、HA、容量、恢复、监控仍须验证。重新报价应按实际节点池、双入口/PLS/LB、两类 Vault、PE/DNS、数据库与备份规格及 West US3 单价核算，不把历史合计或模板数量当成实时证据。

补充实况选型：PG 16 Ready，Entra 认证启用/密码认证禁用、公共访问禁用；Managed Redis 为 `Microsoft.Cache/redisEnterprise`、`Balanced_B0`，数据库禁用访问密钥、使用 Encrypted 协议与 `10000` 端口。Redis 集群 `publicNetworkAccess=null` 不是公网开关证据。ACR Premium 禁用 admin user/公共访问，两类 Key Vault 均启用 RBAC/禁用公共访问；数量、容量及价格仍须按实际账单重新核算。

## 1. 执行摘要

按历史假设，旧 PoC 基础设施固定成本约为：

```text
历史 PoC 对照：约 $319/月（非当前 West US3 成本）
```

原安全增强版基础设施估算有两个成本场景，均未包含正文采集的实际容量与处理费用，不能作为当前基础版的最终报价：

```text
安全增强版（复用客户现有 Hub Firewall）：约 $1,206/月
安全增强版（为 LiteLLM 独立部署 Firewall Premium）：约 $2,484/月
```

对应固定成本增量约为：

| 场景 | 固定月费 | 相对当前版增量 | 约数倍数 |
| --- | ---: | ---: | ---: |
| 当前版 | $319 | - | 1.0x |
| 安全增强版，复用 Hub Firewall | $1,206 | +$887 | 3.8x |
| 安全增强版，专属 Firewall Premium | $2,484 | +$2,165 | 7.8x |

安全版增量主要不是 LiteLLM 软件许可或 AKS 节点，而是：

1. Azure Firewall Premium：约 `$1,277.50/月`；
2. Azure Front Door Premium：基础费 `$330/月`；
3. PostgreSQL Flexible Server Zone-Redundant HA：计算约 `$296.38/月`，另加存储；
4. Private Endpoint、ACR Premium、Defender 和额外 AKS 节点；
5. 基础版原生 Spend Logs 正文带来的 PostgreSQL、备份和维护增量；日志、Sentinel、AI Content Safety、PII 等按批准选用及实际用量另计。

如果客户已有共享 Hub Firewall、Log Analytics、Sentinel、Private DNS 和网络平台，应按资源标签或流量分摊计算增量成本，不应把共享平台全部费用计入 LiteLLM。

**并非这四项服务都必须无条件选 Premium。** 在保持本方案“Front Door 私有源站 + 微软托管 WAF 规则”和“ACR Private Link”要求时，Front Door 与 ACR 的 Premium 是功能门槛；Firewall Premium 则取决于是否需要 IDPS、TLS 检查或完整 URL 过滤；Key Vault 仅存业务 Secret 时 Standard 即可，使用 HSM 保护的 Key 才有 Premium 门槛。仅做受控出站时，应优先评估复用合规 Hub Firewall，其次评估 Standard，详见第 4.2 节。本文的专属 Premium 场景不是客户必须购买的最低配置。

**当前第一阶段已选择[原生内容审计方案](litellm-content-audit-phase1-customer-brief-zh.md)：获批的 Prompt/Response 写入 LiteLLM Spend Logs，正文保存在私有 PostgreSQL。** 不默认部署自建 L3 采集、正文 Blob、独立 HSM 密钥库、恢复及双审批/保全服务；这些只在客户明确要求增强取证能力时另立 BOM。原表未计入 L3 正文容量，不能以“不上 L3”从合计中扣出虚构节省。现有资源数量、PostgreSQL/备份容量和按量费用仍须重算，第 6 节给出新口径；原生发布/受控读取已有代码及隔离验证；云端采集授权、容量/留存/恢复仍须验收，不代表当前正文采集已开启。

## 2. 估算假设

### 2.1 历史 PoC BOM 假设

根据 2026-08-31 的旧仓库/handoff 假设，历史“当前版”采用：

- AKS Standard tier；
- 2 台 `Standard_D2s_v3` Linux 节点；
- 每节点 1 块 P10 Premium SSD OS Disk；
- LiteLLM 单副本；
- AKS 内单副本 PostgreSQL；
- PostgreSQL PVC按 P4 Premium SSD 计费；
- ingress-nginx 使用 Standard Load Balancer；
- 1 个 Standard Static Public IPv4；
- ACR Basic；
- cert-manager 与 Let's Encrypt 无单独 Azure 资源月费；
- 节点级 UAMI、Kubernetes ConfigMap/Secret 无单独资源费。

注意：部署脚本默认节点数仍可配置为 1。本文按历史 handoff 中的 2 节点状态估算；若实际客户环境只有 1 台节点，当前版固定成本约减少 `$85.41/月`。

### 2.2 安全增强版 BOM 假设

以下“小型生产基线”保留历史 SKU 和数量，供复核而非直接部署。当前审计方向改为原生 Spend Logs；历史 128 GB 数据库、7 个 PE 等数量不代表已覆盖新方案的实际容量与资源：

- AKS Standard tier；
- 3 台 `Standard_D2s_v5` Linux 节点，跨可用区/节点分布；
- 每节点 1 块 P10 Premium SSD OS Disk；
- LiteLLM 至少 2 副本；
- Azure Front Door Premium + WAF；
- AKS ingress 使用 Internal Standard Load Balancer；
- 专属 Azure Firewall Premium 作为原估算场景，或复用企业现有 Hub Firewall；是否需要 Premium 独有能力按第 4.2 节确认，不排除 Standard 方案；
- PostgreSQL Flexible Server General Purpose `Standard_D2ds_v5`，2 vCore，Zone-Redundant HA；
- PostgreSQL 主存储 128 GB；
- Azure Managed Redis `Balanced_B0`；
- ACR Premium；
- Key Vault 原估算为 Premium、低操作量；业务 Secret 库建议 Standard，增强审计的 RSA-HSM 密钥库单独评估，见第 4.2 节；
- 7 个 Private Endpoint：3 个 Azure OpenAI/Foundry、Key Vault、ACR、PostgreSQL、Redis；
- 6 个 Private DNS Zone；
- Defender for Containers，按 3 节点合计 6 vCore；
- Log Analytics、Sentinel、Managed Prometheus、Front Door流量、Firewall流量和 AI Guardrail按量计费；
- 第一阶段正文采用原生 Spend Logs，启用前需批准采集范围及留存，并实测 PostgreSQL、WAL、备份和清理负载；原固定合计尚未计入这些增量；
- 自建 L3、独立审计 Blob/HSM 和治理恢复服务不作为基础版必部署项；OpenTelemetry collector 与正文存储分开决策，不默认复制正文到观测平台。

上述是审查基线，不代表最终生产 sizing。节点、数据库、Redis、Private Endpoint 和日志量应在压测及客户 RTO/RPO 确认后调整。

## 3. 历史 PoC BOM 与固定成本

| 组件 | SKU/数量 | Azure Retail 单价 | 月估算 | 说明 |
| --- | --- | ---: | ---: | --- |
| AKS 控制面 | Standard × 1 | $0.10/小时 | $73.00 | Standard Uptime SLA |
| AKS 节点 | `Standard_D2s_v3` Linux × 2 | $0.117/小时 | $170.82 | 不含 Savings Plan/Reservation |
| AKS OS Disk | P10 LRS × 2 | $19.71/月 | $39.42 | 默认磁盘实际规格需现场核对 |
| PostgreSQL PVC | P4 LRS × 1 | $5.28/月 | $5.28 | P4按 32 GiB meter，当前逻辑容量可能更小 |
| ACR | Basic × 1 | $0.1666/天 | $5.00 | 不计超额存储/出口 |
| Standard Load Balancer | 1 个，含规则 | 约 $0.03/小时 | $21.90 | MCP返回值按显示精度估算 |
| Public IPv4 | Standard Static × 1 | $0.005/小时 | $3.65 | ingress 公网入口 |
| UAMI / ConfigMap / Secret | 若干 | 无固定资源费 | $0.00 | 不代表没有安全风险 |
| **固定月费合计** |  |  | **$319.07** | 约 `$319/月` |

### 当前版未计入项

- Azure OpenAI/Foundry 模型 Token；
- 公网数据传输；
- Azure DNS 公网 Zone/域名；
- ACR 超额存储和镜像拉取流量；
- 企业协议价、税费、支持计划和人员运维成本。

## 4. 安全增强版 BOM 与固定成本

本表仍为2026-08-31历史固定资源估算。第一阶段原生正文留痕的完整月费尚未核定：不是沿用128 GB就保证够用，也不是另加一笔固定“Spend Logs许可费”。必须将实际数据库规格、存储、备份、网络及共享资源数量核定后再出报价。

| 组件 | SKU/数量 | Azure Retail 单价 | 月估算 | 相对当前版 |
| --- | --- | ---: | ---: | ---: |
| AKS 控制面 | Standard × 1 | $0.10/小时 | $73.00 | $0.00 |
| AKS 节点 | `Standard_D2s_v5` Linux × 3 | $0.112/小时 | $245.28 | +$74.46 |
| AKS OS Disk | P10 LRS × 3 | $19.71/月 | $59.13 | +$19.71 |
| Internal Load Balancer | Standard × 1 | 约 $0.03/小时 | $21.90 | $0.00 |
| ACR | Premium × 1 | $1.6666/天 | $50.00 | +$45.00 |
| Front Door + WAF | Premium × 1 | $330/月 | $330.00 | +$330.00 |
| Azure Firewall | Premium × 1 | $1.75/小时 | $1,277.50 | +$1,277.50 |
| PostgreSQL HA计算 | `Standard_D2ds_v5` 2 vCore × 2 | $0.203/小时/实例 | $296.38 | +$296.38 |
| PostgreSQL 主存储 | 128 GB | $0.14/GB/月 | $17.92 | +$17.92 |
| Azure Managed Redis | `Balanced_B0` × 1 | $0.02/小时 | $14.60 | +$14.60 |
| Private Endpoint | 7 个 | $0.01/小时/个 | $51.10 | +$51.10 |
| Private DNS Zone | 6 个 | $0.50/Zone/月 | $3.00 | +$3.00 |
| Defender for Containers | 6 vCore | MCP显示约 $0.01/vCore/小时 | $43.80 | +$43.80 |
| Key Vault（原估算 Premium） | 原简化口径 1 个；实际拆分见 4.2 | 无 Vault 基础费；按操作/Key | $0.00 | 非总费用为零；HSM Key 另计 |
| **专属 Firewall 固定合计** |  |  | **$2,483.61** | **+$2,164.54** |
| **复用 Hub Firewall 固定合计** | 去除专属 Firewall |  | **$1,206.11** | **+$887.04** |

### 4.1 固定合计的解释

- “复用 Hub Firewall”不是 Firewall 免费，而是其基础部署费已由企业共享网络平台承担；LiteLLM 仍应分摊数据处理、日志和运营成本；
- PostgreSQL HA按主/备两份 2 vCore计算。最终账单还取决于备份、IOPS、存储增长和实际 HA模式；
- Private Endpoint按 7 个估算。每新增一个模型资源或私有依赖，固定成本约增加 `$7.30/月`，另加数据处理；
- Key Vault 的 Vault 资源无基础月费，Secret 操作的 Standard/Premium 单价相同；HSM Key 的月费及操作、自动轮换、Private Endpoint 和诊断日志需另计，不能将表中 `$0.00` 理解为使用 Premium HSM 免费；
- Defender MCP单价显示精度有限，正式报价应在 Azure Calculator按订阅和计划复核；
- Front Door Premium基础费已经包含 WAF能力，但请求、流量、Bot/CAPTCHA和规则执行仍可能按量计费。

### 4.2 Front Door、Firewall、ACR 与 Key Vault 为什么选择 Premium

**选型原则：先确认必须实现的控制，再选满足控制的最低 SKU。** “生产环境”“安全增强”“使用 HTTPS”本身都不是购买 Premium 的充分理由。以下是产品能力和当前模板事实，不代表客户环境已经部署或通过验收。

| 服务 | 本方案对应要求 | Premium 是否必要 | 更低成本路径与代价 |
| --- | --- | --- | --- |
| Front Door | 公网 API 经 WAF 接入，但 AKS 源站不开放公网；使用微软托管 WAF/Bot 规则 | **保持这两项要求时必要**：Standard 不支持到源站的 Private Link，也不支持微软托管规则集 | 复用具备所需能力的企业入口；或重新设计为区域入口/纯内网入口。Standard + 公网受限源站属于不同架构，不是等价降级 |
| Azure Firewall | 控制 AKS/runner 出站目的地，保留网络日志；高级威胁检测按需 | **有条件必要**：IDPS、TLS 检查、完整 URL 过滤需要 Premium；基础域名放行与 DNS 代理不需要 | 优先复用合规 Hub；不要求 Premium 独有能力时评估 Standard。降级会失去 IDPS 等能力，必须经安全负责人确认 |
| ACR | AKS 与发布 runner 经 Private Endpoint 推拉镜像，禁用注册表公网访问 | **保持 Private Link 要求时必要**：Basic/Standard 不提供该功能 | 优先复用客户已有 Premium ACR；Basic/Standard 可用身份认证，但不能维持同样的私网访问边界 |
| Key Vault | 业务 Secret 管理；增强审计另用 HSM 保护的 CMK | **按用途区分**：Secret、私网访问和软件 Key 不要求 Premium；`RSA-HSM` 等 HSM Key 要求 Premium | 业务库选 Standard；仅在批准 HSM 要求时为独立密钥库选 Premium，不能把现有 HSM Key 直接改成软件 Key |

#### Front Door Premium：支付的是私有源站连接和托管 WAF 能力

本方案的链路为：公网客户端 → Front Door/WAF → Private Link → Private Link Service → AKS 内部负载均衡器。这里“私有”指源站连接，不代表客户端到 Front Door 的入口也是内网。

- **Private Link 是直接门槛。** [当前入口模板](../infra/edge/main.bicep)配置 `sharedPrivateLinkResource`，对接[私有源站服务](../infra/edge-origin/main.bicep)。Front Door Standard 无法沿用此连接方式。
- **微软托管规则也是门槛。** 模板启用 `Microsoft_DefaultRuleSet` 和 `Microsoft_BotManagerRuleSet`。Standard 支持自定义 WAF 规则，但不能等价替换这些微软托管规则。是否实际阻断仍取决于 WAF 模式与规则配置。
- **HTTPS、自定义域名、基本路由和 WebSocket 不是 Premium 独占理由。** 两个等级都具备相关能力；购买 Premium 也不会自动修复当前网关的 Codex/WS 兼容缺口。

若改为 Standard，通常需要提供公网可达源站，并结合 `AzureFrontDoor.Backend` 来源限制、`X-Azure-FDID` 校验、身份认证和 TLS 防止绕过；不能仅相信一个 Header。即使访问严格受限，也不再满足“源站无公网入口”的同一承诺。若客户只需企业内网访问，可使用现有私网入口，按需求评估内部 Application Gateway WAF v2，并非必须额外购买它；区域方案的容量、可用性及全球接入取舍需要重新计价和验证。

**客户解释：**“这笔费用主要用于让公网入口后面的网关源站保持私有，并使用微软维护的 Web 攻击规则。如果不需要公网入口，或企业已有等效入口，我们会重新选型，不强制新增 Front Door。”

#### Azure Firewall Premium：高级检测的条件选项，不是出站控制的默认门槛

Front Door WAF 主要保护进来的 Web/API 请求，Firewall 在本方案中主要控制工作负载向外建立的连接，两者不能互相替代。

| 所需能力 | Standard 是否具备 | 何时需要 Premium |
| --- | --- | --- |
| HTTPS 应用规则按域名/FQDN 放行、网络规则、日志、高可用 | 是；HTTPS 域名过滤不要求解密正文 | 仅这些要求不能证明 Premium 必要 |
| DNS 代理、网络规则 FQDN、威胁情报告警/拒绝 | 是，需正确配置与路由 | 仅这些要求不能证明 Premium 必要 |
| 网络入侵检测与防御 IDPS | 否 | 客户明确需要签名检测/阻断，并承担规则调优和告警处置 |
| TLS 检查、HTTPS 完整 URL 路径过滤 | 否 | 经批准需要解密经过防火墙的流量并检查；需额外完成证书信任、例外和兼容性验证 |

**当前实现不能夸大：**[出站防火墙模板](../infra/modules/firewall-egress/main.bicep)配置 Premium，`intrusionDetection.mode` 为 `Alert`，威胁情报也为 `Alert`，HTTPS 应用规则为 `terminateTLS: false`。因此已配置的 Premium 独有用途是 IDPS 告警能力，而非 IDPS 阻断或 TLS 正文检查；未解密的 HTTPS 正文不能据此宣称已完成内容检测。域名/网络规则仍能按已配置策略拒绝流量，不能把“IDPS 仅告警”误解为所有规则都不阻断。

若客户只要求批准域名放行、DNS 代理和网络日志，**建议评估 Standard，而不是默认新建专属 Premium**。Basic 也具备部分应用域名过滤，但缺少 DNS 代理、网络规则 FQDN 和威胁情报拒绝等能力，不能仅按价格直接替换当前设计。NSG、NAT Gateway 或 Private Endpoint 也不等价于域名级出站治理。

TLS 检查需管理 CA/Key Vault、客户端信任、证书固定或双向 TLS 例外，并验证流式连接、性能与隐私授权；购买 SKU 不会自动启用。所有检测只覆盖实际路由经过防火墙的流量，不自动覆盖所有 Private Endpoint 或同网段访问，也不能替代 Prompt 审计、DLP 或 Guardrail。

**客户解释：**“出站控制需要保留，但不代表必须购买最高等级。先复用企业现有防火墙；只有明确需要 IDPS、TLS 解密检查等高级能力时，才为 Premium 增量付费。”

#### ACR Premium：支付的是镜像仓库的私网边界

- **Private Link 是直接门槛。** [当前 ACR 模板](../infra/modules/container-registry/main.bicep)选择 Premium 并默认禁用公网访问；Basic 和 Standard 不支持 Private Endpoint，不能只改 SKU 后保留同一条私网推拉路径。
- **私有镜像不等于私网仓库。** Basic/Standard 也可通过 Entra 身份认证保护非公开镜像，但访问端点仍不同于 Private Link；官方 SKU 表中的注册表公网 IP 网络规则同样属于 Premium 功能，不能把“Basic + ACR IP 白名单”当作等价替代。
- **不以未用到的能力解释费用。** 此处不是因镜像数量大，也不是因 Entra 登录、RBAC、镜像摘要固定或外部 Cosign 签名验证必须 Premium。地理复制、客户管理密钥等另有使用条件，不作为当前小型单区域部署的默认理由。

优先评估复用企业现有 Premium ACR，通过仓库权限隔离并分摊容量、吞吐和运维成本；复用不等于免费，也不代表当前专用 ACR 工作流无需适配。AKS 和发布 runner 均须验证私网 DNS、网络路径及最小权限，不能为适配公网 runner 而临时长期开放仓库公网。

**客户解释：**“约 $50/月的这一档，主要是为了让应用镜像只能通过批准的私网路径交付。Basic 也能做身份认证，但不支持这条私网链路；已有企业私网镜像仓库时优先复用。”

#### Key Vault Premium：HSM 密钥才是必要条件，不是存放 Secret 的默认等级

**当前把业务 Secret 库统一写成 Premium，缺少已确认的功能必要性，应在选型上更正。** 但不能把增强审计库也一起归为多余配置，因为两类库保存的对象和密码运算方式不同。

| 库的用途 | 当前模板事实 | 建议等级与理由 |
| --- | --- | --- |
| LiteLLM 后端 Secret 库 | [公共 Vault 模块](../infra/modules/key-vault/main.bicep)固定 `premium`，配置 Secret 读取权限，没有创建 HSM Key | **Standard 可满足当前已实现用途**；Master Key、Salt 等虽名称含 Key，作为 Secret 保存不等于 HSM 密码学 Key |
| API/admin 代理 Secret 库 | [代理基础设施](../infra/proxy-foundation/main.bicep)仍保留两库；API已改双凭据，不再保存/挂载用户内部Key；admin仍保存管理凭据、OIDC/会话秘密 | **Standard 可满足管理Secret用途**；API旧库及权限是否退役应核对其他依赖，不能把停止挂载当作资源或角色已删除 |
| 增强审计 CMK 库 | [审计基础设施](../infra/audit-foundation/main.bicep)明确创建 `RSA-HSM`、3072 位、`wrapKey/unwrapKey` 密钥 | **保持该 HSM 设计时必须 Premium**；必要性来自硬件保护类型，不是“CMK”三个字或 3072 位长度本身 |

Standard 同样支持 Secret、证书管理、软件保护的 RSA/EC Key，以及 Private Endpoint、禁用公网、Entra/RBAC、托管身份、软删除和清除保护。这些措施不是 Premium 专属。Standard 的 Secret 同样受到静态加密保护，不能向客户解释成“Standard 明文保存、Premium 才加密”。

Premium 的 HSM Key 用于在硬件保护边界内执行密码运算；把普通 Secret 放进 Premium 并不会自动使其成为不可导出的 HSM Key。挂载给应用的 Secret 或证书私钥仍会由获授权的工作负载读取。**Key Vault Premium 与独立的 Managed HSM 服务也不是同一种资源或计费模式。**

**成本必须分开解释：**

- 仅有 Secret 读写时，两等级官方公开操作单价相同，均为 `$0.03/10,000` 次；不能仅切换 SKU 就宣称节省固定月费。该页面价仅作计费口径核对，不是目标订阅正式报价，也不更新本文历史合计。
- HSM Key 存在 Key 月费和密码操作费；当前审计密钥为 **3072 位高级 HSM Key**，应按对应档位、计费 Key/版本及操作量核价，不能套用 2048 位 Key 的单价。自动轮换和保留旧版本也需纳入生命周期预算。
- 原表“1 个 Key Vault、7 个 PE”是历史简化口径。当前foundation仍创建后端、API、admin三个业务Vault，启用增强审计还会增加独立CMK Vault。API双凭据不再需要用户内部Key存储，但此次没有删除旧API Vault/PE或修改foundation资源数量，不能提前计算节省。按实际保留的PE、日志和操作量重列BOM，也不因使用Standard而省掉PE费用。

第一阶段原生 Spend Logs 方案本身不要求新增独立 L3 HSM 库；是否仍有其他获批的 CMK/HSM 要求，应单独确认。若已有数据依赖某个审计密钥，取消增强组件或调整保护类型须评估解密、备份、保留和恢复依赖，不能删除旧 Key 或直接改 `kty`/SKU。业务库改 Standard 也应先清点实际 Key 和证书类型并验证兼容性；本次只更正文档，模板仍未调整。

**客户解释：**“保存应用凭据和证书，Standard 已能提供私网、身份权限和防删除保护。只有明确要求密码学密钥由 HSM 保护时才选 Premium；这类 Key 的费用单列，不把所有业务库默认升级，也不把普通 Secret 改等级包装成固定月费节省。”

#### 成本与实施决策怎样表达

| 客户确认的条件 | 建议方案 | 本文金额应如何使用 |
| --- | --- | --- |
| 公网 API + 无公网源站 + 私网镜像交付；已有合规 Hub | 保留 Front Door/ACR Premium，复用 Hub | `$1,206.11/月` 仅为原配置固定费参考，另计共享平台分摊、容量和按量费用 |
| 同上；无 Hub，且不需要 IDPS/TLS 检查 | 评估专属 Firewall Standard | 新固定参考 = `$1,206.11 + Standard 小时单价 × 730`；单价本次未查询，不给出新的总额，数据处理等另计 |
| 明确需要 IDPS 或 TLS 检查等高级能力 | 评估共享或专属 Premium，并配置验收 | 专属场景 `$2,483.61/月` 是原估算，不含完整高级检测运营及新增用量成本 |
| 仅内网访问，或已有企业入口/镜像平台 | 重做入口与共享资源 BOM | 不能机械扣掉某项就宣称等效，替代入口、连接、分摊和实施费用均应重算 |
| Key Vault 仅管理业务 Secret，未要求 HSM Key | 业务库建议 Standard，保留权限和网络隔离 | Secret 操作单价不因切换等级下降；按实际 Vault/PE/日志数量重算，不虚构固定费节省 |
| 增强审计明确要求 RSA-HSM CMK | 独立 Premium Vault | HSM Key 月费、操作及生命周期费用单列，未纳入原表的 `$0.00` |

以上是选型建议，不会自动更改部署：当前模板仍固定使用上述 Premium SKU。采用 Firewall Standard、业务 Key Vault Standard、共享 ACR 或其他入口，需同步调整 IaC、策略、workflow 和验收门禁；不能只修改成本表或 SKU 字符串。Front Door Premium 转 Standard 也不是无缝原地降级，应按重建/迁移及切流评估。

客户批准前应留下四项证据：明确的控制要求、选定 SKU 的官方功能依据、现有平台能否复用、正反向验收结果。尤其应验证源站无法绕过入口、未批准出站被拒绝、ACR 公网不可用且私网推拉成功；若采购 IDPS/TLS 检查，另验其实际模式与检测效果。

**官方功能依据（2026-09-10 核对，非本次价格查询）：**

- [Front Door Standard/Premium 功能比较](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-cdn-comparison)：Private Link、微软托管规则与自定义 WAF 的等级差异，以及降级限制。
- [Azure Firewall 各 SKU 功能比较](https://learn.microsoft.com/en-us/azure/firewall/features-by-sku)：Standard 的 FQDN/DNS 能力与 Premium 的 IDPS、TLS、URL 检查能力。
- [ACR SKU 功能与限制](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-skus)：Private Link、公网 IP 规则和身份权限的等级边界。
- [Key Vault 密钥类型与保护方式](https://learn.microsoft.com/en-us/azure/key-vault/keys/about-keys)：软件 Key 与 Premium HSM Key 的区别，以及 Managed HSM 的独立资源边界。
- [Key Vault Private Link](https://learn.microsoft.com/en-us/azure/key-vault/general/private-link-service)：Vault 私网接入及验证；Private Link 并非仅限 Premium。
- [Key Vault 官方计费表](https://azure.microsoft.com/en-us/pricing/details/key-vault/)：另核对 Secret 两等级同价、HSM Key 与轮换的计费方式；不代表已重算目标区域/订阅报价。

## 5. 按量计费项

用户选择“只列单价”，因此以下项目不进入固定月费合计。

| 服务 | Azure Retail 单价/口径 | 成本驱动因素 | 备注 |
| --- | ---: | --- | --- |
| Front Door Premium 请求 | 约 $0.01/10K 请求 | API请求量、WAF处理 | Zone 1 meter；不同阶梯可能变化 |
| Front Door Premium入口数据 | $0.02/GB | 客户端上传 Prompt/文件 | 以 Retail meter为准 |
| Front Door Premium出口数据 | 约 $0.01-$0.08/GB | 响应、区域和用量阶梯 | Zone 1 多阶梯 meter |
| Azure Firewall Premium数据处理 | $0.016/GB | 受控 egress/ingress | 不含 `$1.75/小时` 部署费 |
| Private Link数据处理 | $0.01/GB ingress + $0.01/GB egress | PE流量 | Endpoint小时费已计固定成本 |
| Key Vault普通操作 | $0.03/10K 操作 | Secret读取/写入 | Premium与Standard普通操作同 meter |
| Key Vault高级 Key操作 | $0.15/10K 操作 | HSM/高级加密操作 | HSM key本身另计 |
| PostgreSQL超额备份 | $0.095/GB/月 | 超出免费备份额度的数据 | PITR窗口和变更率影响明显 |
| Log Analytics ingestion | $2.99/GB | AKS、WAF、Firewall、PG、Redis、LiteLLM 日志量 | 最大可控用量项之一 |
| Log Analytics额外保留 | $0.13/GB/月 | 超过包含期的热日志 | Archive/Search另计 |
| Microsoft Sentinel | $5.59/GB analysis | 安全分析数据量和计划 | 需确认与 Log Analytics 的组合计费，避免重复估算 |
| Managed Prometheus | 按 samples ingestion/query | 指标基数、抓取频率、保留 | Pricing MCP返回精度不足，正式报价用 Calculator |
| Azure AI Content Safety | 按文本/图片交易 | 请求数、文本块数、Prompt Shields | Pricing MCP服务族解析失败，正式报价用 Calculator |
| Azure AI Language PII | 按文本记录/字符计费层级 | 审计/Guardrail文本量 | 需按目标 SKU和区域用 Calculator确认 |
| 原生 Spend Logs 正文（基础版） | 沿用 PostgreSQL 计算、存储、备份和实际适用的 IO 计费 | 实际记录大小/数量、留存、查询、WAL、清理及 HA | 无额外自建采集服务；不是存储免费，不与已计 PG 费用重复相加 |
| ADLS/Blob 自建 L3（可选增强） | 按容量、写入、读取、检索和出口 | 独立正文、分片、保全、读取/恢复用量 | 不作为基础版默认项；选择后另计服务、PE、HSM 等资源 |
| Azure OpenAI/Foundry | 按模型 Token/图片/音频/工具 | 实际模型使用 | 两版共同业务成本，未纳入基础设施对比 |

## 6. 员工与 Agent 上下文审计成本影响

### 6.1 基础版采用原生 Spend Logs

第一阶段选择 LiteLLM 的 `store_prompts_in_spend_logs` 能力保存获批输入输出，不要求先建设自建 L3。`SEC-21` 原路线中的独立分片、恢复、原文双审批和案件保全属于可选增强要求，不自动等同于这次受控留痕的目标。

| 成本部分 | 第一阶段落点 | 应计入的增量 |
| --- | --- | --- |
| 调用元数据及批准的正文 | 私有 PostgreSQL 的 Spend Logs | 计算、在线存储、实际 IO、索引及空间维护；与 Key/预算控制数据共库的负载影响 |
| WAL、HA 与数据库备份/PITR | 原数据库配套能力 | 写入变化、复制、备份增长及超额备份费用；不能只计算消息文本大小 |
| 查询与清理 | 受控原生 UI/API 或经验证的受控查询路径 | 查询负载、清理调度及失败告警；原生受控读取已有代码与隔离测试；云端读者/字段权限与负载仍须验收 |
| 基础监控 | 现有运维平台或 Azure Monitor | 健康、容量、写入异常及清理元数据；不把正文同时摄入高价日志平台 |
| 自建 L3、Blob/HSM、恢复与治理 | 基础版不默认部署 | 明确选择增强方案后单独估算；不能沿用基础版费用宣称已包含强取证 |

OpenTelemetry collector 用于遥测，不是保存 Prompt 的前提。PII/DLP/Content Safety 也不是原生正文开关附带的能力；若另行批准，按实际扫描用量计价，不把自建采集器脱敏当作原生 Spend Logs 已有保证。

### 6.2 容量与报价输入

建议用获批隔离试点采集7至14天样本，同时覆盖峰值与长上下文；不是先无限制记录全公司数据再测量。至少收集：实际日志记录数、每条记录及各正文列的平均/峰值字节数、历史上下文重复量、工具参数/结果与多模态比例、查询频率、在线留存、备份/PITR期限、当前PG空闲空间及CPU/IO基线、WAL增长和清理耗时。重试/fallback可能形成多个记录，不能用提问次数代替日志行数。

$$
	ext{正文基础容量} \approx \text{每日日志记录数} \times \text{每条输入输出平均字节数} \times \text{在线保留天数}
$$

例如每天10,000条、每条100 KB、保留7天，原始正文约7 GB（十进制）。这不包含JSON/编码、`proxy_server_request`等补充副本、索引、WAL、HA、备份及空闲空间，也不是应直接购买7 GB存储的结论；应以实际数据库增长复核。不能把正文逻辑大小直接乘PG存储单价，当作完整审计月费。

基础版月费应按“核定后的基础资源 + 正文引起的PG/备份/维护增量 + 实际网络与监控用量 + 共享平台分摊”估算。已有PG容量充足时不重复计费；需要扩容或升规格时计算前后差额。原固定合计未计L3正文容量，也未更新现有Vault/PE数量，本轮不生成未经询价和压测的新总价。

### 6.3 启用、留存与回退边界

- 方案目标是获批后显式启用原生正文记录；**静态默认配置仍关闭，原生发布/静态门禁已有实现；云端启用须批准采集范围并完成实际验收**。本节不是直接执行`true`即可上线的操作指令，更不能删除强审计binding或伪造passed绕过现有门禁。
- 启用前验证配置优先级、环境变量、客户端no-log等覆盖路径和实际日志字段；禁止原生与自建L3无意重复落正文，普通日志/Trace/artifact不得复制正文或凭据。
- 在线留存可先评估7天，最终由客户政策决定；原生行级清理可能连同费用明细删除，不能假定正文与元数据可以各自设置独立期限。备份保留另定，在线删除不会清除旧备份，恢复后须重新检查留存及访问权限。
- PG设置容量趋势、增长率、写入和清理失败告警，70%/85%仅是待验证的阈值起点；预留扩容与维护时间。关闭正文采集通常只影响新记录，不删除历史，不应破坏Key、预算或控制数据。
- 原生日志允许存在延迟、截断或故障缺口，不承诺返回前持久化、零丢失或客户端完整接收。仅保存网关实际收到的模型交互，不是Agent全部本地活动归档。

### 6.4 何时另报自建 L3

只有客户明确要求独立正文存储、持久化确认后转发、逐次原文审批、案件保全或更强的取证边界时，才另行评估自建L3或合适的产品能力。其BOM包含正文Blob容量/事务、PE/DNS、批准使用的HSM Key、采集与恢复计算、治理及运维成本，不能只报Blob容量。已有L3数据及加密Key不能因切换方案自动删除，须按原保留/保全和恢复政策审批退役。

## 7. 成本敏感性

### 7.1 主要固定成本开关

| 决策 | 月成本影响 | 安全/架构影响 |
| --- | ---: | --- |
| 复用企业 Hub Firewall | 约 -$1,277.50 | 推荐在客户已有合规 Hub 时复用，不建议直接取消 egress治理 |
| 仅存 Secret 的 Key Vault Premium 改 Standard | 普通 Secret 操作单价相同，无可宣称的固定月费节省 | 保留网络/RBAC/防删除控制；HSM Key 库不能照此直接降级，PE/日志费用仍需计入 |
| 专属 Firewall Premium 改 Standard | 差额 = `730 ×（1.75 - Standard 每小时单价）`；另核数据处理差额 | 仅不要求 IDPS/TLS 检查等独有能力时评估；需改模板、策略与门禁，本次未重询价格 |
| AKS 3 节点改为 2 节点 | 约 -$101.71 | 降低跨区/维护冗余；需重新验证 PDB和容量 |
| ACR Premium降级 Basic | 约 -$45.00，仅注册表基础费 | 不能保留 Private Link；若私网要求不变则不可等价降级，优先评估复用已有 Premium ACR |
| Front Door Premium取消或替换 | 移除原 $330 基础费，替代入口费用另计 | 纯内网可评估现有私网入口或内部 App Gateway WAF v2，并非必须新增；Standard 无法沿用私有源站连接。公网 App Gateway WAF_v2替代见7.4 |
| PostgreSQL取消 HA | 约 -$148.19 | 恢复数据库单点，不符合当前 P0目标 |
| Private Endpoint每增减 1 个 | 约 +/-$7.30 | 与私网依赖数量直接相关 |

### 7.2 可优化但不应牺牲基线的项目

- AKS节点和 PostgreSQL稳定后评估 1 年/3 年 Reservation 或 Savings Plan；
- 使用 HPA/Cluster Autoscaler在满足最小可用副本前提下降低空闲节点；
- 基础版正文只进入获批的原生Spend Logs，运维平台只收必要元数据，避免正文重复进入Analytics Logs；增强L3另行选择；
- 对 WAF、Firewall、Private Link和审计流量建立月度基线与预算告警；
- 复用客户已有 Hub、DNS、Log Analytics、Sentinel和CI/CD平台；
- 对开发/测试环境使用更小规格、非 HA数据库和工作时间启停，但生产验收环境不能因此失真。

### 7.3 开启多可用区的增量（West US3，2026-10-21零售价）

按当前参考环境（System/User池各2台`Standard_D4s_v4`、PG未开HA）估算`stage4Aks.availabilityZones`选3个区域的固定增量。单价取自Azure Retail Prices API的Pay-As-You-Go Linux/Consumption价格，不含协议价与税费：

| 项目 | 变化 | 单价 | 月增量 |
| --- | --- | ---: | ---: |
| AKS节点 | 两个池的最小节点数2→3，各+1台`Standard_D4s_v4` | $0.192/小时 | 约+$280.32，另加每节点OS盘；池已扩到3台及以上时无增量 |
| PostgreSQL | `ZoneRedundant`增加同规格备用实例，以`Standard_D2s_v3`（2 vCore）、128 GB为例 | $0.0855/vCore·小时；存储$0.115/GB·月 | 约+$124.83计算+$14.72存储 |
| 可选L3 | 内容Blob和spool盘由LRS改ZRS | 按容量 | 小容量时差额很小，按实际容量核算 |
| Firewall、公网IP、ACR、Managed Redis（已开HA）、内部LB | 平台在支持可用区的区域自动跨区 | - | 无额外固定费用 |

合计约+$420/月（不含OS盘、L3和跨区数据传输）。可用区之间的数据传输是否收费以当时的带宽定价页为准，未计入；节点和数据库稳定后可用Reservation/Savings Plan抵消部分增量。

### 7.4 以公网 Application Gateway WAF_v2 替代 Front Door 的增量（West US3，2026-10-27零售价）

`parameters.edge.type=applicationGateway`时不再部署 Front Door Premium 与两条 Private Link Service，改为区域 Application Gateway WAF_v2。单价取自Azure Retail Prices API的Pay-As-You-Go价格，不含协议价与税费：

| 项目 | 变化 | 单价 | 月费用 |
| --- | --- | ---: | ---: |
| Application Gateway WAF_v2 固定费 | 新增 | $0.36/小时 | 约$262.80 |
| 容量单位 | 模板最少2个实例，每实例按至少10个容量单位计 | $0.0144/容量单位·小时 | 约$210.24起，随流量自动扩展 |
| Standard公网IP | 新增1个 | 按区域公网IP单价 | 数美元 |
| 网关证书Key Vault（Standard） | 新增1个，只存两个PFX副本 | 按操作计费 | 低于$1 |
| 网关证书Vault私有端点 | 新增1个 | $0.01/小时 | 约$7.30 |
| Front Door Premium基础费 | 移除 | — | 约-$330，请求与流量费另计 |

合计固定部分约+$151/月（$481对$330，不含两边的请求/流量费、公网IP和日志）。网关证书Vault单独设置，是因为Application Gateway读取Key Vault证书依赖“允许受信任的Microsoft服务”例外，原证书Vault保持关闭该例外。Front Door按请求与出站流量计费，Application Gateway按容量单位（连接数、吞吐、计算量取最大）计费，长连接和SSE会占用容量，需按实际并发重新估算。切换期间两种入口同时存在、同时计费。

## 8. 不在本估算中的费用

- 客户自有Entra认证代理的开发运维成本，以及经批准的Azure AI Guardrail/观测服务成本；本方案不包含LiteLLM Enterprise许可证；
- Microsoft Entra ID P1/P2、Conditional Access、PIM、Purview等用户/租户许可；
- Azure支持计划、税费、EA/MCA折扣、Azure Hybrid Benefit；
- 域名、公共证书或客户自有 PKI；
- CI/CD runner、代码扫描、签名服务和人员实施运维成本；
- 跨区域、跨可用区、Internet和 ExpressRoute/VPN数据传输；
- Azure OpenAI/Foundry模型消费与配额；
- 多区域 DR、Geo Backup和第二套 Warm Standby环境；
- 原生Spend Logs正文引起的实际PG扩容、WAL/备份及查询维护增量（原固定表尚未核定）；
- 可选自建L3全文审计和Legal Hold的实际容量与分析成本。

## 9. 价格依据

本文已通过 Azure Pricing MCP 查询以下 Retail meter：

| 服务/SKU | Retail meter | 单价 |
| --- | --- | ---: |
| VM `Standard_D2s_v3` Linux | D2s v3 | $0.117/小时 |
| VM `Standard_D2s_v5` Linux | D2s v5 | $0.112/小时 |
| AKS Standard | Standard Uptime SLA | $0.10/小时 |
| Premium SSD P4 LRS | P4 LRS Disk | $5.28/月 |
| Premium SSD P10 LRS | P10 LRS Disk | $19.71/月 |
| ACR Basic | Basic Registry Unit | $0.1666/天 |
| ACR Premium | Premium Registry Unit | $1.6666/天 |
| Front Door Premium | Premium Base Fees | $330/月 |
| Azure Firewall Premium | Premium Deployment | $1.75/小时 |
| PostgreSQL Ddsv5 2 vCore | vCore | $0.203/小时/实例 |
| PostgreSQL Flex Storage | Storage Data Stored | $0.14/GB/月 |
| Azure Managed Redis B0 | B0 Cache Instance | $0.02/小时 |
| Private Endpoint | Standard Private Endpoint | $0.01/小时 |
| Standard Public IPv4 | Standard IPv4 Static Public IP | $0.005/小时 |
| Private DNS | Private Zone | $0.50/Zone/月 |
| Log Analytics | Analytics Logs Data Ingestion | $2.99/GB |
| Microsoft Sentinel | Pay-as-you-go Analysis | $5.59/GB |
| Key Vault | Operations | $0.03/10K |

零售价会变化，且不同区域、计费层级和协议价可能不同。正式预算前应：

1. 用客户目标订阅和区域重新运行 Azure Pricing MCP；
2. 用 Azure Pricing Calculator复核 Pricing MCP未返回或精度不足的项目；
3. 从 Azure Cost Management导出现网 30-90 天实际账单；
4. 用压测/试点得到原生Spend Logs的PG/备份增量、元数据日志及流量用量；Guardrail、自建L3仅在选用后单列；
5. 按 EA/MCA价格、Reservation/Savings Plan、税费和共享平台分摊形成最终 TCO。

## 10. 审查时需要确认

| 决策 | 当前估算 | 待确认内容 |
| --- | --- | --- |
| 当前 AKS节点数 | 2 | 客户现场是否一致 |
| 安全版 AKS节点 | 3×D2s_v5 | 压测后是否需 D4s_v5或更多节点 |
| Front Door | Premium，见第 4.2 节 | 是否需要公网入口、源站 Private Link 与微软托管 WAF；能否复用企业入口 |
| Firewall | 原估算为专属 Premium/共享两种；Standard 待评估 | 是否真正需要 IDPS/TLS 检查，是否有合规 Hub；告警与阻断的验收要求 |
| ACR | Premium，见第 4.2 节 | 是否坚持关闭公网并使用 Private Link；能否复用现有注册表与私网 runner |
| Key Vault | 业务库建议 Standard；RSA-HSM 审计库保持 Premium，代码未改 | 是否确有 HSM 要求；实际 Vault/PE 数量、Key/版本及证书类型、操作和恢复依赖 |
| PostgreSQL | D2ds_v5 2 vCore HA + 128 GB | RTO/RPO、连接数、IOPS、增长率 |
| Redis | Balanced_B0 | 内存、连接数、HA/SLA要求 |
| Private Endpoint | 7 个 | 实际模型资源数和共享方式 |
| Private DNS Zone | 6 个 | 是否复用企业中心 DNS Zone |
| 日志/Sentinel | 只列单价 | 预计 GB/月和留存期 |
| AI Guardrail | 只列成本驱动 | 月交易量、文本块和目标 SKU |
| 基础版内容留痕 | 原生Spend Logs；发布/读取已有隔离证据，云端采集与生产验收另行批准 | 批准范围、实际主体/Key归因、查询权限、PG容量、清理、备份与故障策略 |
| 自建L3增强审计 | 非基础版默认项，另行批准报价 | 是否需要独立存储、可靠交付、原文双审批或保全；既有数据的保留和退役要求 |