# 安全增强版LiteLLM Azure基础设施

本目录提供客户安全网关的Bicep模板，不提供旧生产环境的一键原地升级。客户从[项目总览](../README_ZH.md)和[迁移指南](../docs/customer-migration-guide-zh.md)进入，使用Customer staged migration workflow按阶段注入配置、审查What-if，再单独批准部署。

当前West US 3（`westus3`）greenfield测试也可按[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)受控执行。2026-10-09只读管理面显示Private AKS `Running/Succeeded`、Azure CNI overlay、Azure RBAC、禁用Local Accounts、OIDC及Workload Identity启用，System/User各2台`Standard_D4s_v4`。这是当前参考状态，不是模块默认SKU或Kubernetes rollout/应用验收证明。API/Admin分别经Front Door Premium及独立PLS回源；backend与certificate Vault均私有且分离，PostgreSQL/Redis为Entra-only。

当前数据/秘密服务的只读核对：PostgreSQL 16为`Ready`，`activeDirectoryAuth=Enabled`、`passwordAuth=Disabled`、`publicNetworkAccess=Disabled`；Redis为`Microsoft.Cache/redisEnterprise`、`Balanced_B0`，默认数据库`accessKeysAuthentication=Disabled`、`clientProtocol=Encrypted`、端口`10000`。Redis集群`publicNetworkAccess`空值不是公网开关证据，不据此推断。ACR Premium禁用Admin User和公网访问；backend/certificate Vault均启用RBAC并禁用公网访问。Admin WAF当前批准三个精确IPv4 `/32`及一个IPv6 `/128`，不发布实际地址。

## 目录边界

第一阶段审计方向为原生Spend Logs，获批正文进入私有PostgreSQL。原生发布/模式化门禁已实现，实际查询权限、留存、容量及恢复仍须验收。独立audit-foundation/audit-storage/audit-detection按增强需求选用，不是基础版固定BOM；既有Blob/HSM Key及保全数据不能自动删除。详见[部署指南](../docs/customer-deployment-workflows-zh.md)及[成本口径](../docs/litellm-bom-cost-comparison-zh.md)。

- `modules/`：可复用Azure资源模块；
- `environments/`：`dev/test/prod`环境入口与参数；
- [backup-storage](backup-storage/README_ZH.md)：阶段0可选的私有备份存储及初始专用VNet；禁止接管共享VNet或在阶段4后重放bootstrap模板；
- [hub](hub/main.bicep)：阶段0可选的区域Hub防火墙，配合Stage4 hub出口模式，见[可选Hub-Spoke出口](#可选hub-spoke出口)；
- [monitoring](monitoring/README_ZH.md)：阶段1旧网关最小告警，需匹配客户实际日志与命名；
- [audit-storage](audit-storage/main.bicep)：可选增强L3的独立CMK私有审计存储及分离权限，不用于原生Spend Logs正文；
- [audit-detection](audit-detection/main.bicep)：默认禁用的检测规则及响应契约，非已运行的自动响应；
- [edge](edge/README_ZH.md)及[edge-origin](edge-origin/main.bicep)：阶段9 API/Admin独立Front Door endpoint、Admin WAF来源IP白名单、内层登录及两套Private Link Service。

这些模板和本地验证结果不等于客户资源已部署。提交的环境参数默认关闭资源创建；`customer-migration.yml`只提供指导、检查与只读预览，实际写入由独立部署入口或本地批准plan/execute执行。客户边界见[迁移指南](../docs/customer-migration-guide-zh.md)；[收尾台账](../docs/litellm-code-completion-backlog-2026-09-07.md)中的历史接线缺口不代表当前参考环境仍未接线。

## ACR模块

`modules/container-registry`声明Premium ACR，并默认：

- 禁用Admin User；匿名拉取保持服务默认关闭，并在部署后验收；
- 禁用公网访问；
- 禁止网络绕过；
- 禁止镜像导出；
- 开启未标记manifest保留。ACR Soft Delete需在目标API和区域能力确认后单独启用。

环境入口中的 `deployContainerRegistry` 默认均为 `false`。原因是公网禁用的ACR必须先完成Private Endpoint、Private DNS和受控构建/导入执行路径。阶段4网络设计批准前不得为了方便把生产ACR长期开放公网。

## 参数与Secret

客户workflow使用`CUSTOMER_CONFIG_JSON` Environment Secret生成明确的资源名、区域、Owner和组件参数；配置必须与OIDC的订阅/租户一致。`WORKFLOW_ARTIFACT_KEY`是独立Environment Secret，阶段验收账本通过加密artifact传递，不要求手写`MIGRATION_EVIDENCE_JSON`。完整字段和前置条件见[客户配置模板](../config/customer.example.json)及迁移指南。

直接编译环境参数时使用`LITELLM_ACR_SUFFIX`、`AZURE_LOCATION`、`OWNER_EMAIL`和`LOG_ANALYTICS_WORKSPACE_NAME`；`stage3check`、`owner@example.com`等回退只用于离线检查，不是部署默认值。客户入口拒绝占位符，不读取个人本地环境。

任何Secret、Token、Client Secret、数据库密码和连接串都不得写入Bicep或`.bicepparam`。目标值通过Key Vault和Workload Identity提供。

## 验证

在仓库根目录执行`make validate-stage9`覆盖前序阶段的编译、渲染和安全检查；执行`./.venv/bin/python -m unittest tests.test_customer_templates`验证客户生成参数与Bicep入口的契约。两者均不部署云资源。

云端What-if可使用手动workflow的OIDC身份，或迁移指南中的客户受控终端流程。实际资源部署须另行审批，不属于自动验证。

## 阶段4网络和Private AKS模块

阶段4模块已经加入，但所有环境参数的`deployStage4`仍为`false`，当前不会创建云资源。

以下为参考网络规划，客户必须独立批准CIDR与资源名称：

| 用途 | 子网/CIDR |
| --- | --- |
| Azure Firewall | `AzureFirewallSubnet` / `10.30.0.0/26` |
| AKS System Pool | `snet-aks-system` / `10.30.1.0/24` |
| AKS User Pool | `snet-aks-user` / `10.30.2.0/23` |
| Private ingress/PLS | `snet-private-ingress` / `10.30.4.0/24` |
| Private Endpoints | 需预建 `snet-private-endpoints` / `10.30.8.0/24` |
| Pod CIDR | `10.244.0.0/16` |
| Service CIDR | `10.31.0.0/16` |

阶段4模块包括：

- Azure Firewall Premium、Policy、显式HTTPS allowlist和UDR；
- Private AKS、禁用Local Accounts、Entra Azure RBAC、Cilium、OIDC和Workload Identity；
- System/User Node Pool分离和多节点；当前模板不声明`availabilityZones`，客户跨区要求需按实际区域能力另行设计和验证；
- ACR及Azure OpenAI Private Endpoint和Private DNS；
- LiteLLM专用UAMI及Federated Identity Credential；
- 跨订阅Azure OpenAI最小数据平面RBAC。

Private AKS创建模板保留控制面Diagnostic Settings和Defender，但不在`addonProfiles`中直接启用Container Insights。参考环境曾出现workspace解析预检失败；后续独立DCR/DCRA接入仍是待完成项，必须在客户集群中验证实际数据流。

### System池只运行系统组件（可选）

`parameters.platform.stage4Aks.systemPoolCriticalAddonsOnly`默认`false`，已有环境不变。设为`true`时，模板给`systempool`加`CriticalAddonsOnly=true:NoSchedule`污点，只有带该容忍的AKS系统组件（CoreDNS、metrics-server、CSI、Azure Policy等）还能调度上去，System池因此可以用较小规格。limx按3台`Standard_D2s_v5` System池加3台`Standard_D4s_v5` User池部署，设为`true`。

不论此选项取值，平台渲染的全部工作负载都带`nodeSelector: {workload: litellm}`，只调度到User池；模板固定给`userpool`打这个标签。范围包括LiteLLM后端、API/Admin auth-proxy、两套私网Traefik、OTel采集器、L3审计StatefulSet、`l3-retention` CronJob和`l3-recovery` Job。Stage6–8发布前的清单检查会拒绝缺少该选择器的Deployment和CronJob。User池须容纳全部应用Pod，包括滚动发布的maxSurge和HPA上限，按此规划User池规格和autoscaler上限（当前`maxCount`为6）。

升级到含此改动的版本后，各工作负载首次重新发布时Pod模板变化会触发一次滚动更新，原在System池上的应用Pod随之迁到User池。Stage9 edge binding回执记录了proxy的Pod模板指纹，重新发布proxy后按既有流程重新执行edge binding。非ZRS的L3 spool磁盘固定在某个可用区，User池须在该区有节点。

已有集群把选项改为`true`时，Stage4 platform部署会就地更新System池污点。`NoSchedule`不驱逐已在运行的Pod，只是之后不再把应用Pod调度到System池，已在上面的Pod要靠排空才会迁走。须在审批的维护窗口内按以下顺序执行：

1. 先用新版本重新发布全部应用工作负载，用`kubectl get pods -A -o wide`对照节点确认应用Pod都已在User池运行，且User池容量充足；
2. 运行Stage4 platform plan，确认What-if对AKS只显示System池`nodeTaints`变化，再部署；
3. 逐台`kubectl cordon`、`kubectl drain --ignore-daemonsets --delete-emptydir-data`System节点，等被驱逐的Pod在其他节点就绪后`kubectl uncordon`，再处理下一台；drain受PDB约束，不要用`--disable-eviction`绕过；
4. 检查没有Pending的Pod；客户自行安装、只能运行在System池的组件须自带`CriticalAddonsOnly`容忍。

改回`false`后重新部署，再用`az aks nodepool show --name systempool --query nodeTaints`确认污点已清除；如仍存在，在维护窗口内执行`az aks nodepool update --name systempool --node-taints ""`。

真实Azure OpenAI资源ID、订阅和资源组不会进入提交的参数文件，必须在受保护的What-if/部署变量中注入。

启用`deployStage4=true`前必须确认：

1. 全部CIDR与客户Hub、本地网络和其他订阅不重叠；
2. 客户区域内所选AKS版本、AzureLinux和VM SKU可用；要求跨可用区时，须选择支持Availability Zones的区域并评估数据驻留和成本；
3. Azure Firewall FQDN清单通过实际依赖观测补全；
4. GitHub self-hosted runner或其他私网执行路径已设计；
5. 跨订阅Private Endpoint审批和RBAC权限已获得；
6. What-if不会修改或删除现有Blob Private Endpoint子网；
7. 当前生产环境继续保留，不移除VMSS业务UAMI。

本地运行`make validate-stage4`验证Stage3回归、CIDR、Bicep、参数和仓库安全策略。

## 可选Hub-Spoke出口

默认仍是**dedicated**：Stage4 platform在目标VNet内创建`AzureFirewallSubnet`、Firewall Premium、Policy、公网IP和规则集合组`litellm-egress-rules`，路由表把`0.0.0.0/0`指向该防火墙。未配置`parameters.hub`及`stage4Network.egress`的环境，模板编译结果的资源条件、参数取值和What-if都不变。

客户已有或计划区域Hub时，可改为**hub**模式，由同区域一个Hub防火墙集中出口：

| 组件 | 模板 | 内容 |
| --- | --- | --- |
| Stage0 `hub` | [hub](hub/main.bicep)（订阅级）+ [modules/hub-network](modules/hub-network/main.bicep) | 创建并拥有专用Hub RG；Hub VNet（只含`AzureFirewallSubnet`，/26）、Firewall Premium、Policy Premium（DNS代理、IDPS与威胁情报Alert，不含任何workload规则）、Standard公网IP、防火墙诊断写入现有Log Analytics Workspace |
| Stage4 platform（`egress.mode=hub`） | [environments](environments/main.bicep) + [modules/hub-egress](modules/hub-egress/main.bicep) | 不部署spoke的`AzureFirewallSubnet`和`firewall-egress`；创建spoke→hub Peering；在Hub RG创建hub→spoke Peering和本spoke自己的规则集合组；路由表指向Hub防火墙私网IP |

`parameters.hub`字段：`resourceGroupName`、`virtualNetworkName`（≤52字符）、`virtualNetworkAddressPrefix`、`firewallSubnetPrefix`（必须/26）、`logAnalyticsWorkspaceName`，可选`logAnalyticsWorkspaceResourceGroupName`（缺省为目标RG，即bootstrap创建的Workspace）和`reservedPrefixes`。防火墙、Policy和公网IP名称由VNet名派生为`afw-<hubVnet>`、`afwp-<hubVnet>`、`pip-afw-<hubVnet>`。模板不设置`zones`，与上文一致：在支持可用区的区域由平台自动跨区。

`reservedPrefixes`只用于校验和记录地址规划，**本期不创建**这些子网：`AzureFirewallManagementSubnet`（/26）、`AzureBastionSubnet`（/26或更大）、`GatewaySubnet`（/27或更大）。配置校验要求全部Hub子网在Hub VNet内且互不重叠，Hub VNet为私网IPv4，并且不与目标VNet、Private Endpoint/AKS/入口/Application Gateway子网、Pod CIDR和Service CIDR重叠；`stage0-hub`的plan还只读核对执行主机VNet的地址空间。Hub RG必须独立于目标RG和旧RG。

Hub模式要点：

- **防火墙地址不手填**。Stage4 platform plan读取订阅级部署回执`llmgw-<env>-s0-hub-<8位哈希>`的`hub`输出，核对RG、VNet、Policy、区域和地址规划与`parameters.hub`一致、私网IP落在`AzureFirewallSubnet`内，再把它作为`stage4Hub`参数写入本次plan；回执缺失或漂移时plan失败。
- **规则与dedicated完全相同**：来源为system/user/ingress子网，应用规则为`approvedHttpsFqdns`、ARM/登录端点及`privateEndpoint=false`模型账号主机；网络规则为`168.63.129.16:53`和NTP `123`。规则集合组名为`litellm-<env>-<8位哈希>`（哈希来自spoke与Hub VNet ID，同名环境不同RG也不冲突），优先级缺省在1000–9999内派生；plan会列出Hub Policy现有规则集合组，其他组已占用同一优先级时失败，可用`stage4Network.egress.ruleCollectionGroupPriority`（100–65000）显式指定。
- **Peering**：spoke→hub与hub→spoke均`allowForwardedTraffic=true`、`useRemoteGateways=false`、`allowGatewayTransit=false`。AKS子网写入路由前先完成两侧Peering和规则，避免集群创建时出口不通。
- **变更范围**：`hub`组件只能改Hub RG本身及其中资源；hub模式的platform在目标RG外只能改hub→spoke Peering、自己的规则集合组和对应的嵌套部署记录（另有既有的模型账号角色分配），并且不得在目标RG创建Azure Firewall、Firewall Policy或`AzureFirewallSubnet`。
- **已有dedicated环境改为hub**：增量部署不会删除原spoke防火墙、Policy和`AzureFirewallSubnet`，路由切换后需另行审批清理；出口公网IP会变为Hub防火墙的IP，采用模型账号公网白名单（方案B）时须先把Hub IP加入白名单。Hub出口IP由所有spoke共享，白名单无法再区分环境。

多区域：每个区域一个Hub，各区域环境使用同区域Hub（回执区域与`location`不一致会被拒绝）。需要全局统一基线时，可用Azure Firewall Manager把各区域Hub Policy设为同一父Policy的子Policy；本模板只创建独立Policy，父子关系需单独变更。执行主机（Runner）和Bastion本期仍留在各自管理VNet，不迁入Hub。

## 阶段5 Key Vault与托管数据服务

阶段5模块已经加入，但所有环境参数的`deployStage5`仍为`false`，且只有`deployStage4=true`时才会生效。当前不会创建云资源。

目标资源：

- Key Vault Premium：RBAC、90天Soft Delete、Purge Protection、删除锁、公网禁用、Private Endpoint、审计日志；
- PostgreSQL Flexible Server 16：Entra-only、128 GiB自动扩容、14天PITR、公网禁用、Private Endpoint和诊断；
- Azure Managed Redis：Balanced B0、HA、TLS 1.2、NoCluster、AOF、禁用Access Key、Workload Identity Entra访问策略、Private Endpoint和诊断；
- Private DNS：`privatelink.vaultcore.azure.net`、`privatelink.postgres.database.azure.com`和`privatelink.redis.azure.net`。

基础参数沿用参考环境的DNS复用设置，不代表客户已具备对应Zone。客户配置模板显式列出`createStage5*PrivateDnsZone`开关，必须按实际Zone归属填写；由中心团队预建时关闭创建，避免重复管理共享资源。

IaC不会创建任何Key Vault Secret值，也不会接收数据库密码或连接串。PostgreSQL Entra管理员对象信息必须从受保护部署变量注入；默认空值只用于安全编译，不代表可用数据库。数据库应用角色、`DATABASE_URL`、固定Salt和Master Key必须通过受控引导流程写入Key Vault，不能出现在Git、Bicep参数、部署输出或命令日志中。

静态Stage5 Kustomize组件使用Secrets Store CSI和Workload Identity，并保留逐项Kubernetes Secret同步作为该参考组件的显式补偿控制；禁止`envFrom`，启用该组件前须验证最小RBAC、etcd加密和轮换。当前生成的backend运行路径通过只读CSI文件及启动器加载秘密，不等于直接套用该静态组件。PostgreSQL/Redis采用Entra身份，不能把旧`PG_PASSWORD`/Redis Access Key路径用于当前网关；身份、刷新、私网与未授权读取行为须按当前指南验收。

本地运行`make validate-stage5`执行Stage4回归、Bicep编译、Kustomize渲染和Secret仓库检查。该命令不部署Azure或Kubernetes资源。
