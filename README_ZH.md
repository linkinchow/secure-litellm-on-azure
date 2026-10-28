# 安全增强版 LiteLLM on Azure

[English](README.md) | [客户迁移指南](docs/customer-migration-guide-zh.md)

面向客户交付的 Azure 上 [LiteLLM](https://github.com/BerriAI/litellm) 网关部署与迁移项目。方案将 Azure 基础设施即代码、Kubernetes 部署组件、LiteLLM原生认证或可选客户自有Microsoft Entra代理、审计治理与分阶段交付流程整合在同一仓库。

面向客户运维，交付目标同时覆盖已有 LiteLLM 网关的分阶段增强迁移和空白环境的从零部署。客户提供必要身份、资源ID、域名及决策，部署和验证应通过workflow完成，不要求客户自行编写应用清单或测试代码。模型路由仍受已授权Azure OpenAI资源的配额约束，不绕过服务限额。

> **当前参考（2026-10-09）**：West US 3（`westus3`）greenfield测试网关使用Private AKS及LiteLLM原生认证。用户已验证Admin用户名/密码登录和virtual key的Codex Responses推理。这是有边界的测试证据，不代表生产全面验收、一键升级或全部协议、故障场景及模型同步执行均已验证。

## 目标架构

![LiteLLM Azure 安全架构图](images/litellm-azure-security-architecture.png)

图中包含可选能力与生产增强路径，不代表所有组件均已部署或验收；范围及验证边界详见[安全架构设计](docs/litellm-azure-security-hardening-zh.md)。

**第一阶段审计决策（2026-09-10）**：采用原生Spend Logs将获批的Prompt/Response保存在私有PostgreSQL。原生配置生成及按模式区分的发布/证据检查已实现；静态Base清单仍关闭正文。实际留存、查询权限、清理、容量及故障验收需独立证据。自建L3采集、正文Blob/HSM及恢复治理服务是可选增强项，不作为所有客户的首发前提。详见[基础版方案](docs/litellm-content-audit-phase1-customer-brief-zh.md)及[部署指南](docs/customer-deployment-workflows-zh.md)，不得跳过门禁启用。

```mermaid
flowchart TB
    apiClients["API 客户端"]
    admins["使用批准公网出口的管理员"]
    apiEdge["llm-api.&lt;客户域名&gt;<br/>Front Door API endpoint / WAF"]
    adminEdge["llm-admin.&lt;客户域名&gt;<br/>Front Door Admin endpoint / WAF 来源白名单"]
    apiPls["API Private Link Service"]
    adminPls["Admin Private Link Service"]

    subgraph aks["Private AKS"]
        apiIngress["私有 API 入口"]
        adminIngress["私有 Admin 入口"]
        litellm["原生 LiteLLM<br/>Virtual key / Admin 密码登录"]
        apiIngress --> litellm
        adminIngress --> litellm
    end

    apiClients --> apiEdge --> apiPls --> apiIngress
    admins --> adminEdge --> adminPls --> adminIngress
    litellm -->|"Workload Identity / Private Endpoint"| models["Azure OpenAI / Microsoft Foundry"]
    litellm --> postgres["私有 PostgreSQL Flexible Server<br/>预算 / 原生 Spend Logs / 获批正文留存"]
    litellm --> redis["Azure Managed Redis<br/>共享路由与限流状态"]
    backendVault["私有后端 Key Vault"] -.->|"CSI 挂载应用 Secret"| litellm
    certificateVault["独立证书 Key Vault"] -.->|"TLS 证书"| apiIngress
    certificateVault -.->|"TLS 证书"| adminIngress
    acr["私有 ACR<br/>镜像 digest 锁定"] -.-> litellm
    litellm -.->|"仅必要元数据"| monitoring["Azure Monitor / Log Analytics"]
    litellm -.->|"可选增强"| l3["独立 L3 审计存储"]
```

| 领域 | 设计与实现范围 |
| --- | --- |
| 网络隔离 | Private AKS、Private Endpoint/DNS、受控出口和默认拒绝网络策略 |
| 身份授权 | 当前native API使用virtual key；Admin为LiteLLM用户名/密码fallback登录，不是用户Entra SSO。Admin WAF Prevention限制精确批准的公网`/32`和`/128`出口；Azure模型访问使用Workload Identity |
| 数据与秘密 | Entra-only PostgreSQL/Redis；私有backend Vault与certificate Vault分离；备份与恢复控制须验收 |
| 运行基线 | LiteLLM `1.104.8`固定源码构建及digest交付；非Root/只读容器、高可用及路由组件 |
| 审计与观测 | 原生Spend Logs配置及按模式区分的门禁、元数据监控；L3、Trace及Guardrail组件按需选用 |
| 客户交付 | Environment配置、OIDC、阶段证据门禁、IaC预览及离线验证 |

该测试参考的只读管理面核对显示Private AKS为`Running/Succeeded`、Azure CNI overlay、Azure RBAC、禁用本地账号、启用OIDC及Workload Identity；System/User各2台`Standard_D4s_v4`。Front Door Premium使用独立API/Admin endpoint及PLS，回源至隔离native ingress平面。Admin WAF在Prevention模式以否定`SocketAddr` IP匹配阻断白名单外来源，已配置批准的IPv4和IPv6出口。ARM状态不能证明Kubernetes rollout、模型同步或推理。

补充只读核对：PostgreSQL 16为`Ready`，启用Entra认证、禁用密码认证及公网访问；Redis Enterprise（`Microsoft.Cache/redisEnterprise`）为`Balanced_B0`，默认数据库禁用Access Key认证，客户端协议加密，端口`10000`。Redis集群`publicNetworkAccess`为空不能证明公网访问已启用或已禁用。ACR为Premium，禁用Admin User及公网访问；两个Key Vault均启用RBAC并禁用公网访问。Admin白名单为三个批准的精确IPv4 `/32`及一个IPv6 `/128`，不是仅IPv4策略。

以上是有边界的当前观察及目标能力，不是所有客户的部署默认或生产就绪声明。方案不依赖LiteLLM Enterprise；当前路径没有APIM运行时、App Service公网容器、共享Admin/API代理或旧West US直连HTTP IP网关。Entra代理保留为替代分支，不是当前测试网关。

## 客户从这里开始

需要受控更新运行参数或关闭共享环境凭据的 Admin 登录入口时，使用[运行配置更新手册](docs/litellm-runtime-config-runbook-zh.md)。获批设置通过现有后端滚动更新并持久化，不重新部署 Azure 基础设施。

1. 阅读[客户部署与验收指南](docs/customer-deployment-workflows-zh.md)，选择已有网关迁移或从零部署；迁移细节另见[客户迁移指南](docs/customer-migration-guide-zh.md)。
    无法运行GitHub Actions时，可使用支持migration及greenfield的受限[Stage0–9本地手工执行包](local_execution/README_ZH.md)。显式native-auth分支及其发布门禁见[Stage2–9指南](local_execution/stage2-9-guide-zh.md)；仅延期Entra不等于获准启流量。
2. fork本仓库，公开fork也可使用；保护默认分支，创建所选Environment并配置Azure OIDC身份。客户操作仅手动运行，不让外部PR使用私网runner。
3. 使用[迁移配置模板](config/customer.example.json)或[新建配置模板](config/customer.greenfield.example.json)填写`CUSTOMER_CONFIG_JSON` Environment Secret，并添加用于附件加密的独立`WORKFLOW_ARTIFACT_KEY` Secret；新建不填写`legacy`。按指南配置OIDC及[私网runner](docs/customer-private-runner-preparation-zh.md)，无需自动建机平台。
4. 先运行 **Customer staged migration**：阶段`0`、模式`config-check`、组件`none`；再运行 **Customer private runner checks**，`check_target=false`。审核解密结果后再操作资源；单人运维使用`draft → confirm`逐项人工确认，不再手写报告JSON。
5. 迁移在旧网关旁建设隔离新环境；新建Stage0使用`bootstrap → network`，跳过不适用的Stage1。两条路径的实际部署和发布分别经过客户审批，不以基础设施部署成功代替应用验收。

原迁移检查workflow不部署资源；实际执行使用新增的[客户部署与验收工作流](docs/customer-deployment-workflows-zh.md)，提供基础设施plan/deploy、私网备份/恢复和应用发布、单人或双人验收。DNS和旧环境退役不自动执行，运行时集成阻断项仍需完成。Master Key、Salt、数据库凭据和OIDC秘密保存在客户Key Vault，不写入配置JSON。

## 迁移阶段

| 阶段 | 客户里程碑 |
| --- | --- |
| 0-2 | 现状盘点、可恢复备份、旧环境最小加固和架构决策 |
| 3-5 | 供应链、隔离目标基础设施、私网与数据迁移演练 |
| 6-8 | HA/路由、原生认证或可选Entra代理、双域名、协议授权、原生正文留痕与监控；增强审计按需选用 |
| 9 | 批准试点、切流及验证回退窗口；资源退役另立变更 |

回退条件满足前保留旧数据库、网关、身份和Master/Salt路径。禁止将候选版本直接连接旧生产数据库执行自动schema migration。

## 仓库导航

| 路径 | 用途 |
| --- | --- |
| [config](config/customer.example.json) | 通用客户配置与证据示例，不含真实客户值 |
| [.github](.github/README_ZH.md) | 验证、分阶段迁移和镜像晋级workflow |
| [infra](infra/README_ZH.md) | Bicep平台、备份、监控、审计与边缘入口模板 |
| [deploy](deploy/README_ZH.md) | Kustomize基础清单、阶段组件与验证overlay |
| [auth-proxy](auth-proxy/README_ZH.md) | 可选客户自有Entra API/admin认证代理与增强审计治理代码，不是当前native路径 |
| [LiteLLM](LiteLLM/README_ZH.md) | 旧网关参考实现、运维手册及OSS回调适配器 |
| [tests](tests/README_ZH.md) | 离线检查、隔离运行时验证及显式执行的真实环境测试 |
| [scripts](scripts/customer_migration.py) | 客户预检、参数生成和验证工具 |
| [images](images/) | 纳入版本管理的架构图 |

模型发现/同步统一见[专用模型同步runbook](docs/litellm-model-sync-runbook-zh.md)；当前网关不要重跑旧部署脚本或启用UI数据库模型管理。

## 本地验证

需要Python 3.10+、Node.js 24、Azure CLI及Bicep、kubectl和make。OSS回调测试还需要Docker。在仓库根目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix auth-proxy --ignore-scripts
make validate-stage9
make validate-oss-callbacks
.venv/bin/python -m unittest tests.test_customer_migration tests.test_customer_templates tests.test_public_config
```

这些检查不部署资源或调用客户网关；依赖安装和未缓存镜像需要下载访问。真实协议、私网、Entra及Azure数据面验收需使用单独批准的环境，详见[测试说明](tests/README_ZH.md)。

## 上线边界与成本

- 新安全入口有明确的路由白名单。旧网关的图像、视频、WebSocket和Codex验证结果，不能替代新授权层的兼容性验收。
- 第一阶段采用原生Spend Logs，不默认要求自建L3。原生发布及按模式区分的证据检查已实现；实际查询权限、容量/留存及恢复仍须验收。原生日志不保证零丢失或不可变取证；选择增强L3时另行批准并完成专项验收。
- 当前测试路径已有私有入口及身份/数据接线；HA/故障行为、监控数据流及生产就绪仍须独立验收。不能将带占位符的validation overlay直接用于生产。

当前边界见[迁移指南](docs/customer-migration-guide-zh.md)、[本地Stage2–9指南](local_execution/stage2-9-guide-zh.md)和[安全架构](docs/litellm-azure-security-hardening-zh.md)。[代码收尾台账](docs/litellm-code-completion-backlog-2026-09-07.md)及[实施路线](docs/litellm-security-hardening-implementation-roadmap-zh.md)含历史工程背景，不是当前客户验收报告。

成本请结合[安全增强版BOM](docs/litellm-bom-cost-comparison-zh.md)与[Azure定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)评估。区域、HA、Firewall、Private Endpoint、边缘流量、数据库容量及审计留存均影响费用，旧单节点网关报价不适用于本架构。