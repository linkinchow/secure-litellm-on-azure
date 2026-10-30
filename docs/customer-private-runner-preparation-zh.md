# 客户私网 Runner 准备说明

> 核对日期：2026-10-09
>
> 用途：为LiteLLM从零部署、现有环境安全增强及并行迁移准备GitHub Actions执行机；本地执行的身份与计划流程另按本地指南。
>
> 方式：一次性人工准备、注册并复用。不要求自动建机、JIT调度、镜像工厂或自动回收平台。

## 1. 先看结论

**准备一台独立的 Ubuntu 24.04 LTS、x64 Linux VM，能访问 GitHub 和目标环境私网（migration另需旧环境），安装 Docker 及下文工具，注册为客户仓库的 self-hosted runner。** 可以由实施人员协助完成。

当前已运行的是West US 3 greenfield native dev/test网关，Admin原生登录与virtual key的Codex Responses推理已有用户确认；不因此证明GitHub OIDC作业、全部runner权限或生产迁移已验收。greenfield不准备旧AKS/备份路径，不执行Stage1；native-auth不执行Stage7 Graph/代理部署，但仍使用Azure身份、AKS WI/OIDC和私有数据服务。路径边界见[当前部署状态](customer-deployment-workflows-zh.md#当前运行状态与路径边界2026-10-09)，本地操作另见[Stage2–9指南](../local_execution/stage2-9-guide-zh.md)。

没有管理网络时，使用下文Bicep独立创建管理VNet及Runner；已有获准管理子网或专用Linux执行机时也可复用。通过批准的路由和DNS连接新旧环境，不需要GPU，不要求运行在AKS内，也不必安装Kubernetes集群或Docker Desktop。

runner是执行部署、备份恢复和验证任务的机器，**不承载LiteLLM业务流量**。任务结束后停止runner不会停止网关，但需要再次运行workflow时必须提前启动。迁移中不要自动关机或重启。

客户在Windows电脑上通过浏览器操作Actions即可；Windows电脑不必改成Linux，也不需要为了执行workflow安装完整工具链。

### 1.1 用Bicep一次性建机和安装工具

使用[订阅级部署模板](../infra/private-runner/deploy.bicep)，不必逐个创建网络、VM、磁盘、网卡或安装工具。它使用[管理网络模块](../infra/private-runner/management-network.bicep)、[标准Ubuntu VM模块](../infra/private-runner/standard-vm.bicep)和[首启安装脚本](../infra/private-runner/bootstrap.sh)，不依赖Gallery镜像、新AKS或旧AKS的节点子网，也不改变原有可选自动Runner模板。

| 网络模式 | 如何选择 | 新建内容 |
| --- | --- | --- |
| 新建管理网络，默认 | 不传`subnetResourceId` | 独立VNet、Runner子网、NAT Gateway及出站公网IP；默认另建Bastion Standard、Bastion子网和独立公网IP |
| 复用现有子网 | 传入`subnetResourceId`及`sshSourceCidr` | 只建VM、磁盘、NIC和VM级NSG；不修改原网络，不创建NAT/Bastion，也不借用AKS出站 |

新建默认地址为`10.50.0.0/24`：`snet-runner=10.50.0.0/26`、`AzureBastionSubnet=10.50.0.64/26`。可用`managementAddressPrefix`换成已批准的RFC1918 IPv4 /24，两个子网自动计算。部署前必须检查新旧VNet、计划中的网络、公司VPN和Docker网段没有重叠；模板不扫描公司网络，也不代表该默认地址适用于所有客户。

**自动完成：** 新建独立资源组、Ubuntu 24.04 x64 VM、私有网卡和NSG；启用Trusted Launch、Secure Boot、vTPM和主机加密；配置128 GiB系统盘、256 GiB工作盘；安装Docker、Azure CLI/Bicep、kubectl 1.35系列、Azure kubelogin、Node 24/npm、GitHub CLI、PG16客户端、skopeo、Syft、Trivy、Cosign及GitHub Runner程序。系统Python保留Ubuntu版本，workflow用setup-python准备3.13，固定Python依赖仍由workflow安装。

**网络边界：** VM始终没有公网IP，公网22不开放。新建模式的NAT公网IP仅供出站，Bastion公网入口通过Azure身份授权管理连接，SSH密钥仍用于VM登录；VM级NSG只接受Bastion子网的SSH，其余入站拒绝。NAT不是防火墙，也不提供FQDN白名单；需要企业出站过滤时使用获准的现有管理网络或另行配置防火墙，不能把NAT建成说成出站策略已验收。

**保留的人工步骤：** 批准地址范围、提供SSH公钥，最后交互注册GitHub并配置OIDC权限。模板不创建Peering、DNS转发或业务身份，不修改新旧业务资源。复用子网必须已具备第3节的路由、DNS和显式出站。新建时可通过Bastion从当前开发机登录，不必先建跨订阅Peering；Bastion原生客户端不支持普通Cloud Shell。

**费用：** 默认会创建VM、两块Premium磁盘、NAT Gateway、两个Standard公网IP和Bastion Standard，均需按实际区域/用量计费。Bastion/NAT不会随VM关机停止收费。已有VPN等私网管理路径时可选`createBastion=false`并指定`sshSourceCidr`；该选项不替你创建VPN。未提供SSH来源且不建Bastion时默认拒绝所有入站，必须先规划其他获准管理路径。

#### 执行部署

在已登录Azure CLI的Bash终端、仓库根目录执行。可用Cloud Shell创建资源，原生Bastion连接需另用本机终端。管理员需要目标订阅创建资源组/部署的权限及新资源组的资源写权限；复用模式另需现有子网的读取和`join/action`权限。不要求给VM分配Owner或任何业务Managed Identity。

先检查订阅的`EncryptionAtHost`状态：`az feature show --subscription "$SUBSCRIPTION_ID" --namespace Microsoft.Compute --name EncryptionAtHost --query properties.state -o tsv`。未注册时由获准管理员执行下面的注册命令；等查询结果为`Registered`后才部署，不关闭模板里的加密来绕过失败。注册不会给旧VM自动启用加密。

```bash
az feature register --subscription "$SUBSCRIPTION_ID" \
  --namespace Microsoft.Compute --name EncryptionAtHost
```

确认所选区域的默认`Standard_D4s_v3`或选用的`Standard_D4s_v5`/`Standard_D8s_v5`没有区域级订阅限制，且有足够vCPU配额。`az vm list-skus --all`返回的`restrictions`比仅列VM大小更有意义；有配额不等于SKU可用。本模板未指定可用区，仍需以实际部署时的容量、策略和授权校验为准。

**没有管理网络：** 下面只需填写订阅、获准区域和公钥路径；网段及资源名有默认值，Bastion会自动成为SSH来源。不需要旧或新AKS子网ID。

```bash
az deployment sub create \
  --subscription "REPLACE_SUBSCRIPTION_ID" \
  --name llmgw-runner-test \
  --location "REPLACE_APPROVED_REGION" \
  --template-file infra/private-runner/deploy.bicep \
  --parameters \
    sshPublicKey="$(cat ~/.ssh/id_ed25519.pub)" \
    managementAddressPrefix="10.50.0.0/24" \
    createBastion=true \
  --confirm-with-what-if \
  --query properties.outputs
```

`--confirm-with-what-if`先预览并请求确认，确认后才创建收费资源。只预览时将`create`改为`what-if`，去掉`--confirm-with-what-if`和`--query properties.outputs`。SSH公钥支持ED25519或RSA至少2048位；没有专用密钥时先在自己的受控终端交互生成并保管，不能覆盖已有密钥或把私钥/口令提供给聊天、GitHub Secret或Bicep。

**已有管理子网：** 同一命令的`--parameters`改为以下内容，区域使用该VNet所在区域。`sshSourceCidr`填实际VPN管理网段或已有AzureBastionSubnet CIDR，不填`0.0.0.0/0`。

```bash
--parameters \
  subnetResourceId="REPLACE_EXISTING_MANAGEMENT_SUBNET_RESOURCE_ID" \
  sshPublicKey="$(cat ~/.ssh/id_ed25519.pub)" \
  sshSourceCidr="REPLACE_APPROVED_MANAGEMENT_CIDR"
```

默认资源名称为`rg-llmgw-runner-test-<稳定后缀>`及`vm-llmgw-runner-test-<稳定后缀>`，其余资源跟随VM命名；后缀由订阅、子网ID（新建为空）和环境生成，复用模式保留原有命名。同样输入指向同一套资源，不每次创建随机机器。请保持同一网络模式、区域和环境；新建模式每个订阅/环境对应一套管理网络，不能通过改区域或地址范围当作另一套部署。`--name`只是订阅部署记录名。

| 可选参数 | 默认 | 修改方式 |
| --- | --- | --- |
| `environmentName` | `test` | 在`--parameters`后增加`environmentName=prod`，注册标签随之变为`llmgw-prod-private` |
| `subnetResourceId` | 空，创建网络 | 非空时复用，不创建或改变任何VNet/NAT/Bastion |
| `managementAddressPrefix` | `10.50.0.0/24` | 仅新建模式使用，替换为不重叠的私有IPv4 /24 |
| `createBastion` | 新建为true，复用为false | 新建可关闭，但须自行提供私网管理连接；复用模式即使传true也不创建Bastion |
| `sshSourceCidr` | 空 | 自建Bastion模式自动选Bastion子网；其他模式为空则拒绝SSH |
| `virtualMachineSize` | `Standard_D4s_v3`，4 vCPU/16 GiB | 可选`Standard_D4s_v5`或`Standard_D8s_v5`；客户批准降本时可选`Standard_D2s_v5`（2 vCPU/8 GiB，镜像构建、扫描和大库恢复更慢，不同时跑多个重任务）；需区域可用、订阅配额并支持模板中的安全能力 |
| `osDiskSizeGiB` | 128 | 只能按批准容量增加，不缩小已有磁盘 |
| `dataDiskSizeGiB` | 256 | 根据数据库和镜像空间增大；已建盘扩容后仍须单独扩展文件系统 |
| `ubuntuImageVersion` | `latest` | 可指定已批准的Canonical版本；此入口是标准镜像初始化，不是不可变工具链镜像工厂 |

SSH管理员固定为`runneradmin`，Runner独立使用无sudo权限的`actions-runner`账户；Docker组仍具有接近root的能力，不能运行不可信代码。

复用时要求子网与VM同订阅、同区域、可连接普通VM，且订阅支持所选SKU和主机加密。禁止向AKS托管节点资源组或不兼容的委派子网部署；不要通过关闭加密或给VM新增公网入口解决失败。新建网络也只管理本模板的子网，扩展地址/子网或移除Bastion须先审查现有资源，不能把切换布尔值当作已完成退役。新增条件为false不会自动删除以前创建的Bastion或公网IP。

#### 确认安装并注册

部署输出提供`resourceGroupName`、`virtualMachineName`、`virtualMachineId`、`networkMode`、`managementVnetId`、`runnerSubnetId`、`outboundPublicIp`、`bastionName`、`privateIpAddress`、`sshCommand`、`bastionSshCommand`、`runnerLabels`、`bootstrapCheck`及`registrationCommand`。

新建Bastion后，在当前已登录Azure CLI的开发机运行输出的`bastionSshCommand`，即可用本机SSH私钥登录；命令默认引用`~/.ssh/id_ed25519`，使用其他公钥时改成匹配的私钥路径。不把私钥传给Bicep或粘贴进GitHub。发起连接的电脑需安装Azure CLI的`bastion`和`ssh`两个扩展，操作者需拥有VM、NIC和Bastion的读取权限；首次SSH须核对主机指纹，不关闭host key检查。缺少ssh扩展时在该电脑运行`az extension add --name ssh`，不必重建VM。

已有VPN等路由时可运行`sshCommand`直接访问私网IP，没有该路由则不能直接SSH。需要文件传输时使用[Bastion原生隧道](https://learn.microsoft.com/en-us/azure/bastion/connect-vm-native-client-linux#connect-to-a-vm---tunnel-command)；这与开启公网22不同。进入VM后执行：

```bash
sudo cloud-init status --wait --long
sudo test -f /var/lib/llmgw-runner/bootstrap-complete
sudo cat /usr/local/share/llmgw-runner/tool-versions.txt
sudo /usr/local/sbin/llmgw-runner-register
```

最后一条命令交互询问仓库URL和GitHub临时注册令牌。先在仓库**Settings → Actions → Runners → New self-hosted runner**取得令牌，只在VM的终端提示中输入；不要使用`--token`写入命令历史、Azure Run Command或模板参数。注册器自动使用VM名称、`llmgw-test-private`标签和数据盘工作目录，然后安装并启动systemd服务，不需要再执行下载/解包命令。

模板不会验证GitHub分支保护。注册前必须先完成受保护默认分支、Environment和可信作业限制；禁止公共PR使用该私网runner。Azure部署输出中的`githubRegistered=false`是模板声明，不是实时GitHub状态；**VM部署成功不代表cloud-init成功、Runner已注册或业务权限就绪**。

GitHub中确认Idle后，将输出`runnerLabels`的数组值填入Repository Variable `MIGRATION_PRIVATE_RUNNER_LABELS`；再按第7节运行`Customer private runner checks`。Environment Secrets、各OIDC身份、AKS/PG/Graph权限仍按[部署指南](customer-deployment-workflows-zh.md)配置，不能通过给VM挂管理员身份替代。

工作盘挂载为`/srv/runner`，Docker数据、Runner程序、作业和tool cache均在其下；Docker启动依赖该挂载，不退回系统盘。首启只格式化全新无分区/无文件系统的数据盘，不接管已有未知文件系统。安装日志在`/var/log/llmgw-runner-bootstrap.log`，仅root可读。失败时先在受控终端查看cloud-init和该日志，修复出站或软件源后可运行`sudo /usr/local/sbin/llmgw-runner-bootstrap`继续；完成标记存在时不会重装。**重跑Bicep不会重新执行cloud-init**，也不会更新已运行机器上的工具。

直接下载的6个发行包固定版本并校验SHA256；apt组件使用各项目软件源的当前候选版本，实际版本写入上面的清单，不宣称完整可复现。GitHub Runner保留正常自动更新；kubectl固定在1.35仓库且安装后hold，集群升级时单独审核更新。软件源需另放行`download.docker.com`、`packages.microsoft.com`、`deb.nodesource.com`、`cli.github.com`、`pkgs.k8s.io`及其CDN，例如`prod-cdn.packages.k8s.io`；不能仅允许GitHub主页。

2026-09-13已通过Bicep编译、脚本及文档回归，以及一次性Ubuntu 24.04容器的真实工具安装；容器检查替代了挂盘、systemd和Docker daemon步骤，因此不证明云上挂载、服务启动、GitHub注册或客户网络已经通过。删除VM时两块磁盘保留，**删除整个资源组仍会删除其中磁盘**；需先确认备份及留存策略，不把删除资源组作为安装失败重试方式。

#### 新旧集群的接通顺序

以下旧集群与备份步骤仅适用于migration；greenfield先准备管理网络，再按Stage0 network及Stage4连接目标私网，不为填表虚构旧集群或运行备份检查。

1. 先创建独立管理网络和Runner，完成GitHub注册；不依赖新集群存在。旧AKS未指定自建VNet时通常仍有自动创建的节点VNet，不将Runner塞入该节点子网。
2. 旧Kubernetes API是公网模式时，从Runner访问获准API端点并验证身份/RBAC；有来源白名单则批准NAT实际出口。旧PG通过Pod内命令备份，不必给PG新增公网入口。私有API则先具备对应私网连接。
3. Stage0建立私有备份网络后，即需连接管理VNet和备份网络，配置相应Private DNS Zone关联或公司DNS转发，再运行备份上传；不是等新AKS创建才处理网络。
4. Stage0备份资源创建后，可按[主手册0-A2](customer-migration-guide-zh.md#0-a2-由workflow建立runner备份连接)运行`runner-connectivity`组件的plan/deploy，在同订阅内建立双向Peering和Blob Private DNS链接，再运行Runner检查的`check_backup=true`。已有Hub/企业DNS按复用开关保留；目标AKS/PG/ACR等仍需各自获批DNS和端口。跨区域Peering需核算流量费用；Peering本身不自动传播Private DNS。
5. 使用相同或替换后的合格Runner继续部署维护。它不承载LiteLLM流量，旧集群停用不等于Runner退役；模板输出`targetPrivateConnectivityVerified=false`，只创建网络不能替代逐项连通测试。

## 2. 机器要求

下列为迁移执行机的准备建议，不是GitHub软件的最低配置，也不是已测容量承诺。

| 项目 | 建议 |
| --- | --- |
| 操作系统 | Ubuntu 24.04 LTS，正常安装安全补丁，使用systemd管理runner服务 |
| 架构 | x86-64 / amd64，GitHub标签为`X64`；本次不采用ARM或Alpine宿主机 |
| CPU/内存 | 起步4 vCPU、16 GiB；经常同时处理镜像构建和较大数据库时建议8 vCPU、32 GiB |
| Azure规格示例 | 默认`Standard_D4s_v3`；区域允许时可用`Standard_D4s_v5`，扩大时可用`Standard_D8s_v5`；客户批准的最低成本选项为`Standard_D2s_v5`。以区域可用性、配额及实测为准 |
| 系统盘 | 建议128 GiB；安装OS、基础工具和runner程序 |
| 工作数据盘 | 建议从256 GiB SSD起步，承载Actions工作目录、Docker数据及临时备份，按下文核算后扩容 |
| 磁盘保护 | 使用加密持久磁盘；不要把客户备份放在临时盘、无加密共享目录或普通artifact中 |
| 网络接口 | 私有IP即可，无须公网IP；有明确的出站路由和DNS |
| 运行方式 | 一台机器注册一个runner实例，一次执行一个迁移作业；不与其他客户/不可信项目共享 |
| 可用性 | 普通按需VM，不使用Spot、休眠笔记本或可能被随时回收的主机 |

**空间核算：** 不能只看压缩后的数据库dump大小。当前备份演练会在runner上保存原始及回读备份，并在Docker中实际恢复数据库；镜像下载、解包、构建缓存也占空间。

建议在软件和基础镜像已安装后，为迁移另留：**两份dump + 一份展开后的数据库及索引 + 恢复WAL/临时空间 + 至少50 GiB工作余量**。这是准备下限估算，实际峰值通过演练测量并另留安全余量；256 GiB数据盘不保证适合任意客户库。先读取数据库大小、磁盘空闲量及备份样本，不按用户数量换算。

Docker默认数据目录通常在系统盘。即使挂载了数据盘，也要由管理员确认Docker data-root和Actions工作目录实际位于规划的磁盘，否则系统盘仍可能写满。

## 3. 放在哪里、开哪些网络

### 3.1 私网放置

- 放在迁移开始前就可用的管理网络，不依赖“尚未创建的新AKS里先启动一个runner”。新环境建好后再接通其私网路由和DNS。
- 通过VNet Peering、已有Hub、VPN或专线等批准路径访问新旧环境。Peering本身不保证跨Hub转发、DNS或NSG放行，要逐项核验。
- 不为runner临时开放AKS、PostgreSQL、Key Vault或ACR公网访问。
- GitHub主动连接runner的入站端口**不需要开放**。runner通过出站HTTPS领取任务；管理登录可走Bastion/VPN/批准的私网SSH，不开公网22端口。
- runner重启后，DNS、路由、挂载盘、Docker及runner服务应自动恢复；时钟须正常同步，否则OIDC、TLS和计划有效期校验可能失败。

### 3.2 目标与端口

| 从runner访问的目标 | 端口 | 用途与注意事项 |
| --- | --- | --- |
| GitHub Actions、GitHub API及下载服务 | TCP 443出站 | 注册、领任务、下载Actions、OIDC、日志/artifact上传与读取 |
| Azure Resource Manager、Microsoft Entra ID、Microsoft Graph | TCP 443出站 | OIDC换取Azure身份及资源操作；Graph仅用于已批准的Entra目录动作，native-auth延期用户SSO不等于取消Azure身份 |
| 新旧AKS API端点 | TCP 443 | `kubectl`、获取工作负载、发布、exec/cp/port-forward；旧集群若有API来源限制须允许runner的实际出口 |
| 批准的Key Vault、ACR及其数据端点、备份Blob | TCP 443 | 通过各自私有端点访问；ACR登录成功不等于其镜像层下载端点可达 |
| 新PostgreSQL Flexible Server | TCP 5432 | TLS/Entra连接、恢复、角色和schema操作 |
| 所选Redis服务 | 以部署输出为准，当前托管配置为TCP 10000/TLS | 数据连接验证；不要按普通Redis默认6379直接放行 |
| API/Admin入口域名 | TCP 443 | Stage4/6通过显式私网地址与SNI验证两个origin；Stage9后正常DNS均指向各自Front Door Premium endpoint。Admin WAF按实际批准公网IPv4 `/32`及IPv6 `/128`拒绝其他来源，白名单内仍须内层登录；不推断公司CIDR |
| 模型服务端点 | TCP 443，按验证需要 | 走批准私网路径；runner成功不能替代Pod Workload Identity验证 |
| 企业DNS、时间同步服务 | DNS UDP/TCP 53；NTP通常UDP 123 | 仅访问批准的解析器和时间源 |

旧PG目前通过AKS API执行Pod内`pg_dump`和复制备份，不要求为了runner给旧PG新建公网或私网数据库入口。也不需要把Kubernetes Service CIDR全部暴露给runner；后端检查可走现有受限port-forward。

Docker测试创建的本机bridge网段也不能与新旧VNet、VPN、Pod或Service网段冲突。由网络管理员规划Docker地址池，不能靠删除企业路由解决容器网段冲突。

### 3.3 HTTPS出站域名

以下用于网络人员准备规则，**不是完整、永久不变的放行清单**。以固定workflow实际下载源、GitHub官方列表和失败日志核对，使用受控FQDN出口；不要以允许全部互联网作为验收结果。

| 类别 | 主要域名或范围 |
| --- | --- |
| GitHub运行与API | `github.com`、`api.github.com`、`*.actions.githubusercontent.com`、`codeload.github.com` |
| GitHub程序/Action下载 | `objects.githubusercontent.com`、`objects-origin.githubusercontent.com`、`github-releases.githubusercontent.com`、`release-assets.githubusercontent.com`、`raw.githubusercontent.com`；部分资产还经`github-registry-files.githubusercontent.com` |
| Actions结果与artifact | `results-receiver.actions.githubusercontent.com`，以及GitHub官方要求的`*.blob.core.windows.net`；这是GitHub artifact服务出口，不是将客户备份Storage开放公网 |
| Azure与Entra | `management.azure.com`、`login.microsoftonline.com`、`graph.microsoft.com`及所选云环境实际认证端点 |
| Python/Node依赖 | `pypi.org`、`files.pythonhosted.org`、`registry.npmjs.org`；按需使用经过验证的企业镜像源 |
| 容器源镜像 | `docker.litellm.ai`、`mcr.microsoft.com`及其数据端点；Docker Hub的认证、registry和镜像CDN端点；按需`ghcr.io` |
| 扫描、签名 | Trivy漏洞库实际镜像源及Sigstore服务，例如`fulcio.sigstore.dev`、`rekor.sigstore.dev`、`tuf-repo-cdn.sigstore.dev` |
| API自动证书，选择该动作时 | `acme-v02.api.letsencrypt.org`及其签发流程需要的CA端点 |
| 一次性安装/更新 | Ubuntu、Docker、Microsoft及各工具的官方软件源，或批准的企业镜像源 |

某些域名经CNAME跳转，镜像/CDN端点也可能变化。GitHub组织启用IP允许列表时，应登记runner的实际出站公网地址，例如防火墙的出口地址，而非VM私网地址。

DNS应让目标服务的正常FQDN解析到其Private Endpoint；连接仍使用FQDN进行TLS主机名验证，不改成裸IP、不关闭证书验证。选择企业管理证书时，runner的OpenSSL/系统信任库也必须信任其CA链。

**仅配置`HTTPS_PROXY`不保证可用：** 本仓库部分安全敏感HTTP客户端明确不读取环境代理设置，Docker daemon也有独立出口配置。若企业强制显式代理，应在演练前验证每条路径并审批必要适配，不用关闭TLS校验或放宽资源公网策略解决。

## 4. 软件与工具清单

使用第1.1节时由首启脚本安装，下表用于核对用途及兼容性；已有机器也可按官方安装文档或客户批准的软件源人工安装并记录实际版本。不要求自动镜像工厂，也不要求客户把所有Python包逐个手装。

| 工具 | 要求 | 当前workflow行为 |
| --- | --- | --- |
| GitHub Actions runner | GitHub提供的受支持Linux x64版本，保留正常安全更新 | 一次性注册为服务，不使用`--ephemeral`运行连续迁移步骤 |
| Git、Bash、curl、tar、unzip、CA证书 | 系统级可用，runner账户PATH可见 | checkout、下载、脚本及`kubectl cp`等所需；旧Pod也需已有tar |
| Python | workflow选择3.13；基础`python3`可用 | runtime使用`setup-python`准备3.13并安装固定依赖；镜像晋级还会直接调用`python3` |
| Node.js与npm | Node 24，按本仓库版本约束；可由服务账户直接执行 | Node验证/构建所需；runner自带的Action执行Node不等于PATH中有`node` |
| Docker Engine | 可用Linux daemon及正常构建能力，runner账户可执行docker | 构建/拉取镜像、隔离恢复、schema操作；不会替客户安装daemon |
| Azure CLI与Bicep CLI | 支持仓库所用命令和模板的已验证版本 | Azure login Action不会补齐完整工具链 |
| kubectl | 与新旧API Server兼容，通常在相邻一个minor以内；本次1.35集群优先使用1.35系列 | 预装；还使用内置`kubectl kustomize` |
| Azure kubelogin | 使用Azure的`Azure/kubelogin`实现，支持`convert-kubeconfig -l azurecli` | 预装，勿装成同名的其他OIDC插件 |
| GitHub CLI `gh` | 支持`gh api`和artifact读取 | 预装；作业内用GitHub提供的短期Token，不需在VM长驻个人PAT |
| OpenSSL | 使用Ubuntu受支持版本及正确CA信任库 | 入口证书材料和真实TLS校验 |
| `skopeo`、`syft`、`trivy`、`cosign` | 在最终演练版本中验证可用并记录版本 | private-ingress/application分别依赖这些工具；晋级workflow虽安装部分工具，不能假定此前已跑过晋级才能执行其他动作 |
| PostgreSQL客户端 | `psql`、`pg_restore`可用，恢复工具须兼容实际dump格式；旧PG16优先匹配经验证的16系列工具 | 源`pg_dump`在旧Pod内执行；目标恢复在runner执行，使用`PGSSLMODE=verify-full`及Ubuntu系统CA bundle `/etc/ssl/certs/ca-certificates.crt` |

Python依赖的固定版本以[运行workflow](../.github/workflows/customer-runtime.yml)为准，其中已安装PyYAML、cryptography、service-identity、psycopg、Azure Identity/Key Vault SDK；`certificate-renew`另安装ACME/DNS依赖。不要为了“全部预装”改写系统Python或随意升级这些固定版本。

`setup-python`在自托管机器上需要可用且可写的tool cache，以及匹配Ubuntu的下载包。管理员应按所用Action版本准备缓存目录权限；需要自定义位置时参考`AGENT_TOOLSDIRECTORY`配置。**只装了系统Python、或交互式shell能运行Python，不等于作为systemd服务的runner能成功运行`setup-python`。**

工具建议安装在服务可见的系统路径，例如`/usr/local/bin`。不要仅安装在管理员的nvm、交互式venv或`.bashrc`中；注册完成后必须在runner服务环境再次验证。

**不是客户部署runner的必需项：** VS Code、桌面环境、Chrome/Playwright、Go、完整本地回归环境。它们用于开发/CI浏览器和静态测试，除非明确要求在该VM跑完整开发回归，否则无需安装。

## 5. Linux账户与权限

1. 使用独立普通账户，例如`actions-runner`；由管理员安装系统包、配置磁盘和服务。不用root长期运行runner，不给它通用免密sudo。
2. runner账户能写入自己的安装目录、工作目录、tool cache和临时目录。禁止其他普通用户读取该工作区，不设置为全员可写。
3. 允许runner访问Docker socket。加入docker组通常意味着接近主机root权限，因此该VM必须专用，不能以“非root服务”宣称已经隔离恶意作业；不要把Docker socket改成`chmod 666`。
4. 运行期间不允许同一机器上的其他项目或用户读取备份、凭据缓存和kubeconfig。保留企业主机防护，不因构建/恢复失败直接关闭安全软件。
5. 服务账户不保存管理员的日常`az login`/`gh auth login`会话。默认无需给VM挂业务Managed Identity；workflow通过OIDC获取各自权限。

**三类授权必须分开：**

| 授权 | 谁负责/如何检查 |
| --- | --- |
| GitHub接单 | 仓库/组织管理员完成runner注册、访问范围和标签配置；runner出现在列表且Idle |
| Azure资源与数据平面 | 管理员给workflow的OIDC身份授予所需范围的ARM、Storage、Vault、ACR等权限；注册runner不授予这些权限 |
| AKS、PostgreSQL与Entra目录 | 按集群配置分别授予AKS Azure RBAC/namespace权限、PG Entra角色及可选Graph授权；当前新AKS已启用Azure RBAC、禁用本地账号及WI/OIDC，不能使用`--admin`绕过授权。仅有ARM Contributor不等于可读Secret、exec、恢复数据库或创建Entra应用 |

OIDC身份仍配置在仓库/Environment变量中，包括`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`、部署及运行身份Client ID；专项身份按实际动作配置。Runner只提供执行环境，不能用给VM分配订阅Owner来代替这些授权。

## 6. GitHub注册与选择runner

按顺序完成，不需要部署新的注册管理服务：

1. 先fork本仓库并启用Actions，公开或私有均可。主要迁移workflow要求手动触发受保护的默认分支，并配置Environment Secrets `CUSTOMER_CONFIG_JSON`和`WORKFLOW_ARTIFACT_KEY`；后者用于加密附件。不要让外部PR或未经审核的代码使用这台runner。
2. 仓库管理员进入 **Settings → Actions → Runners → New self-hosted runner**，选择Linux/x64，按GitHub当时显示的官方下载及校验步骤安装。
3. 用专用runner账户配置，起名如`migration-prod-01`；保留默认`self-hosted`、`Linux`、`X64`标签，再加自定义标签`llmgw-prod-private`。
4. 注册令牌只在受控终端中按官方流程输入，不发到聊天、Git、文档或截图，不另建永久PAT作为workflow Secret。完成注册后按官方Linux服务说明安装并启动服务。
5. 确认GitHub列表显示runner **Idle**，重启VM后服务仍能上线。不要靠保持一个SSH终端运行`run.sh`承接关键迁移。
6. 在 **Settings → Secrets and variables → Actions → Variables** 设置仓库级变量`MIGRATION_PRIVATE_RUNNER_LABELS`：

```json
["self-hosted", "Linux", "X64", "llmgw-prod-private"]
```

这是JSON数组，不是逗号分隔文本，也不存为Secret。现有workflow使用`fromJSON(vars.MIGRATION_PRIVATE_RUNNER_LABELS)`选择runner，所有标签须匹配。建议放在**Repository variables**，避免仅放Environment级变量而在作业分配时不可用；变量也不授予任何访问权限。

组织级runner还需通过runner group限制允许的仓库；标签只是路由条件，不是安全边界。同一机器不要同时注册多个runner来并发修改同一个环境。若一个仓库管理多环境，先按已审核方式保证它们均可由所选runner访问，不自行假定当前单一变量支持按环境选择不同机器。

**安全边界：** 只运行受保护、审核过的迁移代码；不接公共PR或不可信fork作业，不把`pull_request_target`等可执行外部代码的工作流指向该runner。Environment审批可以辅助控制，但不能把能访问生产私网的runner当作不可信代码沙箱。

## 7. 准备完成后怎样验收

### 7.1 机器层检查

由实施人员在runner账户的环境中执行下列只读检查。不要运行`env`、打印Token、导出kubeconfig或显示Secret值作为验收证据。

```bash
uname -m
cat /etc/os-release
free -h
df -h
git --version
python3 --version
node --version
npm --version
docker version
docker info --format '{{.DockerRootDir}}'
kubectl version --client
gh --version
openssl version
psql --version
pg_restore --version
for tool in az kubelogin skopeo syft trivy cosign; do
  command -v "$tool" || exit 1
done
```

上面只能证明命令存在/daemon可达，不证明所有工具版本兼容、数据平面权限或迁移就绪。`python3`尚未切换为3.13时，应核对真实作业的`setup-python`结果，不直接替换OS自带解释器。

### 7.2 必须从Actions验证的事项

- [ ] 目标runner为Idle，实际作业分配到预期机器，服务账户PATH和Docker访问正常。
- [ ] checkout、`setup-python` 3.13、固定pip依赖下载及artifact读写成功；不能只验证`github.com`首页能打开。
- [ ] OIDC登录到正确租户/订阅；新旧AKS API可达且允许范围内的只读命令成功。
- [ ] 备份Storage、Key Vault、ACR及其数据端点通过正确私网DNS和TLS访问；新资源尚未创建时记录为待验证，不记为失败或通过。
- [ ] 目标PG创建后，真实TLS/Entra连接和所需角色操作成功；不能把TCP 5432可连当成恢复权限已验证。
- [ ] API/admin域名、源站公有证书链和批准来源验证成功；企业代理CA仅按实际管理网络需要安装。Admin用户名/密码来自后台Vault的独立`litellm-ui-password`，不是源站证书Vault或Master Key；模型与Pod身份检查在相应阶段执行。
- [ ] 镜像晋级、私有入口等所选动作所需工具可用；权限和出站缺口按实际失败补齐。
- [ ] 备份恢复前核对空间和执行时间，运行结束检查敏感临时文件及凭据缓存。

可运行`Customer private runner checks`：首次选择environment、`check_target=false, check_backup=false`；migration另读旧集群，greenfield只核对工具/身份、不读取旧集群，目标访问标为待验证。目标AKS建立后再选check_target=true；greenfield保持check_backup=false，不把旧集群检查作为从零部署前置条件。migration Stage0备份网络准备后选择`check_backup=true`，额外检查备份资源、Runner到PE的DNS/TLS及同OIDC身份的Blob容器只读列举。公开摘要仅显示固定检查名/状态，详细结果加密上传。**这不是“runner全部权限就绪”证明**，不验证Blob上传/下载、Pod exec/cp、其他资源写权限或模型调用；失败不会自动放宽NSG或授予角色。

相关入口：[运行操作](../.github/workflows/customer-runtime.yml)、[镜像晋级](../.github/workflows/promote-litellm-image.yml)、[入口检查](../.github/workflows/customer-gateway-checks.yml)。基础设施部署和部分授权/配置检查使用GitHub托管runner，不要误以为其中一个绿勾就验证了这台私网机器。

## 8. 常见故障与处理

| 现象 | 先检查 |
| --- | --- |
| 一直Waiting for a runner | runner是否Idle、自定义标签及JSON数组、仓库/runner group访问范围、变量是否在正确作用域 |
| SSH里有命令，workflow中没有 | 服务账户PATH、nvm/venv依赖、服务启动时环境；修改后在维护窗口重启runner服务 |
| `setup-python`失败 | 3.13下载域名、Ubuntu兼容包、tool cache权限/磁盘；不是直接放宽为root运行 |
| Docker permission denied | 服务账户组成员与socket权限；加入组后需让服务重新取得组信息，不开放socket给所有人 |
| 解析为公网IP或超时 | Private DNS Zone关联/转发、实际路由、NSG、Docker网段重叠；不靠临时公网放行绕过 |
| 私有AKS API A记录缺失、绑定检查挂起 | 先核对AKS是否停止；保持`Running/Succeeded`至rollout/绑定检查结束，获准启动后等待DNS/PE恢复，再按[启动复核](../local_execution/stage2-9-guide-zh.md#私有aks自动停机后的启动复核)检查，不开放AKS公网 |
| `get-credentials`要求覆盖或身份错配 | 当前自动化在每次操作目录用`--overwrite-existing`非交互取得独立kubeconfig，不应要求输入`y`；核对实际代码revision、显式`--kubeconfig`和Azure RBAC，不改默认context或使用admin凭据 |
| Azure登录成功，操作403 | 当前workflow的具体OIDC身份、资源范围、数据平面/AKS/PG/Graph授权，不是重新给VM登录一次 |
| 证书或PG TLS失败 | CA链、域名/SNI、系统时间、libpq支持和企业代理；不使用`--insecure`或关闭验证修复 |
| 镜像登录成功但拉取/签名失败 | ACR数据端点、源registry/CDN、Sigstore出口、工具版本与权限 |
| 作业被取消、磁盘写满或机器重启 | 先检查动作是否部分完成，保存脱敏失败信息；不要自动重跑恢复、轮换或清空目标库 |

## 9. 运行期间与结束后

当前私网runtime作业最长120分钟，镜像晋级最长45分钟，入口检查最长15分钟；首次下载和准备还需时间。任务运行期间不要设置到点关机、抢占回收或自动重启，且保持目标AKS运行至绑定/入口检查完成。修改超时本身不能保证数据迁移在批准窗口内完成。

自托管runner不会像GitHub托管runner一样每次作业后销毁。现有runtime会清理其临时工作目录，但**不保证整个VM、Docker层、runner诊断、Azure CLI、Docker登录缓存或所有失败路径都已清理**。应限制磁盘访问，并在作业空闲后检查本次生成的备份、kubeconfig、认证缓存和失败容器；保留需要的受控备份，禁止直接对共享机器执行全局prune/删除。

迁移及观察结束后，可以保留该VM用于后续维护，也可以在确认无作业后停用/解除分配，待维护时再启动。Azure仅OS关机未必停止计算计费；解除分配后持久磁盘等资源仍可能收费，价格按实际规格/区域计，不在本文承诺固定成本。

runner退役时先确认不再需要执行维护任务，再注销runner、回收其临时授权并按客户策略处理磁盘。**不要把runner的停用与旧LiteLLM集群停用混为一谈。**

## 10. 交接清单与参考

客户准备好下列信息即可交给实施人员核对，不交付任何秘密原值：

| 项目 | 记录内容 |
| --- | --- |
| 机器 | VM名称/资源ID、OS/架构、CPU/内存、工作盘及可用空间 |
| 网络 | 子网、DNS、出站路径、到新旧环境的路由、企业CA是否安装 |
| GitHub | 仓库、runner名称/标签、group范围、Idle状态、变量配置位置 |
| 软件 | 本文工具的实际版本，3.13 setup和Docker作业验证结果 |
| 授权 | 各workflow身份及授权范围、尚缺权限，不包含Token/私钥 |
| 就绪 | 已验证run链接、未创建资源的待验证项、失败及处理结果 |

- [部署与验收workflow指南](customer-deployment-workflows-zh.md)
- [按步骤执行的客户迁移手册](customer-migration-guide-zh.md)
- [入口与证书设计](litellm-ingress-tls-certificate-design-zh.md)
- [GitHub自托管runner要求及网络域名](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [添加自托管runner](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
- [配置runner服务](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application)
- [setup-python自托管说明](https://github.com/actions/setup-python/blob/main/docs/advanced-usage.md#using-setup-python-with-a-self-hosted-runner)
- [Docker Engine的Ubuntu安装说明](https://docs.docker.com/engine/install/ubuntu/)
- [Azure CLI Linux安装说明](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux)
- [Azure kubelogin](https://github.com/Azure/kubelogin)
- [PostgreSQL连接与TLS参数](https://www.postgresql.org/docs/current/libpq-connect.html)

官方安装说明可能展示比仓库更新的Action或工具版本，执行时以审核过的workflow和兼容性测试为准，不因为文档示例更新就自动升级部署链路。