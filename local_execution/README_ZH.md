# Stage0–9 本地手工执行包

> 核对日期：2026-10-09
>
> 适用：客户无法运行GitHub Actions，但已取得本仓库受审核代码，需要从本地完成migration或greenfield的Stage0–9。本文件详细覆盖Stage0–1及两种模式边界；后续按[Stage2–9本地手工部署指南](stage2-9-guide-zh.md)执行。

本目录是一套独立入口：

```text
local_execution/
├── README_ZH.md                # Stage0–1及公共准备
├── stage2-9-guide-zh.md        # Stage2–9顺序、Entra和发布边界
├── customer.example.json       # 可逐Stage扩展的最小配置
├── customer.stage2-9.fragments.example.json # Stage2–9分阶段配置片段
├── merge_config.py             # 分阶段预览/合并customer.json
├── image_supply_chain.py       # 本地镜像SBOM/扫描/签名
├── requirements.txt            # 本地全部Python依赖
├── __main__.py                 # python -m local_execution入口
└── runner.py                   # 本地顺序执行器
```

维护者可独立验证本包，不运行GitHub Actions工作流测试：

```bash
make validate-local-execution
```

它不读取GitHub Environment Variables/Secrets，不需要run ID、artifact或evidence ledger。已验证的Stage0–1继续在同一命令中预览并执行；Stage2–9默认只生成plan，审核后须用plan SHA256另行execute。底层继续复用仓库现有Azure/Kubernetes实现，以保持资源范围、私网目标、恢复校验和幂等行为一致。

Stage2–9入口已提供，但不会自动签发人工验收、最终停写、最终数据对账或停旧。Stage9默认禁止启流量。只延期Entra而未选择已实现的原生认证分支时，不能发布依赖Entra的Stage7/8应用或启用Stage9流量；显式Stage6 `--option native-auth`有独立原生发布/验收路径，按[Stage2–9指南](stage2-9-guide-zh.md)执行，不跳过发布报告和来源/协议门禁。

**当前参考环境**是West US 3（`westus3`）greenfield测试：Private AKS、Azure CNI overlay、Azure RBAC、禁用本地账号、OIDC/Workload Identity启用，System/User各2台`Standard_D4s_v4`；管理面为`Running/Succeeded`。LiteLLM `1.104.0`固定源码构建使用Workload Identity访问Azure模型，PG/Redis为Entra-only，backend/certificate Vault分离且私有。Front Door Premium的独立API/Admin endpoint分别经PLS进入隔离native ingress；Admin WAF Prevention按否定`SocketAddr`规则限制批准的精确公网IPv4 `/32`和IPv6 `/128`。Admin是原生用户名/密码fallback登录，不是用户Entra SSO。用户已验证登录和virtual key的Codex Responses推理，但这些证据不能替代全部rollout、故障/拒绝矩阵或模型同步验收。模型同步使用[项目总览中的专用runbook入口](../README_ZH.md#仓库导航)，不要改用`LiteLLM/deploy_mi_aks_litellm.py`。

## 1. 执行拓扑

客户电脑不在Azure私网时，不能直接访问私有AKS和Storage Private Endpoint。复用已经创建且带Bastion的Runner VM作为**离线手工执行主机**，不再创建第二台VM：

| 位置 | 用途 |
| --- | --- |
| 客户外部电脑 | 登录Azure、发起Bastion SSH、必要时挂载/卸载临时UAMI |
| 现有Runner VM | Runner服务停止后，手工执行本目录命令 |

### 1.1 让Runner离线

确认没有Actions作业运行，然后在Bastion会话停止服务：

```bash
sudo bash -c 'cd /srv/runner/agent && ./svc.sh stop'
sudo bash -c 'cd /srv/runner/agent && ./svc.sh status' || true
```

在GitHub仓库Settings → Actions → Runners确认该机器显示Offline。VM尚未注册GitHub时跳过此项，且不要执行注册命令。手工执行期间不得重新启动服务或派发作业。

### 1.2 Azure登录

本节是Stage0–1已验证的人工登录方式。Stage2–9也可在`localExecution.authentication`中为deploy/runtime/database/certificate/Entra分别选择现有会话或UAMI，见[后续指南第2节](stage2-9-guide-zh.md#2-azure登录方式)。

优先在VM内使用实名客户管理员登录，设备码在客户合规电脑完成：

```bash
az login --tenant "REPLACE_TENANT_ID" --use-device-code
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
az account show \
  --query '{tenantId:tenantId,subscriptionId:id,identity:user.name,type:user.type}' \
  --output json
```

不要使用`sudo az login`，不要创建长期Client Secret，也不要把凭据写入配置。

**Global Administrator不是Azure所有权限的同义词。** Microsoft Entra Global Administrator不会自动获得Azure订阅Owner/Contributor、Azure RBAC角色分配、AKS Cluster User/Kubernetes RBAC或Storage Blob数据权限。实际登录账号需要：

- 在目标订阅/RG执行ARM What-if和Stage0资源部署，并在旧RG部署Stage1日志与告警。
- 获取旧AKS用户凭据，在旧namespace读取Deployment/Pod、执行PostgreSQL Pod的exec/cp、更新两个既定Deployment及可选入口来源设置。
- 对Stage0备份容器执行Blob列举、上传和下载。

#### 客户自建手工执行UAMI的角色

客户不需要创建名为`github-developer`的身份，也不需要复制某个现有环境的全部角色。可创建环境专用UAMI，例如`llmgw-manual-stage01-test`；它不需要GitHub Federated Credential。当前脚本最简单的授权路径为：

| 时间 | 角色/权限 | Scope | 用途 |
| --- | --- | --- | --- |
| S0-L02之前 | `Contributor` | 目标订阅 | 执行订阅级bootstrap、创建目标RG/Workspace，并管理Stage0–1 ARM、网络、旧AKS addon和告警资源；不包含角色分配 |
| S0-L03之前 | `Role Based Access Control Administrator`，带条件 | 新目标RG | 仅允许分配Storage Account Contributor、Storage Blob Data Owner和Storage Blob Data Contributor；接收者仅限批准的备份Owner用户和手工执行UAMI |
| 使用Entra/Azure RBAC的旧AKS | `Azure Kubernetes Service Cluster User Role` | 旧AKS资源 | 获取非admin用户kubeconfig |
| 使用Entra/Azure RBAC的旧AKS | `Azure Kubernetes Service RBAC Writer`或客户等效自定义角色 | 旧AKS的实际legacy namespace | Deployment/Pod读取、Pod exec/cp、Deployment更新及可选Service/Ingress/ConfigMap操作 |

目标RG在bootstrap之前尚不存在，所以可先只授订阅Contributor，成功执行S0-L02后暂停，由客户权限管理员在新目标RG分配带条件RBAC Administrator，再执行S0-L03。不要为了省略这一步授予订阅Owner或无条件User Access Administrator。

RBAC Administrator条件允许的三个RoleDefinition ID为：

```text
Storage Account Contributor:    17d1049b-9a84-46fb-8f53-869881c3d3ab
Storage Blob Data Owner:        b7e6dc6d-f1e8-4753-8033-0f276bb0955b
Storage Blob Data Contributor:  ba92f5b4-2d11-453d-a403-e96b0029c9fe
```

在Portal打开新目标RG → Access control (IAM) → Add role assignment → Role Based Access Control Administrator → Conditions，选择只允许上述三个角色，并把可接收主体限制为customer.json中的`backupOwnerPrincipalId`和手工执行UAMI Principal ID。若客户Portal或政策不支持该条件，由权限管理员在S0-L03窗口代为执行backup部署；不要改为无条件订阅级角色管理。

S0-L03的backup模板会自动完成三条授权：备份Owner用户获得Storage Account Contributor和Storage Blob Data Owner；配置`backupAutomationPrincipalId`时，手工执行UAMI在固定备份容器获得Storage Blob Data Contributor。因此后者不是创建UAMI时预先手工分配的角色，Storage尚未创建时也没有可用scope。

旧AKS若像传统集群一样未启用Entra/Azure RBAC，订阅Contributor通常可以取得本地cluster-user凭据，不另外复制新AKS角色；仍必须以S0-L05的真实读取和S0-L06的exec/cp结果为准。旧集群使用Kubernetes RBAC时，由集群管理员创建等效Role/RoleBinding，而不是给目标新AKS授权。

下列角色不属于Stage0–1手工执行UAMI的最低要求：`LLMGW Resource Lock Writer`、`Key Vault Secrets User`、目标新AKS的Namespace Bootstrapper/RBAC Reader/RBAC Writer，以及任何PostgreSQL数据库管理员角色。它们属于Stage4/5或新平台，不要因其他环境截图中存在就复制。

个人登录时，`backupOwnerPrincipalId`填写该用户在客户租户内的Object ID，Stage0 backup模板会为其建立备份管理和Blob数据授权。

如果条件访问阻止VM内的个人登录，只能在Runner保持Offline期间临时挂载手工执行UAMI。可以复用客户现有的deploy/runtime UAMI，但必须先确认它同时具备本节列出的ARM、旧AKS和备份Blob权限；缺少任一权限就使用独立的手工执行UAMI。不要使用数据库管理员UAMI、LiteLLM应用Workload Identity、AKS控制面/kubelet身份或Runner系统身份替代。在线Runner VM绝不能挂载高权限UAMI，否则Actions作业可能通过IMDS取得Token。

选择现有UAMI时分别核对：Client ID用于VM内`az login --identity`，Principal ID用于customer.json的`backupAutomationPrincipalId`和RBAC，完整资源ID用于`az vm identity assign/remove --identities`。三个值不能互换。

客户外部电脑执行：

```bash
az vm identity assign \
  --ids "REPLACE_RUNNER_VM_RESOURCE_ID" \
  --identities "REPLACE_MANUAL_EXECUTION_UAMI_RESOURCE_ID"
```

VM内执行：

```bash
az login --identity --client-id "REPLACE_MANUAL_EXECUTION_UAMI_CLIENT_ID"
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
```

使用UAMI时，在`parameters.backup`额外填写其Principal ID：

```json
"backupAutomationPrincipalId": "REPLACE_MANUAL_EXECUTION_UAMI_PRINCIPAL_ID"
```

完成后必须先退出Azure，再由客户外部电脑卸载UAMI：

```bash
az vm identity remove \
  --ids "REPLACE_RUNNER_VM_RESOURCE_ID" \
  --identities "REPLACE_MANUAL_EXECUTION_UAMI_RESOURCE_ID"
```

## 2. 网络前提

Runner VM已有Bastion和管理VNet，但Stage0创建的目标VNet仍需连接。migration的`execution-host-connectivity`创建两端Peering和Blob Private DNS链接；greenfield只创建Runner与新目标VNet的双向Peering，并强制`manageBlobDnsLink=false`，因为没有迁移备份Storage。

旧AKS为私有集群时，执行主机VNet还必须已有到旧AKS API的路由和DNS。旧AKS为受限公网API时，允许来源须包含该VM实际NAT出口。Azure管理员权限不能穿透网络，不得临时开放Storage公网来规避Private Endpoint。

## 3. 准备现有VM

客户外部电脑安装Azure CLI扩展，然后使用Runner部署输出中的`bastionSshCommand`：

```bash
az extension add --name bastion
az extension add --name ssh
REPLACE_BASTION_SSH_COMMAND_FROM_RUNNER_DEPLOYMENT_OUTPUT
```

VM需要Linux Bash、Git、Azure CLI、Bicep、Python 3.10或更高版本、Docker、`kubectl`、`kubelogin`、`curl`和`getent`：

```bash
command -v git az docker kubectl kubelogin curl getent
python3 --version
docker version --format '{{.Server.Version}}'
az bicep install
```

Runner模板默认只把`actions-runner`加入Docker组，工作盘父目录也属于该组。由VM管理员把手工登录用户加入本机组，然后退出当前VM会话；组成员关系不会在现有shell中自动生效：

```bash
sudo usermod -aG docker,actions-runner "$USER"
exit
```

回到客户外部电脑，重新执行第3节取得的实际`bastionSshCommand`：

```bash
REPLACE_BASTION_SSH_COMMAND_FROM_RUNNER_DEPLOYMENT_OUTPUT
```

重新进入VM后，先确认两个组均已生效，再访问Runner目录和Docker；任一命令失败就停止，不继续迁移：

```bash
id -nG | tr ' ' '\n' | grep -Fx actions-runner
id -nG | tr ' ' '\n' | grep -Fx docker
cd /srv/runner/agent
docker version --format '{{.Server.Version}}'
cd ~
```

在加密工作盘建立手工目录，不使用Actions工作目录：

```bash
sudo install -d -m 0700 -o "$USER" -g "$USER" /srv/runner/manual
cd /srv/runner/manual
```

## 4. 取得代码和配置

在VM执行：

```bash
git clone "REPLACE_CUSTOMER_REPOSITORY_URL" secure-litellm-on-azure
cd secure-litellm-on-azure
git fetch --tags --prune
git checkout main
git pull --ff-only origin main
git --no-pager show --no-patch --format='commit=%H%nsubject=%s' HEAD

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r local_execution/requirements.txt
```

创建本地配置：

```bash
cp local_execution/customer.example.json local_execution/customer.json
chmod 600 local_execution/customer.json
```

`local_execution/customer.json`已被Git忽略。配置不包含密码、Token、Master Key或Salt。

`customer.example.json`故意只包含Stage0–1可运行的基础字段。进入Stage2后，按[Stage2–9指南的分阶段模板说明](stage2-9-guide-zh.md#分阶段配置模板)使用`python -m local_execution.merge_config`合并当前阶段；不要手工覆盖整个`customer.json`。

greenfield使用`deploymentMode=greenfield`，省略`legacy`、`parameters.backup`、`parameters.legacy-logging`和`parameters.monitoring`，改用与Stage4 VNet同名的`parameters.network`，并在`parameters.bootstrap.logAnalyticsWorkspaceName`填写新目标Workspace名称。该名称来自客户新环境命名规范；部署后用Portal目标RG或`az monitor log-analytics workspace show`核对。`localExecution.postgresRestoreImage`可省略，`executionHost.manageBlobDnsLink`必须为false。greenfield不运行旧集群备份、恢复、Stage1监控/加固或`stage5-restore-target`。

### 4.1 配置字段

| 配置 | 含义与来源 |
| --- | --- |
| `azure`、`location`、`environment` | 客户租户、订阅和批准区域；与当前`az account show`一致 |
| `legacy` | 仅migration：旧RG、AKS、namespace和PostgreSQL PVC |
| `target.resourceGroup` | 新安全环境的独立目标RG；migration还要求与旧RG不同 |
| `parameters.bootstrap` | 新目标Workspace的创建/复用模式、留存期；greenfield还在这里显式填写Workspace名称 |
| `parameters.network` | 仅greenfield：新目标VNet地址空间和Private Endpoint子网；名称必须与后续Stage4一致 |
| `parameters.backup` | 备份Workspace、Owner、VNet和PE子网；UAMI路径可增加`backupAutomationPrincipalId` |
| `parameters.hub` | 可选：区域Hub防火墙的独立RG、Hub VNet与`AzureFirewallSubnet`地址、诊断Workspace；只在选择hub出口时加入，见S0-H01 |
| `parameters.monitoring` | 旧环境Log Analytics Workspace名称 |
| `parameters.legacy-logging` | 创建或复用旧日志Workspace的模式 |
| `legacyAccess` | 可选来源限制；只有真实CIDR和影响已批准时才加入 |
| `localExecution.postgresRestoreImage` | 与旧PG大版本匹配的完整`postgres@sha256:...` |
| `localExecution.executionHost.virtualNetworkId` | Runner部署输出`managementVnetId`；复用子网模式从`runnerSubnetId`去掉`/subnets/<名称>` |
| `localExecution.authentication` | 默认复用现有`az login`；Stage2–9可按动作选择UAMI Client ID |
| `localExecution.features` | `entraMode`决定启用或延期Entra Stage7；Stage6显式选择`--option native-auth`时可保留deferred并跳过Stage7；`allowTrafficRelease`默认false |

### 4.2 旧日志Workspace怎样填写

配置中没有`legacy-monitoring`字段。旧环境日志由下面两个字段共同决定：

| 字段 | 允许值 | 作用 |
| --- | --- | --- |
| `parameters.legacy-logging.workspaceMode` | 只能是`create`或`existing` | 决定`legacy-logging`步骤创建Workspace，还是只引用已有Workspace |
| `parameters.monitoring.logAnalyticsWorkspaceName` | 旧资源组内的真实Workspace名称 | 两种模式都从这里取得Workspace名称；不是Workspace资源ID或新目标Workspace名称 |

选择规则：

- `create`：仅在确认`legacy.resourceGroup`内没有该名称的Workspace，并且客户批准新建时使用。`legacy-logging`会在配置的`location`创建PerGB2018 Workspace，并设置30天留存、禁用本地认证和仅按资源权限访问。若同名Workspace已经存在，Azure部署可能更新其属性，因此不能用`create`试探资源是否存在。
- `existing`：旧资源组内已经有要复用的Workspace时使用。该步骤不会创建或修改Workspace；当前实现只支持与旧AKS位于同一个`legacy.resourceGroup`的Workspace。`legacy-logging`成功本身不证明资源存在，后续步骤会读取它，所以执行前必须先核验。

先从Portal打开 **旧资源组 → Log Analytics workspaces**，记录实际名称和区域；或执行：

```bash
az monitor log-analytics workspace list \
  --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_LEGACY_RESOURCE_GROUP" \
  --query '[].{name:name,location:location,state:provisioningState}' \
  --output table
```

已有Workspace时配置为：

```json
"legacy-logging": {
  "workspaceMode": "existing"
},
"monitoring": {
  "logAnalyticsWorkspaceName": "REPLACE_EXISTING_LEGACY_WORKSPACE_NAME"
}
```

确认没有Workspace并获准新建时配置为：

```json
"legacy-logging": {
  "workspaceMode": "create"
},
"monitoring": {
  "logAnalyticsWorkspaceName": "REPLACE_NEW_LEGACY_WORKSPACE_NAME"
}
```

这两个名称都描述旧环境监控目的地。migration的Stage0备份及后续新网关使用`parameters.backup.logAnalyticsWorkspaceName`；greenfield使用`parameters.bootstrap.logAnalyticsWorkspaceName`，Stage3合并时会复用该值。不要把旧监控Workspace与新目标Workspace因名称相似而互换。

恢复镜像必须使用完整RepoDigest：

```bash
IMAGE="postgres:REPLACE_MATCHING_MAJOR_AND_PATCH"
docker pull --platform linux/amd64 "$IMAGE"
docker image inspect "$IMAGE" --format '{{range .RepoDigests}}{{println .}}{{end}}'
docker run --rm --network none --read-only --entrypoint pg_restore \
  "REPLACE_POSTGRES_REPO_AT_SHA256" --version
```

## 5. 命令格式

所有命令从仓库根目录执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step REPLACE_STEP
```

Stage0–1变更步骤会读取配置、记录Git提交、生成实时预览并立即执行；Stage2–9的两阶段命令见后续指南。结果写入`temp/local-stage09/`。出现Delete、Unsupported、越界资源、配置漂移或底层校验失败时仍会停止，这些是目标安全校验，不是GitHub审批门禁。

执行器会在成功或失败后删除输出目录中的`kubeconfig`、`database.dump`和`downloaded.dump`；Azure Blob正式备份不会删除。

## 6. Stage0顺序

以下S0-L03、S0-L05至S0-L07只适用于migration。greenfield在S0-L02后执行`network`和`execution-host-connectivity`，然后直接进入Stage2；不访问任何旧AKS或旧数据库。

### S0-L01 配置检查

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step config-check
```

### S0-L02 Bootstrap

订阅级ARM deployment名称会绑定目标RG和区域的稳定短哈希；同一个environment并行准备不同目标区域时不会复用旧区域的deployment名称，也不会删除或覆盖旧bootstrap回执。

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step bootstrap
```

确认新RG和目标Log Analytics Workspace成功，旧RG未修改。

### S0-G03 Greenfield目标网络与Runner Peering

仅`deploymentMode=greenfield`执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step network
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step execution-host-connectivity
```

确认新目标VNet已创建，Runner与目标VNet双向Peering为Connected；不得运行`backup`、`connectivity-check`或`backup-restore`。

### S0-L03 备份基础设施

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step backup
```

确认私有Storage、容器、备份VNet、PE/DNS及备份身份授权。此步骤尚未导出数据库。

### S0-L04 执行主机网络

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step execution-host-connectivity
```

只应创建或复用执行主机VNet与备份VNet的Peering和Blob DNS链接。

### S0-H01 可选：区域Hub防火墙

只在Stage2网络决策选择**hub出口**（同区域多个环境共用一个Hub Azure Firewall）时执行；默认dedicated出口（防火墙建在目标VNet内）跳过本步，`customer.json`也不出现`parameters.hub`。migration和greenfield都适用。Hub属于与具体环境无关的基础网络前提，因此编号归Stage0；实际在Stage3合并之后、`stage4-platform`之前执行。

配置不手工拼写：Stage3合并时加`--option hub-egress`（见[Stage2–9指南](stage2-9-guide-zh.md#分阶段配置模板)），一次写入`parameters.hub`、`stage4Network.egress.mode=hub`并删除`stage4Network.firewallSubnetPrefix`。合并后的`parameters.hub`形如下例；片段中的CIDR是参考规划，客户批准的地址不同时，与Stage3其他CIDR一样在合并后改为批准值：

```json
"hub": {
  "resourceGroupName": "REPLACE_HUB_RESOURCE_GROUP",
  "virtualNetworkName": "REPLACE_HUB_VNET",
  "virtualNetworkAddressPrefix": "10.40.0.0/24",
  "firewallSubnetPrefix": "10.40.0.0/26",
  "reservedPrefixes": {
    "AzureFirewallManagementSubnet": "10.40.0.64/26",
    "AzureBastionSubnet": "10.40.0.128/26",
    "GatewaySubnet": "10.40.0.192/27"
  },
  "logAnalyticsWorkspaceName": "REPLACE_TARGET_LOG_WORKSPACE"
}
```

`reservedPrefixes`只做重叠校验，不创建子网。Workspace缺省在目标RG（S0-L02 bootstrap已创建）；放在其他RG时加`logAnalyticsWorkspaceResourceGroupName`。执行身份需在订阅级创建RG（如订阅Contributor，或客户批准的等效自定义角色），并对Workspace有Log Analytics Contributor或等效权限。

与其他Stage0步骤不同，本步骤使用两阶段plan/execute，不直接apply：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step stage0-hub --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step stage0-hub --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE0_HUB_PLAN_SHA256"
```

plan是订阅级What-if，只允许改动Hub RG本身及其中资源；它还只读核对Hub地址不与执行主机VNet重叠。成功后核对回执输出，Stage4 platform会从这里读取防火墙私网IP，不手工填写：

```bash
az deployment sub list --subscription "REPLACE_SUBSCRIPTION_ID" \
  --query "[?starts_with(name, 'llmgw-REPLACE_ENVIRONMENT-s0-hub-')].{name:name,state:properties.provisioningState,hub:properties.outputs.hub.value}" \
  --output json
```

通过条件：`state=Succeeded`；`hub.firewallPrivateIpAddress`在`firewallSubnetPrefix`内；`firewallPolicyName=afwp-<Hub VNet名>`；`firewallPublicIpAddress`即今后所有使用该Hub的环境的共同出口IP。之后修改`parameters.hub`须重新plan/execute本步骤，并重新plan已接入的Stage4 platform。

### S0-L05 连通性检查

旧AKS必须处于`Running`。停止的公网AKS可能暂时没有API DNS记录，此时会表现为`no such host`，不是Runner DNS配置或数据库错误。先查询：

```bash
az aks show \
  --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_LEGACY_RESOURCE_GROUP" \
  --name "REPLACE_LEGACY_AKS_NAME" \
  --query '{state:provisioningState,power:powerState.code,fqdn:fqdn}' \
  --output json
```

返回`power=Stopped`时，在批准的计费和维护窗口启动旧AKS：

```bash
az aks start \
  --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_LEGACY_RESOURCE_GROUP" \
  --name "REPLACE_LEGACY_AKS_NAME"
```

`az aks start`成功返回后，再确认状态和API域名解析；解析仍为空时不要运行备份：

```bash
AKS_FQDN="$(az aks show \
  --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_LEGACY_RESOURCE_GROUP" \
  --name "REPLACE_LEGACY_AKS_NAME" \
  --query fqdn --output tsv)"

az aks show \
  --subscription "REPLACE_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_LEGACY_RESOURCE_GROUP" \
  --name "REPLACE_LEGACY_AKS_NAME" \
  --query powerState.code --output tsv
getent ahostsv4 "$AKS_FQDN"
```

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step connectivity-check
```

检查旧AKS运行状态和Deployment读取、备份PE DNS/TLS及Blob列举。结果在`local-readiness.json`；Pod exec/cp和Blob写入由下一步真实验证。

### S0-L06 备份和隔离恢复

在批准的低峰窗口执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step backup-restore
```

成功条件：旧Pod真实`pg_dump`、无网络临时容器完整`pg_restore`、存在公共业务表、私有Blob上传/回读及SHA256一致。将`acceptance-report.json`中的备份引用、哈希、大小、表数量和耗时记录到客户受控位置，不记录dump正文或凭据。

若看到`database system is shutting down`，说明恢复镜像的临时初始化服务器被过早判为就绪。更新到包含最终服务器就绪检查的最新`main`后重新执行；不要在旧提交上反复碰运气。修复后的脚本要求容器PID 1已经切换为`postgres`且`pg_isready`成功，才开始恢复。失败时会在本次输出目录保留`restore-container-state.json`和`restore-container.log`，先检查`OOMKilled`、退出码和末尾日志；这些文件可能包含客户对象名称，只保存在受控位置。

该错误发生在Blob上传步骤之前，本次失败不会生成正式`backupBlob`。重新执行会重新导出旧库并创建新的临时恢复容器。

### S0-L07 人工核验

实际核对：

- 旧环境盘点完整。
- 备份可完整恢复并回读一致。
- Master Key与Salt可从受控材料恢复。
- 旧入口的实际客户端协议基线通过。

未完成上述项目不进入Stage1。

## 7. Stage1顺序

开始前导出**变更前**两个Deployment配置，确认告警接收人、维护窗口和回退负责人。

### S1-L01 旧日志Workspace

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-logging
```

### S1-L02 Container Insights接入

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step monitoring-onboard
```

确认旧namespace真实日志到达，而不只是addon显示Enabled。

### S1-L03 告警规则

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step monitoring
```

触发批准的测试条件并确认通知真实送达。需要把Action Group接入飞书群时，按独立的[Azure Monitor告警转发飞书操作指南](feishu-alert-notification-zh.md)配置Logic App；不要把飞书Webhook写入customer.json。

### S1-L04 Deployment加固

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-hardening
```

脚本为既定PostgreSQL和LiteLLM Deployment补健康探针，并将PG策略设为Recreate。执行后检查rollout、Pod、PVC、旧入口和实际客户端。

### S1-L05 可选来源限制

仅配置了真实`legacyAccess`时执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-access-restrict
```

需要回退时：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step legacy-access-restore
```

分别验证批准来源成功、未批准来源拒绝。

### S1-L06 人工核验

确认旧服务健康、告警真实送达、回退快照可用；启用来源限制时另核对正反向来源测试。Stage1完成后继续保留旧集群、PVC、备份及密钥材料。

完成后转到[Stage2–9本地手工部署指南](stage2-9-guide-zh.md)。不要直接跳到Stage3资源部署；Stage2必须先冻结网络、身份、数据库、正文审计和协议决策。

## 8. 失败与重跑

- ARM步骤失败可能已部分创建资源，先看Azure Deployment operations，再重跑同一步。
- runtime失败先查实际Deployment、Pod和checkpoint，不假定自动回滚。
- `backup-restore`重跑会生成新Blob，不自动删除旧备份。
- 修改代码或配置后，从受影响步骤重新执行并记录新提交和配置哈希。
- `temp/`仍含明文客户元数据，审核后按客户保留策略清理。
- 本地退出0不是GitHub Actions验收记录；以后恢复Actions时按其流程重新建立对应证据。

## 9. 恢复Runner服务

先清除VM内Azure登录：

```bash
az logout
az account clear
```

删除`local_execution/customer.json`和不再需要的手工输出。若挂载过UAMI，从客户外部电脑卸载并确认VM不再关联该身份。确认无Azure凭据、高权限UAMI、dump、kubeconfig或手工进程后，恢复服务：

```bash
sudo bash -c 'cd /srv/runner/agent && ./svc.sh start'
sudo bash -c 'cd /srv/runner/agent && ./svc.sh status'
```

最后在GitHub确认Runner重新Idle。不得让手工执行与Actions作业同时使用这台VM。