# 客户部署与既有LiteLLM迁移执行手册：Stage0–9

> 核对日期：2026-10-09。本文是按当前workflow输入及控制代码核对的主操作手册，不是客户云上全流程已经验收的证明。
>
> 适用：已有LiteLLM on AKS，先加固旧环境，再并行新建、迁移、验证并通过独立新域名试点；最终迁移和停旧按客户目标另行批准。示例统一使用GitHub Environment `test`；客户实际用`prod`时须整套一致替换，不混用环境。
>
> 一期审计沿用2026-09-10的原生Spend Logs选择：针对高用量用户进行获批的工作用途抽查，原生Logs为主、私网PG只读查询补充。不是所有UI页面、所有Codex协议或独立L3平台都必须首发；必要管理操作和实际首发客户端仍须通过验证。
>
> 并行canary可以把已验收备份的恢复结果作为新环境基线，不要求旧系统停写；新旧数据库只在备份时点一致，之后不会持续同步。最终停写、最终目标库选择及跨库数据回退尚未形成完整workflow编排，仅在客户要求无损承接后续数据时按第5节执行。不能把未完成部分写成可以直接点击的按钮，不能用假passed跨过门禁。

阅读顺序：第1节辨认workflow → 第2节准备配置/身份/Runner → 第3节学会plan批准与附件审核 → 第4节逐Stage执行并发布并行canary → 第5节按需最终迁移/退役 → 第6节排错。资源实现细节和可选增强流程见[部署参考](customer-deployment-workflows-zh.md)，机器准备见[Runner指南](customer-private-runner-preparation-zh.md)。客户资源值、日志正文和现场记录只保存在受控位置，不填进本文或公共Git。

客户无法运行GitHub Actions时，不混用本手册的run ID、Environment和artifact步骤；migration与greenfield均可改走独立的[Stage0–9本地手工执行包](../local_execution/README_ZH.md)。本地路径使用自己的plan哈希、UAMI/现有Azure会话及客户Cosign密钥。greenfield跳过旧集群备份/恢复、Stage1和`stage5-restore-target`，但仍须创建新目标网络、初始化新库/密钥并完成适用的Stage2–9门禁。Entra代理路径Stage7延期时不能发布Stage8应用或启用Stage9流量；显式native-auth路径则不执行Stage7，完成Stage6原生认证及入口验证后继续Stage8/9。

## 当前运行状态与路径选择（2026-10-09）

当前实际运行的是**West US 3 greenfield、native-auth、dev/test canary**：Private AKS为`Running/Succeeded`，Azure RBAC、禁用本地账号、WI/OIDC均开启，CNI Overlay，2个system加2个user节点，SKU `Standard_D4s_v4`；API/Admin分别通过Front Door Premium、PLS及私有入口提供服务。用户已确认Admin `/fallback/login`用户名/密码和virtual key的Codex Responses推理可用，用户侧Entra SSO延期。

Admin使用**后台Vault**中独立的`litellm-ui-password`，不是Master Key或证书Vault秘密；WAF当前配置3个逐项批准的公网IPv4 `/32`及1个IPv6 `/128`。只读管理面回读确认PostgreSQL 16 `Ready`、Entra认证启用/密码认证禁用/公网禁用；Redis为`Microsoft.Cache/redisEnterprise Balanced_B0`，default数据库禁用Access Key认证、使用Encrypted协议/端口10000，但集群`publicNetworkAccess=null`不证明公网开启或关闭。ACR Premium禁用admin账号和公网，后台及证书Vault均启用RBAC并禁用公网。源镜像固定1.104.0；秘密、数据面身份、运行、审计和恢复门禁仍有效，不据这些有限实测宣称完整Stage0–9或production签收。

下文既有迁移的备份/旧环境加固/最终数据对账及Entra代理步骤保留，不用于要求当前greenfield补做旧资源操作。当前原生本地执行顺序见[Stage2–9指南](../local_execution/stage2-9-guide-zh.md)；其他环境、migration、Entra和production须按各自配置与批准范围执行，不能复用本次dev/test结果。

## 1. 迁移原则与入口

采用并行新建、隔离验证和批准客户端试点。旧域名继续指向旧系统，新域名只交给少量获批客户端；旧网关、数据库、密钥/Salt和VMSS业务身份保留到单独批准的退役阶段。不得让1.104.0新版本自动迁移旧生产数据库；新旧网关各自使用独立数据库，不得共用同一个数据库。若以后要求无损承接备份后的新增数据，再进入可选最终迁移，而不是把它作为canary前置条件。

**两种“阶段”不要混淆：** 架构阶段0=本手册Stage0–1（可恢复基线和旧环境加固）；架构阶段1=Stage2–9（决策、隔离新建、迁移与发布）。GitHub表单的`stage`填写仓库编号，不是架构阶段号。每个Stage完成验收后才能执行下一Stage的变更。

在客户自己的fork仓库进入 **Actions → 左侧workflow名称 → Run workflow**。下表链接打开源文件；不是让客户切到上游仓库执行。

| workflow显示名称 | 用途/执行机 | 表单输入 |
| --- | --- | --- |
| [Customer staged migration](../.github/workflows/customer-migration.yml) | 配置检查/阶段预检，GitHub托管Runner；不部署 | environment、stage、mode、component |
| [Customer private runner checks](../.github/workflows/customer-runner-checks.yml) | 检查工具、身份范围、AKS只读访问；可选备份私网/Blob只读检查 | environment、check_target、check_backup |
| [Customer infrastructure deployment](../.github/workflows/customer-deploy.yml) | Azure资源plan/deploy，GitHub托管Runner | environment、stage、component、operation、release、approved_run_id、approved_plan_sha256、confirm_environment |
| [Customer private runtime operations](../.github/workflows/customer-runtime.yml) | 私网备份/恢复、Kubernetes发布、身份/证书动作，自托管Runner | environment、stage、action、operation、approved_run_id、approved_plan_sha256、audit_continue_run_id、confirm_environment |
| [Customer stage acceptance](../.github/workflows/customer-acceptance.yml) | 检查清单与验收记录，GitHub托管Runner | environment、stage、operation、reviewed_run_id、checked_items、evidence_notes、confirm_environment |
| [Check public source image](../.github/workflows/source-image-checks.yml) | 固定公共基线构建及派生运行镜像SBOM/扫描，GitHub托管Runner | 只选分支，没有stage或environment |
| [Promote LiteLLM image](../.github/workflows/promote-litellm-image.yml) | 向私有ACR晋级/构建、扫描及签名，自托管Runner | environment、acr_name、source_image、target_tag、build_azure_runtime、build_auth_proxy；没有stage/plan/deploy |
| [Customer gateway isolation checks](../.github/workflows/customer-gateway-checks.yml) | 私网DNS/TLS/匿名拒绝检查，自托管Runner | 只有environment，没有stage |

`Customer staged migration`的`config-check`不要求前序账本；`preflight`要求前序验收，但仍不部署。其旧`what-if`入口也不能代替`Customer infrastructure deployment / plan`产生的可批准计划。主要workflow手动执行受保护默认分支，本文用`main`指代。

**特殊动作：** `backup-restore`直接execute；镜像晋级只有一次构建/导入运行；阶段验收用draft/confirm。这些不能机械套用“所有workflow都先plan”。

## 2. 客户首次准备

1. fork到客户控制的仓库，公开或私有均可；默认分支须有生效的branch protection/ruleset，并要求受审核的代码才能执行。主要迁移workflow仅允许手动运行受保护默认分支，不能让公共PR使用私网Runner。不要将客户配置提交到Git。
2. 先建立本次使用的GitHub Environment，例如`test`，限制部署分支；不必一次创建全部`dev`、`test`、`prod`。默认双人策略应配置维护者与验收审批者分离、禁止自我审批；仅一位运维时可按新工作流指南显式启用single-operator并记录客户风险接受，不能宣称职责分离。GitHub审批受套餐影响，缺失时用客户外部变更流程。
3. 为每个Environment准备分离的基础设施部署身份和私网运行身份，并配置Azure OIDC联邦信任。Federated Credential issuer为`https://token.actions.githubusercontent.com`，audience为`api://AzureADTokenExchange`，subject精确到`repo:<customer-org>/<customer-repo>:environment:<environment>`。两个Client ID分别填入下表，不能用个人Object ID或Client Secret替代。
4. 确认预算和配额，使用阶段0的bootstrap组件先plan、批准后deploy创建新目标RG和Log Analytics workspace；无需手工预建。已有同RG Workspace可显式选existing。目标RG必须与旧RG不同；跨RG共享资源仍需扩展和测试。旧网关没有日志库时由阶段1legacy-logging创建，再onboard监控。
5. 由网络Owner批准区域、VM SKU、Kubernetes版本、CIDR、路由及私网执行位置。示例CIDR、HA Disabled、Redis规格均不是客户默认架构决策。
6. 按[Runner准备说明](customer-private-runner-preparation-zh.md)部署或复用专用执行机、注册服务并设置仓库标签变量。Runner建机不创建上述OIDC身份或授予业务权限；本机执行过`az login`也不会让Actions自动登录。部署到Private AKS和访问私有ACR/Blob需要获准私网路径，不为GitHub-hosted runner开放生产公网。

**GitHub保护规则：** 在Settings → Rules → Rulesets创建Active规则，目标为默认分支，启用禁止强推/删除及通过PR合并。单人维护时不必要求另一位不存在的维护者审批；生产审核要求按获批策略另行设置。保护后直接`git push origin main`被拒绝是正常现象，按“工作分支 → commit → push工作分支 → PR → 合并”发布。客户workflow只从合并后的默认分支执行，不关闭保护绕过。

**Runner注册收尾：** 在发起Bastion连接的电脑安装Azure CLI的`bastion`和`ssh`两个扩展；它们不是GitHub Runner程序，也不是业务凭据。注册URL为客户fork的`https://github.com/OWNER/REPOSITORY`，临时Token从Settings → Actions → Runners → New self-hosted runner的Configure步骤取得，只在Runner终端提示中输入。没有专用组织Runner Group时使用Default；服务已启动且GitHub显示Idle后可以退出Bastion，但不能在作业期间停VM。详细安装及输出命令见Runner指南第1.1节。

### 2.1 Variables和Secrets

先准备下面的基础清单；表中“StageN前”表示在首次运行该动作前补齐，不是运行失败后再猜变量。`AZURE_TENANT_ID`与`AZURE_RUNTIME_CLIENT_ID`不是二选一。数据库备份镜像也不是从bootstrap输出中取得。

以下位置对应实际workflow读取方式：

- **Environment Variable / Secret**：仓库`Settings → Environments → 本次environment`下的`Environment variables`或`Environment secrets`。运行时选择`test`就配置在`test`，不是只配到`prod`。
- **Repository Variable**：仓库`Settings → Secrets and variables → Actions → Variables → New repository variable`。

| 名称 | 存放位置 | 使用时机与用途 |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | Environment Variable | 基础设施plan/deploy、Azure What-if及镜像晋级使用的部署身份Client ID；Runner检查不读取它 |
| `AZURE_RUNTIME_CLIENT_ID` | Environment Variable | **Customer private runner checks必需**；普通私网运行操作也使用此身份Client ID，不是个人Object ID或VM资源ID |
| `AZURE_TENANT_ID` | Environment Variable | Runner检查及Azure操作必需；必须与客户配置中的tenantId一致 |
| `AZURE_SUBSCRIPTION_ID` | Environment Variable | Runner检查及Azure操作必需；必须与客户配置中的subscriptionId一致 |
| `CUSTOMER_CONFIG_JSON` | Environment Secret | 按[客户配置模板](../config/customer.example.json)填写完整JSON；主要迁移workflow只读取Secret，不再回退到同名Variable；不能含运行时凭据 |
| `WORKFLOW_ARTIFACT_KEY` | Environment Secret | 独立随机32字节的base64值，供计划、Runner检查结果及验收附件加密；生成及保管方法见[部署指南](customer-deployment-workflows-zh.md) |
| `MIGRATION_PRIVATE_RUNNER_LABELS` | Repository Variable | Runner选择器，填匹配实际标签的JSON数组；不能放在Secret，也不要只放Environment |
| `POSTGRES_RESTORE_IMAGE` | Environment Variable | **Stage0/backup-restore前必填**；与旧PG大版本兼容的完整`镜像仓库@sha256:...`，获取方法见0-B；不要求Runner只读检查使用它 |
| `AZURE_DATABASE_CLIENT_ID` | Environment Variable | Stage5/database-roles专用已授权PG管理员服务主体的Client ID；OIDC不能模拟个人用户管理员 |
| `MIGRATION_RESTORE_BLOB` | Environment Variable | Stage5/restore-target前；从Stage0报告`observations.backupBlob`取`pre-change/...dump`，不是Blob URL或SAS |
| `MIGRATION_BACKUP_SHA256` | Environment Variable | Stage5/restore-target前；从同一报告`observations.backupSha256`取得备份文件哈希；不是镜像digest |
| `AZURE_ENTRA_CLIENT_ID` | Environment Variable | Stage7/entra-apps及admin-credentials专用初始化身份，需预先获批Graph和admin Vault权限 |
| `AZURE_ENTRA_ACCESS_CLIENT_ID` | Environment Variable | Stage7/entra-access专用准入授权身份，与Entra初始化身份分离 |
| `AZURE_CERTIFICATE_CLIENT_ID` | Environment Variable | 选择Stage4/certificate-renew时需要，独立DNS挑战/API证书身份 |
| `MIGRATION_RELEASE_JSON` | Environment Secret | Stage9实际启用或改变流量前必需的发布报告，独立于阶段验收账本；模板字段见第4节Stage9 |

已有脚本/特殊分支兼容项，不要为了填满表单一律创建：

| 配置 | 仅何时需要 |
| --- | --- |
| `MIGRATION_EVIDENCE_JSON` | 旧账本兼容入口；本文Actions会自动读取验收artifact，无需反复更新 |
| `MIGRATION_REPORT_JSON`、`MIGRATION_REPORT_URL`、`MIGRATION_APPROVERS_JSON` | 使用acceptance的record外部报告/双人策略入口时；本文单人confirm不需要 |
| `MIGRATION_MANIFEST_YAML` | 旧手工清单路径；本文托管application发布不填，不与自动配置同时使用 |
| `POSTGRES_MIGRATION_USER` | 自管PG角色路径；本文配置databaseAccess后自动使用llmgw_migrator，不填 |
| `PRIVATE_API_INGRESS_CLASS`、`PRIVATE_ADMIN_INGRESS_CLASS` | 客户自管Ingress controller路径；本文private-ingress文件路由不需要 |
| `AZURE_AUDIT_GOVERNANCE_CLIENT_ID` | 仅选择增强L3治理；一期原生日志抽查不需要 |

`GH_TOKEN`、`GITHUB_SHA`、`MIGRATION_AUTO_EVIDENCE`等由workflow设置，不要求另建永久PAT或手填这些变量。真实Master/Salt、证书私钥和后台Key不进入上述JSON；GitHub Runner注册Token也不作为迁移Secret保存。

例如Runner标签变量的值为：

```json
["self-hosted", "Linux", "X64", "llmgw-test-private"]
```

**Runner检查的最小清单：** 三个Azure Variables（`AZURE_RUNTIME_CLIENT_ID`、`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`）、两个Environment Secrets和一个Repository标签变量；还要有已配置联邦信任及所需AKS读取权限的运行身份。这不是全部迁移阶段的权限清单。

当前[Runner检查workflow](../.github/workflows/customer-runner-checks.yml)使用`vars.AZURE_RUNTIME_CLIENT_ID`和`vars.AZURE_TENANT_ID`作为`azure/login`输入。只配置`AZURE_CLIENT_ID`不会自动替代运行身份；放入同名Secret、客户JSON或本机终端变量也不会被这个登录步骤读取。不要为补空值直接把两个身份合并；须确认各自Client ID及批准的授权范围。`SERVICE_PRINCIPAL`是OIDC路径的正常默认值，不需要改成`IDENTITY`或创建Client Secret。

首次先运行`Customer staged migration`的`stage=0, mode=config-check, component=none`；准备齐全后运行`Customer private runner checks`，首次`check_target=false`。变量和Secrets的名称/位置、分支保护及Runner Idle应先核对，不通过后续反复失败来猜配置。

**两个Secrets怎样填写：** `CUSTOMER_CONFIG_JSON`填完整JSON原文，不填文件路径、不base64编码、不包Markdown代码块。修改本地文件后必须同步更新所选Environment的Secret，Actions不会读取你本机文件。`WORKFLOW_ARTIFACT_KEY`在受控终端用`openssl rand -base64 32`生成，直接录入Secret及密码库，不发聊天、不进入Git或Actions输出；已生成过则继续使用同一个有效值，迁移期间不要随意轮换以免旧附件无法解密。

`MIGRATION_EVIDENCE_JSON`仅保留旧入口兼容，不是当前Actions首次运行的必填Secret。单人验收采用`draft → confirm`后，后续workflow自动读取加密账本，不要求逐阶段手改证据Secret，也不要求提供旧`record`入口的报告/审批人JSON。

配置中的`ownerEmail`用于资源标签、告警邮箱以及备份Owner描述，不用于推断RBAC。`backupOwnerPrincipalId`是客户明确批准的Entra用户Object ID；`backupAutomationPrincipalId`是`AZURE_RUNTIME_CLIENT_ID`对应服务主体的Object ID，两者不能替换。当前备份Owner模板的principalType为User，不能填workflow应用ID或用`az ad signed-in-user`推断。需组/服务主体作为人工保管人时先修改并测试该角色边界。两类ID的Portal/CLI获取、核验及回填步骤见[0-A1](#0-a1-备份身份的获取与核验)。

`baseDomain`自动生成`llm-api.<客户域名>`和`llm-admin.<客户域名>`；Stage9为两者创建不同Front Door endpoint、WAF、PLS和私有LB回源，Admin额外用固定Prevention的WAF限制批准公网出口CIDR，内层继续执行Entra或LiteLLM原生登录。`legacy`记录实际旧RG、AKS、namespace和PG PVC；`parameters`按组件填写客户资源值。可留待后续阶段填写其他组件，但使用当前组件前必须清除其占位符。Stage3/4不校验尚未启用的`stage5Data`，Stage3不校验尚未使用的模型连接。

原环境Bicep参数中的`OWNER_EMAIL`、`LOG_ANALYTICS_WORKSPACE_NAME`、`AZURE_LOCATION`只用于独立编译/直接参数文件使用；其示例回退是离线检查用途。客户迁移workflow使用`CUSTOMER_CONFIG_JSON`生成显式ARM参数，不使用这些回退，也不默默读取个人域名配置。

运行时秘密由客户Key Vault承载：已有Master Key、Salt、数据库连接信息、管理OIDC client secret、会话加密密钥、受限管理Key等通过批准的CSI/Workload Identity接入；API客户端自带vkey，不再由代理保存内部Key。不要把它们写入`CUSTOMER_CONFIG_JSON`、ARM非secure参数、workflow inputs或日志。audit-foundation仅为选择增强L3时创建审计CMK及独立身份，不是基础版前置项；不自动轮换或删除客户已有加密材料。

### 2.2 Azure权限与日志

首次身份创建/授权由有权管理员一次性完成，不能依赖尚无法登录的Actions自行提权。可在既有管理RG中创建分离的用户分配托管身份，或使用获批的应用注册/服务主体；名称可按`<项目>-<环境>-deploy/runtime/database/entra-bootstrap/entra-access/certificate`规划，未用专项身份不必一次创建。UAMI的Overview可取Client ID和Principal ID，应用注册Overview取Application (client) ID，其企业应用Overview取服务主体Object ID，不能混用应用对象ID和服务主体ID。

为每个会在该环境登录的身份建立GitHub Actions联邦凭据，精确绑定客户仓库和Environment（例如`repo:OWNER/REPOSITORY:environment:test`），不是`ref:refs/heads/main`。issuer=`https://token.actions.githubusercontent.com`，audience=`api://AzureADTokenExchange`；大小写、仓库和环境须与实际运行一致。无需创建Azure Client Secret，不给Runner VM挂通用管理员身份。GitHub Azure登录成功只能证明身份可登录，不等于下面每项操作都有权限。

| 身份/阶段 | 要准备的权限范围 | 不能替代的权限 |
| --- | --- | --- |
| deploy，Stage0/bootstrap | 订阅范围的RG创建/嵌套部署及目标日志资源部署；plan的What-if也需相应权限 | Reader不够；不默认授予订阅Owner |
| deploy，backup/platform等 | 批准目标RG内的资源部署；涉及角色分配需相应受控RBAC管理权限；旧监控另外限定旧RG | Contributor不含角色分配；跨订阅模型角色须在对应账号范围另行授权 |
| deploy，Stage0/runner-connectivity | 两个指定VNet的读取及Peering写入/peer权限、目标Blob DNS zone的链接管理、Runner VNet的join权限；两侧RG内所需ARM部署/What-if权限 | 不修改整个VNet、NSG或路由；此组件不自行授予网络权限，详见0-A2 |
| deploy，Stage4/certificate-vault | 目标RG的Vault/PE/DNS/诊断部署、`Microsoft.Authorization/locks/read`及`Microsoft.Authorization/locks/write`、Vault范围角色分配；读取两个VNet及DNS链接，获准链接的VNet join权限 | Contributor加RBAC Administrator不包含锁写权限，按[4-A1](#4-a1-证书vault防删除锁权限)补齐；不导入证书，不自动授予部署身份Secret读取权 |
| deploy，Stage4/runner-target-connectivity | 读取原platform部署、AKS与ACR、两者PE/NIC、实际DNS区域/记录/链接及Runner VNet；目标RG及AKS DNS所在RG的ARM部署/What-if权限、两个zone的链接写入及Runner VNet join权限 | AKS系统DNS通常在节点RG，ACR DNS在目标RG；只有其中一处权限或链接仍不够。管理员按[4-B1](#4-b1-runner到新aks的dns连接)预授权，不由workflow自行提权，也不授予Kubernetes数据权限 |
| runtime，Runner检查/备份 | 旧AKS读取、Cluster User凭据获取及Kubernetes Deployment/Pod读取；备份另需postgres exec/cp、目标部署输出读取、Blob容器数据读写 | ARM权限不等于Kubernetes RBAC或Storage数据权限 |
| runtime，新环境发布 | 按[4-B2](#4-b2-runtime身份的kubernetes预授权)准备目标AKS用户凭据、Namespace初始化和命名空间内读写权限；指定Vault、镜像及目标RG回执另行授权 | 不给日常应用身份DDL或管理权限；Azure Contributor不能代替Kubernetes数据权限 |
| database，Stage5/database-roles | 配置为PG Entra管理员服务主体或其获准管理员组成员，能建立新库角色及授权 | 个人User管理员不能通过服务主体OIDC模拟；不能直接填个人UPN |
| Entra bootstrap / access，Stage7 | 前者获批应用创建/凭据管理，后者获批角色分配/委托同意；具体Graph权限见Stage7参考 | Azure RG RBAC不能约束或代替租户级Graph授权 |
| certificate，选择自动签发时 | 批准DNS TXT及API证书/ACME状态Secret权限 | 不授予admin证书或后台Master/Salt读取权限 |

从[部署参考的身份说明](customer-deployment-workflows-zh.md#4-哪些-id-人工提供哪些自动输出)逐项核对实际授权；上述是分工，不是“一项角色覆盖全部动作”。权限缺口交给对应Owner，不通过关闭TLS/租户检查或扩大到订阅Owner解决。

模型分布在多个订阅时，首次Stage4/platform plan前须逐个准备模型RG和账号的部署身份授权，详见[4-0模型账号的一次性授权](#4-0-模型账号的一次性授权)。管理员个人的Owner权限、身份已创建或OIDC登录成功，都不会自动把权限授给workflow。

主要迁移workflow支持公开fork，计划、运行结果和验收账本使用`WORKFLOW_ARTIFACT_KEY`认证加密后上传，通常保留7天；仅选用增强L3时的治理workflow仍有独立限制。绝不上传数据库dump、kubeconfig或原始stderr。公开仓库的运行元数据、workflow输入和非秘密Variables仍可能公开，不能在其中填写正文或凭据；加密附件不替代保护分支、Environment权限及内容审查。解密审核步骤见[客户部署与验收工作流](customer-deployment-workflows-zh.md)。

### 2.3 客户JSON分批准备

从[迁移示例](../config/customer.example.json)整理自己的受控文件；示例是字段参考，不是直接可运行的批准配置。只启用已决定的可选配置块，未准备好的`application/proxy/privateIngress`等不要以占位符顶层块提前启用，否则全局配置校验也可能拒绝前期检查。

| 最迟时机 | 客户JSON内容 | 来源/注意事项 |
| --- | --- | --- |
| 首次检查 | schemaVersion=1、environment、azure、location、baseDomain、ownerEmail、legacy、target、parameters | 来自实际客户租户/订阅、域名及旧部署；目标RG不同于旧RG，不能用Runner RG冒充目标RG |
| 首次验收前，建议开始即冻结 | governance | 单人confirm须有本人Entra用户Object ID、GitHub login及明确风险接受，例子如下；改governance会影响早期证据 |
| Stage0 | parameters.backup、parameters.runner-connectivity、可选parameters.bootstrap | 新备份网络/工作区、两类备份身份及Runner VNet资源ID；见0-A、0-A2 |
| Stage1 | parameters.monitoring、可选parameters.legacy-logging、legacyAccess | 旧日志工作区及真实批准来源；旧/新工作区不同不代表重复 |
| Stage2 | contentAudit | 本文采用native；此后正文决策绑定证据 |
| Stage3 | parameters.platform的ACR/日志名及stage4Network、stage4Aks | 这些网络/集群字段Stage3已要求提供，不能等Stage4才填写；stage5Data和模型连接可稍后补齐 |
| Stage4 | azureOpenAIConnections、parameters.certificate-vault、parameters.runner-target-connectivity；入口发布前补privateIngress；可选certificates | workflow创建证书Vault及Runner到新AKS API/ACR的DNS链接；手动导入证书，自动签发才需专项身份，见4-A至4-C |
| Stage5 | stage5Data、databaseAccess、DNS归属开关 | 批准数据库SKU/HA/Entra管理员，runtime服务主体Object ID；不能用用户ID代替运行身份 |
| Stage6 | application | 派生后台镜像digest和模型组/真实deployment映射，见Stage6 |
| Stage7 | entra、proxy | 分离初始化/准入身份，代理镜像digest，用户或服务主体绑定，见Stage7 |
| Stage8 | 可选observability | telemetry所需固定collector镜像；原生日志不要求自建L3 |
| Stage9 | parameters.origin、parameters.edge；可选dns | 优先使用已实现的auto输出解析；域名/证书审批另行准备 |

单人操作时由客户批准后加入以下顶层块，替换占位符再更新Secret：

```json
"governance": {
  "approvalMode": "single-operator",
  "approverObjectIds": ["REPLACE_OPERATOR_USER_OBJECT_ID"],
  "singleOperatorRiskAccepted": true,
  "githubLogin": "REPLACE_GITHUB_LOGIN"
}
```

Client ID用于登录，Principal/Object ID用于RBAC或用户批准，模型`deploymentName`用于推理路由，这三类值不能互换。原生审计不要填写示例中的L3 writer/reader/retention为同一个个人ID来强行通过；未选增强L3时不执行audit-foundation/audit组件。

### 2.4 真正开始执行前

| 步骤 | workflow | 完整输入 | 成功后/失败时 |
| --- | --- | --- | --- |
| P-01 | Customer staged migration | branch=main，environment=test，stage=0，mode=config-check，component=none | 检查客户基础JSON，不查询Azure；失败先修配置 |
| P-02 | Customer private runner checks | branch=main，environment=test，check_target=false，check_backup=false | 初次检查只核对工具/身份/旧AKS；备份资源及连接建立后再选check_backup=true |
| P-03 | 人工核对 | 保护分支、Environment变量/Secrets、Owner授权、镜像/容量/网络清单 | 清楚下一步收费/作用域后进入S0-01 |

可在组件首次plan前重复P-01，把stage/component换成本次组件，提前发现参数问题。`guide/config-check/preflight`成功不能当作deploy已批准；Runner显示Idle也不等于读写权限和私网已经验证。

## 3. 每次运行的填写、审核与验收

### 3.1 基础设施表单：plan与deploy

打开`Customer infrastructure deployment`。下表是每个Stage表格共同使用的完整默认规则，Stage9的正式release另有明确例外。

| 表单字段 | plan | deploy |
| --- | --- | --- |
| Use workflow from | main | main，与计划同一完整Git SHA |
| environment | test | test，与计划一致 |
| stage | 按第4节表格 | 同一stage |
| component | 按第4节表格 | 同一component |
| operation | plan | deploy |
| release，页面显示Stage 9 edge traffic operation... | 不勾选 | 不勾选，除非Stage9明确批准发布 |
| approved_plan_sha256，页面显示**Legacy direct hash input; prefer approved_run_id** | **留空** | **留空，不把run ID填到这里** |
| approved_run_id，页面显示**Successful matching infrastructure plan run ID** | 留空 | 填成功且已解密审核的同组件plan运行ID |
| confirm_environment，页面显示Type the selected environment again for deploy | 留空 | 填test |

GitHub页面可能把description显示为标签而不是变量名。数字run ID来自计划运行URL的`/actions/runs/<数字>`，不是`/job/<数字>`、页面`#1`编号、artifact ID或64位SHA256。例如审核的是`/actions/runs/12345678901`，就在**Successful matching infrastructure plan run ID**框填`12345678901`，这是格式示例，不是可用于客户部署的真实批准ID。

### 3.2 私网运行表单：plan与execute

打开`Customer private runtime operations`。branch/environment/stage规则相同，选择`action`而不是component。

| 表单字段 | plan | execute |
| --- | --- | --- |
| action、stage | 按第4节 | 与计划相同 |
| operation | plan | execute |
| approved_plan_sha256，页面显示Non-audit runtime plan SHA256... | 留空 | 留空，使用下一框 |
| approved_run_id，页面显示Successful matching plan run ID to approve for execute | 留空 | 已成功且审核过的**runtime plan**的run ID，不是infrastructure plan |
| audit_continue_run_id | 留空 | 留空；仅增强L3分页恢复使用 |
| confirm_environment | 留空 | test |

**唯一本文备份例外：backup-restore只有execute，上述两个approved输入和audit_continue_run_id全部留空。** 不会把Stage0备份变成只读计划；审批窗口与恢复范围必须在执行前人工确认。

### 3.3 plan结果怎样审核

1. 等plan整个workflow成功，在运行Summary底部的Artifacts下载本次附件。失败运行或deploy执行记录不能充当下一次部署的plan。
2. 在受控机器解压到仓库忽略目录`temp/`；一般里面只有密文`sealed-artifact.json`，不是缺失报告。
3. 在同一终端安全注入与该Environment相同的`WORKFLOW_ARTIFACT_KEY`。不要把密钥写进命令参数、shell历史、Git或聊天；也不要临时生成新密钥解旧附件。
4. 使用以下命令，替换为你自己的仓库、plan运行ID、artifact名称和本地路径；命令只把明文写入受控文件，不打印客户内容：

```bash
.venv/bin/python -m scripts.workflow_security open \
  --file temp/downloaded/sealed-artifact.json \
  --repository OWNER/REPOSITORY \
  --run-id 12345678901 \
  --name infrastructure-test-0-bootstrap-12345678901 \
  --output-dir temp/reviewed-evidence
```

本机需准备仓库Python依赖；不要在公开Actions里执行解密后打印内容。`--name`必须是实际artifact名，不是ZIP文件名或workflow显示名称。

| 附件名称形式 | 解密后的主要内容 | 要检查什么 |
| --- | --- | --- |
| infrastructure-test-阶段-组件-runID | plan-summary.json、reviewed-plan.json、operation-receipt.json；执行后有deployment-receipt.json、deployment-outputs.json | 目标订阅/RG、实际资源/属性变化、费用、权限及网络；批准的是否plan且代码/配置一致 |
| runtime-test-阶段-runID | runtime-review.json、runtime-summary.json或acceptance-report.json，视动作而定 | 目标旧/新集群、动作范围、影响和结果；备份是否真实恢复/回读校验 |
| acceptance-draft-test-阶段-runID | acceptance-report.json | pending检查清单，不能直接当passed |
| acceptance-record-test-阶段-runID | migration-evidence.json及验收报告，confirm也使用此artifact名前缀 | 逐项实际核验、主体/配置/代码绑定 |
| runner-checks-test-runID / gateway-checks-test-runID | 就绪或入口检查结果 | 仅代表该探针范围，不代表全部权限/协议验收 |

审核后重新Run workflow选择deploy/execute并填写**这个plan的run ID**。计划与执行须同代码、同配置和实际状态；有变化就重新plan，不手改哈希。`Re-run jobs`沿用原运行代码，不会自动使用刚合并的修复；代码修复或输入改变后从Run workflow创建新运行。

### 3.4 每个Stage结束都做验收

以下是已配置single-operator的主路径；双人策略继续使用已审核外部报告的record入口，见部署参考，不虚构第二位批准人。

| 字段 | 生成清单 | 人工核验后确认 |
| --- | --- | --- |
| workflow | Customer stage acceptance | Customer stage acceptance |
| branch、environment、stage | main、test、当前Stage | 与draft相同 |
| operation | draft | confirm |
| reviewed_run_id | 留空 | 本Stage成功的**acceptance draft**运行ID，不是部署/备份run ID |
| checked_items | 留空 | draft Summary列出的全部check ID，逗号分隔，不漏项不重复 |
| evidence_notes | 留空 | 简短真实结果及安全证据引用，不放Prompt、密码、Token或敏感资源详情 |
| confirm_environment | 留空 | test |

draft可以在阶段操作前生成，用作检查清单；所有实测完成后才confirm。Stage0备份动作的acceptance-report是观察报告，不能把其runtime run ID直接填进reviewed_run_id。备份报告供核验，confirm引用单独生成的acceptance draft。

阶段N的`preflight`/原`what-if`及实际deploy/execute必须具备0至N-1的通过记录；独立config-check和部署plan不要求验收。记录包含阶段、环境、配置哈希、验收时Git SHA、全部checks、与governance匹配的审批者、时间和报告引用。用Customer stage acceptance的draft生成pending报告，实测审核后由已配置的单人操作者confirm生成账本；record保留为外部报告/双人策略兼容入口。后续workflow自动读取受保护分支上的成功加密账本：双人模式要求当前SHA，single-operator允许复用先前SHA且仍校验阶段配置、策略、操作者、检查集和7天时效。不需要更新证据Secret，也不需要先伪造passed才能开始阶段0。

新记录使用stage-config绑定，只覆盖当前及前序阶段相关配置，后续PLS/审计身份输出补填不使阶段0失效；旧full-config记录仍可使用。相关配置或审批策略改变后需重新审核，不直接改哈希冒充验收。single-operator下仅代码提交不会自动使前序记录失效；若代码实际影响已验收行为、检查实现或证据结论，单人操作者负责主动重验。双人模式仍绑定当前SHA。记录仍限最近7天，长期迁移要复核早期备份和回退有效性。新报告哈希是规范JSON哈希，由工具计算。

**合并代码后的重跑边界：** 批准plan仍绑定完整Git SHA，尚未执行的旧plan必须重新生成。single-operator的前序Stage账本不再仅因新提交而失效，无需为无关代码改动依次重跑Stage0–3 draft/confirm；双人模式继续要求当前SHA。两种模式都不能改旧报告的revision或哈希；过期记录、配置指纹变化或实际受代码影响的检查仍须重测。补parameters.certificate-vault本身不改变Stage0–3的配置指纹；更改早期platform字段则另行复核。

**这是人工验收记录，不是自动事实认证**：`independentlyVerified=false`。工具校验清单、代码/配置、时效和已配置操作者，不独立证明技术事实。不要把“workflow绿勾”“resource存在”“本机模拟测试通过”当作所有check均通过；记录实际证据后再确认。阶段N重录会使后续旧账本失效。

## 4. 分阶段操作

### 阶段0：盘点与可恢复备份

**开始前准备：** 第2.1节的基础配置已经就绪；使用`deploymentMode=migration`（省略也为迁移），先完成配置和Runner检查。盘点旧PG实际版本、扩展、容量、PVC、旧LiteLLM镜像digest、模型/Key/预算与Master/Salt保管位置，并批准低峰备份窗口。不得将旧生产凭据或正文上传Actions。

#### 0-A. 本阶段必须补充的配置

| 配置项 | 存放位置/来源 | 何时需要 |
| --- | --- | --- |
| `POSTGRES_RESTORE_IMAGE` | `test`的Environment Variable；按下面方法取得完整镜像digest引用 | **运行backup-restore前必填**，不是Secret、不是workflow输入，也不放进客户JSON |
| `target.resourceGroup`、`location` | `CUSTOMER_CONFIG_JSON`；批准的新目标RG名和区域 | bootstrap前；目标RG必须与旧RG不同 |
| `parameters.backup.logAnalyticsWorkspaceName` | 客户JSON；新日志工作区名，与platform等新环境组件统一 | bootstrap及backup前；不是旧监控工作区 |
| `parameters.backup.backupOwnerPrincipalId` | 客户JSON；备份保管人的Entra用户Object ID，按0-A1从客户租户Users取得 | backup前；不是Client ID，不自动选择当前登录用户 |
| `parameters.backup.backupAutomationPrincipalId` | 客户JSON；按0-A1由`AZURE_RUNTIME_CLIENT_ID`查询服务主体Object ID | 本文OIDC备份路径在backup前填写，模板授予备份容器范围Blob Data Contributor；仅在已另行批准并配置等效数据权限时才省略 |
| 备份VNet与PE子网名称/CIDR | 客户JSON的`parameters.backup` | backup前；与后续platform一致，不能和Runner/企业网络重叠 |

`parameters.bootstrap`可省略，默认创建工作区；可显式配置`{"workspaceMode":"create","logRetentionDays":30}`。已有同RG工作区并获准复用时用`workspaceMode=existing`；不要先删旧集群仍在使用的日志库来“去重”。

#### 0-A1. 备份身份的获取与核验

旧版[客户示例](../config/customer.example.json)漏列了`backupAutomationPrincipalId`。从旧版复制配置的客户应在`parameters.backup`内补上该字段，保留已有`backupOwnerPrincipalId`；不要把新字段建成同名GitHub Variable，也不要照抄其他租户的GUID。模板技术上允许省略它，以兼容另行管理RBAC的客户，但不会因此自动把人工Owner的权限给Actions。

| 值 | 代表谁 | 获取位置与回填位置 |
| --- | --- | --- |
| `AZURE_RUNTIME_CLIENT_ID` | 用于普通runtime workflow的OIDC登录身份 | 第2.2节预先准备的运行身份：应用注册的Application (client) ID，或用户分配托管身份的Client ID；填所选GitHub Environment Variable |
| `backupAutomationPrincipalId` | 上述登录身份在客户租户中的服务主体 | Enterprise applications的Object ID，或该托管身份的Principal ID；填客户JSON的`parameters.backup` |
| `backupOwnerPrincipalId` | 客户明确批准的人工备份保管人 | 客户租户Microsoft Entra ID → Users → 指定用户 → Object ID；填客户JSON的`parameters.backup` |

**取得自动化身份，先锁定Client ID再查Object ID：** 打开客户仓库Settings → Environments → 本次环境，记录`AZURE_RUNTIME_CLIENT_ID`的值。GitHub Variable不会自动成为你本机终端的环境变量；下面的`RUNTIME_CLIENT_ID`要显式填这个值，不取`AZURE_CLIENT_ID`，也不取Runner VM资源ID或登录VM的用户。如果尚无运行身份，先完成第2.2节的身份/OIDC准备，不临时用个人ID凑数。

Portal路径：切换到客户的Entra租户 → Enterprise applications（企业应用）→ All applications，按上述Application ID查找，必要时调整应用类型筛选。在Overview同时核对Application ID与Object ID，后者才是要回填的值。应用注册Overview中另一个Object ID属于application对象，**不是**这里的service principal对象，不能使用。

CLI路径：在已获准读取客户目录的管理终端执行。先核对`az account show`输出与客户JSON的`azure.tenantId`、`azure.subscriptionId`一致；不一致先登录/切换到正确租户和订阅，再继续。以下查询不创建身份、不生成密码、不授予权限：

```bash
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
RUNTIME_CLIENT_ID="REPLACE_WITH_AZURE_RUNTIME_CLIENT_ID"
az ad sp show --id "$RUNTIME_CLIENT_ID" \
  --query '{displayName:displayName,clientId:appId,objectId:id}' --output json
```

检查输出`clientId`与输入完全一致，`displayName`对应客户批准的运行身份，把输出`objectId`填入`parameters.backup.backupAutomationPrincipalId`。Object ID是租户内标识，同一多租户应用在不同客户租户里的值也可能不同；不要使用`az ad app show`的`id`代替它。

若运行身份是**用户分配托管身份（UAMI）**，也可从Azure Portal → Managed Identities → 该身份 → Overview取Principal ID。下面的RG、身份名称和订阅来自这个身份资源本身，不默认是Runner RG或VM名称；同样核对返回的`clientId`等于GitHub中的运行Client ID：

```bash
az identity show --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_IDENTITY_RESOURCE_GROUP" --name "REPLACE_IDENTITY_NAME" \
  --query '{tenantId:tenantId,clientId:clientId,objectId:principalId}' --output json
```

如果`az ad sp show`因目录读取权限失败或找不到对象，请客户Entra管理员在正确租户核验并提供匹配的两项ID，或使用已获准读取的UAMI资源查询。订阅Contributor不等于Microsoft Graph目录读取权限；不要为查询ID而给日常运行身份添加宽泛Graph权限，也不要随意新建一个同名应用。

**取得人工Owner：** 先由客户确认谁负责保管备份，再查询该用户。`ownerEmail`可能只是通知邮箱，当前CLI登录人也可能是顾问，不能自动当作Owner。Portal在客户租户Users中核验该用户的UPN、用户类型和Object ID；CLI可使用该用户在客户租户中的实际UPN：

```bash
BACKUP_OWNER_UPN="REPLACE_APPROVED_USER_UPN_IN_CUSTOMER_TENANT"
az ad user show --id "$BACKUP_OWNER_UPN" \
  --query '{displayName:displayName,userPrincipalName:userPrincipalName,objectId:id}' --output json
```

将这里的`objectId`填入`parameters.backup.backupOwnerPrincipalId`。来宾用户应取**客户租户内**的来宾对象ID与UPN（可能含`#EXT#`），不是其主租户Object ID，也不是仅按显示名称匹配的另一个用户。当前模板为该User授予Storage Account Contributor和Storage Blob Data Owner；自动化服务主体则仅获备份容器范围Storage Blob Data Contributor，两者须分别批准。

**这类故障中怎样反查实际调用者：** 如果已有workflow运行，可在旧AKS的Activity log中按运行时间定位`Microsoft.ContainerService/managedClusters/listClusterUserCredential/action`，检查事件JSON的`claims.appid`与`claims["http://schemas.microsoft.com/identity/claims/objectidentifier"]`，分别对应Client ID和服务主体Object ID，再与上面的身份查询交叉核验。不能只看`caller`的显示形式就猜是哪种ID。活动日志只证明该条管理操作由谁执行及其结果，不证明Blob上传或数据库恢复成功；全新客户无需先故意运行失败来获取ID。

**回填与授权后核验：** 修改受控客户JSON后，更新同一GitHub Environment的`CUSTOMER_CONFIG_JSON` Secret。Stage0内补填该字段后，对`component=backup`重新运行`plan`并审核RBAC变更，再用该plan的run ID执行`deploy`；不是重跑bootstrap或直接重用旧plan。已有阶段验收时按第3节复核受影响证据；Stage4以后不要回套Stage0网络模板，应单独审批授权变更。

可从Storage Account → Containers → litellm-postgresql → Access control (IAM)核验容器范围授权，或使用以下只读查询。`RUNTIME_OBJECT_ID`来自上述输出；`BACKUP_CONTAINER_SCOPE`中的Storage ARM资源ID从备份账户Portal的JSON View取得，后接实际容器路径（当前模板固定为`litellm-postgresql`），不是Blob URL：

```bash
RUNTIME_OBJECT_ID="REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"
BACKUP_CONTAINER_SCOPE="REPLACE_BACKUP_STORAGE_RESOURCE_ID/blobServices/default/containers/litellm-postgresql"
az role assignment list --assignee-object-id "$RUNTIME_OBJECT_ID" \
  --scope "$BACKUP_CONTAINER_SCOPE" --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,scope:scope,principalId:principalId}' --output json
```

核对容器范围的Storage Blob Data Contributor（或客户另行批准的等效数据权限）与运行Object ID匹配；新授权还需等待传播并验证实际访问。此查询不证明网络可达，也不完整评估组成员、自定义角色或条件权限，不能仅凭角色名称认定备份已可执行。继续完成S0-05的Runner私网/DNS/443核验，不打开Storage公网，也不授予订阅Owner代替这些步骤。

#### 0-A2. 由workflow建立Runner备份连接

`backup`创建备份侧资源，`runner-connectivity`是独立的Stage0组件：在已存在的Runner和备份VNet上创建双向Peering，并把备份Blob Private DNS链接到Runner VNet。使用现有Customer infrastructure deployment的plan/deploy与相同加密批准机制，不需要新增workflow、GitHub Secret或Client ID。管理面操作在GitHub托管Runner执行，不要求私网已通。

在客户JSON的`parameters`内加入以下块，示例配置已包含它：

```json
"runner-connectivity": {
  "runnerVirtualNetworkId": "REPLACE_RUNNER_VNET_RESOURCE_ID",
  "managePeering": true,
  "manageBlobDnsLink": true
}
```

`runnerVirtualNetworkId`的来源：Runner VM → Networking → NIC → IP configurations → 关联的VNet → JSON View中的`id`。应以`/providers/Microsoft.Network/virtualNetworks/<VNet名称>`结尾，是完整ARM资源ID，不带`/subnets/<子网>`，也不是VM/NIC资源ID。可在0-C1核实名称后，用`az network vnet show --subscription "$SUBSCRIPTION_ID" --resource-group "$RUNNER_RG" --name "$RUNNER_VNET" --query id --output tsv`只读取得。更新本地JSON后须同步Environment Secret `CUSTOMER_CONFIG_JSON`。

当前自动化支持**同订阅、不同VNet、可跨RG/区域**，要求CIDR不重叠；目标VNet/Storage/PE/DNS从成功的backup部署输出读取并与实际资源核验，不重新手填。跨订阅或同VNet部署不走此组件，沿用批准的网络方案。Global VNet Peering产生相应流量费用；Peering允许两VNet间的网络访问，不是仅放行Blob的应用权限，NSG和网络范围仍须由网络Owner批准。

- `managePeering=true`：管理固定名称的双向直接Peering，允许VNet访问，不启用转发流量/网关传递/远程网关。名称由两个VNet ID确定，重跑不会随机增加连接。已有同目标但不同名的Peering时拒绝创建重复连接，核对后改为false复用；已有同名但不同目标或网关设置时停止，不覆盖。
- `manageBlobDnsLink=true`：管理固定名称的Runner DNS链接，关闭自动注册。已有不同名链接时选择false复用；Runner使用自定义DNS时也必须选择false，由DNS Owner核验转发。不会修改DNS服务器、创建重复zone或改hosts。
- 已有Hub/企业DNS时可将两个开关均设false并跳过连接部署，保留真实Runner VNet ID供备份检查使用；仍须实测私网。false表示停止管理该类资源，不自动删除以前创建的连接。

**一次性授权与计划审核：** 基础设施登录身份`AZURE_CLIENT_ID`需预先获准读取backup部署、Storage、PE/NIC、两VNet及DNS链接，并能在两个精确VNet写入Peering；DNS链接写入还需要目标zone和Runner VNet对应权限。Runner RG需要嵌套ARM部署的读取/What-if/部署权限，目标RG继续使用既有部署权限。由客户权限管理员在批准范围授予所需操作，不把日常runtime身份改成网络管理员。runtime为check_backup需读取备份部署、Storage、PE/NIC及既有Blob容器列表权限，不需Peering写权限。

组件的变更allowlist只包括两个固定Peering、一个固定DNS链接和Runner RG中的固定嵌套部署记录。plan不准修改VNet主体、子网、NSG、路由、VM或RBAC；依然阻止Delete、Unsupported和越界变更。审核创建范围和费用后用该plan的run ID执行deploy。plan期间还会检查已有连接冲突与地址重叠，但不证明数据面已通。

部署完成后按S0-07运行`check_backup=true`。公开摘要仅显示固定的`backup-resources`、`backup-private-dns`、`backup-private-tls`、`backup-blob-read`及结果；详细范围与未覆盖项在加密runner-readiness报告。DNS错误时不会继续请求Blob；TLS检查必须连到PE的实际私有IP，随后使用同一次OIDC登录身份做容器只读列举。NSG/Hub/企业DNS造成的失败交给网络Owner修复，workflow不会自动放宽规则。检查不执行数据库备份、不上传/下载已有Blob，也不自动签发Stage0验收。

#### 0-B. 获取POSTGRES_RESTORE_IMAGE

先核对旧服务器和Pod内`pg_dump`的大版本，选匹配大版本及所需扩展的批准镜像。下面以PG16、Linux x64 Runner为例，在有Docker的受控机器上执行；客户不是PG16时必须换成相应批准版本，不能直接照抄。

```bash
IMAGE="postgres:16.15"
docker pull --platform linux/amd64 "$IMAGE"
docker image inspect "$IMAGE" --format '{{range .RepoDigests}}{{println .}}{{end}}'
```

把输出中对应镜像的完整`postgres@sha256:...`填入 **Settings → Environments → test → Environment variables → POSTGRES_RESTORE_IMAGE**。不是`docker images`的IMAGE ID，不是只填64位哈希。选定digest后用该完整引用运行`docker run --rm --network none --read-only --entrypoint pg_restore <完整镜像引用> --version`核对版本；不启动旧库或执行恢复。

本项目曾验证的PG16镜像为：

```text
postgres@sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5
```

这不是客户备份artifact中的值。环境兼容且获批时可以复用；它是完整PostgreSQL恢复容器，不是只有客户端工具的镜像。同大版本仍需核对扩展、locale/排序规则和实际恢复结果；digest锁定内容，不证明备份可恢复，也不会自动获得后续补丁。

#### 0-C. 按顺序运行

下面**每一行都是一次新的Run workflow**。所有行均选受保护的`main`和`environment=test`；其他输入按第3节通用填写规则。`S0-xx`只是本文步骤号，不是要填入GitHub的run ID。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S0-01 | Customer infrastructure deployment | 0 | component=bootstrap | plan | 留空 | 留空 |
| S0-02 | Customer infrastructure deployment | 0 | component=bootstrap | deploy | S0-01成功且已审核的run ID | test |
| S0-03 | Customer infrastructure deployment | 0 | component=backup | plan | 留空 | 留空 |
| S0-04 | Customer infrastructure deployment | 0 | component=backup | deploy | S0-03成功且已审核的run ID | test |
| S0-05 | Customer infrastructure deployment | 0 | component=runner-connectivity | plan | 留空 | 留空 |
| S0-06 | Customer infrastructure deployment | 0 | component=runner-connectivity | deploy | S0-05成功且已审核的run ID | test |
| S0-07 | Customer private runner checks | - | check_target=false，check_backup=true | - | - | - |
| S0-08 | Customer private runtime operations | 0 | action=backup-restore | execute | 留空 | test |

**执行完S0-08不等于完成Stage0验收。** 上表是资源准备与备份操作；旧版顺序表没有列出下面的验收收尾，容易误以为八步绿勾后可直接进入Stage1。不能将这八个步骤号填入`checked_items`，也不能把四项检查ID视为八步自动完成的结果。

四项检查的覆盖范围、通过标准及confirm填写方法见本节以下两表和[第3节批准与附件审核](#3-每次运行的填写审核与验收)。S0-10逐项核对，不需要另外寻找四个对应的workflow；不得用已删除的独立Stage验收文档作为另一套运行顺序。

| confirm中的检查ID | S0-01至S0-08实际覆盖 | 确认前还需核对 |
| --- | --- | --- |
| `inventory` | 配置中记录旧RG/AKS/namespace/PVC；S0-08读取两个Deployment及镜像、验证PG挂载PVC | 复核实际旧版本、数据库容量/扩展、模型/Key/预算配置及恢复材料位置；仅填了JSON不等于盘点完成 |
| `backup_restore` | S0-08成功后有pg_dump、隔离PG恢复、基础表检查、Blob上传/下载及SHA256一致的观察结果 | 审核本次成功运行报告和备份引用，按批准范围复核数据完整性；不把任意旧Blob或一次green job当作全部验收 |
| `key_salt_recovery` | 这八步未自动检查 | 获准保管人从受控备份材料取回原Master/Salt，核对与旧部署实际使用值一致并记录恢复方法；不能用artifact key替代，也不能新生成/轮换旧密钥来验证。涉及恢复/解密实测时使用批准的隔离环境，不打印秘密 |
| `protocol_baseline` | 这八步未自动检查 | 在旧入口用实际首发客户端和获准测试内容验证所需调用，例如连续对话、流式、工具调用；记录客户端版本、时间、结果。近期实际使用记录经复核可作为证据，不要求此时测试尚未部署的新入口 |

**验收收尾按下面顺序完成，branch=main、environment=test、stage=0。** 单人路径需先完成第2.3节的governance配置并同步Secret；draft生成后不再变更同阶段配置、代码或artifact key。draft可提前运行，但confirm只能在四项实际核验后运行。

| 步骤 | 执行方式 | operation | reviewed_run_id | checked_items / evidence_notes | confirm_environment |
| --- | --- | --- | --- | --- | --- |
| S0-09 | Customer stage acceptance | draft | 留空 | 均留空；生成pending清单 | 留空 |
| S0-10 | 人工核对上表四项并记录证据，不是workflow按钮 | - | - | 未完成项继续核验；已有近期证据可复核引用，不重建已有资源 | - |
| S0-11 | Customer stage acceptance | confirm | S0-09成功的draft运行ID，不是S0-08的备份ID | 全部通过后填`inventory,backup_restore,key_salt_recovery,protocol_baseline`；说明中逐项填真实结果与证据引用 | test |

截图中`For confirm, every check ID personally verified...`对应`checked_items`，`For confirm, actual observations and evidence references...`对应`evidence_notes`。**如果只执行了八步、尚未核验密钥恢复和协议基线，现在没有可以如实提交的完整confirm填写值。** 当前confirm不支持部分通过；不能只填`backup_restore`提交，也不能补两个未做过的检查ID绕过门禁。可在私下工作记录写“资源准备/备份操作已完成，人工验收待完成”，不要提交成Stage0通过记录。已核验的项目不必为填表重复执行，缺失或不可读的证据须先通过获准渠道补齐。

四项完成后，`evidence_notes`用普通文本逐项说明：盘点记录引用；成功备份run ID及恢复/回读结果；Master/Salt恢复核验记录引用；旧客户端版本及测试结果引用。长度12至4000字符，不填凭据、Prompt、完整客户配置或敏感资源详情；公开仓库workflow输入可能公开。没有完成的检查不能编造“通过”说明。S0-11成功产生验收账本后再进入Stage1；若补governance等相关配置，旧plan失效，应重新plan/审核，而不是重跑已完成的资源部署。

所有上述操作的`approved_plan_sha256`留空；基础设施的`release`不勾选；runtime的`audit_continue_run_id`留空。**backup-restore只支持execute，不运行plan，也不填bootstrap/backup的批准ID。** 执行前可以用Stage0验收draft查看检查范围，但draft不是自动批准备份。

**网络及权限检查不能跳过：** 默认按S0-05/06建立连接；已批准Hub/既有网络时按0-A2设置复用开关，两个均false可跳过这两次部署，但S0-07仍须通过。VM同订阅不代表私网互通，NAT也不能代替Peering。Runner需解析备份Blob FQDN到该账户PE私有IP且TLS/443可达；不得打开Storage公网解决。运行身份还须读旧AKS信息/获取用户凭据、读Deployment/Pod并执行postgres Pod的exec/cp、读目标部署输出及上传/下载Blob。Azure Contributor不自动授予这些数据权限。

具体计划审核与失败排查见[0-C1：S0-05逐项审查](#0-c1-s0-05逐项审查)。一项不通过就停在连接/检查步骤，不把重复执行backup-restore当作网络探针。

**成功后核对：** bootstrap只创建目标RG和新Log Analytics工作区；backup创建私有Storage、容器、VNet/PE/DNS及获批RBAC，但尚未导出旧库。backup-restore才会执行Pod内pg_dump、拷到Runner、在无网络/无宿主端口的临时Docker PG中恢复，再上传Blob并回读校验。它不会把数据恢复到新生产PG，也不覆盖旧库。

解密S0-08的`runtime-test-0-<run ID>`附件，查看`acceptance-report.json`的`observations`：`fullRestoreSucceeded`、`backupBlob`、`backupSha256`、`backupBytes`、`publicTableCount`和`restoreSeconds`。安全保留备份引用和SHA256，Stage5要用；`restoreSeconds`不是包含所有步骤的生产停机时间。无错误完成恢复也不代表角色/ACL或业务密文已验证，流程使用了`--no-owner --no-acl`。

**失败后：** 停在本阶段，先看错误分类及加密结果。可能已产生一次独立备份Blob，重跑会使用新的备份名；不要删旧库、清空PVC或将dump上传GitHub。临时容器和文件会按流程清理，但机器异常中断后的残留仍须受控检查。不要只凭`pg_restore -l`成功认定恢复成功。

**阶段验收：** 按第3节运行`Customer stage acceptance`，`stage=0, operation=draft`，实测后`confirm`。本阶段检查为`inventory`、`backup_restore`、`key_salt_recovery`、`protocol_baseline`，以该draft实际列出的ID为准。备份/密钥恢复或客户实际客户端基线未通过时不进入Stage1。详见[备份模板说明](../infra/backup-storage/README_ZH.md)。

#### 0-C1. S0-05逐项审查

**先分清执行位置和身份：** 本节保留plan审查与故障定位命令；常规执行优先使用0-A2及S0-05至07的自动化。Azure Portal/管理终端核对已部署配置；DNS/TLS由实际Runner检查，不从笔记本或Cloud Shell代替。Blob/Kubernetes授权验证须使用`AZURE_RUNTIME_CLIENT_ID`对应身份，管理员自己的成功不能代替Actions。以下命令只读，不创建Peering或修改NSG；可由组件处理的连接先plan/批准/deploy，其他缺项交相应Owner。

**1. 取实际资源值，不猜名称。** 在已登录正确客户租户的管理终端填写前三项；值来自客户JSON的`azure.subscriptionId`、`target.resourceGroup`和`environment`。它们是本机shell变量，不会从GitHub自动同步。

```bash
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
ENVIRONMENT_NAME="test"
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
az deployment group show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "llmgw-${ENVIRONMENT_NAME}-s0-backup" \
  --query '{state:properties.provisioningState,runtimeObjectId:properties.parameters.backupAutomationPrincipalId.value,storage:properties.outputs.backupStorage.value.storageAccountName,container:properties.outputs.backupStorage.value.containerName,vnet:properties.outputs.backupStorage.value.virtualNetworkName,pe:properties.outputs.backupStorage.value.privateEndpointName,dnsZone:properties.outputs.backupStorage.value.privateDnsZoneName}' \
  --output json
```

通过条件：部署状态为`Succeeded`；使用模板授权时，`runtimeObjectId`与0-A1核实的运行Object ID一致，不是空值；另外核验实际角色，部署记录本身不能证明授权仍存在。其他检查要用的值按下表取得，记录在受控位置，不上传公开日志。

| 变量/值 | 客户侧获取位置 |
| --- | --- |
| `BACKUP_VNET`、`PE_NAME`、`DNS_ZONE`、`STORAGE_ACCOUNT` | 上述部署输出的vnet、pe、dnsZone、storage；当前模板在TARGET_RG内创建它们 |
| `RUNNER_RG`、`RUNNER_VNET`、`RUNNER_NIC_ID` | Runner VM → Networking → 实际NIC → IP configurations：核对私有IP、VNet/子网；VNet的RG不一定是VM的RG，NIC资源ID从其JSON View取得 |
| `BLOB_HOST` | 备份Storage → Endpoints → Blob service，只取主机名，不含https://或路径；不要改用privatelink域名或裸IP访问HTTPS |
| `PE_IP` | Storage → Networking → Private endpoint connections → 对应PE → Network interface → IP configurations；核对它确实属于上述备份账户 |

以下各块依次执行。替换全部`REPLACE_`，命令查询为空或失败就停止核对，不把空字符串当成通过。

**2. 核验Private Endpoint和双向路径，管理终端执行。** Portal先确认PE目标为该Storage、子资源为`blob`、连接状态为`Approved`，并位于批准的PE子网。下面通过PE关联的NIC读取IP，与后面的Runner DNS结果比较：

```bash
PE_NAME="REPLACE_PE_NAME_FROM_DEPLOYMENT"
az network private-endpoint show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "$PE_NAME" \
  --query '{subnet:subnet.id,connections:privateLinkServiceConnections[].{target:privateLinkServiceId,groups:groupIds,state:privateLinkServiceConnectionState.status}}' --output json
PE_NIC_ID=$(az network private-endpoint show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "$PE_NAME" --query 'networkInterfaces[0].id' --output tsv)
az network nic show --ids "$PE_NIC_ID" --query 'ipConfigurations[].privateIPAddress' --output json

RUNNER_RG="REPLACE_RUNNER_VNET_RESOURCE_GROUP"
RUNNER_VNET="REPLACE_RUNNER_VNET_NAME"
BACKUP_VNET="REPLACE_BACKUP_VNET_FROM_DEPLOYMENT"
az network vnet peering list --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$RUNNER_RG" --vnet-name "$RUNNER_VNET" \
  --query '[].{remote:remoteVirtualNetwork.id,state:peeringState,sync:peeringSyncLevel,allowAccess:allowVirtualNetworkAccess}' --output json
az network vnet peering list --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --vnet-name "$BACKUP_VNET" \
  --query '[].{remote:remoteVirtualNetwork.id,state:peeringState,sync:peeringSyncLevel,allowAccess:allowVirtualNetworkAccess}' --output json
```

直接Peering方案的通过条件：两端remote都指向正确的对方VNet，两端`Connected`、`allowAccess=true`，地址空间同步（返回sync时应为`FullyInSync`）；两侧CIDR不重叠。Portal分别打开两个VNet → Peerings检查，不能只查一侧。返回`[]`是没有连接，不是“没有错误”。跨订阅时分别使用VNet所属订阅；此处命令示例为同订阅。不同区域可使用获准的Global VNet Peering，不要求为此搬迁Runner。

Hub方案不要求直接Peering，但网络Owner须提供到PE及返回Runner的有效路由、NVA/Firewall转发规则与实测结果。VNet Peering不自动传递：仅有Runner→Hub和Hub→备份VNet两条Peering不等于互通；NAT公网出口也不是私网路径。

**3. 核验DNS归属，管理终端执行。** 打开部署输出的Private DNS zone → Recordsets，确认Storage账户的A记录指向刚查到的PE_IP；再看Virtual network links。

```bash
DNS_ZONE="REPLACE_DNS_ZONE_FROM_DEPLOYMENT"
az network private-dns link vnet list --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --zone-name "$DNS_ZONE" \
  --query '[].{network:virtualNetwork.id,state:virtualNetworkLinkState,registration:registrationEnabled}' --output json
```

Runner使用Azure提供的DNS时，该zone应有到Runner VNet的有效链接（`state=Completed`，PE场景无需自动注册）。只有备份VNet链接还不够，Peering不会自动共享Private DNS。Runner使用自定义DNS时，应由DNS Owner核验企业解析器/Private Resolver的条件转发和zone可见性；仅加VNet链接未必生效，不创建同名冲突zone，不修改hosts绕过。最终都以第5项的Runner解析结果为准。

**4. 核验NSG及有效路由，管理终端执行。** Runner必须运行。Portal从Runner NIC看Effective routes / Effective security rules，并检查PE子网关联的NSG和路由表。CLI可读取Runner端有效配置：

```bash
RUNNER_NIC_ID="REPLACE_RUNNER_NIC_RESOURCE_ID"
az network nic show-effective-route-table --ids "$RUNNER_NIC_ID" --output json
az network nic list-effective-nsg --ids "$RUNNER_NIC_ID" --output json
```

网络Owner须核对**实际Runner私有源IP → PE_IP:TCP/443**没有命中更高优先级Deny，Runner NIC/子网出站、PE子网入站（网络策略生效时）均允许；PE目标匹配的有效路由走获准私网路径而非Internet/None。Hub/NVA还要核验回程；NSG有状态连接无需再开宽泛反向入站规则。DNS服务器的UDP/TCP 53也需可达。无自定义NSG规则不等于拒绝或通过，要结合默认规则、服务标签和有效配置；不要删除NSG、开放0.0.0.0/0或关闭PE网络策略来验证。

**5. 从实际Runner测试DNS与HTTPS，无需Azure登录。** 通过已批准的Bastion/SSH进入Runner；不要在VS Code所在的另一台VM代跑。按第1项取得BLOB_HOST，使用正常域名/SNI验证，不用`curl -k`、裸PE_IP URL或`--resolve`掩盖DNS问题。

```bash
BLOB_HOST="REPLACE_BLOB_ENDPOINT_HOST"
getent ahostsv4 "$BLOB_HOST"
curl --noproxy '*' --connect-timeout 5 --max-time 15 --silent --show-error \
  --output /dev/null --write-out 'remote_ip=%{remote_ip} http_code=%{http_code}\n' \
  "https://${BLOB_HOST}/"
```

通过条件：解析结果与该账户PE_IP匹配，不是“任意10.x地址”；curl正常完成TLS且remote_ip也匹配PE_IP。这条匿名请求常返回400/403，**这里只能证明DNS/TCP/TLS路径，不能证明Blob授权**。不使用`--fail`是为了区分HTTP响应与连接失败，不是忽略权限错误。http_code=000、超时、证书失败、公网IP均不通过。`--noproxy '*'`验证Runner直连路径；客户强制私有代理时由网络Owner单独核验代理的PE解析、路由和证书链，不以代理公网403冒充PE直连成功。

**6. 审查Actions身份、Kubernetes和Blob权限。** 先按0-A1检查部署参数与**容器范围**RBAC：运行Object ID应具备Storage Blob Data Contributor或获准等效数据权限，只有Contributor/RBAC管理员不算通过。运行`Customer private runner checks`，选择`test, check_target=false, check_backup=true`；`azure-scope`、`legacy-cluster-read`、`docker-daemon`及四项backup检查均应通过。该选项覆盖备份管理面读取、PE DNS/TLS和Blob容器列举，**不覆盖Pod exec/cp、Blob上传/下载**。

下列命令是自动化检查的人工定位参考，只供实施人员在已获准的**相同运行身份上下文**执行。手工终端的个人`az login`、root或`kubectl --as`不能替代；工作流完成后OIDC登录会清理，不应指望登录VM即可复用它。Blob只读探针已由check_backup覆盖；额外Pod权限探针仍需在审核后的同身份诊断步骤中执行，不索取/复制OIDC Token、生成长期Client Secret或关闭日志保护。

```bash
STORAGE_ACCOUNT="REPLACE_STORAGE_FROM_DEPLOYMENT"
az account show --query '{tenantId:tenantId,subscriptionId:id,identity:user.name,type:user.type}' --output json
az storage blob list --account-name "$STORAGE_ACCOUNT" --container-name litellm-postgresql \
  --auth-mode login --num-results 1 --query 'length(@)' --output json --only-show-errors
```

确认当前身份对应预期Client ID后才使用结果；命令成功返回0也可能只是空容器，表示列举可用，不打印Blob名或内容。403可能来自数据RBAC、网络规则或条件策略，要结合第5项区分。**列举成功不能证明上传/下载成功**；纯审查只能核对所需dataActions，若客户要求备份前实测写入，应另行批准无业务内容的唯一测试Blob上传/回读/哈希校验及清理范围，不拿已有备份试写或删除。

Kubernetes检查使用同一身份的非admin kubeconfig，`PRIVATE_KUBECONFIG`为受控临时路径，`LEGACY_NAMESPACE`取客户JSON。由实施人员按现有`connect_cluster`流程获取/转换凭据，不使用个人默认context，不打印或上传kubeconfig：

```bash
PRIVATE_KUBECONFIG="REPLACE_PRIVATE_KUBECONFIG_PATH"
LEGACY_NAMESPACE="REPLACE_LEGACY_NAMESPACE"
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" auth can-i get deployments.apps
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" auth can-i list pods
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" auth can-i create pods --subresource=exec
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get deployment postgres litellm-mi-proxy -o name
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get pods -l app=postgres \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,DELETING:.metadata.deletionTimestamp'
```

前三项应返回yes，两个Deployment存在，标签匹配的Postgres恰好一个Running且无deletionTimestamp，挂载PVC与JSON中的postgresPvc一致。`kubectl cp`通过Pod exec和容器内tar工作，不存在独立的“cp角色”；还须确认postgres容器有pg_dump/tar、Runner磁盘及Docker资源足够。can-i仅检查授权，不能证明Admission、Pod状态、导出或容器恢复成功，不能据此签发Stage0通过。

**审查结论怎样记录：** 第6.1节记录实际Runner、身份、时间、PE_IP与DNS/TLS结果、双向路径、作用域授权和未覆盖项，凭据/正文不入记录。S0-07及其他未覆盖权限满足后才允许S0-08；S0-08真实导出/恢复/上传/回读成功后，再做Stage0人工验收。这不是要求把checklist手写成passed来解锁workflow。

### 阶段1：旧环境最小加固

**开始前：** Stage0已验收；批准维护窗口、原Deployment安全快照、通知接收人和来源限制方案。沿用基础Variables/Secrets，无新的GitHub必填Secret。deploy身份需旧RG的日志/告警权限，runtime需旧AKS监控配置及限定Deployment更新权限。

**客户JSON：** `parameters.monitoring.logAnalyticsWorkspaceName`是**旧RG**的日志工作区名，不是新工作区名。已有获准工作区可用`parameters.legacy-logging.workspaceMode=existing`；没有则create（默认）。不能把仍被AKS/App Insights/告警使用的手工资源当作重复资源直接删除。已有Container Insights指向不同工作区时，代码会停止，需先批准迁移目的地。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S1-01 | Customer infrastructure deployment | 1 | component=legacy-logging | plan | 留空 | 留空 |
| S1-02 | Customer infrastructure deployment | 1 | component=legacy-logging | deploy | S1-01的plan ID | test |
| S1-03 | Customer private runtime operations | 1 | action=monitoring-onboard | plan | 留空 | 留空 |
| S1-04 | Customer private runtime operations | 1 | action=monitoring-onboard | execute | S1-03的plan ID | test |
| S1-05 | Customer infrastructure deployment | 1 | component=monitoring | plan | 留空 | 留空 |
| S1-06 | Customer infrastructure deployment | 1 | component=monitoring | deploy | S1-05的plan ID | test |
| S1-07 | Customer private runtime operations | 1 | action=legacy-hardening | plan | 留空 | 留空 |
| S1-08 | Customer private runtime operations | 1 | action=legacy-hardening | execute | S1-07的plan ID | test |
| S1-09 | Customer private runtime operations | 1 | action=legacy-access-restrict | plan | 留空；仅选择来源限制时 | 留空 |
| S1-10 | Customer private runtime operations | 1 | action=legacy-access-restrict | execute | S1-09的plan ID | test |

S1-04之后先验证真实Container Insights日志已到达，再预览依赖这些表的告警。当前查询要求旧namespace=`litellm`、PG PVC=`pg-data`；不匹配须调整查询并测试，不修改真实资源名掩盖差异。S1-08只补缺少的探针并设PG Recreate，不自动锁定旧镜像digest、轮换密钥或完成管理面隔离；可能触发Pod滚动重启，需检查原有业务。

选择S1-09/10时，先在顶层配置`legacyAccess`，根据真实入口选择`nginx-ingress`或`load-balancer`，`allowedCidrs`填批准客户端的实际出口，`accessImpactAccepted=true`。NGINX模式只支持本项目既定Ingress/Service结构，额外入口/snippet等会阻断。限制后必须分别验证批准来源成功、未批准来源拒绝；API与管理路径共用入口时，整个入口白名单不等于独立管理隔离。

**回退：** 来源规则恢复使用同workflow、stage=1、action=legacy-access-restore，另跑plan再execute并引用该恢复plan ID；不能把restrict的plan用来restore。探针等回退按受控旧快照批准处理，不自动删除PVC、重置数据库或轮换Salt。

**阶段验收：** draft/confirm，stage=1；`legacy_health`、`alerts_received`、`rollback_snapshot`，配置legacyAccess后另有`legacy_source_access`。此外，批准的一期旧管理路径、废弃凭据及业务连续性必须实际检查；workflow做不到的必需控制仍须补齐，不能以探针成功代替整套加固。

**S1-08之前**，在已获准且显式选择旧集群的管理终端保存两个Deployment的完整变更前配置，禁止上传公开artifact或打印环境秘密：

```bash
set -euo pipefail
SNAPSHOT_DIR="REPLACE_CONTROLLED_BACKUP_ROOT/stage1-REPLACE_APPROVED_CHANGE_REFERENCE"
umask 077
mkdir -m 700 "$SNAPSHOT_DIR"
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" \
  get deployment postgres litellm-mi-proxy -o json \
  > "$SNAPSHOT_DIR/deployments-before.json"
sha256sum "$SNAPSHOT_DIR/deployments-before.json" > "$SNAPSHOT_DIR/SHA256SUMS"
```

备份根目录选客户批准的仓库外受控位置，已存在且仅保管人可访问；本次子目录必须不已存在，不提交Git。核对旧namespace、镜像、完整spec、原密钥/依赖引用及保管人可取回，并由Owner审核恢复清单及并发变更处理；原始导出不能未经审核直接apply。已执行且未保存时，从可信变更前导出、受控备份或部署历史重建，经Owner审核后明确记录来源，不能把当前配置称为变更前快照。ReplicaSet仅可辅助还原Pod template，`rollout undo`不恢复完整Deployment策略；无法证明原状态则`rollback_snapshot`继续待核验。runtime审核摘要不是完整快照，Stage0数据库备份也不替代Deployment配置。

验收时检查两个Deployment Ready、PG PVC Bound及实际旧客户端/管理操作无回归；在旧Workspace核对真实日志、告警规则与Action Group实际收件（飞书另见[通知指南](../local_execution/feishu-alert-notification-zh.md)）。Action Group样本测试不证明真实告警规则触发；需规则端到端证据时先批准无业务影响的测试。未选来源限制且明确获批接受本次演练范围时，可跳过S1-09/10，但仍须完成三项默认验收，不能推广为客户生产豁免。

| 步骤 | 执行方式 | operation | reviewed_run_id | checked_items / evidence_notes | confirm_environment |
| --- | --- | --- | --- | --- | --- |
| S1-11 | Customer stage acceptance | draft | 留空 | 均留空；main、test、stage=1，查看pending清单 | 留空 |
| S1-12 | 按本节核对旧业务健康、告警实际收件、完整回退材料；已配置legacyAccess时另验证来源准入/拒绝 | - | - | 记录真实结果与证据；不是新增workflow按钮 | - |
| S1-13 | Customer stage acceptance | confirm | S1-11成功且仍有效的draft运行ID | 默认三项全通过填`legacy_health,alerts_received,rollback_snapshot`；已配置来源限制另加`legacy_source_access`，说明逐项真实结果 | test |

当前confirm不支持部分通过；采集/规则已配置不等于邮件已收到，当前Deployment导出不等于变更前快照。没有对应证据的项目保持待核验，不填完整checked_items推进Stage2。

### 阶段2：冻结客户决策

本阶段没有infrastructure组件部署。确定区域/SKU配额、VNet/Pod/Service/Docker网段、模型私网与访问身份、API/admin域名、PG认证/HA/RTO、实际客户端版本、必要管理操作、正文读取者及留存策略。开发人数不能代替数据库大小或一小时停机可行性实测。

一期原生审计在客户JSON顶层加入以下决策，示例7天须由客户批准：

```json
"contentAudit": {
  "mode": "native",
  "retentionDays": 7,
  "contentPolicyAccepted": true
}
```

此时不必提前提供application/proxy镜像；实际发布按所选认证路径要求后台镜像，Entra代理路径另需proxy镜像，native-auth不部署proxy。不要同时开启auditRuntime/auditGovernance或L3 audit_reader/auditTeamId。历史已启用L3的环境需要独立迁移批准，不能直接删除其绑定。

运行`Customer staged migration`：branch=main、environment=test、stage=2、mode=config-check、component=none；随后`Customer stage acceptance`的stage=2 draft，完成决策核验后confirm。原生模式检查为`network_capacity`、`identity_owners`、`pg_auth_ha`、`content_audit_policy`、`protocol_scope`；不再把增强L3的旧`l3_policy`当作必选。

逐项核对本节五项决策，并参考[本地指南Stage2决策范围](../local_execution/stage2-9-guide-zh.md#stage2冻结决策不部署资源)及[部署参考的路径选择](customer-deployment-workflows-zh.md#交付目标与两条路径)。本阶段验收的是获批、可实施的方案及责任分工，不要求提前部署新AKS/PG，也不能把后续HA、恢复或协议测试写成已通过。`contentPolicyAccepted`只确认政策接受，正文读取权限另按原生后台角色或管理代理绑定控制。

| 步骤 | workflow/执行方式 | 输入/操作 | 成功后 |
| --- | --- | --- | --- |
| S2-01 | Customer staged migration | main、test、stage=2、mode=config-check、component=none | 配置合法；不代表配额/网络/政策已核验 |
| S2-02 | Customer stage acceptance | main、test、stage=2、operation=draft；其他确认字段留空 | 取得本Stage pending清单及draft运行ID |
| S2-03 | 按本节及Stage2决策范围人工核对五项 | 保存真实决定、批准记录、后续实施节点和阻断条件 | 未决定或不可行项目先解决，不提交部分通过 |
| S2-04 | Customer stage acceptance | operation=confirm；reviewed_run_id填S2-02有效draft ID；confirm_environment=test | 五项全完成后填写`network_capacity,identity_owners,pg_auth_ha,content_audit_policy,protocol_scope`及真实evidence_notes |

更新完整CUSTOMER_CONFIG_JSON Secret后再运行；同完整Git SHA、环境和有效账本要求沿用第3节。Stage2指纹不自动锁定后续parameters.platform的全部决策，部署前须与受控批准记录核对。Stage3/platform首次plan前就要填齐stage4Network/stage4Aks，不等到Stage4再补；当前confirm不支持部分通过。

### 阶段3：供应链与目标基础

**开始前：** Stage2已验收；准备deploy身份、ACR唯一名称、目标Workspace以及完整stage4Network/stage4Aks配置。Stage3只创建平台早期资源，不创建新PG，不关闭旧模型账号的访问路径；Stage4才建立私网连接。

先运行`Check public source image`，只选main，没有environment/stage。核对`source-image-<run ID>`中的source-summary、SBOM及扫描结果；当前阻断策略为可修复CRITICAL，不是零漏洞或上游发布者签名证明。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S3-01 | Customer infrastructure deployment | 3 | component=platform | plan | 留空 | 留空 |
| S3-02 | Customer infrastructure deployment | 3 | component=platform | deploy | S3-01的plan ID | test |

**检查结果：** 新ACR属于目标RG、公网禁用，旧环境不受影响。此时不为测试拉取而打开ACR公网；源镜像扫描与目标ACR签名/拉取是两个不同阶段的检查。

**阶段验收：** stage=3 draft/confirm；`oidc_scope`、`source_image_sbom_scan`、`target_isolation`。旧名称`image_signature_sbom`不再是Stage3的清单，具体用draft输出。

三项操作及通过标准按本节检查ID逐项核对，资源与源镜像检查顺序见[本地指南Stage3](../local_execution/stage2-9-guide-zh.md#stage3供应链检查和目标基础资源)。source-image附件是明文，核对固定1.104.0基线、派生镜像SBOM和扫描策略结果，不需要解密；platform计划/部署及验收附件仍使用原WORKFLOW_ARTIFACT_KEY，按第3节核对当前revision/config及实际资源。Stage3的ACR公网关闭但尚未建立本阶段之外的私网连接，不要求提前完成Stage4目标镜像签名/拉取。

| 步骤 | 执行方式 | operation | reviewed_run_id | checked_items / evidence_notes | confirm_environment |
| --- | --- | --- | --- | --- | --- |
| S3-03 | Customer stage acceptance | draft | 留空 | 均留空；main、test、stage=3，查看pending清单 | 留空 |
| S3-04 | 按Stage3指南人工核对OIDC、源报告和实际目标隔离 | - | - | 保留真实运行/资源及旧业务观察证据；不是新增workflow按钮 | - |
| S3-05 | Customer stage acceptance | confirm | S3-03成功且仍有效的draft运行ID | 三项全通过填`oidc_scope,source_image_sbom_scan,target_isolation`，说明逐项真实结果 | test |

当前confirm不支持部分通过。源扫描或部署回执的stageAccepted=false正常，不手改报告；输出里的未来AKS/PG名称不代表这些资源已创建，须核对阶段开关和实际资源。

### 阶段4：私网、Private AKS与身份

**开始前：** Stage3已验收；目标VNet/PE子网由Stage0建立。核对AKS版本、节点SKU、区域限制/配额、网络段和Firewall出站；模型连接填真实账号Resource ID及别名。共享模型账号的公网/Local Auth关闭不能先于旧业务依赖核对，不能误伤其他使用者。

**先完成4-0的模型授权再执行S4-01。** 这是管理员一次性准备，不是新增workflow按钮；同订阅也要核对，不能只检查新AKS所在RG。多订阅是受支持的模型容量来源，不应为了绕过授权错误而删除已批准的模型连接。

**首次platform部署的角色命名：** 新目标AKS/工作负载身份尚未创建时，在客户JSON的`parameters.platform`加入`"stage4RoleAssignmentNaming":"resource-id"`，新示例已提供。该模式按目标AKS/UAMI资源ID、授权资源及角色生成稳定的角色分配名称，实际principalId仍取创建后的正确身份输出，授权范围不扩大。旧模式用尚未知的Object ID计算角色分配名称，会使What-if返回`Unsupported`并被门禁拒绝；不能把Unsupported当作通过。已有Stage4角色分配时保持原模式，省略字段等同`principal-id`，不要直接切换以免产生重复角色分配。首次部署后Stage5继续使用同一模式；AKS/UAMI若被删除重建并改变Principal ID，须单独审核旧授权处理，不能自动删除或覆盖旧角色。此字段仅从Stage4绑定配置指纹，代码合并后的同SHA证据要求仍按第3节执行。

**本阶段配置：** 先按4-A填写`parameters.certificate-vault`，由S4-03/04创建两套证书共用的**独立证书Vault**；不是合并Stage5/7业务Vault。此时可不启用顶层privateIngress，不要求Vault内已有证书。S4-04之后按4-C手动导入，入口发布前再补privateIngress的API/admin `tlsSecretId`和`allowedCidrs`，结构见[私有入口参考](customer-deployment-workflows-zh.md#stage4自动私有入口)。

自动API签发可选：另配`AZURE_CERTIFICATE_CLIENT_ID`、已委派Azure DNS Zone以及顶层`certificates={zoneResourceId,termsAccepted:true,publicApiHostnameAccepted:true}`。API tlsSecretId必须无版本；当前自动流程不签Admin源站证书。选择Stage9 Admin Front Door后，Admin源站证书也必须由根在Microsoft Trusted CA List中的公有CA签发，并由客户在4-C手工导入；企业内部CA或自签名证书只能用于Stage9前的隔离演练，不能作为Front Door回源。未选择自动签发则跳过S4-09/10，按4-C手工导入两套证书。本组件不为自动签发身份授予整库权限。来源CIDR覆盖实际Runner私网路径；PLS NAT子网由workflow自动加入两个平面。

#### 4-0. 模型账号的一次性授权

**适用范围与分工。** 本节用于同一Entra租户内、同订阅或跨订阅的Azure OpenAI模型账号；跨租户身份不属于本节已支持路径。根据`parameters.platform.azureOpenAIConnections`逐个核对，每个不同模型RG和账号都要覆盖。角色分配模块使用该连接自己的订阅/RG，Private Endpoint使用完整accountResourceId，不要求把模型迁到网关订阅。各订阅quota仍独立，后续由LiteLLM路由利用多个兼容部署的容量。

| 身份 | 本阶段权限 | 由谁准备 |
| --- | --- | --- |
| 部署身份，GitHub Environment的`AZURE_CLIENT_ID`对应UAMI/企业应用服务主体 | 模型RG的ARM部署/What-if及私网连接审批；模型账号范围的受限角色分配 | 有权管理员在首次plan前一次性授予 |
| 新LiteLLM工作负载身份 | 每个获批模型账号的`Cognitive Services OpenAI User`调用权限 | platform部署按实际工作负载Principal ID创建，不给它部署管理员角色 |
| 现场操作人/客户IT | 有权创建或复用批准的角色定义、分配角色；PIM资格须先激活 | 按客户授权政策准备，不把个人权限视作Actions权限 |

**1. 取得客户自己的标识。** 下面的变量是受控终端中的本机变量，不是新增GitHub配置。模型订阅可以与`azure.subscriptionId`不同；主订阅和workflow登录变量保持不变。

| 值 | 获取与核验方法 |
| --- | --- |
| `DEPLOY_CLIENT_ID` | 客户仓库Settings → Environments → 本次环境 → Variables中的`AZURE_CLIENT_ID`。不是`AZURE_RUNTIME_CLIENT_ID`；演练复用身份不代表客户也应合并 |
| `DEPLOY_OBJECT_ID` | UAMI：Azure Portal → Managed Identities → 该身份Overview的Principal ID；企业应用：Entra ID → Enterprise applications，按上述Application ID查找，取Object ID，并核对Client ID。不能使用App registrations的应用对象Object ID或现场人员ID |
| `MODEL_SUBSCRIPTION_ID`、`MODEL_RG`、`MODEL_ACCOUNT`、`MODEL_ACCOUNT_SCOPE` | 从每个模型账号Overview及JSON View取得，与azureOpenAIConnections的subscriptionId、resourceGroupName、accountName、accountResourceId逐项比较；账号scope须以`/providers/Microsoft.CognitiveServices/accounts/<账号名>`结尾，不是模型deployment子资源 |

在已登录客户正确租户的获准管理终端，只读查询部署服务主体和模型账号；目录读取失败时请Entra管理员核验，UAMI也可按0-A1的`az identity show`方法取得Principal ID：

```bash
az account show --query '{tenantId:tenantId,subscriptionId:id,identityType:user.type}' --output json
DEPLOY_CLIENT_ID="REPLACE_AZURE_CLIENT_ID_FROM_ENVIRONMENT"
az ad sp show --id "$DEPLOY_CLIENT_ID" \
  --query '{name:displayName,clientId:appId,objectId:id,type:servicePrincipalType}' --output json
DEPLOY_OBJECT_ID="REPLACE_VERIFIED_DEPLOY_SERVICE_PRINCIPAL_OBJECT_ID"
MODEL_SUBSCRIPTION_ID="REPLACE_MODEL_SUBSCRIPTION_ID"
MODEL_RG="REPLACE_MODEL_RESOURCE_GROUP"
MODEL_ACCOUNT="REPLACE_MODEL_ACCOUNT_NAME"
az account show --subscription "$MODEL_SUBSCRIPTION_ID" \
  --query '{tenantId:tenantId,subscriptionId:id}' --output json
az cognitiveservices account show --subscription "$MODEL_SUBSCRIPTION_ID" \
  --resource-group "$MODEL_RG" --name "$MODEL_ACCOUNT" \
  --query '{id:id,name:name,resourceGroup:resourceGroup}' --output json
```

确认两个订阅属于同一客户租户。没有相应订阅访问或资源值不匹配时停止，不改配置掩盖差异。授权时Portal的Members选择**Managed identity**，在身份所在订阅查找UAMI，该订阅未必是模型订阅；企业应用使用**User, group, or service principal**选项并按已核对的应用标识定位，不能只按显示名称猜对象。

**2. 创建或复用模型部署角色，再在每个模型RG分配。** 建议使用下述不含角色管理能力的自定义角色`LLMGW Model Deployment Operator`。已有同名角色时先核对权限和Assignable scopes，符合则复用，不因重跑再创建或擅自修改其他团队的角色。

管理员从**模型资源组 → Access control (IAM) → Add → Add custom role**进入。创建自定义角色需要在所有Assignable scopes上具备`Microsoft.Authorization/roleDefinitions/write`；只有`Role Based Access Control Administrator`不包含该权限。角色分配需要的是另一项`Microsoft.Authorization/roleAssignments/write`。有RG级User Access Administrator但无订阅级定义写权限时，从RG入口创建且scope仅保留获批RG；仍无权限则请管理员预建角色，不申请订阅Owner或自制管理员等价角色绕过限制。已生效PIM权限下Portal按钮仍灰色时先刷新/重新进入，尚未激活的Eligible资格不能当作有效授权。

在JSON页点击Edit，使用下面的**Portal角色定义**，不是写入CUSTOMER_CONFIG_JSON。`*/read`、`Microsoft.Resources/deployments/*`不能在Add permissions选择器中逐项找到，必须用JSON页；私网审批动作属于Microsoft.CognitiveServices，不是Microsoft.Network。用客户模型RG的完整ARM ID替换两个示例scope，只有一个RG时删去第二项，多个RG时逐项加入；不要保留整个订阅scope。

```json
{
  "properties": {
    "roleName": "LLMGW Model Deployment Operator",
    "description": "Read model resource metadata, manage ARM deployments and approve private endpoint connections in approved resource groups.",
    "assignableScopes": [
      "/subscriptions/REPLACE_MODEL_SUBSCRIPTION_ID_1/resourceGroups/REPLACE_MODEL_RG_1",
      "/subscriptions/REPLACE_MODEL_SUBSCRIPTION_ID_2/resourceGroups/REPLACE_MODEL_RG_2"
    ],
    "permissions": [{
      "actions": [
        "*/read",
        "Microsoft.Resources/deployments/*",
        "Microsoft.CognitiveServices/accounts/privateEndpointConnectionsApproval/action"
      ],
      "notActions": [],
      "dataActions": [],
      "notDataActions": []
    }]
  }
}
```

Save → 核对Assignable scopes → Review + create。通配符包含匹配的未来管理操作，`*/read`覆盖所授RG内所有资源的管理面元数据，私网审批也覆盖该RG内的模型账号，须由客户批准；它不包含读取账号Key、模型数据调用、模型资源写删或角色分配权限。Private Endpoint本体部署在网关目标RG，沿用该侧既有网络授权；本角色不能代替目标RG权限或资源提供程序注册。相关订阅的Microsoft.Network、Microsoft.CognitiveServices注册由有权管理员核对，不为此给日常应用订阅权限。

角色定义创建成功不等于已授权。分别打开**每个模型RG → IAM → Add role assignment**，选择该自定义角色，Members选择已核实的部署身份并Review + assign。多个账号在同一RG时无需重复这条RG授权；不授给现场人员、Runner VM身份或新LiteLLM工作负载身份。

**3. 在每个模型账号授予受限制的角色管理能力。** 模型账号 → IAM → Add role assignment → Privileged administrator roles，选择**Role Based Access Control Administrator**（`f58310d9-a9f6-439a-9e8d-f62e7b41a168`），Members仍选择部署身份。在Conditions选择**Allow user to only assign selected roles to selected principals (fewer privileges)** → Select roles and principals，按客户批准策略选择：

| 条件模板 | 配置与实际边界 |
| --- | --- |
| `Constrain roles` | Roles只选`Cognitive Services OpenAI User`（`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`）。限制可分配/删除的角色，但不限制接收者，可给用户、组或服务主体授予该角色。客户明确接受此范围时可用，功能上满足当前模板，不宣称主体隔离 |
| `Constrain roles and principal types` | 推荐的收紧方式：同一角色，Principal types只选`Service principals`；仍允许该账号上其他服务主体获得此角色，不是仅限本次LiteLLM身份 |
| `Constrain roles and principals` | 新工作负载身份已存在、其Principal ID已验证时，可进一步只允许该身份；不能在首次创建前猜测Object ID |

Save → Review + assign。检查条件版本为`2.0`，**同时限制新增和删除**：`Microsoft.Authorization/roleAssignments/write`使用`@Request`属性、`Microsoft.Authorization/roleAssignments/delete`使用`@Resource`属性，两者均只允许上述模型调用RoleDefinitionId。若选择主体类型/具体主体限制，两条动作分别以AND加入相应限制，不用OR放宽。不要选择无条件管理员授权或包含Owner、User Access Administrator等其他角色。

已有授权需要调整时，从账号IAM → Role assignments → 对应部署身份的Condition **View/Edit**进入，不删除重建。记录中的顶层`principalType=ServicePrincipal`只表示接收RBAC管理员授权的部署身份类型，不能代替条件里的`PrincipalType`限制。操作人已有的带条件User Access Administrator若禁止转授RBAC Administrator，必须由有权管理员明确批准并配置这项委派；自定义部署角色不解决该限制。

**4. 回读授权并以真实Actions身份验证。** 以下只读查询逐个模型账号执行；MODEL_ACCOUNT_SCOPE来自第1步账号id，不填订阅、RG或模型deployment的ID。模型账号上的查询使用include-inherited，同时检查继承的RG部署角色和账号直接RBAC角色：

```bash
MODEL_ACCOUNT_SCOPE="REPLACE_VERIFIED_MODEL_ACCOUNT_RESOURCE_ID"
az role definition list --subscription "$MODEL_SUBSCRIPTION_ID" \
  --name "LLMGW Model Deployment Operator" \
  --query '[].{id:id,role:roleName,assignableScopes:assignableScopes,permissions:permissions}' --output json
az role assignment list --subscription "$MODEL_SUBSCRIPTION_ID" \
  --assignee-object-id "$DEPLOY_OBJECT_ID" --scope "$MODEL_ACCOUNT_SCOPE" \
  --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,roleDefinitionId:roleDefinitionId,scope:scope,principalId:principalId,principalType:principalType,condition:condition,conditionVersion:conditionVersion}' --output json
```

通过条件：部署身份Object ID匹配；自定义角色权限/可分配范围符合批准内容，实际分配在模型RG；受限RBAC管理员角色实际分配在**模型账号级**，条件与客户选择一致。还要核对组成员、继承角色和条件，不能仅凭角色显示名称判断有效权限；已有更宽授权不会被新增条件自动收紧。管理面读取成功不是模型调用或数据面私网已通过。

授权传播生效后，从**Run workflow新建S4-01**，选择main、environment=test、stage=4、component=platform、operation=plan，release不勾选，两个approved字段及confirm_environment均留空。仅补Azure授权且代码SHA、阶段配置和artifact key不变时，无需因此重录已有有效验收；独立plan本身不要求前序验收，但deploy仍检查同SHA有效的Stage0–3账本。合并文档或代码产生新SHA时仍按第3节复核，不能把本段当作跨版本豁免。

先确认plan整个workflow成功，解密并审核实际资源/RBAC/网络变化后才运行S4-02 deploy，引用本次成功plan的run ID。个人账号的本地plan成功不能替代Actions身份验证；IAM查询也不能保证不存在Policy、配额、传播或其他阻断。如果仍失败，保留新run链接并定位具体错误，不删除跨订阅模型连接、不移除What-if门禁或直接扩为订阅Owner。参考[自定义角色JSON编辑](https://learn.microsoft.com/en-us/azure/role-based-access-control/custom-roles-portal)和[受限角色委派](https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-portal)。

#### 4-A. 共用证书Vault的配置与取值

在客户JSON的`parameters`内增加以下块，再同步完整Environment Secret `CUSTOMER_CONFIG_JSON`。这些是客户JSON字段，不是新增GitHub Variables/Secrets：

```json
"certificate-vault": {
  "vaultName": "REPLACE_GLOBALLY_UNIQUE_CERTIFICATE_VAULT_NAME",
  "ingressReaderPrincipalId": "REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID",
  "certificateImporterPrincipalId": "REPLACE_APPROVED_CERTIFICATE_IMPORTER_OBJECT_ID",
  "certificateImporterPrincipalType": "User",
  "runnerVirtualNetworkId": "REPLACE_RUNNER_VNET_RESOURCE_ID",
  "createPrivateDnsZone": true,
  "manageTargetDnsLink": true,
  "manageRunnerDnsLink": true
}
```

| 字段 | 含义、获取及核验方法 |
| --- | --- |
| `vaultName` | 客户批准的新证书Vault名称，3–24位小写字母/数字/连字符，字母开头、字母数字结尾，不连续连字符；不得用后台`kv-lt-`名称。名称需全局唯一；在Portal创建Key Vault表单只检查名称可用性，不点击创建；已删除但保留中的同名Vault须由Owner处理，不擅自purge |
| `ingressReaderPrincipalId` | 从所选Environment的`AZURE_RUNTIME_CLIENT_ID`定位服务主体，按0-A1执行`az ad sp show --id "$RUNTIME_CLIENT_ID" --query '{clientId:appId,objectId:id}' --output json`；取objectId。UAMI取Principal ID，不能填Client ID或AKS应用工作负载身份 |
| `certificateImporterPrincipalId` / `certificateImporterPrincipalType` | 客户批准的人工证书导入人或组，不自动取当前登录者/ownerEmail。Entra ID → Users或Groups → 指定对象 → Object ID，类型对应User或Group；用户可按0-A1查询，组用`az ad group show --group "REPLACE_APPROVED_GROUP_OBJECT_ID" --query '{name:displayName,objectId:id}' --output json`核验。必须与读取身份不同 |
| `runnerVirtualNetworkId` | 从实际Runner VM → NIC → IP configurations → VNet → JSON View取完整id，方法同0-A2；可复用parameters.runner-connectivity中已核验的值。只支持同订阅VNet，可跨RG或与目标VNet相同，不填子网/VM ID |
| `createPrivateDnsZone` | 在目标RG的Private DNS zones中查`privatelink.vaultcore.azure.net`。本组件创建/持续管理用true；已有区域由其他Owner管理则false复用。目前只支持目标RG内的区域，跨RG集中DNS须先扩展实现，不新建冲突区域绕过 |
| `manageTargetDnsLink` / `manageRunnerDnsLink` | 查看该zone的Virtual network links及两个VNet的DNS servers。Azure提供DNS且无既有链接时true；自定义DNS或复用不同名链接时对应false，由DNS Owner验证解析/转发。目标链接固定`<目标VNet>-link`、Runner链接固定`certificate-runner-link`，不启用自动注册；同VNet不重复建链接 |

目标VNet/PE子网和日志工作区直接取`parameters.platform.stage4Network`及`logAnalyticsWorkspaceName`，不用再次填另一套网络。组件不新建Peering、改NSG/路由；沿用Stage0批准路径，DNS链接不等于网络可达。

Vault使用Standard SKU，禁用公网、RBAC模式、90天软删除、清除保护、防删除锁和AuditEvent诊断；只保存证书材料。读取身份获此Vault范围的Key Vault Secrets User，导入人/组获此Vault范围的Key Vault Secrets Officer，后者有写入/删除Secret能力，须明确批准。不会授予LiteLLM Pod身份读取入口私钥的权限，也不撤销客户已有继承授权；已有宽泛角色须单独核验，不能据此宣称身份已经完全隔离。

Stage5复用该DNS区域和链接：配置了certificate-vault时，`parameters.platform.createStage5KeyVaultPrivateDnsZone`必须为false，工具自动禁用Stage5的Vault链接管理，PG/RedisDNS不受影响。不要同时让两个组件管理同一区域；新示例已设false，旧配置在Stage5前核对，改动早期platform字段仍按第3节处理证据。

计划会拒绝未标记为`purpose=ingress-certificates`的既有同名Vault、自定义DNS下的自动链接、同VNet不同名链接及同名链接目标/注册设置冲突。不会自动接管业务Vault、扩大VNet权限或读取/创建Secret值；关闭管理开关不会自动删除已有资源。

#### 4-A1. 证书Vault防删除锁权限

**S4-03首次plan前核对，不等deploy失败再补。** [证书Vault模板](../infra/certificate-vault/main.bicep)会在Vault上创建`protect-key-vault-from-deletion`，级别为`CanNotDelete`。Azure What-if也需要相应部署权限。Contributor的NotActions排除了`Microsoft.Authorization/*/Write`，而Role Based Access Control Administrator只补充角色分配写入/删除能力，两者相加仍不包含`Microsoft.Authorization/locks/write`。Key Vault Secrets User/Officer的数据权限也不能替代锁管理权限。platform成功不证明新证书组件的权限已齐全。

**1. 确定授权对象和范围。** 按4-0第1步，由所选GitHub Environment的`AZURE_CLIENT_ID`查询部署UAMI的Principal ID或企业应用服务主体Object ID，作为`DEPLOY_OBJECT_ID`；不是Client ID本身、人工证书导入人、`ingressReaderPrincipalId`或Pod身份。演练中deploy/runtime复用身份不改变客户侧的识别方法。目标订阅/RG从客户JSON的`azure.subscriptionId`、`target.resourceGroup`取得，再与Portal目标RG的JSON View `id`交叉核验；**不是模型所在RG，也不是Runner RG**。

**2. 由有权管理员创建或复用最小锁角色。** 在目标RG → Access control (IAM) → Add → Add custom role，从JSON页Edit填入下列通用定义；替换scope占位符后Save → Review + create。这是Portal角色定义，不写入CUSTOMER_CONFIG_JSON，不需要修改workflow或证书配置。创建者须在Assignable scopes上具备`Microsoft.Authorization/roleDefinitions/write`；只有RBAC Administrator时不能创建自定义角色，可请管理员预建，不能自行扩大为订阅Owner。已有同名角色先核对内容，符合则复用，不覆盖其他团队的定义。

```json
{
  "properties": {
    "roleName": "LLMGW Resource Lock Writer",
    "description": "Read, create and update resource locks in the approved gateway resource group; no lock deletion or secret access.",
    "assignableScopes": [
      "/subscriptions/REPLACE_TARGET_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
    ],
    "permissions": [{
      "actions": [
        "Microsoft.Authorization/locks/read",
        "Microsoft.Authorization/locks/write"
      ],
      "notActions": [],
      "dataActions": [],
      "notDataActions": []
    }]
  }
}
```

然后在**目标RG → IAM → Add role assignment**中选择`LLMGW Resource Lock Writer`，Members选择上述部署身份并Review + assign；角色定义存在不等于已分配。UAMI从其实际所在订阅选择Managed identity，企业应用按已核对的服务主体选择。此时Vault可能尚不存在，因此首次按目标RG授权，不为创建它预先授予整个订阅权限。

该角色本身不授予`Microsoft.Authorization/locks/delete`、角色分配或Secret访问权限；`locks/write`仍允许创建/更新所授RG范围内的锁及锁级别，不是只允许某一个Vault锁，也不是只能设置CanNotDelete，须由客户批准。其他既有角色仍可能提供更宽权限，不能把本角色未包含delete视为全局拒绝。保留模板的防删除锁、软删除及清除保护，不通过删锁资源、关闭保护或放宽What-if门禁解决权限缺口。

**3. 回读并重跑证书plan。** 下面只读核验角色定义和实际分配；变量来源同第1步，不从当前登录人推断部署身份，确认无REPLACE后执行：

```bash
TARGET_SUBSCRIPTION_ID="REPLACE_AZURE_SUBSCRIPTION_ID_FROM_CUSTOMER_JSON"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP_FROM_CUSTOMER_JSON"
DEPLOY_OBJECT_ID="REPLACE_VERIFIED_DEPLOY_SERVICE_PRINCIPAL_OBJECT_ID"
TARGET_RG_SCOPE="/subscriptions/${TARGET_SUBSCRIPTION_ID}/resourceGroups/${TARGET_RG}"
az role definition list --subscription "$TARGET_SUBSCRIPTION_ID" \
  --name "LLMGW Resource Lock Writer" \
  --query '[].{id:id,role:roleName,assignableScopes:assignableScopes,permissions:permissions}' --output json
az role assignment list --subscription "$TARGET_SUBSCRIPTION_ID" \
  --assignee-object-id "$DEPLOY_OBJECT_ID" --scope "$TARGET_RG_SCOPE" \
  --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,roleDefinitionId:roleDefinitionId,scope:scope,principalId:principalId,condition:condition}' --output json
```

确认角色定义只有上述两项Actions且DataActions为空，实际分配指向正确部署Object ID和目标RG；同时核对继承/组授权、条件及有效期。权限传播生效后，从Run workflow新建S4-03（test、stage=4、component=certificate-vault、operation=plan，release=false，两个approved字段及confirm_environment留空）。plan成功并解密审核后，S4-04引用本次成功plan ID执行deploy；这一步不上传证书。

仅补锁权限且代码SHA、阶段配置、artifact key不变时，不需重跑已成功的platform或因此重录验收。文档或代码合并产生新SHA时，deploy前仍按第3节处理证据绑定。个人账号本地plan成功不能替代Actions验证；失败时先读日志中的`Context`、`Result category`、`Error`和`Source`，不能仅凭一个错误类别认定锁权限是唯一原因。仍失败时保留新run链接和加密`operation-status.json`再定位。参考[Azure资源锁权限](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources#who-can-create-or-delete-locks)。

#### 4-B. 按顺序运行

旧版手册的S4-03之后编号已顺延，按本表的component/action辨认；不是重跑已经完成的平台部署。新增DNS连接使用S4-04A/04B，不再次改动S4-05及后续编号。S4-03也可先用`Customer staged migration`的stage=4、mode=config-check、component=certificate-vault检查参数。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S4-01 | Customer infrastructure deployment | 4 | component=platform | plan | 留空 | 留空 |
| S4-02 | Customer infrastructure deployment | 4 | component=platform | deploy | S4-01的plan ID | test |
| S4-03 | Customer infrastructure deployment | 4 | component=certificate-vault | plan | 留空 | 留空 |
| S4-04 | Customer infrastructure deployment | 4 | component=certificate-vault | deploy | S4-03成功且已审核的plan ID | test |
| S4-04A | Customer infrastructure deployment | 4 | component=runner-target-connectivity | plan | 留空；先完成4-B1配置和权限 | 留空 |
| S4-04B | Customer infrastructure deployment | 4 | component=runner-target-connectivity | deploy | S4-04A成功且已审核的plan ID | test |
| S4-05 | Customer private runtime operations | 4 | action=cluster-bootstrap | plan | 留空 | 留空 |
| S4-06 | Customer private runtime operations | 4 | action=cluster-bootstrap | execute | S4-05的plan ID | test |
| S4-07 | Customer private runtime operations | 4 | action=monitoring-onboard | plan | 留空 | 留空 |
| S4-08 | Customer private runtime operations | 4 | action=monitoring-onboard | execute | S4-07的plan ID | test |
| S4-09 | Customer private runtime operations | 4 | action=certificate-renew | plan | 留空；可选自动API签发 | 留空 |
| S4-10 | Customer private runtime operations | 4 | action=certificate-renew | execute | S4-09的plan ID | test |
| S4-11 | Customer private runtime operations | 4 | action=private-ingress | plan | 留空 | 留空 |
| S4-12 | Customer private runtime operations | 4 | action=private-ingress | execute | S4-11的plan ID | test |

**S4-02部分失败时：** 先读GitHub结构化`Error`和`Source`，再到目标RG → Deployments → `llmgw-<environment>-s4-platform`查看失败子部署及Operation details；公开摘要经过脱敏，不能替代ARM原始详情。`aksNetwork`若报`AnotherOperationInProgress`，先核对失败子网和错误中指定的网络操作状态；同一VNet的子网并行写入会发生冲突，模板应通过dependsOn依次更新系统、业务、入口子网。即使顶层Failed，Firewall、DNS、Private Endpoint或其他子网也可能已经成功并产生费用，须逐项核对，不能声称自动回滚或删除已成功资源。冲突操作结束、模板修复经审核后，新建S4-01并审核当前状态下的增量变化，再用新的成功plan ID执行S4-02；旧plan和Re-run jobs不能代替重新审批，也不要回放Stage0网络模板或关闭门禁。代码修复合并产生新SHA时，deploy前仍须按第3节复核前序验收；后续AKS/身份资源是否创建以实际状态为准。

**S4-04之后、S4-05之前：** 按4-C完成获批证书导入；走临时受限公网路径时随后立即关闭Vault公网，已通过批准私网导入的不重复上传。按4-B1完成S4-04A/04B的Runner到AKS DNS连接；由网络Owner核验管理VNet到新AKS/ACR/证书Vault的路由、Private DNS和允许端口。管理员补齐Kubernetes数据权限后，运行`Customer private runner checks`，environment=test、check_target=true、check_backup=false。该检查不读取证书Secret，Vault访问和CA信任另按4-C核验；私网检查失败不重新开放公网掩盖问题，私有AKS始终不开放公网。

`cluster-bootstrap`实际创建litellm和两个ingress命名空间，不安装完整应用或自动授予所有Kubernetes权限。`private-ingress`会扫描/晋级固定Traefik镜像，创建API/admin两套私有入口并核验TLS、Host和内部LB前端；它不是Entra登录或模型调用测试。certificate-renew只更新Vault，不发布证书到入口，S4-11/12仍需运行。

#### 4-B1. Runner到新AKS的DNS连接

**标准交付由IaC管理链接，管理员负责预授权，检查workflow保持只读。** `runner-target-connectivity`复用现有Customer infrastructure deployment，不新增独立workflow。它在Stage4 platform成功后，分别从真实AKS和ACR发现私有API/registry域名、DNS区域、Private Endpoint、NIC及A记录；只创建或维护两个zone到获批Runner VNet的链接，不让客户手填系统DNS区域中的随机GUID，也不把ACR公网403误判为镜像权限问题。

在客户JSON的`parameters`中加入下面的块，并同步完整Environment Secret `CUSTOMER_CONFIG_JSON`；不是放到`parameters.platform`内，也不是新增GitHub Variable：

```json
"runner-target-connectivity": {
  "runnerVirtualNetworkId": "REPLACE_RUNNER_VNET_RESOURCE_ID",
  "manageDnsLink": true
}
```

| 值 | 来源与边界 |
| --- | --- |
| `runnerVirtualNetworkId` | 实际执行Actions的Runner VM → NIC → IP configurations → VNet → JSON View的完整id；可复用0-A2或4-A已核验的同名值，组件间须一致。不是开发机VNet、Bastion子网、VM或NIC ID。目前支持同订阅，可跨RG/区域或与目标VNet相同 |
| `manageDnsLink` | Runner VNet → DNS servers为Azure提供DNS时用true（省略也为true），同时管理AKS API和`privatelink.azurecr.io`链接；企业DNS/Private Resolver由网络Owner管理时显式false，并另行核验两类转发。false不自动删除已有链接，也不证明外部DNS已可用 |
| 目标AKS及VNet | 从既有`target.resourceGroup`、`parameters.platform.stage4Aks.name`和`stage4Network.virtualNetworkName`取得，组件再与云上AKS身份、节点池子网核对，不新填第二套集群信息 |
| 目标ACR | 从`parameters.platform.containerRegistryName`取得；组件核对ACR状态、公网关闭、批准的registry PE/NIC、`privatelink.azurecr.io`中登录端点和区域data端点A记录，不新填资源ID或IP |
| 实际Private DNS区域 | AKS的`apiServerAccessProfile.privateDnsZone=system`时，从实际privateFqdn和nodeResourceGroup发现；自定义zone模式使用AKS返回的完整zone ID。ACR区域固定从目标RG内实际`privatelink.azurecr.io`发现。支持Azure公有云、同订阅zone；缺失或其他云配置停止，不猜测区域或关闭校验 |

**1. 预授权和只读核验。** 授权对象是所选Environment的`AZURE_CLIENT_ID`对应部署服务主体，不是人工导入人或`AZURE_RUNTIME_CLIENT_ID`对应的日常运行身份。需要第2.2节列出的管理面操作：`Microsoft.Network/privateDnsZones/virtualNetworkLinks/write`在AKS和ACR两个实际zone范围、`Microsoft.Network/virtualNetworks/join/action`在精确Runner VNet范围；两个DNS所在RG还需嵌套ARM部署/What-if权限。AKS系统区域通常位于**节点RG**，ACR区域位于目标RG，不能只给其中一个RG权限就认为已覆盖；不为方便扩大到订阅Owner，不给runtime身份DNS写权限。

管理员可从AKS → Properties/JSON View取得nodeResourceGroup、privateFqdn和apiServerAccessProfile，或在管理终端用客户JSON提供的订阅/RG/AKS名只读查询：

```bash
SUBSCRIPTION_ID="REPLACE_AZURE_SUBSCRIPTION_ID_FROM_CUSTOMER_JSON"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP_FROM_CUSTOMER_JSON"
TARGET_AKS="REPLACE_STAGE4_AKS_NAME_FROM_CUSTOMER_JSON"
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" \
  --query '{id:id,state:provisioningState,power:powerState.code,nodeResourceGroup:nodeResourceGroup,privateFqdn:privateFqdn,dns:apiServerAccessProfile.privateDnsZone}' \
  --output json --only-show-errors
```

再分别检查AKS DNS所在RG和目标RG → Private DNS zones → AKS区域及`privatelink.azurecr.io` → Virtual network links，核对目标VNet链接和已有Runner链接。ACR区域还应有registry登录记录和区域data记录，地址集合与ACR PE NIC一致。已有不同名但正确的Runner链接时组件只读复用，不接管其他Owner资源；同VNet场景复用原有链接。自动管理名称分别由AKS/ACR和Runner VNet资源ID稳定计算。关闭自动注册（registrationEnabled=false），已有链接须Succeeded/Completed；名称冲突、重复链接、自动注册开启或未完成时停止，不能删除重建来掩盖问题。

**2. 按S4-04A/04B执行。** 可先用Customer staged migration的main、test、stage=4、mode=config-check、component=runner-target-connectivity验证离线配置。真实计划使用Customer infrastructure deployment：main、test、stage=4、component=runner-target-connectivity、operation=plan，release不勾选、两个approved字段和confirm_environment留空。旧Customer staged migration的what-if入口不支持本组件的云端发现，不能代替这个plan。

plan会检查原`llmgw-<environment>-s4-platform`部署Succeeded、AKS Running/Succeeded、ACR Succeeded且公网关闭，并核对两套批准PE/NIC及DNS记录与实际私有IP一致；记录与PE不一致会阻断并要求排查，不自动覆盖A记录。自动模式只允许两个实际zone下的固定Runner链接和DNS所在RG中的固定嵌套部署记录，继续禁止Delete、Unsupported和越界变更。不修改AKS、ACR、托管PE、DNS区域/A记录、Peering、NSG、路由或任何RBAC；此组件不替代既有私网路由准备。

解密S4-04A的`infrastructure-test-4-runner-target-connectivity-<run ID>`，审核`plan-summary.json`、`reviewed-plan.json`和`connectivity-review.json`。后者同时列出AKS的`aksResourceId/apiHostname/privateDnsZoneId/privateEndpointIps/management`和ACR的`acrResourceId/acrLoginServer/acrPrivateDnsZoneId/acrPrivateEndpointIps/acrManagement`，以及`runnerVirtualNetworkId`。managed表示本组件管理，reused表示复用外部既有链接，external表示客户管理DNS。不得把reused/external当作运行探针已通过。

审核后新Run workflow，operation=deploy、approved_run_id填S4-04A的成功plan ID、confirm_environment=test，其余按第3节。deploy会重新发现实际资源并执行What-if；代码、配置、zone、PE地址、链接管理状态或变更内容变化时必须重新plan。成功后管理模式再次核对AKS和ACR两个Runner链接均已Completed；输出`runnerTargetConnectivity`仍不是DNS/TLS/Kubernetes已实测或Stage4验收。

**3. 真实Runner检查与权限。** 管理员按[4-B2](#4-b2-runtime身份的kubernetes预授权)授予runtime身份获批的Kubernetes数据权限，再运行Customer private runner checks：main、test、check_target=true、check_backup=false。Contributor/RBAC Administrator不等于Kubernetes数据角色；该检查需要`litellm`命名空间的Deployment读取，后续cluster-bootstrap创建命名空间另需集群级相应权限，不能把Reader当作全部后续权限。组件不获取kubeconfig、不代替OIDC身份执行kubectl、不自动发起验收。

**已经部署过platform的升级路径：** 本次只增加连接组件、既有platform/certificate-vault仍成功且实际资源与批准配置一致时，**不用重跑platform或certificate-vault部署，也不用重新上传证书**。代码合并到受保护main后，再补新配置并同步Secret；旧代码不认识新字段。single-operator可直接复用仍在7天内且配置指纹匹配的Stage0–3账本，无需仅因新SHA依次draft/confirm；源镜像或供应链检查逻辑受影响、记录过期或实际结论变化时仍须实测重验。新块只从Stage4进入配置指纹，不改变Stage0–3的stage-config指纹；旧full-config记录只有完整配置哈希仍匹配时才能复用。双人模式继续按第3节在新SHA重验。

随后直接从S4-04A新plan、审核、S4-04B deploy开始，再做Runner检查、S4-05及后续操作。组件读取旧platform的成功部署和当前AKS，不要求旧platform回执SHA等于新SHA；本次deploy在single-operator下接受有效的旧SHA Stage0–3账本，但仍要求本组件当前SHA的匹配plan。若platform失败、有漂移或确实改变了平台参数/模板，另行评估重跑，不能用本段跳过受影响资源的部署。不要回放Stage0网络模板。

**ACR登录端点解析到公网并返回403的升级恢复：** 修复代码合并后，现有`runner-target-connectivity`配置无需增加字段，重新执行S4-04A/04B即可补ACR DNS Runner链接；不得直接重跑旧SHA的S4-12。部署后在实际Runner确认ACR登录端点及区域data端点解析到`acrPrivateEndpointIps`，再新建S4-11并用其run ID执行S4-12。无需重跑S4-01/02 platform、证书Vault、证书导入、cluster-bootstrap或monitoring-onboard。

**入口Service一直Pending、`lb-ready-api`超时的升级恢复：** 在AKS cloud-controller日志或Service事件确认`AuthorizationFailed`/`LinkedAuthorizationFailed`指向目标VNet中的入口或节点子网时使用本流程。需要授权的是实际AKS资源的系统分配控制面身份`identity.principalId`，不是kubelet身份、workload identity、Runner VM或`AZURE_RUNTIME_CLIENT_ID`。入口创建后，控制面还要把系统/用户节点池VMSS加入内部LB backend pool，该更新会校验节点子网的`subnets/join/action`。本方案的目标VNet专用于新网关，因此新建环境的platform模板在该**专用目标VNet**范围向控制面身份授予内置`Network Contributor`，覆盖系统节点、用户节点和入口子网；不扩大到目标RG或订阅。已经部署过platform的环境使用独立`aks-ingress-role`组件修复，不重放完整platform。组件从现有platform配置派生AKS和VNet，并从实际AKS读取控制面Principal ID，`CUSTOMER_CONFIG_JSON`不新增字段。

代码合并到受保护main并按第3节处理新SHA证据后，新建`Customer infrastructure deployment`：environment=test、stage=4、component=aks-ingress-role、operation=plan，release不勾选，两个approved字段和confirm_environment留空。解密并审核`infrastructure-test-4-aks-ingress-role-<run ID>`；修复计划应只有专用目标VNet下的一条`Microsoft.Authorization/roleAssignments` Create，scope形如`.../virtualNetworks/<vnet>/providers/Microsoft.Authorization/roleAssignments/<guid>`，角色为内置`Network Contributor`，principalId等于实际AKS `identity.principalId`。出现AKS、VNet、Subnet修改，其他scope/角色、Delete或Unsupported时停止；不能为通过What-if扩大到目标RG或订阅级Network Contributor。旧版已经创建的入口子网级同角色可暂时并存且其权限已被VNet授权覆盖，后续清理须单独审批，不在本恢复部署中自动删除。

审核通过后新建同workflow，operation=deploy、approved_run_id填上述成功plan ID、confirm_environment=test。部署成功并等待RBAC传播后，**新建S4-11**让`private-ingress`针对当前部分落地状态重新plan；审核新的runtime plan后，S4-12引用该新S4-11 run ID执行。失败的S4-12可能已创建API Traefik、Service、Secret或其他对象，不能假定自动回滚，也不能复用旧S4-11、Re-run failed jobs或直接执行旧plan。无需重跑S4-01/02 platform、certificate-vault、runner-target-connectivity、证书导入、cluster-bootstrap或monitoring-onboard；若独立计划已是NoChange但Service仍Pending，继续读取最新Service事件和cloud-controller日志，不重复授权或扩大权限。

#### 4-B2. runtime身份的Kubernetes预授权

**本节由有权管理员手动执行，不由DNS组件或Runner checks自行授权。** 适用于本项目新AKS的managed Entra集成和`aadProfile.enableAzureRbac=true`模式。此开关为false时停止，按客户实际认证/RBAC设计处理，不为照抄角色而临时切换认证模式。以下授权只针对新AKS，不扩到旧AKS、节点RG或整个订阅。

**1. 取得正确的身份和AKS作用域。** 从客户仓库Settings → Environments → 本次环境 → Variables取得`AZURE_RUNTIME_CLIENT_ID`，不是`AZURE_CLIENT_ID`。UAMI从Managed Identities → 对应身份 → Overview核对Client ID并取Principal ID；企业应用从Entra ID → Enterprise applications按Application ID查找，取服务主体Object ID，不能用App registrations的应用对象Object ID、个人用户ID、Runner VM或Pod身份。演练复用deploy/runtime身份时仍按runtime变量确认，不把复用当作客户默认配置。

管理员在已登录正确客户租户的管理终端执行下面的只读查询；订阅、RG和AKS名称分别取客户JSON的`azure.subscriptionId`、`target.resourceGroup`和`parameters.platform.stage4Aks.name`。`AKS_ID`取实际返回值，不手拼节点RG路径：

```bash
set -euo pipefail
SUBSCRIPTION_ID="REPLACE_AZURE_SUBSCRIPTION_ID_FROM_CUSTOMER_JSON"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP_FROM_CUSTOMER_JSON"
TARGET_AKS="REPLACE_STAGE4_AKS_NAME_FROM_CUSTOMER_JSON"
RUNTIME_CLIENT_ID="REPLACE_AZURE_RUNTIME_CLIENT_ID_FROM_ENVIRONMENT"
az account show --subscription "$SUBSCRIPTION_ID" \
  --query '{tenantId:tenantId,subscriptionId:id}' --output json
az ad sp show --id "$RUNTIME_CLIENT_ID" \
  --query '{name:displayName,clientId:appId,objectId:id,type:servicePrincipalType}' --output json
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$TARGET_AKS" \
  --query '{id:id,tenant:aadProfile.tenantId,managed:aadProfile.managed,azureRbac:aadProfile.enableAzureRbac,private:apiServerAccessProfile.enablePrivateCluster}' --output json
AKS_ID=$(az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --query id --output tsv)
RUNTIME_OBJECT_ID="REPLACE_VERIFIED_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"
```

核对租户与客户JSON一致、managed/azureRbac/private均为true、返回clientId与runtime变量一致，再将objectId填入本机`RUNTIME_OBJECT_ID`。目录查询受限时由Entra管理员提供或按0-A1查询UAMI，不能因此给runtime添加目录管理权限。所有授权命令都由**管理员身份**执行，接收者才是runtime服务主体。

**2. 按操作选择权限，不能混淆四种角色。** 以下是Stage4的分阶段方案，不声称已覆盖Stage5以后的全部发布对象：

| 何时 | 角色/能力 | 实际分配Scope |
| --- | --- | --- |
| Runner目标检查及后续获取用户kubeconfig | `Azure Kubernetes Service Cluster User Role`（`4abbcc35-e782-43d8-92c5-2d3f1bd2253f`）；若已有有效等效读取/获取用户凭据权限，不重复添加 | `AKS_ID`，新AKS资源自身 |
| Runner checks读取Deployment | `Azure Kubernetes Service RBAC Reader`（`7f6c6a51-bcf8-42ba-9220-52d62157d7db`） | `AKS_ID/namespaces/litellm` |
| S4-05/06创建/更新三个Namespace；S4-11读取Namespace对象 | 下面的`LLMGW AKS Namespace Bootstrapper`，仅Namespace read/write，不含delete | `AKS_ID`；Namespace是集群级对象，不能只授在命名空间内部 |
| S4-11/12入口对象和TLS Secret发布 | `Azure Kubernetes Service RBAC Writer`（`a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb`） | 分别为`AKS_ID/namespaces/llm-api-ingress`、`AKS_ID/namespaces/llm-admin-ingress` |

Cluster User Role只解决获取凭据，不授予Deployment读取。RBAC Reader不能发布，RBAC Writer不能创建Namespace；`Azure Kubernetes Service RBAC Admin`也排除了Namespace写入。不要误选名字接近的`Azure Kubernetes Service Cluster Admin Role`（获取管理员凭据），也不默认授予`Azure Kubernetes Service RBAC Cluster Admin`。保持本地管理员账户禁用，不用`--admin`或`az aks command invoke`绕过身份和私网控制。

**3. Portal授予集群范围角色，CLI精确授予命名空间角色。** 管理员须有新AKS范围的`Microsoft.Authorization/roleAssignments/write`，PIM资格需已激活，且条件允许向该服务主体授予选定角色。没有授权能力就交对应Owner，不让runtime通过已有RBAC管理员角色自行给自己提权。

新AKS → Access control (IAM) → Add → Add role assignment，搜索并选择上表的**Cluster User Role**。Members中UAMI选Managed identity，从身份实际所在订阅选择已核对的UAMI；企业应用选User, group, or service principal，按核对过的对象选择。Review + assign前再次确认Scope是**新AKS自身**、接收者是runtime Principal ID。已有效覆盖的角色跳过，不删除重建。

命名空间Scope使用下面的管理员CLI命令，避免在AKS IAM页面误授成整个集群。先按第5步回读该Scope，已有同角色/同主体的有效授权就跳过对应create；同名Namespace尚未创建也不需要先给集群管理员权限，此角色分配本身不会创建Namespace：

```bash
set -euo pipefail
az role assignment create --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service RBAC Reader" --scope "${AKS_ID}/namespaces/litellm" \
  --query '{id:id,scope:scope,principalId:principalId,roleDefinitionId:roleDefinitionId}' --output json
```

**4. 初始化和入口发布前补齐写权限。** 管理员在目标RG → IAM → Add custom role → JSON → Edit创建下面的Portal角色定义，或复用已审核同名角色。需要目标RG的`Microsoft.Authorization/roleDefinitions/write`，仅RBAC Administrator不具备创建定义的能力。这里的Assignable scopes仅决定角色可在哪里分配，**真正的分配仍在新AKS资源级**，不能直接分配在整个RG。角色JSON不放进CUSTOMER_CONFIG_JSON：

```json
{
  "properties": {
    "roleName": "LLMGW AKS Namespace Bootstrapper",
    "description": "Read and write Kubernetes namespaces in an approved AKS cluster; no namespace deletion, workload, secret or role management.",
    "assignableScopes": [
      "/subscriptions/REPLACE_TARGET_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
    ],
    "permissions": [{
      "actions": [],
      "notActions": [],
      "dataActions": [
        "Microsoft.ContainerService/managedClusters/namespaces/read",
        "Microsoft.ContainerService/managedClusters/namespaces/write"
      ],
      "notDataActions": []
    }]
  }
}
```

Review + create后，再到**新AKS → IAM → Add role assignment**选择该自定义角色，Members选择同一runtime身份，Review + assign。创建角色定义不等于完成分配。该权限允许读写此集群的**所有Namespace对象及其标签**，不是只允许上述三个名称；修改标签也可能影响Pod Security等策略。现有workflow只申请三个Namespace并不能把RBAC变成按名称限制，必须由客户明确批准这一边界。更严格的客户须由平台管理员预建/维护命名空间并调整发布流程，不能声称当前版本已有该替代按钮。

入口发布权限只授两个入口命名空间，不给Writer整个AKS、`kube-system`或默认扩到`litellm`。下面两次create同样先回读并跳过既有匹配授权：

```bash
set -euo pipefail
for NAMESPACE in llm-api-ingress llm-admin-ingress; do
  az role assignment create --subscription "$SUBSCRIPTION_ID" \
    --assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal \
    --role "Azure Kubernetes Service RBAC Writer" --scope "${AKS_ID}/namespaces/${NAMESPACE}" \
    --query '{id:id,scope:scope,principalId:principalId,roleDefinitionId:roleDefinitionId}' --output json
done
```

Writer包含命名空间内Secret读写、Pod创建/执行及使用其中ServiceAccount的能力，并非“只写Deployment”，须批准证书私钥可读和工作负载身份风险；不授予Role/RoleBinding管理。`litellm`目前只有Reader，Stage6应用发布前还需另行批准应用命名空间写权限和SecretProviderClass等自定义资源权限，不能把本表当成全阶段权限完成。后续清单仍包含Namespace时也需要相应写权限，按批准窗口和后续动作收回或续期，不在S4-06成功后盲目撤销，也不删除命名空间。

**5. 回读并用实际workflow身份验证。** 在管理员终端核对自定义角色定义及每个Scope下的实际授权；Portal在AKS资源页可能不显示命名空间子Scope授权，不要因为看不到就改授集群范围：

```bash
set -euo pipefail
az role definition list --subscription "$SUBSCRIPTION_ID" --name "LLMGW AKS Namespace Bootstrapper" \
  --query '[].{role:roleName,assignableScopes:assignableScopes,permissions:permissions}' --output json
for ROLE_SCOPE in "$AKS_ID" "${AKS_ID}/namespaces/litellm" \
  "${AKS_ID}/namespaces/llm-api-ingress" "${AKS_ID}/namespaces/llm-admin-ingress"; do
  az role assignment list --subscription "$SUBSCRIPTION_ID" \
    --assignee-object-id "$RUNTIME_OBJECT_ID" --scope "$ROLE_SCOPE" \
    --include-inherited --fill-principal-name false \
    --query '[].{role:roleDefinitionName,scope:scope,principalId:principalId,principalType:principalType,condition:condition}' \
    --output json
done
```

核对principalId、ServicePrincipal类型、Scope与角色定义符合批准内容，并检查组/继承角色、条件和有效期；新增小范围授权不会收紧已存在的宽泛授权。新角色通常需要最多约5分钟传播，以实际调用为准。仅补Azure授权且代码/配置不变，不要求重部署platform或重录仍有效的验收；代码合并后的新SHA要求仍按4-B1执行。

DNS连接完成后，新运行Customer private runner checks：main、test、check_target=true、check_backup=false，`target-cluster-read`必须通过。该检查使用`AZURE_RUNTIME_CLIENT_ID`的OIDC登录，不需要你在Runner执行个人`az login`；个人管理员kubectl成功不能代替它。随后S4-05的服务器端dry-run验证Namespace写权限，批准后S4-06才实际创建；S4-11/12验证入口发布，plan本身也需要相应写授权。没有单独“自动授予Kubernetes权限”按钮，不伪造passed；DNS失败、Forbidden、Admission拒绝和Secret/ACR权限分别排查。

参考[AKS Azure RBAC及命名空间作用域](https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac)和[AKS内置角色定义](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers#azure-kubernetes-service-rbac-writer)。本节命令是管理员操作说明，不表示角色已经在客户环境分配或实测通过。

#### 4-C. 部署后手动导入两个Secret

客户已交付阿里云格式的 API/Admin `.pem` 和 `.key` 四个文件时，可按
[客户证书导入 Runbook](litellm-ingress-certificate-import-runbook-zh.md) 执行文件合并、
目标环境核对、人工身份上传及入口发布；以下保留完整网络窗口与无证书时的申请说明。

**这里要上传的是两个Secret，不是两张新证书，也不是CA证书与服务器证书各上传一份。** 已有API证书和admin证书满足域名、信任及有效期要求时直接复用。`.crt`、`.pem`是文件名后缀，不能单凭后缀判断内容；本流程要求PEM格式。每个Secret的值都是一个完整材料包：该入口的证书链加对应叶私钥。

| 已有文件的用途 | 如何使用 | Vault中的目标 |
| --- | --- | --- |
| API域名的`.pem`和对应`.key` | 确认证书文件为叶证书在前、随后中间链，与叶私钥合并；只有叶证书时先向签发方取得中间链/fullchain | `api-tls` |
| Admin域名的公有CA叶证书及对应私钥 | 叶证书在前并附完整中间链；根须位于Microsoft Trusted CA List，供Front Door验证私有origin | `admin-tls` |
| 任意CA私钥或签发申请CSR | CA私钥独立保管；CSR仅是申请材料，不是已签发证书 | 均不上传 |

**简化操作路径：客户本机准备并验证 → 管理员临时开启Vault受限公网 → 客户本机上传 → 立即关闭公网 → Runner私网验证。** 上传不需要登录Runner，也不需要Bastion/SCP传输私钥。以下命令在客户持有证书的受控机器、仓库根目录的Bash终端执行，需OpenSSL 3、Azure CLI及仓库Python依赖；Windows可使用获批的WSL环境，Portal操作使用客户本机浏览器，不把私钥传给Cloud Shell或GitHub。

本流程仅为**证书Vault**的短时人工导入例外，须事先批准操作人、出口IP、结束时间和负责关闭的人；不是开放LiteLLM API/admin、AKS或其他业务Vault。IaC仍默认`publicNetworkAccess=Disabled`，不修改模板、客户JSON或门禁来长期保留公网。客户Policy或Network Security Perimeter不允许此例外时停止，沿用批准的私网导入方式，不绕过策略。已有合格证书跳过第2步，不重新生成或覆盖原材料。

**1. 取得实际输出。** 解密S4-04的`infrastructure-test-4-certificate-vault-<run ID>`附件，在deployment-outputs.json查看`certificateVault.value`；或从目标RG → Deployments → `llmgw-test-s4-certificate-vault` → Outputs取得。输出只有Vault、PE、DNS及两个预定Secret地址；`certificateMaterialsImported=false`表示此部署没有导入材料，不是证书上传失败。管理终端也可只读查询：

```bash
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID_FROM_CUSTOMER_JSON"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP_FROM_CUSTOMER_JSON"
ENVIRONMENT_NAME="test"
az deployment group show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "llmgw-${ENVIRONMENT_NAME}-s4-certificate-vault" \
  --query '{state:properties.provisioningState,certificateVault:properties.outputs.certificateVault.value}' --output json
```

确认Succeeded，把输出`name`作为下面的CERT_VAULT，`apiTlsSecretId`/`adminTlsSecretId`分别填入privateIngress.api/admin.tlsSecretId，来源CIDR按真实批准范围填写并同步Secret。地址固定为同一Vault中的`/secrets/api-tls`、`/secrets/admin-tls`，不要填`/certificates/`、相同Secret或业务Vault。支持手工固定版本，但须使用对应Secret的真实版本。

**2. 没有证书时怎样创建。** 两个域名均由客户JSON的`baseDomain`派生，不使用Vault域名签发入口证书。

- **API：公有CA签发。** 向客户批准且被Front Door信任的公有CA申请`llm-api.<baseDomain>`，获取PEM叶证书及中间链/fullchain，并保留匹配的叶私钥。下面仅在本机生成私钥和CSR，不是已签发证书；将CSR提交给CA，按签发方指引完成DNS TXT域名验证、下载fullchain。只提交CSR，不提交私钥。DNS验证不要求提前开放源站80/443、切换业务A/CNAME或打开Vault；公共证书透明度会披露域名。不能用自签名或仅CDN厂商信任的Origin证书替代公有CA证书。
- **Admin：公有CA签发。** 向客户批准且根位于Microsoft Trusted CA List的公有CA申请`llm-admin.<baseDomain>`服务器证书，要求SAN包含该域名、用途为serverAuth，并取得完整中间链。需要CSR时可按下面命令改为Admin域名及独立文件名；只提交CSR，不提交叶私钥。Stage9的Admin来源门禁由Front Door WAF公网CIDR规则实现，不需要另行签发客户端证书。

```bash
set -euo pipefail
umask 077
BASE_DOMAIN="REPLACE_BASE_DOMAIN_FROM_CUSTOMER_JSON"
mkdir -p temp
API_DIR=$(mktemp -d temp/api-csr.XXXXXXXX)
openssl req -new -newkey rsa:2048 -noenc -sha256 \
  -keyout "$API_DIR/api.key" -out "$API_DIR/api.csr" \
  -subj "/CN=llm-api.${BASE_DOMAIN}" \
  -addext "subjectAltName=DNS:llm-api.${BASE_DOMAIN}"
openssl req -in "$API_DIR/api.csr" -noout -verify
printf 'API CSR directory: %s\n' "$API_DIR"
```

自签名或企业内部CA的Admin服务器证书可用于Stage9前的私网功能演练，但必须在创建Admin Front Door origin前替换为上述公有链。不得通过`enforceCertificateNameCheck=false`规避：该开关只放宽名称检查，Front Door仍拒绝不受信任的证书链。

**3. 整理并验证文件。** 每个Secret都包含**该域名的叶证书在前、其后中间链、最后对应未加密叶私钥**，不是一个Secret只放证书、另一个只放私钥。API和Admin源站均须由未来Front Door信任的公有CA签发。签发CA私钥独立保管，绝不合入这两个文件。以下在仓库根目录的受控终端执行，只填写文件路径；已合并的合格PEM可直接使用，不必再次拼接：

四个输入路径就是上表的现有材料：`API_CHAIN_FILE`填API证书/fullchain的`.pem`，`API_LEAF_KEY_FILE`填其`.key`，`ADMIN_CHAIN_FILE`填Admin叶证书及完整中间链，`ADMIN_LEAF_KEY_FILE`填Admin叶私钥。不要把任何根证书、CA私钥或不匹配的其他证书填成后两项。所有路径均属于**运行该命令的机器**；另一台电脑上的文件先安全传来。输出目录已有同名材料时先核对并保留恢复副本，不盲目覆盖。

```bash
set -euo pipefail
umask 077
mkdir -p temp
mkdir -m 700 temp/certificate-import
API_CHAIN_FILE="REPLACE_API_LEAF_AND_CHAIN_FILE"
API_LEAF_KEY_FILE="REPLACE_API_LEAF_PRIVATE_KEY_FILE"
ADMIN_CHAIN_FILE="REPLACE_ADMIN_LEAF_AND_CHAIN_FILE"
ADMIN_LEAF_KEY_FILE="REPLACE_ADMIN_LEAF_PRIVATE_KEY_FILE"
API_PEM_FILE="temp/certificate-import/api.pem"
ADMIN_PEM_FILE="temp/certificate-import/admin.pem"
{ cat "$API_CHAIN_FILE"; printf '\n'; cat "$API_LEAF_KEY_FILE"; } > "$API_PEM_FILE"
{ cat "$ADMIN_CHAIN_FILE"; printf '\n'; cat "$ADMIN_LEAF_KEY_FILE"; } > "$ADMIN_PEM_FILE"
chmod 600 "$API_PEM_FILE" "$ADMIN_PEM_FILE"
BASE_DOMAIN="REPLACE_BASE_DOMAIN_FROM_CUSTOMER_JSON"
.venv/bin/python - "$BASE_DOMAIN" "$API_PEM_FILE" "$ADMIN_PEM_FILE" <<'PY'
from pathlib import Path
import sys
from scripts.private_ingress import certificate_material

try:
    for plane, filename in zip(("api", "admin"), sys.argv[2:], strict=True):
        raw = Path(filename).read_bytes()
        if len(raw) > 25 * 1024:
            raise ValueError("Key Vault Secret size limit")
        result = certificate_material(raw.decode("utf-8"), f"llm-{plane}.{sys.argv[1]}")
        print(f"{plane}: sha256={result['sha256']} expiresAt={result['expiresAt']}")
except Exception:
    raise SystemExit("Certificate validation failed; no private material printed.") from None
PY
```

本机需具备仓库Python依赖。目录已存在时命令会停止，请核对旧材料或换新输出路径，不覆盖。记录两份叶证书的`sha256`和`expiresAt`，第7步要与上传后的材料比对。验证覆盖域名/SAN、私钥匹配、有效期至少7天和Secret 25KB限制，不证明CA链已被实际Runner/Front Door信任。

**4. 管理员临时开启受限公网。** 先完成证书准备与本地验证，再开始计时的上传窗口。Portal操作只作用于第1步输出的证书Vault，不改`privateIngress.allowedCidrs`；该CIDR限制的是LiteLLM入口，与Vault防火墙无关。

1. 管理员记录Vault原网络设置和结束时间；需具备该Vault的`Microsoft.KeyVault/vaults/write`管理权限。导入人仍需已批准的Key Vault Secrets Officer数据权限，网络管理员权限不能代替它。
2. Vault → **Networking → Firewalls and virtual networks**，选择**Allow public access from specific virtual networks and IP addresses**（部分Portal显示Selected networks），只添加上传机器的实际公网出口IPv4，单地址使用`/32`。从Portal的Add your client IP取值，并由网络Owner核对公司代理/VPN/NAT出口；浏览器与CLI出口不同时分别核对，只批准实际需要的地址。不要填本机10.x地址、Runner/Bastion网段或PE IP，不选择All networks，不填`0.0.0.0/0`。
3. 保持默认拒绝及trusted services bypass关闭，即`defaultAction=Deny`、`bypass=None`，Save。保留PE、Private DNS、RBAC、软删除、清除保护和CanNotDelete锁。并行IaC部署会与临时设置冲突，窗口内不要同时运行certificate-vault部署。

开启公网仍需正常Azure登录和数据授权，不是匿名上传。上传机器须能通过正常Vault域名走获批公网路径；若企业DNS仍解析到不可达PE，交网络Owner处理，不改hosts或跳过TLS。如果授权/网络失败或到了结束时间，**无论上传成功、失败或中断，都立即执行第6步关闭公网**，不等待Runner排障完成。Policy拒绝修改时不自行申请Owner或删除锁绕过。

**5. 客户在本机上传。** Portal为主路径：用获批导入人账号登录正确租户，进入证书Vault → **Secrets → Generate/Import**，不是Certificates。分别创建`api-tls`和`admin-tls`，Secret value粘贴第3步对应完整合并PEM，保留换行；Content type填`application/x-pem-file`，Enabled设为Yes，日期如设置须覆盖使用窗口。不把文件路径、base64文本、CSR或CA私钥当作值。已有同名Secret时先批准轮换，不删除旧版本；已删除保留中的同名Secret由Owner处理，不擅自purge。

也可在同一获批出口的**客户本机**使用CLI文件上传，避免手动粘贴出错。租户/订阅来自客户JSON的`azure`，Vault名称来自第1步输出；不是GitHub Client ID，也不使用Actions登录身份。以下先登录并仅列举元数据，任何一步失败都不继续上传，并由管理员关闭窗口：

```bash
set -euo pipefail
TENANT_ID="REPLACE_TENANT_ID_FROM_CUSTOMER_JSON"
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID_FROM_CUSTOMER_JSON"
CERT_VAULT="REPLACE_NAME_FROM_CERTIFICATE_VAULT_OUTPUT"
API_PEM_FILE="temp/certificate-import/api.pem"
ADMIN_PEM_FILE="temp/certificate-import/admin.pem"
az login --tenant "$TENANT_ID" --output none
az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{tenant:tenantId,subscription:id,identity:user.name,type:user.type}' --output json
az ad signed-in-user show --query '{objectId:id,upn:userPrincipalName}' --output json
az keyvault secret list --subscription "$SUBSCRIPTION_ID" --vault-name "$CERT_VAULT" \
  --query '[].{id:id,enabled:attributes.enabled}' --output json --only-show-errors
```

核对当前用户Object ID与certificateImporterPrincipalId一致；配置Group时核对实际成员资格。列表不返回值，也不证明写权限。出现同名Secret时停止并走轮换审批；全部核对后执行下面两条命令，不启用`--debug`或shell trace，不使用`--value`把私钥写入命令行：

```bash
set -euo pipefail
az keyvault secret set --subscription "$SUBSCRIPTION_ID" --vault-name "$CERT_VAULT" \
  --name api-tls --file "$API_PEM_FILE" --encoding utf-8 --content-type application/x-pem-file \
  --query '{id:id,enabled:attributes.enabled}' --output json --only-show-errors
az keyvault secret set --subscription "$SUBSCRIPTION_ID" --vault-name "$CERT_VAULT" \
  --name admin-tls --file "$ADMIN_PEM_FILE" --encoding utf-8 --content-type application/x-pem-file \
  --query '{id:id,enabled:attributes.enabled}' --output json --only-show-errors
```

在Portal每个Secret的当前版本页面，或CLI成功输出中记录**完整带版本Secret Identifier**与Enabled状态；没有两份成功结果就记为未完成。`certificateMaterialsImported=false`是基础设施部署的固定输出，手工上传后不会变成true，不用重跑S4-04更新它。首次手动导入两份证书时跳过S4-09/10；选自动API签发时只手动导入admin，关闭公网后按已批准权限运行S4-09/10，不能把两条路径混成重复写入。

**6. 立即关闭公网并核对。** 管理员回到Networking，将Public network access设为**Disable public access**并Save；清除本次新增的临时IP规则并保存，不删除PE或其他Owner的资源。刷新确认`Disabled`、`defaultAction=Deny`、`bypass=None`，PE仍为Approved。本组件基线IP规则为空，应恢复`ipRules=[]`。这些是人工步骤，关闭窗口不靠下次部署自动收回；上传失败、中途退出也必须完成关闭。

Portal JSON View或下面的管理面只读查询均可核对（客户本机执行，TARGET_RG取客户JSON的target.resourceGroup）：

```bash
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP_FROM_CUSTOMER_JSON"
az keyvault show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$CERT_VAULT" \
  --query '{publicNetworkAccess:properties.publicNetworkAccess,defaultAction:properties.networkAcls.defaultAction,bypass:properties.networkAcls.bypass,ipRules:properties.networkAcls.ipRules,privateEndpoints:properties.privateEndpointConnections[].properties.privateLinkServiceConnectionState.status}' \
  --output json
```

关闭后，从同一本机公网路径用原获批账号重新列举Secret，应被网络策略拒绝；保留网络拒绝信息而非Secret正文。凭据过期、普通RBAC拒绝、匿名404或仍能打开Portal的Vault Overview均不是关闭证明。公网DNS仍能解析是正常现象，不能据此认定未关闭。网络设置未恢复、仍可公网读取时停止后续步骤，交管理员处理。参考[Key Vault网络设置](https://learn.microsoft.com/en-us/azure/key-vault/general/network-security)。

**7. 验证上传材料真正可用。** 不要求把私钥再传到Runner；由已有runtime workflow从Vault私网读取。验证分三层，不能仅凭Portal显示两个Secret就签验收：

| 检查 | 操作与通过标准 |
| --- | --- |
| CA信任和私网 | Runner使用系统公有信任验证两份源站证书；Admin完整链的根须同时位于Microsoft Trusted CA List。Runner解析Vault正常域名须与PE的NIC私有IP一致，TLS/443可达。Stage9来源准入由Front Door WAF的公网CIDR规则配置，不进入源站证书材料 |
| 上传后读取与内容校验 | 完成S4-05/06及所需前置后，新建S4-11：Customer private runtime operations，main、test、stage=4、action=private-ingress、operation=plan，所有approved/audit_continue/confirm字段留空。公网已关闭时真实runtime身份成功读取两份Secret、验证Enabled、域名/SAN、私钥匹配、剩余有效期及Runner信任链，并完成Kubernetes dry-run；私网失败不打开Vault公网兜底 |
| 与本地材料一致且已发布 | 解密成功plan的runtime-review.json，逐项比对`certificates.api/admin.secretId`等于第5步版本地址、`sha256`等于第3步本地叶证书指纹、`expiresAt`一致。审核后S4-12引用该plan ID，成功结果应含`applied=true`、`verified=true`，实际两个私有入口的TLS指纹、域名校验及错误Host拒绝通过 |

证书地址仍填写在客户JSON**顶层**privateIngress的api/admin.tlsSecretId，且allowedCidrs覆盖批准来源；同步完整GitHub Environment Secret `CUSTOMER_CONFIG_JSON`，不是把PEM填进去。手动路径可固定第5步版本；使用无版本地址时须确认选中的当前版本正确。变更配置/代码/证书版本后重新plan，不能沿用旧批准。PE/DNS、AKS、ACR或权限检查失败须分别定位，S4-11不是独立的证书专用按钮。

**完成条件：** 公网窗口已关闭且临时规则已清理、两份Secret版本及证书指纹一致、实际Runner私网读取和公有链信任通过、S4-12实际入口验证通过。Front Door源站TLS在Stage9继续验证，业务认证另行验收；上传成功不代表入口已发布，也不自动生成Stage4验收。按保管策略处理本机临时PEM并保留原始恢复材料，不删除唯一副本。

**Stage4验收前的镜像步骤：** 私有ACR可达后运行`Promote LiteLLM image`，environment=test、acr_name=客户ACR名称、source_image保持仓库固定源digest、target_tag=`litellm-azure:rehearsal-1`（示例，按发布版本命名）、build_azure_runtime=true、build_auth_proxy=false。没有stage/approved_run_id输入。deploy身份需批准的ACR推送权限；Runner需访问源registry、扫描库和Sigstore。记录成功输出的完整`ACR/repository@sha256:...`，并审核加密artifact中的SBOM、`target-image-verification.json`和`target-image-summary.json`；下一阶段应用配置使用该完整镜像引用。扫描、签名或同作业验证失败时，即使已推送也不能作为已批准镜像。

**阶段验收：** stage=4 draft/confirm；`private_dns_egress`、`private_runner`、`target_image_signature_sbom`、`workload_identity`、`private_ingress`。五项全部通过时，`checked_items`填写`private_dns_egress,private_runner,target_image_signature_sbom,workload_identity,private_ingress`。按本节S4步骤、[本地Stage4取值与验证](../local_execution/stage2-9-guide-zh.md#stage4私网aks证书入口和目标连通性)及第3节附件审核核对：Runner私网DNS/TLS及所需出站、实际私网访问、批准镜像签名/SBOM/拉取、WI允许与拒绝、双入口TLS/Host/来源隔离。目标镜像签名/拉取以及WI正反向访问要实际验证，不能用公共源扫描、UAMI存在或静态YAML替代；当前缺少标准WI探针时保持待核验，不能编造通过。Stage4之后不得重放Stage0 backup/network模板覆盖已扩展VNet。

### 阶段5：数据服务与迁移演练

**开始前：** Stage4已验收；在JSON中补齐`parameters.platform.stage5Data`（新PG库名/SKU/批准HA模式、Entra管理员Object ID/名称/类型、Redis SKU）及DNS Zone归属开关。HA Disabled不是已验证HA；区域不支持或配额不足时先批准调整，不自行退回更弱认证。

**本阶段新增Variables：** `AZURE_DATABASE_CLIENT_ID`、`MIGRATION_RESTORE_BLOB`、`MIGRATION_BACKUP_SHA256`。后两项必须来自同一个已核对Stage0备份报告。顶层另加`"databaseAccess":{"migrationPrincipalId":"REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"}`，填runtime身份的Principal ID而非其Client ID。

**PG管理员注意：** `database-roles`使用专项OIDC服务主体，必须已是该PG的Entra管理员或获准管理员组成员。若PG配置只填某个人的User Object ID，该个人不是此OIDC服务主体，不能把个人UPN填入变量就完成初始化。先与DBA确认受支持管理员配置/组成员，再运行；Azure RBAC不自动产生数据库内角色。

#### 5-A. Stage5开始前由客户人工准备的身份与权限

**先区分人工授权和自动授权。** 客户管理员只为workflow登录身份准备OIDC、管理面读取/回执写入及旧AKS Secret读取权限；S5-02和S5-04会建立下表右侧的资源内授权。不要因为四种身份都参与Stage5，就把它们合并或全部授予目标RG Contributor、PG管理员和Vault写权限。

| 身份 | Stage5前由客户人工准备 | 本流程自动完成 |
| --- | --- | --- |
| deploy，`AZURE_CLIENT_ID` | 保留Stage4已批准的作用域；目标RG内资源部署/What-if、受限角色分配及锁写入权限 | S5-02创建PG、Redis、后台Vault、PE/DNS、诊断和下述数据面授权 |
| database，`AZURE_DATABASE_CLIENT_ID` | 专用应用/UAMI、GitHub Environment OIDC、目标RG Reader或等效自定义读取角色；使用管理员组时另加组成员关系 | 直接配置`ServicePrincipal`时，S5-02把该Principal ID设为目标PG的Entra管理员 |
| runtime，`AZURE_RUNTIME_CLIENT_ID` | 目标RG Reader或等效读取、目标RG回执写入；migration模式另需旧AKS用户凭据和旧namespace Secret读取 | S5-02授予后台Vault Secrets Officer；S5-04创建`llmgw_migrator`；Stage0配置正确时已有备份容器Blob Data Contributor |
| application，Stage4创建的LiteLLM Workload Identity | Stage5不要求客户手工增加PG管理员、Vault写入或Redis管理权限 | S5-02授予后台Vault Secrets User和Redis default访问策略；S5-04创建`llmgw_app` |

上述Reader只解决ARM元数据读取，不产生PG、Key Vault、Blob、Redis或Kubernetes数据权限。反过来，PG Entra管理员或Vault Secrets Officer也不能读取ARM部署输出。Runner私网DNS/TLS可达性同样不是角色分配，仍须在S5-02后实测。

**1. 创建并核对database身份和GitHub OIDC。** 由有权管理员在客户管理RG创建专用UAMI或应用服务主体；不使用个人用户、Runner VM身份、runtime身份或应用Pod身份。UAMI的Client ID填GitHub Environment Variable，Principal ID填客户JSON。下面以UAMI为例，所有值都从客户自己的资源取得：

```bash
set -euo pipefail
SUBSCRIPTION_ID="REPLACE_AZURE_SUBSCRIPTION_ID_FROM_CUSTOMER_JSON"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP_FROM_CUSTOMER_JSON"
DATABASE_IDENTITY_RG="REPLACE_DATABASE_IDENTITY_RESOURCE_GROUP"
DATABASE_IDENTITY_NAME="REPLACE_DATABASE_IDENTITY_NAME"
az account show --subscription "$SUBSCRIPTION_ID" \
  --query '{tenantId:tenantId,subscriptionId:id,identity:user.name}' --output json
az identity show --subscription "$SUBSCRIPTION_ID" --resource-group "$DATABASE_IDENTITY_RG" \
  --name "$DATABASE_IDENTITY_NAME" \
  --query '{name:name,tenantId:tenantId,clientId:clientId,principalId:principalId,id:id}' --output json
az identity federated-credential list --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$DATABASE_IDENTITY_RG" --identity-name "$DATABASE_IDENTITY_NAME" \
  --query '[].{name:name,issuer:issuer,subject:subject,audiences:audiences}' --output json
```

联邦凭据必须使用issuer=`https://token.actions.githubusercontent.com`、audience=`api://AzureADTokenExchange`，subject必须对应实际客户仓库和所选Environment。GitHub默认格式为`repo:<owner>/<repo>:environment:<environment>`；组织配置了OIDC subject自定义模板时可能包含仓库Owner/Repository的数字ID，应按该仓库实际Token声明或已验证的同环境身份核对，不能把其他仓库的subject照抄过来。

在GitHub仓库 **Settings → Environments → 本次environment → Environment variables** 设置：

```text
AZURE_DATABASE_CLIENT_ID=<上述UAMI的clientId>
```

客户JSON的`parameters.platform.stage5Data`使用同一个对象：

```json
{
  "postgresqlEntraAdministratorObjectId": "REPLACE_DATABASE_UAMI_PRINCIPAL_ID",
  "postgresqlEntraAdministratorPrincipalName": "REPLACE_DATABASE_UAMI_NAME",
  "postgresqlEntraAdministratorPrincipalType": "ServicePrincipal"
}
```

如果客户选择Entra管理员组，JSON填写组Object ID、显示名称和`Group`，并由Entra管理员把database服务主体加入该组；GitHub变量仍填database服务主体的Client ID。不要把组Object ID、UAMI Principal ID或个人Object ID填入`AZURE_DATABASE_CLIENT_ID`。

**2. 给database身份最小ARM读取权限。** `database-roles`会用该身份读取目标RG中的Stage5 deployment、PG属性和应用UAMI，然后才以PG管理员Token连接数据库。最简单的内置角色是目标RG级Reader；生产客户也可预建只含`Microsoft.Resources/deployments/read`、`Microsoft.DBforPostgreSQL/flexibleServers/read`及`Microsoft.ManagedIdentity/userAssignedIdentities/read`的等效自定义角色。先回读，已有等效继承授权时不重复创建：

```bash
DATABASE_OBJECT_ID="REPLACE_DATABASE_UAMI_PRINCIPAL_ID"
TARGET_RG_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${TARGET_RG}"
az role assignment list --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$DATABASE_OBJECT_ID" --scope "$TARGET_RG_SCOPE" \
  --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,scope:scope,principalId:principalId,condition:condition}' --output json
az role assignment create --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$DATABASE_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Reader" --scope "$TARGET_RG_SCOPE" \
  --query '{scope:scope,principalId:principalId,roleDefinitionId:roleDefinitionId}' --output json
```

Reader不是PG管理员授权。直接`ServicePrincipal`路径的PG管理员子资源由S5-02根据客户JSON创建；管理员组路径的组成员关系由客户Entra管理员预先维护。database身份不需要目标RG Contributor、Owner、Key Vault Secrets Officer或Kubernetes写权限。

**3. 核对deploy身份仍能创建Stage5资源和模板内授权。** S5-01/02继续使用`AZURE_CLIENT_ID`，不是database或runtime身份。该身份在目标RG需要三类现有授权：

1. Contributor或客户等效自定义角色，用于PG/管理员、Redis/访问策略、Vault、PE、DNS链接和诊断资源的What-if及部署。
2. `Microsoft.Authorization/roleAssignments/read/write`能力，用于只向runtime和application身份分配后台Vault的Secrets Officer/User。优先使用带条件的Role Based Access Control Administrator，只允许Key Vault Secrets User（`4633458b-17de-408a-b874-0445c86b69e6`）和Key Vault Secrets Officer（`b86a8fe4-44ce-4948-aee5-eccb2c155cd7`），并限制到已核实的两个Principal ID；不能把这项权限授给runtime或database身份。
3. [4-A1](#4-a1-证书vault防删除锁权限)中的`LLMGW Resource Lock Writer`，目标RG范围的`Microsoft.Authorization/locks/read`和`locks/write`即可；后台Vault也有CanNotDelete锁，不需要`locks/delete`。

S5-01重新使用platform模板，因此Stage4模型账号、目标网络和ACR等已批准作用域也必须仍然有效；不要在Stage4完成后提前撤销deploy身份的必要跨RG授权。已有更宽的Owner/User Access Administrator不因新增受限角色而自动收紧，客户应先检查继承和条件，再决定是否回收。回读时使用`AZURE_CLIENT_ID`对应的服务主体Object ID，不用Client ID本身：

```bash
DEPLOY_CLIENT_ID="REPLACE_AZURE_CLIENT_ID_FROM_ENVIRONMENT"
DEPLOY_OBJECT_ID=$(az ad sp show --id "$DEPLOY_CLIENT_ID" --query id --output tsv)
az role assignment list --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$DEPLOY_OBJECT_ID" --scope "$TARGET_RG_SCOPE" \
  --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,scope:scope,condition:condition,conditionVersion:conditionVersion}' --output json
```

**4. 给runtime身份读取和写回执权限。** S5-05/06的backend-secrets与S5-09/10的schema-migrate会在目标RG创建只含输出的ARM deployment回执；Reader不能写回执，Contributor又超出该用途。由管理员在目标RG创建或复用下面的自定义角色，再分配给`AZURE_RUNTIME_CLIENT_ID`对应的服务主体Object ID。角色定义不是客户JSON内容：

```json
{
  "properties": {
    "roleName": "LLMGW Runtime Receipt Writer",
    "description": "Read and write reviewed ARM deployment receipts in the approved gateway resource group; no business resource or deployment deletion permissions.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RG"
    ],
    "permissions": [{
      "actions": [
        "Microsoft.Resources/deployments/read",
        "Microsoft.Resources/deployments/write",
        "Microsoft.Resources/deployments/validate/action",
        "Microsoft.Resources/deployments/operations/read"
      ],
      "notActions": [],
      "dataActions": [],
      "notDataActions": []
    }]
  }
}
```

创建自定义角色需要目标scope上的`Microsoft.Authorization/roleDefinitions/write`；只有RBAC Administrator时不能创建定义，应由权限管理员预建，不扩大为订阅Owner。然后回读runtime身份并分配目标RG Reader和该回执角色；已有等效继承权限时跳过重复分配：

```bash
RUNTIME_CLIENT_ID="REPLACE_AZURE_RUNTIME_CLIENT_ID_FROM_ENVIRONMENT"
RUNTIME_OBJECT_ID=$(az ad sp show --id "$RUNTIME_CLIENT_ID" --query id --output tsv)
az role assignment create --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Reader" --scope "$TARGET_RG_SCOPE" --output none
az role assignment create --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "LLMGW Runtime Receipt Writer" --scope "$TARGET_RG_SCOPE" --output none
az role assignment list --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$RUNTIME_OBJECT_ID" --scope "$TARGET_RG_SCOPE" \
  --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,scope:scope,principalId:principalId,condition:condition}' --output json
```

`deployments/write`允许在该RG创建或更新部署记录，但不单独授予其中业务资源的写入权限；它仍能覆盖已有同名回执，必须依靠受保护workflow、plan批准和固定命名约束使用。该角色不包含deployment删除、取消、What-if或任何PG/Vault数据权限。

**5. migration模式核对旧AKS Secret读取。** backend-secrets会从旧namespace精确读取`litellm-env`中的Master Key和Salt。若Stage0使用同一个runtime身份完成过backup-restore，通常已具备旧AKS Cluster User及更广的Pod操作权限，但Secret读取仍须单独实测，不能从Pod exec成功推断。先查询旧集群授权模式：

```bash
LEGACY_RG="REPLACE_LEGACY_RESOURCE_GROUP"
LEGACY_AKS="REPLACE_LEGACY_AKS_NAME"
LEGACY_NAMESPACE="REPLACE_LEGACY_NAMESPACE"
LEGACY_AKS_ID=$(az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" \
  --name "$LEGACY_AKS" --query id --output tsv)
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{managed:aadProfile.managed,azureRbac:aadProfile.enableAzureRbac,id:id}' --output json
az role assignment create --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service Cluster User Role" --scope "$LEGACY_AKS_ID" --output none
```

旧AKS启用Azure RBAC时，由管理员创建仅含`Microsoft.ContainerService/managedClusters/secrets/read` dataAction的自定义角色，将Assignable scope限制在旧AKS所属RG，并把实际角色分配限制在`${LEGACY_AKS_ID}/namespaces/${LEGACY_NAMESPACE}`。Azure RBAC的namespace scope仍允许读取该namespace内其他Secret，不能声称只限`litellm-env`；客户不接受该边界时须先改造密钥交接流程，不能改授整个集群RBAC Writer。

旧AKS使用Kubernetes RBAC时，由集群管理员创建仅允许`get`指定Secret的Role，并绑定runtime服务主体Object ID；不要把这份权限给database或application身份：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: llmgw-legacy-key-reader
  namespace: REPLACE_LEGACY_NAMESPACE
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["litellm-env"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: llmgw-legacy-key-reader
  namespace: REPLACE_LEGACY_NAMESPACE
subjects:
  - kind: User
    name: REPLACE_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID
    apiGroup: rbac.authorization.k8s.io
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: llmgw-legacy-key-reader
```

使用管理员的受控kubeconfig应用后，再通过实际runtime OIDC上下文取得用户kubeconfig并执行`kubectl auth can-i get secret/litellm-env -n "$LEGACY_NAMESPACE"`；必须返回yes。不要打印、base64解码或上传Secret正文来证明权限。greenfield模式不读取旧Secret，跳过本项。

**6. 不要重复手工分配模板已管理的权限。** 审核S5-01计划时应看到预期的自动授权；缺失就停止并修配置/模板，不在Portal临时堆叠权限：

| 自动发生的步骤 | 授权结果 | 人工只需核对 |
| --- | --- | --- |
| Stage0 backup，已配置`backupAutomationPrincipalId` | runtime在固定备份容器获得Storage Blob Data Contributor | S5-07前确认角色仍在、Blob引用/哈希来自同一报告；不要改授Storage账号Owner |
| S5-02 platform | database对象成为PG Entra管理员；runtime获得后台Vault Secrets Officer；application获得后台Vault Secrets User和Redis default访问策略 | Principal ID、资源scope和角色/策略名称与计划一致；PG、Vault、Redis均保持公网关闭 |
| S5-04 database-roles | runtime映射为`llmgw_migrator`，application映射为`llmgw_app`并按DDL/DML边界授权 | 审核database-roles计划和执行结果；不要再给application管理员或migrator成员关系 |

**7. 按时间点完成回读。** S5-01之前完成database OIDC/Variable、database Reader、deploy权限、runtime Reader/回执角色和适用的旧AKS权限。S5-02之后先在PG → Authentication核对实际Entra管理员，再核对Vault及Redis自动授权和Runner私网DNS/TLS；这些资源创建前无法用手工角色代替。S5-03/04只使用database身份，S5-05至S5-10切回runtime身份。任一实际登录身份、Object ID、scope或授权类型不匹配就停止，不用个人管理员Token或订阅Owner成功来代替workflow身份验证。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S5-01 | Customer infrastructure deployment | 5 | component=platform | plan | 留空 | 留空 |
| S5-02 | Customer infrastructure deployment | 5 | component=platform | deploy | S5-01的plan ID | test |
| S5-03 | Customer private runtime operations | 5 | action=database-roles | plan | 留空 | 留空 |
| S5-04 | Customer private runtime operations | 5 | action=database-roles | execute | S5-03的plan ID | test |
| S5-05 | Customer private runtime operations | 5 | action=backend-secrets | plan | 留空 | 留空 |
| S5-06 | Customer private runtime operations | 5 | action=backend-secrets | execute | S5-05的plan ID | test |
| S5-07 | Customer private runtime operations | 5 | action=restore-target | plan | 留空 | 留空 |
| S5-08 | Customer private runtime operations | 5 | action=restore-target | execute | S5-07的plan ID | test |
| S5-09 | Customer private runtime operations | 5 | action=schema-migrate | plan | 留空 | 留空 |
| S5-10 | Customer private runtime operations | 5 | action=schema-migrate | execute | S5-09的plan ID | test |

S5-02之后先确认Runner到新PG/Redis/后台Vault的Private DNS及TLS连接、数据库管理员权限，再执行S5-03。database-roles创建独立llmgw_migrator和llmgw_app并授予应用所需数据权限；backend-secrets从旧litellm-env导入原Master/Salt到新Vault，目标已有不一致值或旧Salt不明确时停止，不生成替代值冒充恢复。

restore-target只恢复到Stage5输出绑定的**空目标库**，不使用手填任意DATABASE_URL；非空/失败残留须由DBA评估，不自动DROP或加`--clean`。schema-migrate之后才可发布新版应用，固定1.95.0→1.104.0合成升级已测试，但客户实际历史指纹/扩展/密文必须核对；未知历史不能baseline/reset绕过。

**输出/失败：** 后台密钥与schema动作保存ARM操作回执，后续application检查其SHA和配置，不手写这些回执。失败可能已部分创建角色、Vault版本或提交迁移；先检查现场再新plan，禁止删除旧数据或密钥以求重跑成功。

**阶段验收：** stage=5 draft/confirm；`pg_migration_restore`、`key_salt_decryption`、`redis_entra`、`csi_rotation`。需要的身份、密文、TLS、备份恢复及CSI/Redis验证不能只靠ARM存在证明；若需受控临时探针，由实施人员提供并获批，不能提前运行要求Stage5通过的Stage6 execute来绕过依赖。

### 阶段6：HA、路由与容量

**开始前：** Stage5已验收；无需新的GitHubSecret。确认Stage4得到的派生后台镜像签名仍绑定当前仓库、代码SHA和environment；若代码变过，应重新处理相关证据与镜像，不只改镜像字段。未提前构建时先按Stage4的Promote填写方式构建，不能使用上游默认入口镜像代替Azure派生运行时。

客户JSON增加application，填成功晋级后的digest和实际模型映射：

```json
"application": {
  "backendImage": "REPLACE_ACR/litellm-azure@sha256:REPLACE_BUILT_DIGEST",
  "models": [{
    "modelGroup": "coding",
    "connectionAlias": "primary",
    "deploymentName": "REPLACE_EXISTING_MODEL_DEPLOYMENT",
    "id": "primary-coding",
    "apiVersion": "v1"
  }]
}
```

`connectionAlias`必须匹配platform.azureOpenAIConnections中的alias，deploymentName是该账号实际部署名，不是模型展示名。**客户端**API base使用`https://<api-domain>/v1`，请求`model`使用LiteLLM `model_name`别名（此处`modelGroup`，例如`coding`），不要使用deploymentName。API只开放固定Responses/Chat/Embedding推理路径，不开放`/v1/models`；客户端模型发现被拒绝不等于推理失败。不要填写MIGRATION_MANIFEST_YAML；schema/后台密钥/无密码数据库连接与CSI从前序回执生成。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S6-01 | Customer private runtime operations | 6 | action=application | plan | 留空 | 留空 |
| S6-02 | Customer private runtime operations | 6 | action=application | execute | S6-01的plan ID | test |

核对新后台双副本、无DDL应用角色、模型调用、Redis共享状态及真实身份续期。Entra路径此时API/admin代理还没发布，不能把从外网调API失败误判为后台部署失败，也不开放后台公网临时测试。显式native-auth使用独立后台Vault UI密码，从批准私网验证Admin `/fallback/login`及virtual key，完成后跳过Stage7并继续Stage8；具体配置及回执刷新见[本地Stage6指南](../local_execution/stage2-9-guide-zh.md#stage6发布新litellm后端)，不是Entra SSO已启用。

**阶段验收：** stage=6 draft/confirm；`replica_failure`、`load_affinity`、`capacity_limits`、`no_legacy_db_writes`。负载、故障与费用测试须批准；确认没有把新版连到旧库。失败修复新环境，不切旧流量。

### 阶段7：Entra和双域名授权

**仅Entra代理路径适用，当前native-auth跳过本节。开始前：** Stage6已验收；补`AZURE_ENTRA_CLIENT_ID`和`AZURE_ENTRA_ACCESS_CLIENT_ID`两个Environment Variables并完成对应Graph授权，不能以runtime/PG管理员代替。先构建代理镜像，再冻结entra/proxy配置，避免凭据回执因后补digest反复失效。

运行`Promote LiteLLM image`：environment=test、acr_name=客户ACR、source_image保持固定默认、target_tag=`auth-proxy:rehearsal-1`（示例）、build_auth_proxy=true、build_azure_runtime=false。两个构建选项不能同时勾选，输出代理digest不等于后台digest。

客户JSON的entra填两个**服务主体Object ID**，proxy填实际客户端Application Client ID、API准入主体和管理用户；角色/模型不使用通配符。建议首次即决定nativeUi及哪些管理员可读正文，避免后续扩大已有管理Key合同：

```json
"entra": {
  "bootstrapPrincipalId": "REPLACE_ENTRA_BOOTSTRAP_SERVICE_PRINCIPAL_OBJECT_ID",
  "accessPrincipalId": "REPLACE_ENTRA_ACCESS_SERVICE_PRINCIPAL_OBJECT_ID",
  "credentialLifetimeDays": 90
},
"proxy": {
  "image": "REPLACE_ACR/auth-proxy@sha256:REPLACE_BUILT_DIGEST",
  "nativeUi": true,
  "apiClientIds": ["REPLACE_CALLING_APPLICATION_CLIENT_ID"],
  "bindings": [
    {"oid": "REPLACE_API_USER_OBJECT_ID", "principalType": "User", "plane": "api", "role": "internal_user"},
    {"oid": "REPLACE_ADMIN_USER_OBJECT_ID", "principalType": "User", "plane": "admin", "role": "proxy_admin_viewer", "models": ["coding"], "nativeAuditRead": true}
  ]
}
```

这是字段示例；viewer只读，不代表客户必要管理写入已经满足。管理models引用application.modelGroup；**API绑定不填models或内部Key**，模型/预算权限由客户端vkey在LiteLLM中决定。apiClientIds不是deploy/runtime Client ID清单，而是实际调用API的获准客户端应用。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S7-01 | Customer infrastructure deployment | 7 | component=proxy-foundation | plan | 留空 | 留空 |
| S7-02 | Customer infrastructure deployment | 7 | component=proxy-foundation | deploy | S7-01的plan ID | test |
| S7-03 | Customer private runtime operations | 7 | action=entra-apps | plan | 留空 | 留空 |
| S7-04 | Customer private runtime operations | 7 | action=entra-apps | execute | S7-03的plan ID | test |
| S7-05 | Customer private runtime operations | 7 | action=entra-access | plan | 留空 | 留空 |
| S7-06 | Customer private runtime operations | 7 | action=entra-access | execute | S7-05的plan ID | test |
| S7-07 | Customer private runtime operations | 7 | action=admin-credentials | plan | 留空 | 留空 |
| S7-08 | Customer private runtime operations | 7 | action=admin-credentials | execute | S7-07的plan ID | test |
| S7-09 | Customer private runtime operations | 7 | action=proxy-credentials | plan | 留空 | 留空 |
| S7-10 | Customer private runtime operations | 7 | action=proxy-credentials | execute | S7-09的plan ID | test |
| S7-11 | Customer private runtime operations | 7 | action=application | plan | 留空 | 留空 |
| S7-12 | Customer private runtime operations | 7 | action=application | execute | S7-11的plan ID | test |

proxy-foundation创建身份及Vault，不等于已生成登录凭据；entra-apps创建应用，entra-access才授予精确准入；admin-credentials生成管理OIDC和会话秘密，proxy-credentials初始化代理管理Key，**不替客户签发普通API vkey**。生成秘密写Vault，客户不复制进Actions；实际CA/MFA和客户端登录仍需客户身份管理员配合。Graph与Vault的部分成功可能留下待处理凭据，失败后按[轮换/恢复参考](customer-deployment-workflows-zh.md#管理员oidc轮换与孤立凭据恢复)处理，不能反复生成或删除状态。

**实际验证：** API同时携带`Authorization: Bearer <企业Token>`和`X-LiteLLM-API-Key: <客户端vkey>`；缺任一凭据、过期Token、无效/撤销Key、越权模型或预算超限应拒绝。记录客户实际Codex CLI、VS Code扩展及SDK版本，验证连续对话、工具调用、JSON/SSE、取消/重连和Token续期。不是要求实现全部Codex协议，但当前代理拒绝WS升级、部分对象引用/加密上下文；所选客户端确实依赖时仍是切流阻断，不能绕过企业认证。

管理员从私网登录原生UI，测试批准的日常Key/用户/Team/模型/预算操作。Logs核心读取已有本地测试，但未开放的必需管理写入不能用直改PG替代。`Customer gateway isolation checks`只有environment=test，待域名确实指向候选网关后运行；若API DNS还指向旧入口，结果不能算新网关验收。

**阶段验收：** stage=7 draft/confirm；`tenant_negative_tests`、`object_ownership`、`admin_private`、`required_protocols`。以客户真实首发范围验证，不要求所有插件/移动端页面；不支持的对象能力应明确拒绝并记录范围，不能填虚假的“对象授权已完成”。

### 阶段8：原生正文留痕与观测，增强L3可选

**开始前：** Entra路径Stage7已验收；native-auth路径Stage6原生入口及认证已验收且跳过Stage7。Stage2已配置并批准contentAudit native、正文留存1–30天、读取者、备份保留和容量/IO预算。一期用途是先按用户/时间统计Token费用，再由授权人员抽查高用量用户的工作内容；不是默认建设独立Blob/HSM或不可变取证平台。

**普通原生路径没有新增GitHub身份或Secret。** 托管application在Stage8生成`store_prompts_in_spend_logs=true`、`disable_spend_logs=false`及留存配置；不需要auditRuntime，也不走旧静态L3 overlay。Stage6/7正文仍默认关闭，不能只配置contentAudit而不发布Stage8就认为已采集。

选择遥测collector时，先把固定版本0.148.0镜像晋级到目标ACR，并加入顶层`observability.collectorImage`；批准AMPLS/Private DNS的影响。Promote的两个build开关均为false、source_image使用`otel/opentelemetry-collector-contrib@sha256:8164eab2e6bca9c9b0837a8d2f118a6618489008a839db7f9d6510e66be3923c`、target_tag用独立collector仓库标签。目标引用须保留此digest。未选择collector时跳过S8-01/02，仍保留基础日志和约定故障监测。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S8-01 | Customer infrastructure deployment | 8 | component=observability | plan | 留空；仅选择collector时 | 留空 |
| S8-02 | Customer infrastructure deployment | 8 | component=observability | deploy | S8-01的plan ID | test |
| S8-03 | Customer private runtime operations | 8 | action=application | plan | 留空 | 留空 |
| S8-04 | Customer private runtime operations | 8 | action=application | execute | S8-03的plan ID | test |

**审计实测：** 使用真实首发客户端发送获准测试内容，等原生日志写入后，在原生UI Logs按时间/用户/Key/request ID查请求和响应；固定版本Chat/SSE正文主要在PG的`proxy_server_request`与`response`，`messages`为空不代表没Prompt。UI只展示元数据时，由明确获批的IT实名只读PG身份查询限定日志表/视图；该身份和网络访问需DBA另行配置，**当前没有一个自动授予PG审计只读权限的workflow按钮**。不要给审计者数据库超级管理员或共用llmgw_app/llmgw_migrator。

核对实际企业主体与Key指纹的关联；共享Key的Owner不能代表实际调用者。高Token也可能来自代码上下文、重复历史、工具结果或重试，不能只凭用量或不完整片段认定不当使用；按获批用途、时段和人员范围抽查，结论由人工结合工作任务核实。

**正常能力与边界：** 正文随Spend Logs写PG，不需要另一套Prompt存储。开关之前的历史不会自动补齐；异步日志队列在故障时可能丢失，脱敏/超大载荷截断及过期清理也可能使原文不完整。一期不承诺逐帧零丢失，但须确认正常请求能入库、日志写入故障可发现、留存足够覆盖抽查周期。备份中的正文可能比在线数据保留更久，恢复会重新出现，权限和处理策略应一致。

已有管理凭据的nativeAuditRead/nativeUi合同改变不会静默扩权；需独立批准的凭据更新，不能删旧Key/Vault重新初始化绕过。已有L3/guardrail/遥测不能因原生发布静默关闭；选择增强L3的客户另按[增强流程](customer-deployment-workflows-zh.md#stage8可选增强l3交付核心进展)执行，不把它列为一期默认。

**阶段验收：** stage=8 draft/confirm；原生模式为`native_spend_logs`、`native_audit_access`、`native_retention_recovery`、`guardrail_scope`，加`telemetry_received`或`telemetry_disabled`（取决于是否配置observability）。不是要求全部UI页面兼容；授权查询及客户实际使用的必要能力未通过仍不能切流。

### 阶段9：并行试点、可选最终迁移与退役

**开始前：** 所选路径适用的Stage0–8均已验收（greenfield无旧库备份/Stage1，native-auth无Stage7）；已完成必要客户端、管理操作和日志抽查。migration须验收目标库来自哪个备份时间点，并接受新旧数据库在此后独立写入；greenfield须确认新库/新密钥初始化。并行canary不要求旧系统停写或完成第5节，但须有停止试点方案、独立试点virtual key和获批客户端名单。只有客户要求无损承接备份后的旧系统新增数据时，才把第5节作为后续production/退役前置条件。

**客户JSON：** origin的VNet/ingress子网沿用目标网络。使用托管privateIngress时，`apiLoadBalancer`和`adminLoadBalancer`的`resourceGroupName/name/frontendName`均可填`auto`；代码从Stage4回执及实际LB分别解析两个前端并拒绝复用。edge的`privateOrigin`和`adminPrivateOrigin`可分别把PLS ID填`auto`，location填批准区域。

`adminAllowedCidrs`必须填写管理员浏览器实际使用路径的稳定公网出口，不能填写Laptop私网地址、`0.0.0.0/0`或推断的公司网段。IPv4逐项使用`/32`，IPv6逐项使用`/128`；分别确认浏览器的两类实际出口并取得批准，当前运行环境两类均已配置。Admin WAF固定Prevention，并以`SocketAddr`、`IPMatch`、`negateCondition=true`阻断白名单外来源；`adminRateLimitPerMinute`按真实企业NAT/UI基线设置。来源IP只是一层网络门禁，不能替代内层登录、强凭据、审计或FDID源站绑定。

Admin客户端到Front Door不使用mTLS，不创建边缘CA信任Vault或Front Door Secret，也不依赖Preview API。两份Traefik源站证书仍须链到Microsoft Trusted CA List中的根并匹配各自域名；这是Front Door到私有origin的服务端TLS，不是管理员身份认证。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S9-01 | Customer infrastructure deployment | 9 | component=origin | plan | 留空 | 留空 |
| S9-02 | Customer infrastructure deployment | 9 | component=origin | deploy | S9-01的plan ID | test |
| S9-03 | Customer infrastructure deployment | 9 | component=edge | plan | 留空；release=false | 留空 |
| S9-04 | Customer infrastructure deployment | 9 | component=edge | deploy | S9-03的plan ID；release=false | test |
| S9-05 | Customer private runtime operations | 9 | action=edge-bind | plan | 留空 | 留空 |
| S9-06 | Customer private runtime operations | 9 | action=edge-bind | execute | S9-05的plan ID | test |

origin为API和Admin各创建一条Private Link Service并绑定不同内部LB frontend；edge创建一个Premium profile下的两个独立endpoint/domain/origin/route/WAF，默认两边流量均禁用，API WAF为Detection，Admin WAF固定Prevention，不改DNS。**S9-04成功后、S9-05之前**，Owner须分别审批两条托管Private Endpoint请求并核对profile/origin/PLS/frontend归属，同时确认两个自定义域/边缘证书、两份公有CA源站TLS和Admin来源CIDR。edge-bind会实时要求每条PLS恰有一个Approved且没有Pending连接，再把同一实际Front Door ID绑定到两个受管代理或两个原生Traefik业务router，分别rollout并保存双平面回执；健康router不绑定FDID，本步仍不启流量。

保持AKS `Running/Succeeded`直至绑定、rollout与入口检查结束；停止状态下私有API DNS A记录可能缺失，不代表需要开放公网。获准启动后等待管理面恢复再核验，按[私有AKS启动复核](../local_execution/stage2-9-guide-zh.md#私有aks自动停机后的启动复核)处理，不盲重放平台部署。当前native dev/test已发布后的精确Admin地址新增走[专用批准流程](../local_execution/stage2-9-guide-zh.md#已发布native-devtest-canary新增admin来源ip)，不重跑禁流量edge准备，也不直接在Portal制造漂移。

**独立发布报告：** 实际启用流量前，将获批报告填入Environment Secret `MIGRATION_RELEASE_JSON`。它不是验收账本，也不能直接把acceptance JSON复制进去。字段与检查集由[发布校验器](../scripts/stage9_release.py)定义，主要字段如下：

| 字段 | 来源与含义 |
| --- | --- |
| environmentName、baseDomain、logAnalyticsWorkspaceName、rateLimitPerMinute、adminRateLimitPerMinute | 与客户配置保持一致 |
| phase、wafMode | prepare/Detection保持禁用；canary/Detection启流量；production/Prevention正式发布。native Responses WSS方法契约随首次Stage4/9部署接入；已发布dev/test升级及真实协议验收见[WSS执行单](../local_execution/native-wss-upgrade-zh.md)，不重跑禁流量prepare |
| privateOrigin、adminPrivateOrigin | origin实际两条PLS ID和区域，不保留auto或REPLACE，且ID不得相同 |
| adminAllowedCidrs | 与edge配置完全一致的批准公网出口CIDR数组；必须是实际客户端路径的全局可路由地址，不是内网地址 |
| frontDoorId | edge输出中的profileId，是Front Door GUID，不是CDN profile的ARM Resource ID |
| revision、configSha256 | 当前同版本Stage9 plan-summary中的revision和configSha256，不手算也不用镜像digest代替 |
| auditMode、telemetryEnabled | 本文auditMode=native；telemetryEnabled与是否配置observability一致 |
| changeTicket、approvedBy | 获准变更引用及governance对应Entra批准人Object ID；单人1位、双人2位 |
| checks | dev/test native canary可省略；其他canary和production为当前phase必需的实际结果，每项含passed=true、report安全引用、带时区observedAt，须7天内有效 |

`environmentName=dev/test`、`phase=canary`、`authenticationMode=native`且`auditMode=native`时，报告可省略整个`checks`对象；已有空值或`passed=false`条目也会被忽略。S9-07仍实时核对当前revision/config、后端入口回执、双PLS、源站TLS、Admin WAF/CIDR、Private Link批准及双平面edge-bind，因此简化的是重复的人工证据索引，不是绕过实时安全状态。前序Stage验收及canary期间的真实客户端、virtual key、Admin登录和Spend Logs验证仍须执行。Entra或增强L3路径不使用这个简化分支。

`environmentName=prod`的native canary仍要求prepare检查加企业认证、实际协议、数据库恢复、WAF/试点和原生日志/受控查询/留存恢复证据；未选collector时用telemetry_disabled，不伪造telemetry_alerts。production另需canary SLO、WAF Prevention评估、DNS切流/回退证据。严格路径没有自动采集全部证据并生成报告的按钮，实施负责人需整理真实结果。

| 发布phase | 必须具备的checks名称，沿用前一行集合再增加 |
| --- | --- |
| prepare | private_origin_tls、origin_bypass_denied、admin_source_ip_allowlist、admin_private_origin_isolation、waf_diagnostics_privacy、private_link_approval、rollback_plan |
| dev/test native canary | 无必填checks；使用报告元数据、批准信息及S9-07实时门禁 |
| prod native canary | native_virtual_key_acl、native_admin_password_login、required_protocol_matrix、database_restore、waf_detection_review、approved_pilot_clients、native_spend_logs、native_audit_access、native_retention_recovery；启用collector用telemetry_alerts，否则telemetry_disabled |
| native production | canary_slo、waf_prevention_review、dns_cutover_and_rollback |

dev/test canary报告元数据和批准信息完成后即可执行下列S9-07/08，不需要先完成第5节；prod canary/production须再满足对应严格证据。每次phase变化先更新已审核MIGRATION_RELEASE_JSON并重新plan；普通S9-03的禁用计划不能批准启流量。若以后进入无损production迁移，再先完成第5节并使用production报告。

| 步骤 | workflow显示名称 | stage | component或action | operation | approved_run_id | confirm_environment |
| --- | --- | --- | --- | --- | --- | --- |
| S9-07 | Customer infrastructure deployment | 9 | component=edge | plan | 留空；**release=true**，报告为本次获批phase | 留空 |
| S9-08 | Customer infrastructure deployment | 9 | component=edge | deploy | S9-07的plan ID；**release=true** | test |
| S9-09 | Customer private runtime operations | 9 | action=dns-publish | plan | 留空；仅已批准Azure DNS路径 | 留空 |
| S9-10 | Customer private runtime operations | 9 | action=dns-publish | execute | S9-09的plan ID | test |

启流量并不自动限制canary用户，试点名单、Admin公网出口、virtual key和UI凭据必须实际受控。DNS动作支持同订阅已委派Azure DNS中的`llm-api.<baseDomain>`与`llm-admin.<baseDomain>`两条CNAME，分别指向对应endpoint；需提前配置顶层`dns={zoneResourceId,ttl}`和runtime DNS写权限，TTL范围60–3600秒。两条记录共享一个带ETag的检查点；中途失败可从已知前态/目标态继续，但任何第三方值都会阻止自动继续或回退。并行canary新增这两个域名，不替换旧系统域名。其他DNS提供方由客户DNS管理员人工新增等效双记录，不运行Azure DNS动作，也不临时修改脚本猜API。

DNS回退为同runtime、stage=9、action=dns-rollback，另跑plan/execute并引用恢复plan ID；它按检查点恢复API/Admin两条记录，不是数据库回退。第三方DNS按客户变更记录撤销新CNAME。停止试点时先撤销试点key并让客户端恢复旧API域名；新库独有数据不会自动并回旧库。canary验收通过后，可继续维持并行运行；只有客户决定正式迁移时，生产报告才使用phase=production/wafMode=Prevention并重复S9-07/08。若DNS已指向同一获准目标，不为重复操作而重做DNS变更。

候选入口实际就绪后运行`Customer gateway isolation checks`，environment=test：API匿名/错误Host须拒绝；Admin从不在白名单中的公网出口须得到HTTP 403。从白名单内出口继续验证错误UI密码拒绝和正确Entra或原生登录，并确认共享NAT内的来源准入不被误写成用户身份认证。负向探针不证明业务成功。**阶段验收：** stage=9 draft/confirm；`enabled_what_if`、`origin_tls_private_link`、`pilot_regression`、`rollback_rehearsal`，加single_operator_release或dual_owner_release。stage acceptance不能替代MIGRATION_RELEASE_JSON、备份基线核验或后续可选最终迁移的数据对账。

## 5. 可选最终迁移、观察和旧环境退役

本节不阻止独立新域名canary。客户可以明确接受已验收备份作为新环境起点，让新旧系统使用不同域名和独立数据库并行运行；这种选择必须记录备份时间点和数据分叉边界，不能声称两边持续一致。试点不达标时撤销新key、恢复客户端旧API配置并停用新入口，不需要覆盖旧库。

只有客户要求新环境无损承接备份时点之后的旧系统新增数据时，才执行下面的最终迁移检查点。目前最终写入冻结、最终空目标库选择/切换及数据回退编排尚未完整实现，不能拿Stage0旧时间点备份加一次DNS更新当作无损迁移，也不能直接覆盖已有试点写入的目标库。

| 顺序 | 责任/执行方式 | 继续条件 |
| --- | --- | --- |
| F-01 演练和窗口 | 实施负责人/DBA核对实际库大小、备份/传输/恢复/升级/核验/切流耗时；现有Stage0/5提供部分操作 | 批准停机窗口、超时阈值和回退条件；不把开发者人数或合成测试秒数当RTO |
| F-02 最终写入冻结 | **尚无完整workflow动作**；实施人员须提供并批准实际入口停写、排空及后台写入停止步骤 | 旧推理、管理、预算和后台写入均已停止，保留原恢复状态 |
| F-03 最终备份和目标恢复 | **最终目标选择/接线待补齐**；DBA明确干净最终库，用批准备份恢复/迁移流程核验 | 不能对非空演练库直接重复restore-target，不自动DROP；保留原Master/Salt |
| F-04 数据/功能核对 | 对账用户/Team/Key/预算/模型配置和必要历史密文；用真实Codex/SDK和管理路径测试 | 新库包含最终数据；认证、权限、正常请求及正文查询符合一期范围 |
| F-05 批准正式迁移 | 完成本节前置后，以production报告重复S9-07/08；新CNAME已指向同一edge时不重复发布DNS | 新系统独占后续业务写入，不随机将用户分到两个不同步的预算库 |
| F-06 稳定观察 | 人工确定观察期及频率，检查真实业务错误/延迟/预算、PG连接、正文写入、用户反馈 | 覆盖实际业务使用，未达到通过标准不宣布迁移完成 |
| F-07 停旧 | **当前无stop-legacy workflow按钮**；Owner按单独批准的停用方案执行并验证 | 新系统不依赖旧资源；保留旧PG/PVC、备份及密钥恢复材料，停用不是删除 |

若客户不要求最终数据合并，可跳过F-01至F-05，在足够长的并行观察后单独进入F-07；退役前仍须确认所有计划迁移的客户端已改用新域名、旧入口无请求/写入，并决定旧库中的Key、预算、配置和日志如何归档。停止旧AKS应先于资源删除，删除须另立变更。

正式迁移窗口前失败继续使用旧系统；窗口内新库未产生独有业务写入时，可按已演练步骤恢复旧入口/旧写入。新库已产生独有Key、预算或日志等写入后，先暂停并对账，再决定修复新系统或数据回退；**只改DNS不保证无损回退**。不混用新旧schema，不自动跨schema回迁。

不把本次演练中的手工旧监控清理推广成客户前置步骤。现有工作区、App Insights、DCR、告警、网络/共享模型必须先查依赖；没有逐项授权不删除。删除旧RG、清理日志正文或移除共享身份是另一项变更，不是workflow成功后的自动收尾。

## 6. 常见错误与重跑规则

| 现象/日志 | 首先核对 | 下一步 |
| --- | --- | --- |
| authorize的GITHUB_REF_PROTECTED检查失败 | main是否匹配Active规则，是否手动从默认分支运行 | 修复分支保护；不删门禁 |
| runs-on / fromJSON: empty input | MIGRATION_PRIVATE_RUNNER_LABELS是否为Repository Variable、有效JSON数组 | 不放Secret或仅Environment；标签匹配实际Idle Runner |
| 等待Runner | VM/systemd服务、标签、仓库/Runner Group准入 | 先恢复执行机，不更换业务集群 |
| azure/login: Not all values are present | 当前workflow读取的是deploy还是runtime Client ID；tenant/subscription Variables是否在所选环境 | 不能把AZURE_CLIENT_ID当作所有动作的身份，也不能只放同名Secret |
| OIDC没有匹配联邦凭据 | issuer/audience/subject以及仓库和environment大小写 | 修正已批准身份的信任；不改成VM IDENTITY或长期Client Secret |
| WORKFLOW_ARTIFACT_KEY/CUSTOMER_CONFIG_JSON在env中空白 | 两者是否为本次Environment Secrets，而不是Variables、文件路径或本机变量 | 补齐后新运行；密钥已生成则不随意轮换 |
| Workflow evidence protection failed | base64解码后密钥是否32字节，附件是否同key/仓库/run ID/name | 不打印密钥，检查存放位置及元数据；不用新key解旧附件 |
| backup-restore: execution-failed且POSTGRES_RESTORE_IMAGE为空 | Stage0新增镜像Variable，完整repo@sha256引用 | 按0-B获取，不填备份SHA或IMAGE ID，不运行plan |
| backup-restore在plan失败 | 该动作只有execute | 先完成备份前置，再直接execute并确认环境 |
| deploy/execute批准字段为空或run ID填到hash框 | 页面两个description标签 | 把数字填入Successful matching ... plan run ID，Legacy direct hash框留空 |
| stale / plan changed / 签名或回执版本不符 | plan/execute是否同代码、配置、实际资源；镜像签名是否当前revision/environment | 修配置后重新plan/审核；不复制新哈希越过检查 |
| Azure 403 / Kubernetes Forbidden / PG拒绝 | 本动作实际身份和具体资源范围，ARM/数据平面/Kubernetes/PG分别授权 | 找对应Owner补最小权限；不因为登录成功就授予Owner兜底 |
| Blob解析公网IP或上传超时 | 管理VNet到备份PE路由、Private DNS、NSG及Blob数据角色 | 不开放Storage公网、不把NAT视为私网接通 |
| bootstrap plan在旧版本报通用执行错误 | 是否已包含处理What-if delta:null的修复 | 合并修复后新Run workflow，不能重跑旧SHA任务 |
| 日志显示失败 | 依次读取`Context`、`Result category`、脱敏`Error`、`Source`，再解密`operation-status.json` | 按具体Azure错误码、命令类别、校验消息及源码位置定位；需要原始云详情时查对应Azure Operation details |
| Git push main报GH013 | main要求PR | 推工作分支，通过PR合并，不强推或关闭保护 |
| Bastion命令缺少ssh扩展 | 发起连接机器的Azure CLI扩展 | 同时具备bastion和ssh，不重建Runner |

公开日志中的`***`是遮罩，不表示脚本真的含这些字符；不要照着遮罩日志复制命令。Secrets空白和`***`也不是一回事。不要启用公开shell trace、打印环境变量或上传dump/原始客户配置来定位失败。

原始stdout/stderr仍只保留在作业私有临时目录并在结束时清理，绝不上传。公开日志和加密`operation-status.json`保留的是同一份有长度限制的脱敏诊断，不是完整失败响应；其中`Source`必须结合该次run的Git SHA阅读。若摘要不足，先查Azure/AKS对应Operation details，或用同版本、同Actions身份在受控环境做只读最小复现；本机管理员身份成功不能代替Actions运行身份。不要为定位错误盲目重跑有副作用的恢复或凭据动作。

### 6.1 每次运行的记录表

在受控位置维护下表即可，不提交客户信息到公开Git，不要求另建证据平台：

| 日期/环境 | 本文步骤号 | workflow及stage/action | Git SHA | plan run ID | execute/deploy run ID | 实际观察/失败处理 | 验收draft/confirm run ID |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

**进入下一Stage前：** 前序适用检查全部真实通过，验收账本有效且同修订；所需新Variables/Secrets/JSON字段已准备；计划已解密审核；知道会修改哪些旧/新资源和怎样处理失败。文档完整、CI通过或单个workflow成功，都不等于客户迁移已经完成。