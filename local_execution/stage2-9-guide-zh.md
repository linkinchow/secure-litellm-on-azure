# Stage2–9本地手工部署指南

> 核对日期：2026-10-09
>
> 适用：migration已完成本地Stage0–1，或greenfield已完成Stage0 bootstrap/network/Runner Peering；客户不能使用GitHub Actions，需要继续在受控执行主机上部署Stage2–9。

## 当前运行状态（2026-10-09）

**可选L3增强（Stage2–9之后）：** 默认仍为批准的原生Spend Logs/PG正文路径，
不执行额外步骤不改变基线。需要独立Blob原文、LAW索引及IT管理员CLI查询/保全时，
另按[TypeScript L3原文审计runbook](../docs/litellm-optional-l3-runbook-zh.md)部署并验收，
不要混用下文历史Entra代理L3步骤或提前加入`l3Audit`。

**Native WSS部署/升级补充：** 新版本Stage4私有ingress和Stage9 API WAF默认支持两个Responses路径的合法WebSocket握手，保留原POST路径及FDID隔离，不需要升级LiteLLM 1.104.0。已有West US 3等native dev/test canary环境须按[WSS升级执行单](native-wss-upgrade-zh.md)重跑`stage4-private-ingress`、专用API WAF plan/execute和`stage9-edge-bind`，再刷新报告及做真实两轮/工具验收；不要重跑`edge-prepare`或DNS切换。当前运行状态段落中的Codex推理成功不等于WSS已验收。Entra代理不在本次协议扩展范围。

当前greenfield native dev/test网关已在West US 3运行：Private AKS为`Running/Succeeded`，Azure RBAC与禁用本地账号、WI/OIDC均开启，Azure CNI Overlay，2个system加2个user节点，节点SKU `Standard_D4s_v4`。API/Admin通过Front Door Premium分别连接两条PLS和两个私有入口；这不是生产容量或其他区域可用性保证。

用户已确认Admin `/fallback/login`原生用户名/密码登录及virtual key的Codex Responses推理。UI密码来自**后台Vault**中独立的`litellm-ui-password`，不是Master Key或证书Vault秘密；用户侧Entra SSO延期，native路径跳过Stage7。Admin WAF当前精确配置3个批准公网IPv4 `/32`及1个IPv6 `/128`。源镜像固定1.104.0，秘密/身份/续期、审计、恢复及各阶段实际检查仍是门禁，不据当前可用性声明完整Stage0–9或production签收。

只读管理面回读确认PostgreSQL 16为`Ready`，`activeDirectoryAuth=Enabled`、`passwordAuth=Disabled`且公网禁用；Redis为`Microsoft.Cache/redisEnterprise Balanced_B0`，default数据库`accessKeysAuthentication=Disabled`、`clientProtocol=Encrypted`、端口10000。Redis集群`publicNetworkAccess=null`不是公网启用或禁用的证据，不能省略私网/访问控制验证。ACR Premium禁用admin账号及公网，后台与证书Vault均启用RBAC并禁用公网；这些状态不替代实际Pod数据面权限、TLS和续期测试。

本文同时保留migration、Entra和production替代路径；示例只用通用占位符，不把当前新建测试环境当作所有客户的唯一部署模式。

## 1. 执行边界

本指南只扩展`local_execution`，不修改或替代现有GitHub workflows。底层继续调用同一套Bicep、What-if范围校验、Kubernetes dry-run、数据库迁移和运行时检查。

本地路径与Actions有三个差异：

- 不读取GitHub Environment、run ID或验收账本；基础设施和运行时变更使用本地两阶段plan/execute，并在execute前重新计划检查漂移。
- 本地操作者负责在命令前取得变更批准，命令输出不等于Stage验收通过。
- 镜像使用客户自管Cosign密钥签名；Actions仍使用GitHub OIDC keyless签名，两种信任路径不混用。

Stage9支持在旧系统保持运行时发布独立新域名做并行canary：migration以已验收备份的恢复结果为数据基线，greenfield使用初始化的新库/新密钥；完成发布报告后可启用新Front Door route并新增DNS CNAME，不要求旧系统停写或替换旧域名。migration新旧数据库只在备份时点一致，之后不会持续同步，试点须使用新环境单独签发的virtual key。最终停写、最终增量同步和旧环境退役仍没有自动按钮；只有客户以后要求无损承接备份后的旧系统新增数据时，才进入可选最终迁移流程。

## 2. Azure登录方式

### 2.1 复用现有az登录

客户能够在执行主机登录时，保留默认配置：

```json
"authentication": {
  "default": {"method": "existing"}
}
```

执行器不会重新登录，只会切换到customer.json中的订阅并核对租户和订阅。可以使用交互用户，也可以是客户已通过证书等合规方式登录的服务主体。不要使用`sudo az login`或长期Client Secret。profile未声明`clientId`时适合一般deploy/runtime/database人工操作；一旦声明`clientId`，执行器还会在内存中解析Azure CLI Token的`appid/azp`并拒绝身份错配，Token不会写入日志。

Entra Security Defaults可能阻止设备码登录；这时不要关闭Security Defaults来迁就脚本，改用浏览器支持的登录方式或下面的UAMI。

### 2.2 推荐的两种登录方式

**客户现场：直接登录为主。** 客户能够在Runner VM执行交互登录时，保持`default=existing`，Stage3、Stage4、Stage5 platform及普通AKS操作不需要为deploy/runtime/certificate分别创建UAMI：

```bash
az login --tenant "REPLACE_TENANT_ID"
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
```

脚本中的deploy/runtime/database等名称是权限类别；没有专项profile时会复用当前Azure CLI会话。客户账号仍需具备对应Stage列出的Azure、AKS、PG或DNS权限。

但有两个例外不能用普通用户账号代替：

- Stage5的`backend-secrets`和`schema-migrate`必须使用`databaseAccess.migrationPrincipalId`对应的服务身份Token；migration模式的`restore-target`也使用该身份。数据库角色固定映射为服务主体`llmgw_migrator`。客户可在这些步骤前切换到已有runtime UAMI，或使用下面的单一operator UAMI。
- Stage7的Entra动作必须使用两个彼此分离、且与迁移身份不同的服务身份，见第2.5节。

**实施方验证：单一临时UAMI。** 无法在Runner交互登录时，只创建一个`llmgw-test-local-operator`，并只配置`default`：

```json
"authentication": {
  "default": {
    "method": "managed-identity",
    "clientId": "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID"
  }
}
```

所有未单独配置的profile会自动回退到这个身份。默认`entraMode=deferred`且未选native-auth时，这一个UAMI可用于Stage2–6、可选Stage8观测基础设施，以及Stage9禁流量的origin/edge准备。显式native-auth路径不部署Stage7代理，可按本文继续原生Stage8/9；所需Azure、AKS及数据库权限仍逐项核验，身份数量少不等于自动获权。

### 2.3 创建单一验证UAMI

执行位置：**客户外部管理终端**。已有获批验证UAMI时只查询，不重复创建：

已有`github-developer`等UAMI且客户批准复用时，可以直接把它作为local-operator，不必再创建新身份；先核对其Client ID、Principal ID和现有角色。Runner必须保持Offline，恢复Actions Runner服务前必须卸载该高权限UAMI。

```bash
set -euo pipefail
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
LOCATION="REPLACE_AZURE_REGION"
IDENTITY_RG="REPLACE_CUSTOMER_IDENTITY_RESOURCE_GROUP"
OPERATOR_NAME="llmgw-test-local-operator"

az account set --subscription "$SUBSCRIPTION_ID"
if ! az identity show --resource-group "$IDENTITY_RG" --name "$OPERATOR_NAME" \
  --output none 2>/dev/null; then
  az identity create --resource-group "$IDENTITY_RG" --name "$OPERATOR_NAME" \
    --location "$LOCATION" --output none
fi
az identity show --resource-group "$IDENTITY_RG" --name "$OPERATOR_NAME" \
  --query '{name:name,clientId:clientId,principalId:principalId,resourceId:id}' \
  --output json
```

保存三个值：Client ID填`authentication.default.clientId`；Principal ID用于RBAC、`databaseAccess.migrationPrincipalId`和PG管理员组成员；Resource ID用于挂载/卸载。

### 2.4 单一验证UAMI的临时权限

这是**隔离测试环境的简化模式**，权限高于生产最小权限。Runner必须Offline，身份只在验证窗口挂载，完成后卸载并回收角色。不要授予订阅Owner。下列角色组合是现场验证的操作建议，代码仍以每项真实Azure/AKS/数据面调用结果为准，并不会把这些角色名称当作安全证明。

```bash
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
RUNNER_VNET_ID="REPLACE_RUNNER_VNET_RESOURCE_ID"
OPERATOR_OBJECT_ID="REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID"
TARGET_RG_SCOPE="$(az group show --subscription "$SUBSCRIPTION_ID" \
  --name "$TARGET_RG" --query id --output tsv)"

# 目标RG内集中完成资源部署、锁和角色分配。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role Owner \
  --scope "$TARGET_RG_SCOPE" --output none

# 只在Runner VNet授权网络链接管理。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role "Network Contributor" \
  --scope "$RUNNER_VNET_ID" --output none
```

Stage4前，每个模型RG也需给该UAMI临时Owner，或使用附录中的最小模型角色加受约束RBAC Administrator；不要扩大到模型订阅。Stage4 AKS创建后再补节点RGContributor、AKS Cluster User/RBAC Cluster Admin和ACR AcrPush。Stage9自动DNS切换前再给目标DNS zone的DNS Zone Contributor。

Stage5使用同一UAMI时，客户Entra管理员创建一个PG管理员安全组，把该UAMI加入组；`stage5Data`填写组Object ID/名称/`Group`，`databaseAccess.migrationPrincipalId`填写UAMI Principal ID。这样PG管理员对象与`llmgw_migrator`服务主体保持不同，符合脚本的分离检查。

### 2.5 Stage7是唯一例外

当前代码强制Entra应用初始化身份、授权身份和数据库迁移身份三者分离。因此一个UAMI**不能**执行Stage7全部动作。当前客户若保持`entraMode=deferred`，先不创建额外身份。

将来启用Stage7时，再新增两个UAMI并只覆盖两个profile：

```json
"authentication": {
  "default": {
    "method": "managed-identity",
    "clientId": "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID"
  },
  "entraBootstrap": {
    "method": "managed-identity",
    "clientId": "REPLACE_ENTRA_BOOTSTRAP_UAMI_CLIENT_ID"
  },
  "entraAccess": {
    "method": "managed-identity",
    "clientId": "REPLACE_ENTRA_ACCESS_UAMI_CLIENT_ID"
  }
}
```

entra-bootstrap需Graph `Application.ReadWrite.OwnedBy`；entra-access需`Application.Read.All`、`AppRoleAssignment.ReadWrite.All`、`DelegatedPermissionGrant.ReadWrite.All`和`User.Read.All`，均需Admin consent。两者在目标RG临时Contributor即可保存回执；不要把Graph权限授给通用operator UAMI。

### 2.6 挂载和卸载单一验证UAMI

外部管理终端挂载一次：

```bash
RUNNER_VM_RESOURCE_ID="REPLACE_RUNNER_VM_RESOURCE_ID"
OPERATOR_RESOURCE_ID="REPLACE_LOCAL_OPERATOR_UAMI_RESOURCE_ID"
az vm identity assign --ids "$RUNNER_VM_RESOURCE_ID" \
  --identities "$OPERATOR_RESOURCE_ID"
```

Runner VM核验：

```bash
az login --identity --client-id "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID" --output none
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
az account show --query '{tenantId:tenantId,subscriptionId:id,identity:user.name}' --output json
```

本地执行器每一步都会自动重新选择同一个default UAMI，无需手工切换deploy/runtime/database profile。完成验证后先在Runner执行`az logout && az account clear`，再从外部管理终端卸载：

```bash
az vm identity remove --ids "$RUNNER_VM_RESOURCE_ID" \
  --identities "$OPERATOR_RESOURCE_ID"
```

最后回收临时Owner、节点RG、AKS、ACR、模型RG和DNS角色；仅卸载UAMI不会自动删除其Azure角色分配。

## 3. Entra ID Free决策

当前Stage7使用应用注册、服务主体、OAuth scope/app role、定向用户授权和托管身份；代码不调用Conditional Access、PIM、Identity Protection或Access Reviews。Microsoft Entra ID Free本身不阻止这些基础对象，也不对Azure Managed Identity收费，因此**Free不是自动跳过Stage7的理由**。

需要单独评估的是登录保护：

- Free可启用Security Defaults，对整个租户提供统一MFA和旧认证阻断。
- 按应用、用户组、设备、位置等条件精细控制的Conditional Access需要Entra ID P1。
- 风险登录策略和PIM等能力需要更高许可。
- Security Defaults可能阻止设备码登录；UAMI不依赖人工设备码。

客户应在Stage2决定以下三选一：

1. `entraMode=enabled`：确认Free + Security Defaults满足本次范围，或先取得所需P1/P2能力，然后按Stage7执行。
2. `entraMode=deferred`：先完成Stage2–6。执行器会阻止Stage7身份/代理、Stage8应用发布、Stage9 edge绑定、启流量和DNS变更；仍可创建Stage8观测基础设施，以及Stage9禁流量的origin/edge资源。
3. Stage6选择`--option native-auth`：客户无法批准或验证Graph应用权限时，显式改用LiteLLM原生管理员登录和virtual key。该路径保留AKS、Workload Identity、Entra-only PG/Redis、Key Vault、API/Admin双LB、Front Door和原生Spend Logs，但不部署用户侧Entra代理，也不提供用户Entra MFA、Conditional Access、代理guardrail或增强L3；不得自动删除已有安全能力。Stage6先从批准私网验收Admin；Stage9后日常Admin入口改为独立Front Door，先由WAF Prevention限制批准公网IPv4 `/32`及IPv6 `/128`，再输入原生用户名/密码。API只允许固定推理路径；不能把它写成与Entra路径等价。原生管理员使用后台Vault中独立的`litellm-ui-password`，不复用或分发Master Key，也不使用证书Vault。

配置示例默认使用保守值：

```json
"features": {
  "entraMode": "deferred",
  "allowTrafficRelease": false
}
```

Stage5的PG/Redis Entra-only和应用Workload Identity不属于用户侧Stage7认证，native-auth也继续保留它们。不要把普通`entraMode=deferred`解释为允许密码数据库、Redis Key或匿名公网API；未显式选择并验证Stage6 `native-auth`时，不得删除Stage7后直接启用edge。native路径使用独立UI密码和virtual key，不允许匿名API。

## 4. 执行主机和配置

除Stage0–1工具外，还需要：

```bash
command -v docker jq psql pg_restore openssl skopeo syft trivy cosign
az bicep install
```

继续使用受控的`local_execution/customer.json`。Stage2–9客户业务字段按[主手册第2.3节](../docs/customer-migration-guide-zh.md#23-客户json分批准备)分批加入；不要一次复制未决的占位块。`localExecution`只保存本地执行方式和非秘密引用，不保存Token、Master Key、Salt、PEM正文或Cosign口令。

### 分阶段配置模板

[基础模板](customer.example.json)保持Stage0–1最小可运行范围；[Stage2–9片段目录](customer.stage2-9.fragments.example.json)补充后续全部字段。片段目录本身不是合法的客户配置，不能整份复制为`customer.json`，也不能把`catalogVersion`或`stages`写入客户配置。

使用合并器，不手工递归编辑：

```bash
# 1. 预览当前Stage；不会修改customer.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json \
  --stage REPLACE_STAGE --operation plan
```

最后一行JSON会给出：

- `preview`：递归合并后的完整配置预览。
- `requiredValues`：本Stage尚未替换的`REPLACE_*`列表。
- `changedPaths`：本次会修改的JSON路径。

若`missingPlaceholders`非空，把生成的`values.required.json`复制到Git忽略的本地文件并填写客户值：

```bash
cp "REPLACE_REQUIRED_VALUES_PATH" \
  "local_execution/stage-REPLACE_STAGE-values.local.json"
chmod 600 "local_execution/stage-REPLACE_STAGE-values.local.json"
# 使用受控编辑器填写右侧空字符串，不添加密码、Token、Master Key或Salt。
```

然后再次预览，确认`missingPlaceholders=[]`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json \
  --stage REPLACE_STAGE --operation plan \
  --values "local_execution/stage-REPLACE_STAGE-values.local.json"
```

审核`preview`和`changedPaths`后才真正写入：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json \
  --stage REPLACE_STAGE --operation apply \
  --values "local_execution/stage-REPLACE_STAGE-values.local.json"
```

`apply`会先调用现有配置校验器，成功后以0600权限原子替换`customer.json`，并把变更前完整配置保存到输出目录的`customer.before.json`。校验失败或仍有占位符时不会修改原文件。

可选块必须显式加`--option`：

| Stage | 选项 | 作用 |
| --- | --- | --- |
| 2 | `single-validation-identity` | 配置单一local-operator UAMI；客户直接登录不选 |
| 4 | `automatic-api-certificate` | 启用自动API证书；手工导入不选 |
| 5 | `database-admin-identity` | 使用独立database UAMI作为PG管理员；无管理员组时选择 |
| 5 | `legacy-master-key-salt` | 旧环境无显式Salt且已批准复用Master Key兼容路径时选择 |
| 6 | `native-auth` | 不部署Stage7 Entra代理；Stage6私网验收原生Admin，Stage9切换到来源IP白名单加原生密码的Admin边缘；API使用virtual key |
| 8 | `observability` | 加入可选collector |
| 8 | `enhanced-l3` | 删除原生`contentAudit`并切换增强L3；不能与observability在同一次合并 |
| 9 | `azure-dns` | 使用仓库自动发布Azure DNS |
| 9 | `approved-release` | 打开canary/production流量开关并加入release报告路径；默认只合并禁流量prepare |

例如验证环境Stage2：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 2 --operation plan \
  --option single-validation-identity
```

合并器会自动复用customer.json中已有的订阅、区域、baseDomain、目标Workspace/VNet、Runner VNet、ACR和证书Vault名称；其他实际客户值从values文件提供。

片段覆盖关系如下：

| Stage | 合并内容 |
| --- | --- |
| 2 | `governance`、原生`contentAudit`决策；可选UAMI认证profile |
| 3 | `parameters.platform`的ACR、Workspace、目标网络和AKS |
| 4 | 模型账号、稳定角色命名、证书Vault、Runner目标DNS、私有入口及本地Cosign配置；自动证书为可选块 |
| 5 | `stage5Data`、数据库初始化身份及Private DNS归属；仅migration加入Stage0备份引用/SHA256 |
| 6 | `application`后端镜像digest和模型部署映射；可选原生认证模式/管理员用户名 |
| 7 | `entra`、`proxy`和`entraMode=enabled` |
| 8 | 可选原生观测collector；增强L3是单独替代方案 |
| 9 | origin、edge；Azure DNS和最终release开关均为可选/后置块 |

合并后先运行当前Stage的`--operation plan`。plan会校验该组件配置并生成What-if或运行时预览；配置缺字段或仍有占位符时不得进入execute。

### 4.1 Stage5恢复输入（仅migration）

`deploymentMode=greenfield`不得执行本节：Stage5合并器会省略`backupBlob`、`backupSha256`、`postgresMigrationUser`和`legacySaltSource`，也不得选择`legacy-master-key-salt`。greenfield直接在空目标库初始化新秘密和Schema。

从**当前Runner本地执行Stage0**时生成的同一个成功备份报告取得Blob名和SHA256；不要使用Actions artifact、开发机`temp/reviewed-backup-*`或另一台机器保存的旧报告。先在Runner仓库根目录定位并核验报告：

```bash
set -euo pipefail
mapfile -t BACKUP_REPORTS < <(
  find temp -mindepth 3 -maxdepth 3 -type f \
    -path 'temp/local-stage*/*-backup-restore-*/acceptance-report.json' -printf '%T@ %p\n' \
    | sort -nr | cut -d' ' -f2-
)
BACKUP_REPORT=
for candidate in "${BACKUP_REPORTS[@]}"; do
  run_dir="${candidate%/acceptance-report.json}"
  if jq -e '.step == "backup-restore" and .result.status == "completed"' \
      "$run_dir/local-execution.json" >/dev/null \
    && jq -e '.observations.fullRestoreSucceeded == true
      and (.observations.backupBlob | test("^pre-change/[0-9a-f]{32}\\.dump$"))
      and (.observations.backupSha256 | test("^[0-9a-f]{64}$"))
      and .observations.backupBytes > 0
      and .observations.publicTableCount > 0' "$candidate" >/dev/null; then
    BACKUP_REPORT="$candidate"
    break
  fi
done
[[ -n "$BACKUP_REPORT" ]] || {
  echo 'No successful local Stage0 backup report found on this Runner' >&2
  false
}
printf 'Using local Stage0 report: %s\n' "$BACKUP_REPORT"
jq '.observations | {backupBlob,backupSha256,backupBytes,publicTableCount,fullRestoreSucceeded}' \
  "$BACKUP_REPORT"
```

只有上述命令找到成功报告时才继续。将输出的完整`backupBlob`和`backupSha256`原样用于Stage5；values文件中的`REPLACE_32_HEX`只填写`pre-change/`与`.dump`之间的32位标识：

```json
"runtimeInputs": {
  "backupBlob": "pre-change/REPLACE_32_HEX.dump",
  "backupSha256": "REPLACE_64_HEX_SHA256",
  "postgresMigrationUser": "llmgw_migrator",
  "legacySaltSource": "secret"
}
```

配置了`databaseAccess`时，`postgresMigrationUser`可省略，执行器固定使用`llmgw_migrator`。两项备份值不是Blob URL、SAS、镜像digest或任意旧备份。旧`litellm-env`已有独立`LITELLM_SALT_KEY`时，`legacySaltSource`填写`secret`；只有旧Salt明确不存在、且客户已批准并验证“当前Master Key作为永久Salt”的兼容路径时才填写`master-key`。该字段不包含秘密值。

### 4.2 本地镜像签名

在客户受控密钥目录生成独立Cosign密钥对。私钥必须加密、权限为0600并进入客户密码库/备份策略；不要提交Git或放入Actions工作目录。

```bash
install -d -m 0700 /srv/runner/manual/keys
cd /srv/runner/manual/keys
cosign generate-key-pair
chmod 600 cosign.key
chmod 644 cosign.pub
cd /srv/runner/manual/secure-litellm-on-azure
```

`cosign generate-key-pair`提示输入和确认私钥口令时，必须设置客户批准的**非空口令**，不能直接回车。执行器拒绝空口令密钥。若误生成空口令密钥且尚未签过任何镜像，先从工作路径移走该密钥对，再重新生成；若已用于签名，按客户密钥轮换流程保留旧公钥和历史验证证据，不直接覆盖。

在customer.json加入路径和目标tag。tag不是最终digest；每次步骤成功后从`target-image-summary.json`取完整`ACR/repository@sha256:...`回填业务配置。

```json
"imageSigning": {
  "privateKeyPath": "/srv/runner/manual/keys/cosign.key",
  "publicKeyPath": "/srv/runner/manual/keys/cosign.pub",
  "backendTargetTag": "litellm-azure:rehearsal-1",
  "proxyTargetTag": "auth-proxy:rehearsal-1",
  "collectorTargetTag": "otel-collector:0.148.0"
}
```

每次镜像步骤前在当前shell安全输入口令，命令结束后清除：

```bash
COSIGN_PASSWORD=
while [[ -z "$COSIGN_PASSWORD" ]]; do
  read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
  printf '\n'
  [[ -n "$COSIGN_PASSWORD" ]] || echo 'Cosign key password must not be empty' >&2
done
export COSIGN_PASSWORD
# 执行一个镜像步骤
unset COSIGN_PASSWORD
```

本地镜像步骤会构建或导入、推送、解析不可变digest、生成SPDX SBOM、用Trivy阻断可修复CRITICAL、签名并以同一公钥回验。ACR Token只写入本次0700临时目录，并在结束时删除。

### 4.3 每个变更步骤都要执行plan和execute

后文会为每个变更明确列出两条命令。第一条生成plan，最后一行会显示`result.planSha256`和`outputDirectory`；先审核`<outputDirectory>/plan/`中的`plan-summary.json`、`reviewed-plan.json`或`runtime-review.json`。确认无误后，把该64位哈希原样填入紧接着的execute命令：

```bash
# 1. 生成并审核计划
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step REPLACE_STEP --operation plan

# 2. 审核通过后真正部署/执行
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step REPLACE_STEP --operation execute \
  --approved-plan-sha256 "REPLACE_PLAN_SHA256_FROM_PREVIOUS_COMMAND"
```

execute会重新生成实时plan；代码、配置、云状态或发布报告变化导致哈希不一致时停止，必须重新plan和审核。**只运行plan不算完成该步骤。** `--operation apply`仅保留给Stage0–1，本地执行器会拒绝用它绕过Stage2–9批准。

下面三类步骤没有plan/execute命令对：

- `stage2-decisions`、`stage3-source-check`和`stage4-target-check`是直接检查。
- 三个`promote-*-image`会直接构建/导入、扫描、签名和推送镜像，须显式使用`--operation execute`。
- 可选的最终停写、最终数据同步及旧环境下线仍是人工批准操作，不伪装成已有自动化步骤，也不作为独立新域名canary的前置条件。

## 5. Stage2–6顺序

所有本地执行命令都在**Offline Runner VM的仓库根目录**运行；身份创建、角色分配和UAMI挂载命令在**客户外部管理终端**运行。

### Stage2：冻结决策，不部署资源

1. 合并Stage2的`governance`和`contentAudit`。客户直接登录不加option；验证环境的单一UAMI加`--option single-validation-identity`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 2 --operation plan \
  --values local_execution/stage-2-values.local.json \
  --option single-validation-identity
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 2 --operation apply \
  --values local_execution/stage-2-values.local.json \
  --option single-validation-identity
```

客户直接登录时删除两条命令中的`--option single-validation-identity`。确认网络CIDR、区域/SKU配额、PG HA、域名证书、实际客户端协议、正文留存和Entra Free决策均已有客户负责人。
2. Runner VM执行：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage2-decisions
```

3. 打开命令输出中的`outputDirectory/stage2-decision-checklist.json`，逐项记录负责人和决定。该步骤不创建Azure资源；存在`pending`或未决项就停在Stage2。

### Stage3：供应链检查和目标基础资源

**执行身份：客户当前登录账号，或验证模式的operator UAMI。** 确认该主体在目标RG有部署权限；模型RG权限可在Stage4前补齐。

1. 合并Stage3 `parameters.platform`；先确认CIDR不与Runner、旧环境或企业网络重叠：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 3 --operation plan \
  --values local_execution/stage-3-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 3 --operation apply \
  --values local_execution/stage-3-values.local.json
```
2. Runner VM先检查固定上游源镜像：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage3-source-check
```

该步骤先从固定LiteLLM `1.104.0`上游digest构建当前仓库的派生运行镜像，再验证非root UID、LiteLLM/AnyIO/PyJWT版本、GPT-6.1系列识别及Azure GPT-6 Astra/Sol/Luna目录项，然后生成SBOM并执行Trivy门禁；不会推送ACR。派生构建继续用SHA256固定的`security-requirements.txt`约束AnyIO `4.14.2`，防止依赖回退。不要把上游层单独作为最终应用镜像部署。

失败时查看本次四项结果；`build`、`runtime`、`sbom`和`scan`必须全部为`passed`：

```bash
RUN_DIR="$(ls -1dt temp/local-stage09/*-stage3-source-check-* | head -n 1)"
jq '{sourceImage,evaluatedImage,evaluatedImageId,buildInputsSha256,status,results}' \
  "$RUN_DIR/source-summary.json"
```

发现新的可修复CRITICAL或runtime模型合同不匹配时停在Stage3，更新受审查的安全覆盖或选择通过完整兼容验证的新稳定LiteLLM版本；不得改报告、降低严重级别或改用RC/dev镜像绕过。`1.104.0`的数据库迁移会有意省略两个大表索引；Stage5严格允许的仅是这两项operator-managed差异，详见[升级验证](../docs/litellm-1.104.0-upgrade-validation-2026-10-07.md)。

3. 计划并真正部署Stage3 platform：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage3-platform --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage3-platform --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE3_PLATFORM_PLAN_SHA256"
```

第二条命令中的哈希来自第一条命令最终输出；执行成功后应创建/确认目标ACR和Stage3基础资源，不创建PG或切换流量。

4. Runner VM只读核验：

```bash
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
ACR_NAME="REPLACE_GLOBALLY_UNIQUE_ACR_NAME"

az acr show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$ACR_NAME" \
  --query '{state:provisioningState,publicNetworkAccess:publicNetworkAccess,sku:sku.name}' \
  --output json
```

通过条件：`state=Succeeded`、`publicNetworkAccess=Disabled`，SKU与客户批准一致；Stage3源扫描报告为passed。旧AKS、旧PG和旧入口仍正常，未发生流量变化。

### Stage4：私网AKS、证书、入口和目标连通性

**客户模式使用当前登录账号；验证模式使用同一个local-operator UAMI。** 文中的deploy/runtime是权限类别，不再代表两个身份。Stage4开始前，IAM管理员完成：

- 客户账号按客户最小权限策略授权；验证operator在目标RG临时Owner。
- 客户账号在每个模型RG使用附录最小角色；验证operator可在每个批准模型RG临时Owner，但不得扩大到模型订阅。
- 当前执行主体在Runner VNet有Network Contributor。
- Stage4模板中的模型账号、区域、AKS版本/SKU和CIDR已获批。

1. 合并Stage4片段。手工导入证书使用下列命令；选择自动API证书时，两条命令都追加`--option automatic-api-certificate`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 4 --operation plan \
  --values local_execution/stage-4-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 4 --operation apply \
  --values local_execution/stage-4-values.local.json
```

确认模型账号、证书Vault、Runner VNet、入口证书地址和CIDR后，部署私网AKS和模型连接：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-platform --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-platform --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_PLATFORM_PLAN_SHA256"
```

2. 平台创建成功后，客户外部管理终端取得新AKS、节点RG和ACR资源ID，并补runtime和DNS连接权限：

```bash
TARGET_AKS="REPLACE_NEW_PRIVATE_AKS_NAME"
ACR_NAME="REPLACE_GLOBALLY_UNIQUE_ACR_NAME"
AKS_ID="$(az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --query id --output tsv)"
NODE_RG="$(az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --query nodeResourceGroup --output tsv)"
NODE_RG_SCOPE="$(az group show --subscription "$SUBSCRIPTION_ID" --name "$NODE_RG" \
  --query id --output tsv)"
ACR_ID="$(az acr show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$ACR_NAME" --query id --output tsv)"

az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service Cluster User Role" --scope "$AKS_ID" --output none
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service RBAC Cluster Admin" --scope "$AKS_ID" --output none

# runner-target-connectivity需要在AKS系统DNS所在节点RG写入嵌套部署和DNS链接。
# 这里使用Stage4窗口内的节点RG Contributor；客户有等效最小自定义角色时优先替代。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role Contributor \
  --scope "$NODE_RG_SCOPE" --output none

# 同一operator负责后端、入口和后续代理镜像晋级。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role AcrPush \
  --scope "$ACR_ID" --output none
```

若客户ACR启用了ABAC repository permissions，不使用旧`AcrPush`，改由ACR管理员授予等效的目标仓库Writer/Reader角色并实测推送。

3. 创建独立证书Vault：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-vault --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-vault --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_CERTIFICATE_VAULT_PLAN_SHA256"
```

4. 建立Runner到AKS API和ACR的Private DNS链接：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-target-connectivity --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-target-connectivity --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_TARGET_CONNECTIVITY_PLAN_SHA256"
```

5. 验证模式只需挂载operator UAMI；deploy/runtime两个逻辑profile都会回退到它。运行只读私网检查：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step stage4-target-check
```

通过条件：AKS API和ACR域名只解析到已发现的Private Endpoint IP、TLS命中相同私网IP、runtime身份能读取新AKS Deployment。失败时修DNS/路由/NSG或RBAC，不开放AKS/ACR公网。

AKS私有API证书由集群CA签发，检查器会从本次生成的kubeconfig提取当前context的`certificate-authority-data`，写入0600临时文件并仅给AKS的curl使用；ACR仍使用系统CA。若看到curl 60，先确认Runner已拉取包含该逻辑的版本，不使用`-k`、不关闭证书校验，也不把集群CA永久加入系统信任库。执行器退出时会删除kubeconfig和临时`aks-ca.crt`。

6. 创建三个namespace并接入Container Insights：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-cluster-bootstrap --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-cluster-bootstrap --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_CLUSTER_BOOTSTRAP_PLAN_SHA256"

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-monitoring-onboard --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-monitoring-onboard --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_MONITORING_PLAN_SHA256"
```

7. 按第4.2节准备Cosign口令，构建、扫描、签名并推送后端镜像：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-promote-backend-image --operation execute
unset COSIGN_PASSWORD
```

命令成功后，从本Runner的受控输出中选择**当前Git revision、当前environment、Azure后台runtime、签名已回验**的最新产物。不要使用旧revision的成功镜像，也不要从ACR tag猜digest：

```bash
CURRENT_REVISION="$(git rev-parse HEAD)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"
ACR_NAME="$(jq -er '.parameters.platform.containerRegistryName' \
  local_execution/customer.json)"
BACKEND_IMAGE_PREFIX="${ACR_NAME}.azurecr.io/litellm-azure@sha256:"

BACKEND_IMAGE_SUMMARY=
while IFS= read -r candidate; do
  if jq -e --arg revision "$CURRENT_REVISION" \
      --arg environment "$ENVIRONMENT_NAME" \
      --arg prefix "$BACKEND_IMAGE_PREFIX" \
      '.revision == $revision
       and .environment == $environment
       and .runtime == "azure"
       and .signatureVerified == true
       and (.image | type == "string"
            and startswith($prefix)
            and test("@sha256:[0-9a-f]{64}$"))' \
      "$candidate" >/dev/null; then
    BACKEND_IMAGE_SUMMARY="$candidate"
    break
  fi
done < <(
  find temp/local-stage09 -mindepth 2 -maxdepth 2 -type f \
    -path '*-stage4-promote-backend-image-*/target-image-summary.json' \
    -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-
)

if [[ -z "$BACKEND_IMAGE_SUMMARY" ]]; then
  echo 'No verified backend image for the current revision/environment' >&2
  false
else
  BACKEND_IMAGE="$(jq -er '.image' "$BACKEND_IMAGE_SUMMARY")"
  BACKEND_DIGEST="${BACKEND_IMAGE##*@sha256:}"
  [[ "$BACKEND_DIGEST" =~ ^[0-9a-f]{64}$ ]]
  printf 'Backend image: %s\nStage6 digest value: %s\n' \
    "$BACKEND_IMAGE" "$BACKEND_DIGEST"
fi
```

`Backend image`是完整不可变引用；`Stage6 digest value`输出的纯64位十六进制值才填写`REPLACE_BUILT_64_HEX_DIGEST`，不包含`sha256:`。镜像推送成功但扫描、签名或上述revision/environment核对失败时不得使用。

8. 准备入口证书。客户已提供API/Admin两套`.pem`和`.key`时，按[客户证书导入Runbook](../docs/litellm-ingress-certificate-import-runbook-zh.md)核对目标Vault、合并并上传两个Secret；网络窗口及无证书时的申请说明按[Stage4证书导入步骤](../docs/customer-migration-guide-zh.md#4-c-部署后手动导入两个secret)上传`api-tls`和`admin-tls`。Stage9的Admin源站不得继续使用私网演练期间的自签名/企业CA证书，也不能靠给Runner或Laptop安装该CA放行；须先轮换为根位于Microsoft Trusted CA List中的公有链。入口计划使用独立`certifi`公有根包预检，不继承Runner本地附加CA。选择自动API证书时，先给当前客户账号或operator UAMI授予公共DNS zone的DNS Zone Contributor和证书Vault的Key Vault Secrets Officer，再执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-renew --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-renew --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_CERTIFICATE_PLAN_SHA256"
```

9. 发布API/admin两套私有入口：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-private-ingress --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-private-ingress --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_PRIVATE_INGRESS_PLAN_SHA256"
```

只有Service事件明确显示AKS控制面缺目标VNet权限时，才额外执行下面的plan和execute；正常路径不要重复授权：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-aks-ingress-role --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-aks-ingress-role --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_AKS_INGRESS_ROLE_PLAN_SHA256"
```

10. Runner VM验证资源和工作负载：

```bash
set -euo pipefail
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' local_execution/customer.json)"
TARGET_RG="$(jq -er '.target.resourceGroup' local_execution/customer.json)"
TARGET_AKS="$(jq -er '.parameters.platform.stage4Aks.name' local_execution/customer.json)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"
VERIFY_KUBECONFIG="$(mktemp)"
trap 'rm -f "$VERIFY_KUBECONFIG"' EXIT
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" get namespace \
  litellm llm-api-ingress llm-admin-ingress
kubectl --kubeconfig "$VERIFY_KUBECONFIG" get pods,service \
  -n llm-api-ingress
kubectl --kubeconfig "$VERIFY_KUBECONFIG" get pods,service \
  -n llm-admin-ingress
```

通过条件：三个namespace存在；两套入口Pod Ready、内部LoadBalancer有不同私网IP；证书域名/指纹正确；错误Host和未批准来源被拒绝；AKS、ACR和证书Vault公网均关闭。此时仍不切业务流量。

#### 私有AKS自动停机后的启动复核

绑定、rollout和入口检查期间保持AKS为`Running/Succeeded`，不要在操作尚未完成时自动停机。集群停止时私有API的DNS A记录可能不存在；这是先核对电源状态的信号，不是临时开放公网、添加公网DNS或改用admin凭据的理由。

客户Policy夜间执行AKS stop/start时，通常不需要重跑Stage3–5部署。Private Link模式的AKS会在启动时重建由AKS管理的API Server Private Endpoint，其私网IP可能变化；用户另行创建且目标指向该AKS的Private Endpoint则不由AKS恢复，需要网络Owner删除后重建。ACR、证书Vault、PostgreSQL和Redis各自的Private Endpoint不属于“目标指向AKS”的PE，不因该提示删除。

每次启动后先等待管理面完全恢复；`power=Running`但`state=Starting`仍不能继续：

```bash
set -euo pipefail
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' local_execution/customer.json)"
TARGET_RG="$(jq -er '.target.resourceGroup' local_execution/customer.json)"
TARGET_AKS="$(jq -er '.parameters.platform.stage4Aks.name' local_execution/customer.json)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"

az aks wait --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --updated --interval 30 --timeout 1800
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" \
  --query '{state:provisioningState,power:powerState.code,privateFqdn:privateFqdn}' \
  --output json
```

只有`state=Succeeded`且`power=Running`时继续。先运行只读检查，它会重新发现本次API PE地址，验证Runner DNS、TLS、ACR和实际Kubernetes读取，不依赖停机前IP：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step stage4-target-check
```

然后重新取得临时kubeconfig，确认节点、系统Pod和两套入口恢复，并把当前入口IP与Stage4回执比较：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
trap 'rm -f "$VERIFY_KUBECONFIG"' EXIT
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" wait --for=condition=Ready nodes --all --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n kube-system get pods
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-api-ingress rollout status \
  deployment/llm-api-ingress --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-admin-ingress rollout status \
  deployment/llm-admin-ingress --timeout=15m

EXPECTED_API_IP="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query properties.outputs.privateIngress.value.api.privateIpAddress --output tsv)"
EXPECTED_ADMIN_IP="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query properties.outputs.privateIngress.value.admin.privateIpAddress --output tsv)"
ACTUAL_API_IP="$(kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-api-ingress \
  get service llm-api-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
ACTUAL_ADMIN_IP="$(kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-admin-ingress \
  get service llm-admin-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
printf 'api expected=%s actual=%s\nadmin expected=%s actual=%s\n' \
  "$EXPECTED_API_IP" "$ACTUAL_API_IP" "$EXPECTED_ADMIN_IP" "$ACTUAL_ADMIN_IP"
[[ -n "$ACTUAL_API_IP" && -n "$ACTUAL_ADMIN_IP" && \
   "$ACTUAL_API_IP" == "$EXPECTED_API_IP" && \
   "$ACTUAL_ADMIN_IP" == "$EXPECTED_ADMIN_IP" ]]
```

处理边界：

- `stage4-target-check`通过且入口IP一致、rollout Ready：不重跑platform、证书Vault、target-connectivity、cluster-bootstrap、monitoring、private-ingress或Stage5。
- `stage4-target-check`显示Runner VNet的AKS Private DNS链接缺失时：重新执行`stage4-target-connectivity`的plan，审核后仅在计划显示修复该链接时用新哈希execute；不重跑Stage4 platform。
- 实际API PE NIC与AKS私有区域A记录长期不一致时：等待AKS托管资源协调，仍不一致则交Azure/网络Owner处理。`stage4-target-connectivity`不拥有或改写AKS托管PE/A记录，不能靠反复execute修复。
- 入口Pod/Service未恢复或入口IP与回执不同：先查看Service事件；控制面授权错误才执行`stage4-aks-ingress-role`。随后为当前状态重新plan/execute `stage4-private-ingress`，让TLS在线验证和ARM回执绑定新地址，不复用停机前哈希。
- 查询到用户自建且目标为该AKS的Private Endpoint时，停止自动恢复，由网络Owner按客户变更流程删除并重建该PE；不要删除AKS管理的API PE，也不要误删ACR/Vault/PG/Redis PE。
- 已进入Stage9时，还要复核Private Link Service、Front Door origin和实际客户端；入口前端地址或资源映射变化时重新plan受影响的Stage9步骤，不直接启流量。

### Stage5：PostgreSQL、Redis、后台秘密与数据库初始化

**客户模式按步骤使用IT部署账号、PG管理员和migration/runtime身份；验证模式除可选的独立database UAMI外继续使用operator。** Stage5开始前：

- PG管理员使用安全组时，把获批执行主体加入组，`stage5Data`填写组Object ID、组名和`Group`；没有组管理权限时可选择独立database UAMI路径。
- `databaseAccess.migrationPrincipalId`填operator UAMI Principal ID，不能填PG管理员组ID。
- migration模式的operator还需Stage0备份容器Blob Data Contributor及旧AKS `litellm-env` Secret读取权；greenfield不需要这些旧环境权限。
- migration模式的`runtimeInputs.backupBlob`和`backupSha256`来自同一个成功Stage0报告；greenfield配置不得包含这两项。
- deploy仍有目标RG Contributor、Lock Writer和受约束角色分配权限。

两种模式共用platform、database-roles、backend-secrets和schema-migrate。仅migration执行`stage5-restore-target`；greenfield的backend-secrets计划必须显示新Master Key/Salt为`generate`，且目标库在生成秘密前必须没有业务表。

验证模式中，`REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID`填写operator UAMI的Principal ID；两个`REPLACE_PG_ADMIN_GROUP_*`字段必须填写另一个真实Entra安全组的显示名和Object ID，不能重复填写UAMI名称/Principal ID。这个组不是另一个UAMI：它没有Client ID、凭据或VM挂载，只把现有operator列为成员。不能用Azure RBAC角色分配代替Entra组成员关系，也不能把同一个operator Principal ID直接同时配置成PG管理员和`migrationPrincipalId`；执行器会阻止该权限合并。若客户尚无批准的PG管理员组，由Entra组管理员在客户外部管理终端创建专用安全组并加入operator服务主体：

```bash
PG_ADMIN_GROUP_NAME="llmgw-test-pg-admins"
OPERATOR_OBJECT_ID="REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID"

PG_ADMIN_GROUP_OBJECT_ID="$(az ad group create \
  --display-name "$PG_ADMIN_GROUP_NAME" \
  --mail-nickname "$PG_ADMIN_GROUP_NAME" \
  --query id --output tsv)"
az ad group member add --group "$PG_ADMIN_GROUP_OBJECT_ID" \
  --member-id "$OPERATOR_OBJECT_ID"

az ad group show --group "$PG_ADMIN_GROUP_OBJECT_ID" \
  --query '{name:displayName,objectId:id,securityEnabled:securityEnabled}' --output json
az ad group member check --group "$PG_ADMIN_GROUP_OBJECT_ID" \
  --member-id "$OPERATOR_OBJECT_ID" --output json
```

已有批准组时只查询并复用，不重复运行`group create`；先核对`securityEnabled=true`和成员检查`value=true`。组Object ID填`REPLACE_PG_ADMIN_GROUP_OBJECT_ID`，显示名填`REPLACE_PG_ADMIN_GROUP_NAME`。新组成员关系及托管身份Token可能延迟生效，以实际PG Entra登录为准；失败时等待传播并重新登录，不把PG管理员改成migration UAMI绕过分离检查。

没有组管理权限但已有独立database UAMI时，外部管理员把该UAMI挂载到Runner，并确认它在目标RG具有Reader或等效读取权限。该选项固定使用`ServicePrincipal`作为PG管理员，并只让`stage5-database-roles`使用`authentication.database`；platform和后续secrets/restore/schema仍使用operator。database UAMI与`databaseAccess.migrationPrincipalId`必须是两个不同Principal ID。

选择此路径后，先带option重新生成required文件；不能沿用之前未带option生成、仍包含两个`REPLACE_PG_ADMIN_GROUP_*`字段的文件：

```bash
STAGE5_DISCOVERY="$(
  .venv/bin/python -m local_execution.merge_config \
    --config local_execution/customer.json --stage 5 --operation plan \
    --option database-admin-identity
)"
printf '%s\n' "$STAGE5_DISCOVERY" | jq .
STAGE5_REQUIRED_VALUES="$(jq -er '.requiredValues' <<<"$STAGE5_DISCOVERY")"
cp "$STAGE5_REQUIRED_VALUES" local_execution/stage-5-values.local.json
chmod 600 local_execution/stage-5-values.local.json
```

新文件应包含`REPLACE_DATABASE_ADMIN_UAMI_NAME`、`REPLACE_DATABASE_ADMIN_UAMI_CLIENT_ID`和`REPLACE_DATABASE_ADMIN_UAMI_PRINCIPAL_ID`，且不再包含两个组占位符。填写备份引用、operator Principal ID、database UAMI三项标识和PG SKU后，执行完整的预览及应用命令：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation plan \
  --values local_execution/stage-5-values.local.json \
  --option database-admin-identity
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation apply \
  --values local_execution/stage-5-values.local.json \
  --option database-admin-identity
```

使用PG管理员组的默认路径才运行以下不带option的命令：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation plan \
  --values local_execution/stage-5-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation apply \
  --values local_execution/stage-5-values.local.json
```

1. 客户模式使用当前Azure部署账号，验证模式使用operator，创建Stage5私有数据资源：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-platform --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-platform --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_PLATFORM_PLAN_SHA256"
```

该步骤创建/配置私有Entra-only PostgreSQL、Managed Redis、后台Key Vault、Private Endpoint/DNS、诊断设置和应用Workload Identity；不会恢复数据库。

Stage5数据面操作从Runner发起，因此PG和Redis私有DNS区域还必须链接Runner VNet；VNet Peering不会传递Private DNS区域可见性。平台成功后先取得部署输出中的主机名并核对解析，结果必须只包含对应PE私网IPv4：

```bash
PLATFORM_OUTPUT="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s5-platform" \
  --query properties.outputs.platform.value --output json)"
PG_HOST="$(jq -er '.postgresqlServerName + ".postgres.database.azure.com"' \
  <<<"$PLATFORM_OUTPUT")"
REDIS_HOST="$(jq -er '.managedRedisHostName' <<<"$PLATFORM_OUTPUT")"
getent ahostsv4 "$PG_HOST"
getent ahostsv4 "$REDIS_HOST"
```

若`stage5-database-roles --operation plan`在`database_roles.py`返回泛化的数据库错误，先查本次`azure-auth.json`确认profile为`database`、method为`managed-identity`，再执行上述DNS检查。PG Entra管理员已正确但PG主机解析为公网IP或无法解析时，不要改管理员、开放PG公网或使用`PGHOSTADDR`绕过DNS。更新到包含Stage5 Runner DNS链接的版本，重新plan/execute `stage5-platform`；审核计划应新增PG和Redis区域到批准Runner VNet的链接，不删除或重建数据资源。平台成功后重新解析，再重新运行`stage5-database-roles` plan；不得复用旧哈希。

若错误明确为`SSL error: certificate verify failed`，先用`openssl s_client -starttls postgres -connect "$PG_HOST:5432" -servername "$PG_HOST" -verify_return_error`核对服务端链。仓库固定的psycopg binary/libpq组合不得使用模糊的`sslrootcert=system`；本地Stage5数据库操作使用Ubuntu明确CA bundle`/etc/ssl/certs/ca-certificates.crt`并保持`verify-full`。不要改成`sslmode=require`、关闭主机名校验或下载临时CA替代系统信任库。

若TLS修复后plan仍返回`PostgreSQL role plan could not inspect the target database`，确认Runner已更新到包含Azure PG角色契约修复的版本。PG16的`pgaadauth_list_principals(false)`返回列名`rolname`；ARM创建的空数据库可由`azure_pg_admin`同时持有数据库和`public` schema，这是本流程允许的初始状态。execute会在创建并核验`llmgw_migrator`后把`public` owner转交给该角色，再撤销`PUBLIC`的CREATE权限。不要手工改owner、提前创建同名角色或重建数据库；更新代码后重新plan并审核`before.schema`和计划中的owner转交，不得复用旧哈希。

若旧版本在`send-managed-redis-to-log-analytics`报`CategoryGroup: 'allLogs' is not supported`，本次Stage5父部署为Failed，不能继续`database-roles`。Azure Managed Redis集群资源只提供`AllMetrics`，`default`数据库子资源提供`ConnectionEvents`日志；旧模板把集群误配为`allLogs`。失败时Redis集群/数据库及其他Stage5资源可能已经成功创建，但集群诊断设置整项未创建，因此缺少送往Log Analytics的Redis集群指标。不要删除这些部分成功资源，也不要手工把父部署改成成功。更新到包含“集群`AllMetrics`、数据库`ConnectionEvents`”修复的版本后，重新运行`stage5-platform --operation plan`，审核当前实际状态下的全部增量，再用新plan哈希execute；不得复用失败前的plan哈希。成功后确认父部署为Succeeded，并分别回读两级诊断设置。

2. 创建`llmgw_migrator`、`llmgw_app`及DDL/DML边界。身份取决于前面选择的管理员路径：

- PG管理员组路径：当前Azure CLI账号必须是该组成员；客户IT管理员可保持交互登录，验证operator必须已加入组。
- `database-admin-identity`路径：无需手工切换账号；本地执行器在本步骤前自动执行`az login --identity --client-id <authentication.database.clientId>`，使用独立database UAMI取得PG Token。

客户IT账号在目标RG拥有Azure `Owner`或`Contributor`只代表管理面部署权限，不会自动成为PostgreSQL Entra管理员、数据库owner或schema owner。当前自动化把业务对象归`llmgw_migrator`所有；需要IT人员直接进入PG时，须另行批准并把该用户配置为额外Entra管理员，或使用包含该用户的PG管理员组，不能用Azure RBAC代替数据库数据面授权。

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-database-roles --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-database-roles --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_DATABASE_ROLES_PLAN_SHA256"
```

3. 在此处使用`databaseAccess.migrationPrincipalId`对应的runtime服务身份；验证模式继续使用operator。客户若在`localExecution.authentication.runtime`配置了managed identity，执行器会自动登录；未配置专项runtime profile、而`default=existing`时，必须在运行本步骤前切换到已挂载的runtime UAMI。

greenfield不读取旧AKS：确认`runtimeInputs`没有备份和旧Salt字段，然后直接运行本步骤末尾的`stage5-backend-secrets` plan/execute。计划中缺失的`litellm-master-key`和`litellm-salt-key`必须为`generate`，`source`必须为`null`。

**以下登录、旧Secret检查和`legacy-master-key-salt`兼容说明仅适用于migration。** migration从database UAMI或IT用户会话切换到migration/runtime UAMI：

```bash
az logout
az account clear
az login --identity --client-id "REPLACE_MIGRATION_RUNTIME_UAMI_CLIENT_ID" --output none
az account set --subscription "$SUBSCRIPTION_ID"
```

该身份从旧`litellm-env`读取原Master Key/Salt并写入新后台Vault。执行前先确认同一身份能读取旧Secret，但不要打印内容：

```bash
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' local_execution/customer.json)"
LEGACY_RG="$(jq -er '.legacy.resourceGroup' local_execution/customer.json)"
LEGACY_AKS="$(jq -er '.legacy.aksClusterName' local_execution/customer.json)"
LEGACY_NAMESPACE="$(jq -er '.legacy.namespace' local_execution/customer.json)"
LEGACY_KUBECONFIG_DIR="$(mktemp -d "$PWD/temp/legacy-runtime-kubeconfig.XXXXXX")"
chmod 700 "$LEGACY_KUBECONFIG_DIR"
LEGACY_RUNTIME_KUBECONFIG="$LEGACY_KUBECONFIG_DIR/config"
trap 'rm -rf "$LEGACY_KUBECONFIG_DIR"' EXIT

az aks get-credentials --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --file "$LEGACY_RUNTIME_KUBECONFIG"
chmod 600 "$LEGACY_RUNTIME_KUBECONFIG"
kubelogin convert-kubeconfig --kubeconfig "$LEGACY_RUNTIME_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$LEGACY_RUNTIME_KUBECONFIG" -n "$LEGACY_NAMESPACE" \
  auth can-i get secret/litellm-env
kubectl --kubeconfig "$LEGACY_RUNTIME_KUBECONFIG" -n "$LEGACY_NAMESPACE" \
  get secret litellm-env -o json \
  | jq '{hasMaster:(.data|has("LITELLM_MASTER_KEY")),hasSalt:(.data|has("LITELLM_SALT_KEY"))}'
```

若`auth can-i`不是`yes`，由旧AKS管理员按[主手册Stage5身份与旧Secret读取步骤](../docs/customer-migration-guide-zh.md#5-a-stage5开始前由客户人工准备的身份与权限)中第5项授予精确读取权；不要把Secret正文复制到customer.json或终端日志。

若旧Secret没有`LITELLM_SALT_KEY`，不要修改旧Secret或生成随机Salt。本项目已批准路径A的环境把`localExecution.runtimeInputs.legacySaltSource`设置为`master-key`；执行器只在确认旧Salt键确实不存在后，才在内存中复用旧Master Key并把该非秘密来源写入plan。其他客户必须先完成自己的兼容性验证与批准；默认`secret`会继续拒绝缺失Salt。

本项目环境重新合并Stage5时，在原有命令上同时追加`--option legacy-master-key-salt`；使用独立database UAMI时两个选项都要保留：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation plan \
  --values local_execution/stage-5-values.local.json \
  --option database-admin-identity --option legacy-master-key-salt
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation apply \
  --values local_execution/stage-5-values.local.json \
  --option database-admin-identity --option legacy-master-key-salt
```

当前`customer.json`已经完整应用过相同Stage5身份路径时，合并器会按现有字段复用PG SKU、迁移Principal、备份引用及database UAMI标识，因此上述命令不需要再次填写这些值。plan的`missingPlaceholders`应为空，`changedPaths`应只有`localExecution.runtimeInputs.legacySaltSource`；若仍出现其他占位符或变更路径，停止apply并核对所选option是否与现有管理员类型一致，不复制空的`values.required.json`覆盖原values文件。

migration确认`customer.json`中的`legacySaltSource`为获批值；greenfield确认四个迁移运行输入均不存在。然后生成backend-secrets计划；execute必须使用本次新计划哈希：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-backend-secrets --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-backend-secrets --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_BACKEND_SECRETS_PLAN_SHA256"
```

4. **仅migration**保持上述迁移服务身份登录，下载Stage0备份、核对SHA256，并只恢复到绑定的空目标库。greenfield跳过整个`stage5-restore-target`步骤：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-restore-target --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-restore-target --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_RESTORE_TARGET_PLAN_SHA256"
```

目标库非空时脚本会拒绝覆盖。不得手工加`--clean`、DROP数据库或改连任意DATABASE_URL来强行重跑。

5. 两种模式都保持runtime服务身份登录，执行固定Schema迁移并保存回执：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-schema-migrate --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-schema-migrate --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_SCHEMA_MIGRATE_PLAN_SHA256"
```

恢复成功后，长期升级形成的旧Prisma历史可能与“全新安装同版本”不同：迁移名称仍相同，但旧库保留当年执行时的checksum和顺序。迁移器只接受代码中已通过固定旧镜像、恢复副本和隔离升级验证的精确源历史指纹；匹配后只执行目标镜像中尚未出现的迁移，原141条历史不改写。若plan仍报`Database migration history differs from the approved image`，不得跳过plan、直接execute、运行`prisma migrate resolve/reset`或手工更新`_prisma_migrations`；保存只读的名称/checksum/完成状态供实施负责人新增受审查的兼容指纹，并重新完成隔离升级测试。

greenfield在空库上执行当前受审镜像的完整迁移，不应出现旧Prisma历史兼容分支；plan必须显示目标库状态与全新初始化一致。

schema plan绑定稳定的运行镜像内容指纹（RootFS层、完整镜像配置、固定源digest、构建输入和运行时代码），实际本地Docker image ID只记录在本次观察/执行回执中。相同内容因Docker创建时间元数据产生不同image ID时不应让plan漂移；RootFS、配置、迁移资产或数据库state任一变化仍会改变plan哈希并阻止execute。

6. Runner VM核验Stage5部署输出及公网关闭状态：

```bash
ENVIRONMENT_NAME="test"
az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s5-platform" \
  --query '{state:properties.provisioningState,platform:properties.outputs.platform.value}' \
  --output json

PG_NAME="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s5-platform" \
  --query properties.outputs.platform.value.postgresqlServerName --output tsv)"
az postgres flexible-server show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "$PG_NAME" \
  --query '{state:state,host:fullyQualifiedDomainName,auth:authConfig,public:network.publicNetworkAccess}' \
  --output json
```

通过条件：部署Succeeded；PG为Ready、`activeDirectoryAuth=Enabled`、`passwordAuth=Disabled`、公网Disabled；恢复和Schema摘要均`applied=true`；新Vault中两份秘密存在且版本固定，但日志中没有秘密值；Redis和应用身份授权与plan一致。完成受控解密、Redis Token和CSI轮换实测后再进入Stage6。

### Stage6：发布新LiteLLM后端

**客户模式使用当前登录账号；验证模式使用operator UAMI。** 它已获得新AKS Cluster User/Cluster Admin；Stage5数据库、秘密和Schema回执必须属于当前代码和配置。

1. 按Stage4第7步的提取命令取得当前revision的`BACKEND_DIGEST`，将其纯64位值填入Stage6 values文件的`REPLACE_BUILT_64_HEX_DIGEST`；不要填写完整镜像引用、`sha256:`前缀、tag、schema plan哈希或SBOM哈希。`REPLACE_EXISTING_MODEL_DEPLOYMENT_NAME`填写`connectionAlias=primary`对应Azure OpenAI账号中实际Succeeded的deployment资源名称；`REPLACE_EXISTING_MODEL_BASE_MODEL`填写同一deployment的底层模型名（例如`gpt-6-luna`，values中不要加`azure/`，合并器会生成`baseModel=azure/gpt-6-luna`）。可从Portal的模型部署页取得，或运行：

```bash
az cognitiveservices account deployment list \
  --subscription "REPLACE_MODEL_SUBSCRIPTION_ID" \
  --resource-group "REPLACE_MODEL_RESOURCE_GROUP" \
  --name "REPLACE_MODEL_ACCOUNT_NAME" \
  --query '[].{deployment:name,model:properties.model.name,version:properties.model.version,state:properties.provisioningState}' \
  --output table
```

只有`state=Succeeded`的同一行可用于这两个字段。新Stage6分阶段合并要求`baseModel`；升级前已经完整合并的旧配置可暂时省略以保持读取兼容，但在启用GPT-6或下一次重合并前必须补齐。需要在新shell继续时，重新运行Stage4提取块从证据恢复变量，然后合并：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 6 --operation plan \
  --values local_execution/stage-6-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 6 --operation apply \
  --values local_execution/stage-6-values.local.json
```

选择原生认证时，保持`localExecution.features.entraMode=deferred`，在Stage6 values文件同时填写`REPLACE_NATIVE_ADMIN_USERNAME`（例如`gateway-admin`），并给两条命令都追加`--option native-auth`。该用户名不是秘密。apply后、运行Stage6 application前，重新plan/execute `stage5-backend-secrets`：计划中原Master/Salt必须为`keep`，仅新增`litellm-ui-password=generate`。execute生成随机密码、固定版本并刷新Stage5回执，不输出正文；若计划尝试导入/替换Master或Salt就停止。随后还要按当前revision刷新`stage5-schema-migrate`回执，确认`pendingMigrations=0`。

由客户Key Vault管理员在Portal中打开后台Vault的`litellm-ui-password`当前版本，直接转存到客户批准的密码库，不把值复制到终端、工单、聊天、values或customer.json。Master Key继续不向任何人工用户或API调用方分发。当前自动化不提供UI密码轮换步骤；轮换须另立变更并重新绑定固定Secret版本。
2. native路径先为当前配置重新plan/execute `stage4-private-ingress`：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-private-ingress --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-private-ingress --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_PRIVATE_INGRESS_PLAN_SHA256"
```

新计划必须把两套Traefik上游改为`litellm.litellm.svc.cluster.local:4000`；API只允许六个固定POST推理路径（`/responses`、`/chat/completions`、`/embeddings`及其`/v1`形式）和私有健康检查`GET /readyz`，**不允许`/v1/models`**；admin保留私网UI。此时后端尚未发布，execute只验证TLS、双平面Host隔离、私有LB和源地址策略，并记录`backendRoutesVerified=false`。不得手工删除代理、改NetworkPolicy或复用旧Stage4哈希。Entra路径无需在此重复Stage4。

3. 计划并发布后端：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage6-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage6-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE6_APPLICATION_PLAN_SHA256"
```

该步骤再次用本地Cosign公钥验证镜像签名，生成无密码PG/Redis/CSI配置，发布双副本、PDB、HPA和NetworkPolicy；不切流量。Entra路径此时仍等待Stage7代理；native路径在LiteLLM rollout完成后在线验证API健康、API拒绝admin登录路径、admin登录页可达，并把`backendRoutesVerified=true`及当前Stage4回执哈希保存为Stage6 Azure回执。任一路由检查失败都使Stage6 execute失败。

4. Runner VM验证：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/litellm --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get deployment litellm \
  -o 'custom-columns=READY:.status.readyReplicas,DESIRED:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image'
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get pdb,hpa,networkpolicy
rm -f "$VERIFY_KUBECONFIG"
```

通过条件：Ready副本等于Desired且至少2个；镜像是批准digest；Pod重建后会话/Redis行为正常；应用身份只能DML不能DDL；新版没有连接旧PG。

5. native路径从批准私网浏览`https://llm-admin.<baseDomain>/fallback/login`，使用Stage6配置的用户名和客户密码库中的独立UI密码登录。登录后创建限定模型、预算和有效期的virtual key；API调用只使用该virtual key。客户端API base为`https://<api-domain>/v1`，请求`model`必须使用LiteLLM `model_name`别名（本配置的`modelGroup`），不是Azure deployment name；下例`coding`须替换成客户实际批准别名。Codex使用固定Responses路径，不能依赖被拒绝的`/v1/models`自动发现：

```bash
BASE_DOMAIN="$(jq -er '.baseDomain' local_execution/customer.json)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"
API_PRIVATE_IP="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query properties.outputs.privateIngress.value.api.privateIpAddress --output tsv)"
read -r -s -p 'LiteLLM virtual key: ' LITELLM_VIRTUAL_KEY
printf '\n'
curl --fail-with-body --silent --show-error \
  --resolve "llm-api.${BASE_DOMAIN}:443:${API_PRIVATE_IP}" \
  --header "Authorization: Bearer $LITELLM_VIRTUAL_KEY" \
  --header 'Content-Type: application/json' \
  --data '{"model":"coding","messages":[{"role":"user","content":"connectivity test"}]}' \
  "https://llm-api.${BASE_DOMAIN}/v1/chat/completions"
unset LITELLM_VIRTUAL_KEY
```

通过条件：错误密码拒绝；Stage9发布前admin UI只从批准私网可达，发布后日常入口仅经独立Front Door和精确WAF批准来源；virtual key正常调用且不能创建其他Key；缺Key、错误Key、管理路径和非批准API路径均拒绝。native路径随后跳过整个Stage7，直接进入Stage8；Entra路径继续下一节。当前用户确认的登录/Responses结果仅覆盖其实际客户端与请求，不替代全部协议或生产验收。

## 6. Stage7启用或延期

`application.authentication.mode=native`时本节不适用，执行器会拒绝所有Stage7步骤；不要构建代理镜像或创建Entra UAMI来“占位”。

若保持`entraMode=deferred`，在Stage6停止。可以提前执行`stage7-promote-proxy-image`准备镜像，但不能部署代理或发布后续流量。

客户批准Stage7后，将`entraMode`改为`enabled`。Free租户还要确认Security Defaults、管理员MFA和实际客户端现代OAuth兼容性；客户明确要求按应用/组/位置细分策略时，先取得Entra ID P1，不以关闭MFA换取测试成功。

**客户登录账号或operator负责deploy/runtime；Stage7额外使用entra-bootstrap和entra-access。** 开始前确认：

- 两类Entra UAMI已按第2.5节获得Graph应用权限和Admin consent。
- 验证operator仍有目标RG临时Owner；两个Entra身份在目标RG有Contributor用于保存回执。
- 客户最小权限模式下，runtime、entra-bootstrap和entra-access使用`LLMGW Runtime Receipt Writer`，deploy保留Lock Writer和受约束角色分配权限。
- Stage6后端Ready，API/admin证书和私有入口仍正常。

1. deploy身份构建、扫描、签名并推送代理镜像：

```bash
read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
printf '\n'
export COSIGN_PASSWORD
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-promote-proxy-image --operation execute
unset COSIGN_PASSWORD
```

保存输出完整digest，在Stage7 values文件填写代理digest、真实调用客户端Application Client ID、API用户/服务主体Object ID、管理员用户Object ID及两个Entra身份，然后合并：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 7 --operation plan \
  --values local_execution/stage-7-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 7 --operation apply \
  --values local_execution/stage-7-values.local.json
```

2. deploy身份创建API/admin代理Workload Identity、独立Vault、Private Endpoint/DNS和最小Vault角色：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-foundation --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-foundation --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_PROXY_FOUNDATION_PLAN_SHA256"
```

3. entra-bootstrap身份创建本方案自有的API/admin应用及service principal，不授予用户访问：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-apps --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-apps --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ENTRA_APPS_PLAN_SHA256"
```

4. entra-access身份只创建配置中声明的app role assignment和逐用户`llm.invoke`委托授权：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-access --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-access --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ENTRA_ACCESS_PLAN_SHA256"
```

5. entra-bootstrap身份创建admin OIDC凭据并立即写入私有admin Vault；秘密正文不进入输出：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-admin-credentials --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-admin-credentials --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_CREDENTIALS_PLAN_SHA256"
```

6. runtime身份通过本机loopback到AKS后台初始化配置中声明的管理用户和Key，不创建普通API客户端vkey：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-credentials --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-credentials --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_PROXY_CREDENTIALS_PLAN_SHA256"
```

7. runtime身份再次验证代理镜像签名，发布API/admin代理和更新后的后端网络策略：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_APPLICATION_PLAN_SHA256"
```

8. Runner VM验证：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/llm-api-proxy --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/llm-admin-proxy --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get deployment \
  litellm llm-api-proxy llm-admin-proxy
rm -f "$VERIFY_KUBECONFIG"
```

再用客户批准的真实客户端做正反向测试：正确tenant+Token+vkey成功；缺Token、错误tenant、未授权主体、缺vkey、越权模型和预算超限均拒绝；admin域名只能从批准私网访问。Free租户无法提供的细粒度Conditional Access不能写成已通过。

下列维护动作不属于首次部署。只有对应变更获批时才执行；每个动作的execute哈希都来自它自己的plan，不能互换：

```bash
# 撤销配置中已disabled主体的授权
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-entra-revoke --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-entra-revoke --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ENTRA_REVOKE_PLAN_SHA256"

# 轮换admin OIDC应用密码
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-rotate --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-rotate --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_ROTATE_PLAN_SHA256"

# 清理Graph成功但Vault未记录的孤立密码
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-recover --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-recover --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_RECOVER_PLAN_SHA256"

# 轮换admin会话密钥；会使现有会话失效，需另行安排代理rollout
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-session-rotate --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-session-rotate --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_SESSION_ROTATE_PLAN_SHA256"

# 退役已过期且非当前的Graph密码
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-retire --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-retire --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_RETIRE_PLAN_SHA256"
```

Graph成功但Vault失败时先运行recover检查孤立凭据，不盲目重跑初始化。轮换后必须重新plan/execute `stage7-application`，让固定Secret版本和Pod实际使用新值。

## 7. Stage8

一期默认使用原生Spend Logs。**执行身份：deploy负责可选观测基础设施和镜像，runtime负责应用发布。** Entra路径必须先完成Stage7；native路径显式跳过Stage7后可直接发布Stage8。普通`entraMode=deferred`且未选择native-auth时仍会被阻止。

### 7.1 原生Spend Logs路径

1. Stage2已经配置`contentAudit`。native认证路径只支持该原生Spend Logs方案，不支持`--option observability`或增强L3，直接跳到第3步。Entra路径不启用独立collector时也直接跳到第3步。
2. 启用collector时，deploy身份导入固定digest、扫描并签名，再部署私有Application Insights/AMPLS相关资源：

```bash
read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
printf '\n'
export COSIGN_PASSWORD
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-promote-collector-image --operation execute
unset COSIGN_PASSWORD
```

将输出完整digest填入Stage8 values文件，用`observability`选项合并，再部署：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation plan \
  --values local_execution/stage-8-values.local.json --option observability
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation apply \
  --values local_execution/stage-8-values.local.json --option observability
```

然后执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-observability --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-observability --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_OBSERVABILITY_PLAN_SHA256"
```

3. runtime身份发布Stage8应用配置；Entra路径再次验证后端、代理及可选collector签名，native路径只验证后端签名并更新其Spend Logs配置：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_APPLICATION_PLAN_SHA256"
```

4. 用批准测试内容发起一次真实请求，在原生UI Logs按时间、用户、Key和request ID核对请求/响应；再确认留存配置。启用collector时额外执行：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/otel-collector --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get deployment otel-collector
rm -f "$VERIFY_KUBECONFIG"
```

通过条件：正常请求写入Spend Logs且正文范围符合政策；native管理员从私网UI可查、API调用方不可访问管理路径；过期清理和恢复边界已记录。Entra路径另验证授权管理员/未授权主体；可选collector仅适用于Entra路径。

### 7.2 增强L3替代路径

只有客户明确批准增强L3时使用。合并器会删除`contentAudit`并加入独立审计Team映射和审计读取者：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation plan \
  --values local_execution/stage-8-values.local.json --option enhanced-l3
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation apply \
  --values local_execution/stage-8-values.local.json --option enhanced-l3
```

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-foundation --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-foundation --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_FOUNDATION_PLAN_SHA256"

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-storage --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-storage --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_STORAGE_PLAN_SHA256"

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_L3_APPLICATION_PLAN_SHA256"
```

`audit-pause → audit-recover → audit-resume`只用于受控恢复演练或事故处理：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-pause --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-pause --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_PAUSE_PLAN_SHA256"

.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-recover --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-recover --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_RECOVER_PLAN_SHA256"

.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-resume --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-resume --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_RESUME_PLAN_SHA256"
```

恢复分页时将上一页非秘密`nextCursor`填入`runtimeInputs.auditRecoveryCursor`后重新plan；未成功resume前禁止再次发布应用或切流。

## 8. Stage9

Stage9分为“禁流量准备”“独立新域名并行canary”和“可选最终迁移/退役”三个阶段。**执行身份：deploy创建双PLS和双endpoint edge，runtime核验API/Admin绑定；只有Azure DNS自动路径才由runtime修改DNS。** Entra路径的`stage9-edge-bind`绑定Stage7两个代理；native路径核验受管API/Admin Traefik的业务路由、健康改写、私有LB和后端目标。普通Entra延期且未选择native-auth时只能准备origin和禁流量edge。

禁流量准备创建API/Admin两条Private Link Service，以及默认Disabled的两个Front Door endpoint/route，不会形成可访问的新网关。Admin custom domain关联固定Prevention的独立WAF，以`SocketAddr`、`IPMatch`、`negateCondition=true`阻断白名单外来源；canary release和两条新域名DNS CNAME发布前仍不作为日常入口。普通`entraMode=deferred`且未选择native-auth时代理尚未部署；native路径可继续完成双平面edge-bind，但在canary release前route同样不承载请求。当前网关已完成canary发布，不要把以下禁流量准备作为已发布入口的普通更新动作。

### 8.1 禁流量准备

1. deploy仍需读取AKS节点RG中的内部LB；保留Stage4窗口的节点RG读取/部署权限。默认合并禁流量prepare，只把`allowTrafficRelease`保持为false并保留客户当前的`entraMode`；使用Azure DNS时追加`--option azure-dns`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation plan \
  --values local_execution/stage-9-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation apply \
  --values local_execution/stage-9-values.local.json
```

两个`privateLinkServiceId`和两组LB名称可保留模板中的`auto`。Stage9 values还必须填写`adminAllowedCidrs`和独立的Front Door Private Link受支持区域；地址是管理员浏览器实际使用路径的稳定公网出口，不是Laptop私网地址。IPv4逐项使用`/32`、IPv6逐项使用`/128`，分别确认两类实际出口并批准，不从公司/VPN名称推断宽CIDR；当前运行环境两类均已配置。Admin Traefik源站证书仍须为Front Door信任的公有链，但不再需要客户端CA Vault、客户端证书或Preview API。
2. 创建两条Private Link Service源站：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-origin --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-origin --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_ORIGIN_PLAN_SHA256"
```

3. 创建默认禁流量的Front Door/edge；API WAF为Detection，Admin WAF固定Prevention：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-prepare --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-prepare --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_PREPARE_PLAN_SHA256"
```

4. S9-04完成后，先由Owner分别审批API/Admin两条Front Door托管Private Endpoint请求，核对请求对应当前profile、正确origin、PLS和不同LB frontend；两条PLS均须恰有一个Approved连接且没有Pending连接。再执行edge-bind：Entra路径只有Stage7完成且`entraMode=enabled`时才绑定API/Admin代理；native路径核验并记录两个Traefik合同；普通deferred路径跳过。本步仍不启流量：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-bind --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-bind --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_BIND_PLAN_SHA256"
```

Front Door控制面在已启用route实际返回业务响应时仍可能长期报告`deploymentStatus=NotStarted`。edge-bind不会仅凭该值放行：当route为`Enabled + NotStarted`时，会解析对应endpoint并以自定义域SNI执行公网HTTPS探测，API必须到达认证拒绝层，Admin必须到达WAF或登录页；兜底404、TLS错误和5xx仍会阻断。`Disabled + NotStarted`仍按禁流量状态处理。

5. 检查双PLS实时连接、两个禁用route、API WAF Detection、Admin WAF Prevention及准确的`adminAllowedCidrs`、两份公有CA源站TLS和错误Host拒绝。canary release前不运行公网业务回归；Entra路径等待代理。native路径的两个业务router已经绑定实际Front Door ID，必须从批准私网分别执行带实际`X-Azure-FDID`的正例和无header的负例；下面保留API示例，Admin用同一方法解析Admin私网IP并请求`/ui/`：

```bash
BASE_DOMAIN="$(jq -er '.baseDomain' local_execution/customer.json)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"
API_PRIVATE_IP="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query properties.outputs.privateIngress.value.api.privateIpAddress --output tsv)"
FRONT_DOOR_ID="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s9-edge" \
  --query properties.outputs.edge.value.profileId --output tsv)"
read -r -s -p 'LiteLLM virtual key: ' LITELLM_VIRTUAL_KEY
printf '\n'
curl --fail-with-body --silent --show-error \
  --resolve "llm-api.${BASE_DOMAIN}:443:${API_PRIVATE_IP}" \
  --header "X-Azure-FDID: ${FRONT_DOOR_ID}" \
  --header "Authorization: Bearer ${LITELLM_VIRTUAL_KEY}" \
  --header 'Content-Type: application/json' \
  --data '{"model":"coding","messages":[{"role":"user","content":"post-bind connectivity test"}]}' \
  "https://llm-api.${BASE_DOMAIN}/v1/chat/completions"
NO_FDID_STATUS="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --resolve "llm-api.${BASE_DOMAIN}:443:${API_PRIVATE_IP}" \
  --header "Authorization: Bearer ${LITELLM_VIRTUAL_KEY}" \
  --header 'Content-Type: application/json' \
  --data '{"model":"coding","messages":[{"role":"user","content":"missing fdid negative test"}]}' \
  "https://llm-api.${BASE_DOMAIN}/v1/chat/completions")"
test "$NO_FDID_STATUS" = 404
unset LITELLM_VIRTUAL_KEY FRONT_DOOR_ID
```

正例必须成功且实际调用批准模型；无header请求必须为404。此时新域名CNAME尚未发布且edge route仍为Disabled，旧系统域名和流量不受影响。

### 8.2 独立新域名并行canary

本节默认客户接受“已验收备份的恢复结果”作为新环境起点。发布前须重新核对用户、Team、Key、预算、模型配置和Schema迁移与该备份基线一致，并记录备份时间点。旧系统继续服务旧域名，新系统使用独立数据库和新域名；两边在备份后产生的Spend Logs、预算计数、Key和管理配置会各自变化，不得宣称实时一致，也不得让两个网关共用同一个数据库。

并行canary不要求旧系统停写、取得最终备份、覆盖当前新库或替换旧域名。只给获批研发配置新API域名，并在新环境单独签发限定模型、预算和有效期的virtual key；不要让同一个key跨两个不同步的预算库使用。若客户以后要求把备份时点之后的旧系统新增数据无损带入新环境，再按第8.3节另行批准最终迁移。

完成canary发布报告和回退方案后，在customer.json设置。Entra路径保持`entraMode=enabled`；native路径保留现有`entraMode=deferred`，执行器依据`application.authentication.mode=native`允许发布：

Entra路径：

```json
"features": {
  "entraMode": "enabled",
  "allowTrafficRelease": true
},
"releaseReportPath": "temp/customer-private/stage9-release.json"
```

native路径：

```json
"features": {
  "entraMode": "deferred",
  "allowTrafficRelease": true
},
"releaseReportPath": "temp/customer-private/stage9-release.json"
```

报告必须符合当前Stage9 release校验器，绑定当前revision、启用发布开关后的Stage9配置哈希、Front Door ID、两条PLS、`adminAllowedCidrs`、两组限流、phase、change ticket和批准人。

`environmentName=dev/test`、`phase=canary`、`authenticationMode=native`且`auditMode=native`时，`checks`可以整个省略；已有草稿中的空值或`passed=false`也不会阻止发布。此路径由release plan实时核对当前revision/config、native后端入口回执、双PLS、源站TLS、Admin WAF/CIDR、Private Link批准及双平面edge-bind，不要求把前序步骤的结果再次人工抄进报告。前序Stage验收和真实试点测试仍要执行，只是不重复作为canary plan输入。

`environmentName=prod`的canary及所有`phase=production`继续使用严格证据索引：`native_virtual_key_acl`、Admin登录、真实协议、数据库恢复、原生Spend Logs/读取/留存恢复、WAF/试点和遥测决策均须引用近7天真实证据；production另需canary SLO、WAF Prevention及DNS切换/回退证据。不能把test canary的简化报告改成production报告复用。

推荐使用合并器打开canary发布开关；若前面使用Azure DNS，此处同时保留`--option azure-dns`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation plan \
  --values local_execution/stage-9-values.local.json --option approved-release
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation apply \
  --values local_execution/stage-9-values.local.json --option approved-release
```

`approved-release`打开本地高风险动作门禁并指定报告路径。发布报告及现有edge-bind回执必须匹配当前revision和Stage9配置哈希；代码、客户配置或绑定对象有变化时，须重新执行`stage9-edge-bind`的plan/execute并使用新哈希，不能沿用失效回执。Stage6后端入口回执按Stage6配置哈希判断是否仍属于当前应用；release同时使用独立公有根包对当前Stage4证书、私有IP和native路由做实时探测。因此仅Git revision变化或合规源站证书轮换不要求回滚重跑`stage6-application`，但Stage6配置变化或实时入口探测失败仍会阻止release。

#### 自动生成native dev/test canary发布报告

`edge-bind execute`生成绑定回执，不生成或更新发布报告。对于`dev/test + canary + native authentication + native audit`，应用已审核的`approved-release`配置后，可使用正式生成器，避免复制旧环境报告：

```bash
.venv/bin/python -m local_execution.release_report \
  --config "$CFG" \
  --change-ticket "REPLACE_ACTUAL_APPROVED_CHANGE_REFERENCE" \
  --approved-by "REPLACE_ACTUAL_APPROVER_OBJECT_ID"
```

双人审批策略需重复传入`--approved-by`；单人策略仍须符合配置中的具名批准人和显式风险接受。参数必须代表本次实际批准，不会自动推定旧审批有效，也不会验证外部工单的真实性。

命令使用配置中的deploy身份及既有认证流程，读取当前revision/Stage9哈希、实际Front Door GUID和双PLS，复用live edge校验（域名、TLS、PLS审批、路由及Admin WAF），并核对部署的工作区和限流参数。报告以0600写入`localExecution.releaseReportPath`指定的ignored `temp/`路径。已有报告默认拒绝覆盖；人工确认替换后添加`--replace`，旧报告会先以0600备份到本次输出目录的`previous-release.json`。命令不修改客户配置，不部署资源、不开启流量、不修改DNS；生成后仍需审核报告和release plan。

此命令仅生成上述简化canary报告，不用于Entra/L3、prod canary或production；这些路径仍须提交完整真实证据。仅更新报告无需重跑edge-bind，但代码或客户配置发生变化后，应先生成当前报告，再重新执行edge-bind plan/execute。release plan始终独立检查回执和实时入口，生成报告不替代发布验收。

native release在实时探测前核验当前双平面edge-bind回执和live路由。绑定后Admin `/fallback/login`仅携带正确`X-Azure-FDID`时应返回200；缺失或错误FDID必须返回404。API健康探针仍不依赖FDID，管理路径仍拒绝；绑定后的API推理探测使用正确FDID但不携带virtual key，必须得到401/403，缺失或错误FDID必须得到404。遇到探测失败不要删除FDID绑定或提前切CNAME；诊断包含实际HTTP状态码，但不输出响应正文、Cookie或密钥。

Front Door origin的`sharedPrivateLinkResource.status`可能返回null。已开启入口不会仅凭该空字段判断批准：必须核对正确PLS及区域、PLS侧恰有一个Approved连接且无额外Pending、autoApproval为空，并通过保留自定义域SNI/Host的严格HTTPS公网探测。显式Pending/Rejected/Disconnected仍拒绝；不要为修复报告生成而重复审批或关闭入口。

deploy身份启用获批的canary phase：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-release --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-release --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_RELEASE_PLAN_SHA256"
```

使用Azure DNS自动切换时，先由客户DNS管理员给当前客户账号或operator UAMI在实际zone授予DNS Zone Contributor，再执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-publish --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-publish --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_DNS_PUBLISH_PLAN_SHA256"
```

Azure DNS动作在同一检查点中新增或更新`llm-api`和`llm-admin`两条CNAME，分别指向独立endpoint；中途失败可从已知状态继续，第三方漂移会阻止操作。第三方DNS由客户DNS管理员人工新增等效双记录，不选择`--option azure-dns`，也不运行`stage9-dns-publish`。新记录与旧系统域名并存，不是替换旧入口。新域名可解析后立即验证错误率/延迟、PG连接、Redis、预算和Spend Logs。API需确认缺Key、管理路径和非批准推理路径均拒绝；Admin需从白名单外公网出口验证403，并从白名单内出口验证错误密码拒绝、正确Entra或原生登录成功。私有Admin LB仅作Front Door回源，不再作为日常客户入口。

### 已发布native dev/test canary新增Admin来源IP

不要只在Portal修改WAF，也不要用`edge-prepare`更新已发布入口：前者造成配置漂移，后者会关闭API/Admin。已发布的native dev/test canary可使用专用命令新增精确公网IPv4 `/32`或IPv6 `/128`，保留原有白名单及两个流量开关。

先从访问Admin的实际浏览器网络路径确认公网出口（终端和浏览器可能使用不同代理）。使用新代码revision前，按前述流程重新生成报告并执行edge-bind plan/审核/execute，以建立当前revision的基线；无需重新edge-release或更改DNS。

```bash
.venv/bin/python -m local_execution.admin_allowlist \
  --config "$CFG" --operation plan \
  --add-cidr "REPLACE_ACTUAL_PUBLIC_IPV4/32" \
  --change-ticket "REPLACE_ACTUAL_APPROVED_CHANGE_REFERENCE" \
  --approved-by "REPLACE_ACTUAL_APPROVER_OBJECT_ID"

# 审核本次输出目录中的plan/reviewed-plan.json和desired-release.json后执行：
.venv/bin/python -m local_execution.admin_allowlist \
  --config "$CFG" --operation execute \
  --add-cidr "REPLACE_ACTUAL_PUBLIC_IPV4/32" \
  --change-ticket "REPLACE_ACTUAL_APPROVED_CHANGE_REFERENCE" \
  --approved-by "REPLACE_ACTUAL_APPROVER_OBJECT_ID" \
  --approved-plan-sha256 "REPLACE_CURRENT_ALLOWLIST_PLAN_SHA256"
```

可重复`--add-cidr`添加多个精确地址；双人审批需重复`--approved-by`。plan不修改Azure、客户配置或正式报告。命令要求当前双平面入口已开启、live资源和原配置一致、审批符合现有策略，只允许客户配置中的Admin CIDR增加，不允许移除已有地址或同时修改其他字段。命令从已核验的live Admin WAF生成单资源模板，完整保留现有策略设置、managed rules、其他custom rules和服务默认值，仅替换来源IP匹配值；不重新部署域名、证书、诊断、endpoint、route或API WAF。What-if展开Azure嵌套数组差异，只允许Admin WAF首条来源IP规则`matchValue`叶节点变更，拒绝规则整体替换、action及其他字段变更。原edge部署输出和参数保留，新输出仅更新CIDR，以供后续报告及绑定校验。保留原流量状态，不执行DNS动作；粗粒度或不明差异仍停止，不自动放宽校验。

execute会在stderr实时提示基线/What-if检查、WAF部署、配置及报告保存、绑定回执刷新等阶段，stdout保留JSON摘要。临时kubeconfig使用`--overwrite-existing`非交互获取，不需要输入`y`；该覆盖仅针对本次操作输出目录，不覆盖用户默认kubeconfig，也不替代计划哈希或人工审批。WAF部署成功后立即更新`allowlist-summary.json`的`deploymentPerformed`，保留本次已验证的批准`planSha256`；绑定尚未完成时仍为`status: executing`。执行期间须保持AKS运行；API/Admin rollout各最多等待15分钟，阶段提示不代表已完成。

execute成功后，以0600备份并更新Git-ignored客户配置和发布报告，再刷新edge-bind回执（只允许已有ingress规则及Pod template均不变）。以最终`status: completed`及`bindingRefreshed: true`确认整个操作成功，无需再运行release。新增IP生效仍需WAF边缘传播；从批准出口验证登录页，并从未批准出口验证拒绝，内层密码认证保持不变。家庭公网地址可能变化，下一次变化需单独审核新增，不能为方便扩大CIDR。

若Azure已更新但本地写入或回执刷新失败，命令明确失败，并在输出目录保存`allowlist-summary.json`、`desired-customer.json`、`desired-release.json`及旧配置备份。检查实际部署后再恢复本地一致性；配置已更新时可重新执行edge-bind plan/审核/execute刷新回执。不要使用旧哈希盲重试、回滚整个配置或重跑edge-prepare。该命令不支持prod、production、Entra或L3路径，也不代替实际批准及公网验收。

### 8.3 停止试点、可选最终迁移和旧环境退役

观察期内若新环境不达标，先撤销试点virtual key并让试点客户端恢复旧API域名；按批准变更禁用新route或删除新CNAME。旧系统原本就保持运行，不需要通过数据库回退才能继续服务。只有Azure DNS路径存在自动DNS checkpoint时，才执行独立的DNS rollback：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-rollback --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-rollback --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_DNS_ROLLBACK_PLAN_SHA256"
```

DNS rollback不要求重新打开`allowTrafficRelease`，但只恢复已有checkpoint；它不回迁新库独有数据。第三方DNS由管理员按原变更记录撤销新CNAME。新库已有Key、预算或日志写入时，这些数据不会自动合并回旧库，须按留存政策保存或由DBA另行对账。

若客户后来要求无损承接备份时点之后的旧系统新增数据，须另设维护窗口，手工完成旧入口停写、请求排空、最终备份、干净目标库选择、恢复/迁移和最终对账；当前本地包不自动覆盖正在试点且已有独有写入的新库。若客户明确接受两边数据在备份后分叉，则可跳过最终迁移，但删除旧资源前仍须确认旧库中的Key、预算、配置和日志哪些需要归档或保留。

客户批准的稳定观察期结束后再软下线旧环境：

1. 确认所有计划迁移的客户端已显式改用新域名，旧入口在批准观察期内无请求/写入，新系统不依赖旧AKS、旧PG或旧Key Vault。
2. 完成新旧数据分叉对账，保留旧PG/PVC、Stage0备份、退役前快照、原Master/Salt和旧部署快照；没有最终迁移时不得把Stage0备份描述为停机时最终状态。
3. 旧AKS为本项目专用且无其他工作负载时，由客户管理员执行可逆停止；共享集群不得运行此命令：

```bash
LEGACY_RG="REPLACE_LEGACY_RESOURCE_GROUP"
LEGACY_AKS="REPLACE_LEGACY_AKS_NAME"
az aks stop --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS"
az aks show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{state:provisioningState,power:powerState.code}' --output json
```

4. 回退窗口内需要恢复旧环境时：

```bash
az aks start --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS"
```

5. 旧资源删除、日志/备份清理、身份撤权和DNS删除必须另立变更单；本地包没有`stop-legacy`或`delete-legacy`步骤，不把“停止”写成“已删除”。

### 8.4 可选：以Application Gateway替代Front Door作为公网入口

`parameters.edge.type`可选`frontDoor`（缺省，即上文流程）或`applicationGateway`，二选一。Application Gateway WAF_v2在目标VNet内直接访问Stage4两个私有Traefik LB，不使用Private Link Service，因此`stage9-origin`在此模式下拒绝执行。适用于需要等待首字节超过240秒的请求（Front Door对首字节固定240秒上限，Application Gateway后端超时缺省900秒、最长24小时）、或需要检查最多2MB请求体的场景；用户分布在多个国家/地区且确需全球就近接入时仍选Front Door。本期仅支持native认证、本地执行（GitHub Stage9 workflow拒绝此模式）；Admin白名单增量工具、API WSS WAF更新工具、模型同步edge证据和WAF安全探针在此模式下明确报错，尚不支持。

与Front Door路径的差异：

- 子网：模板在目标VNet中创建专用子网（建议`/24`，不小于`/26`），挂专用NSG、不挂防火墙路由表；订阅已注册`EnableApplicationGatewayNetworkIsolation`时自动委派给`Microsoft.Network/applicationGateways`。该子网必须包含在**两个**`privateIngress.<plane>.allowedCidrs`中，否则配置校验失败；修改`allowedCidrs`会改变Stage4指纹，须先按Stage4起的标准链路重跑并在Front Door模式下确认入口正常，再切换入口类型。
- 证书：监听器使用与Traefik相同的公有证书，另存为PKCS#12密钥`api-tls-pfx`/`admin-tls-pfx`，放在模板新建的**网关证书Vault**（`kvagw-<env>-<hash>`）中。Application Gateway读取Key Vault证书必须走“允许受信任的Microsoft服务”例外，仅有私有端点不够；为了不放宽存PEM私钥和ACME账户的证书Vault（保持`bypass=None`），网关证书Vault单独设`bypass=AzureServices`，公网仍关闭、使用私有端点。网关托管身份在该Vault获Secrets User，证书导入人/组（`certificateImporterPrincipalId`）获Secrets Officer。`stage9-edge-certificates`从证书Vault读当前PEM、生成或更新网关证书Vault中的PFX；自动API证书续期在此模式下同时写入PFX。certificate配置使用的身份若不是导入人/组（例如operator UAMI），须另行在网关证书Vault上授予Key Vault Secrets Officer，与证书Vault的授权方式相同。监听器引用无版本URI，续期后自动生效。
- 来源绑定：网关以重写规则覆盖请求头`X-LLMGW-Edge-Id`为部署输出`edgeId`，`stage9-edge-bind`把两个Traefik业务router的`X-Azure-FDID`绑定替换为该头；直接访问私有LB而不带正确值返回404。与FDID相同，该值只证明请求经过网关，不是密钥；网络准入仍由`allowedCidrs`控制。
- 流量开关：监听器在禁流量时挂始终Prevention、全部拦截的闸门WAF策略；release后API监听器使用API策略（canary为Detection），Admin监听器使用固定Prevention、`RemoteAddr`白名单的Admin策略。API仅6个推理路径路由到后端，其余路径进入空后端池返回502。
- 长连接：公网IP空闲超时30分钟、关闭HTTP/2（其前端空闲超时固定180秒）、关闭响应缓冲以逐条转发SSE。无数据等待的非流式请求最长约30分钟；Traefik与LiteLLM自身超时仍然生效。
- DNS：`stage9-dns-publish`把两条CNAME都指向网关公网IP的`*.<region>.cloudapp.azure.com`名称；检查点与部署记录使用独立名称（`...-s9-edge-appgw`、`...-s9-dns-checkpoint-appgw`），Front Door的记录保持不变，便于回退。

1. 在Stage4先把Application Gateway子网加入两个`privateIngress.<plane>.allowedCidrs`，按标准链路重跑并确认Front Door仍正常。然后合并Stage9时追加`--option app-gateway`（片段会删除`parameters.origin`及Front Door私有origin，并设置`edge.type=applicationGateway`）：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation plan \
  --values local_execution/stage-9-values.local.json --option app-gateway
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation apply \
  --values local_execution/stage-9-values.local.json --option app-gateway
```

2. 第一次`stage9-edge-prepare`：网关证书Vault中还没有两个PFX，准备阶段自动推导`deployGateway=false`，只创建网关证书Vault及其私有端点、角色分配、子网、NSG、公网IP、托管身份和三个WAF策略，不创建网关。plan前会只读检查子网冲突、网络隔离注册和`privatelink.vaultcore.azure.net`私网DNS链接。

```bash
.venv/bin/python -m local_execution   --config local_execution/customer.json   --step stage9-edge-prepare --operation plan
.venv/bin/python -m local_execution   --config local_execution/customer.json   --step stage9-edge-prepare --operation execute   --approved-plan-sha256 "REPLACE_STAGE9_EDGE_PREPARE_PLAN_SHA256"
```

3. 生成监听器使用的PFX副本，执行身份使用certificate配置：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-certificates --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-certificates --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_CERTIFICATES_PLAN_SHA256"
```

4. 第二次`stage9-edge-prepare`（同上命令）：两个PFX已存在，自动推导`deployGateway=true`，创建禁流量的网关及其诊断设置。What-if只允许网关证书Vault及其私有端点/诊断、子网、NSG、公网IP、托管身份、三个WAF策略、网关、网关诊断设置和网关证书Vault范围的两条角色分配，Front Door资源不受影响。执行后核对网关Running、两个后端池Healthy、监听器证书指纹与PEM一致，以及经网关访问返回403（闸门）。
5. 维护窗口内执行`stage9-edge-bind`：plan中两个平面的`replacesEdgeHeader`为`X-Azure-FDID`表示从Front Door切换。执行后经Front Door的请求返回404，直到DNS切换完成。
6. 生成发布报告并release（报告含`edgeType=applicationGateway`和`edgeId`，不含PLS和`frontDoorId`；检查项以`app_gateway_backend_health`、`admin_listener_isolation`替代Private Link项），再执行`stage9-dns-publish`。从批准私网验证时把`X-Azure-FDID`换成`X-LLMGW-Edge-Id`，取值来自`...-s9-edge-appgw`部署输出`edge.edgeId`。
7. 回退到Front Door：在Application Gateway配置下执行`stage9-dns-rollback`恢复原CNAME，再以`stage9-edge-prepare`把监听器放回闸门；恢复Front Door配置（保留新增的`allowedCidrs`），重新执行`stage9-edge-bind`绑定回FDID。Application Gateway资源保留，删除须另立变更单。

## 9. 输出和现场收尾

默认输出位于`temp/local-stage09/<UTC时间>-<step>-<随机后缀>/`。每次至少审核：

- `local-execution.json`：本地步骤、实际operation、revision、配置哈希和结果。
- `azure-auth.json`：本次选择的profile、认证方式和Azure scope，不含Token。
- `plan/`与`execute/`：实时计划、部署回执或运行时摘要。
- 镜像步骤的`target-image-summary.json`、SBOM、扫描和签名验证结果。

失败可能留下部分Azure、Graph、PG、Vault或Kubernetes状态。先检查本次输出和实际资源，再重新运行同一步；不要删除数据库、Secret或Entra对象来追求幂等。

全部手工操作结束后执行`az logout`和`az account clear`，卸载临时UAMI、删除ACR临时认证和无须保留的明文输出，再按Stage0–1指南恢复Runner服务。恢复在线前确认VM不再挂载高权限UAMI。

## 10. 附录：客户生产最小权限替代方案

本附录用于客户不接受临时高权限operator、希望拆分生产权限时。使用第2.4节单一验证UAMI时不需要创建这些角色。创建者必须在Assignable Scope拥有`Microsoft.Authorization/roleDefinitions/write`。在对应RG → **Access control (IAM) → Add custom role → JSON → Edit**粘贴定义，替换Scope后创建；已有同名角色时先核对，不覆盖其他团队定义。

### 10.1 模型部署角色

每个获批模型RG创建或复用：

```json
{
  "properties": {
    "roleName": "LLMGW Model Deployment Operator",
    "description": "Read model metadata, manage reviewed deployments and approve private endpoint connections in approved model resource groups.",
    "assignableScopes": [
      "/subscriptions/REPLACE_MODEL_SUBSCRIPTION_ID/resourceGroups/REPLACE_MODEL_RESOURCE_GROUP"
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

实际分配在模型RG给获批deploy主体。模型账号上的受约束RBAC Administrator只允许分配/删除`Cognitive Services OpenAI User`（`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`），推荐再限制接收者类型为ServicePrincipal。

### 10.2 资源锁角色

目标RG创建并分配给获批deploy主体：

```json
{
  "properties": {
    "roleName": "LLMGW Resource Lock Writer",
    "description": "Read, create and update resource locks in the approved gateway resource group; no lock deletion.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
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

### 10.3 运行回执角色

目标RG创建，分配给runtime、entra-bootstrap和entra-access UAMI：

```json
{
  "properties": {
    "roleName": "LLMGW Runtime Receipt Writer",
    "description": "Read and write reviewed ARM deployment receipts in the approved gateway resource group; no resource or deployment deletion.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
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

### 10.4 AKS Namespace初始化角色

角色定义的Assignable Scope为目标RG，但实际只在新AKS资源Scope分配给获批runtime主体：

```json
{
  "properties": {
    "roleName": "LLMGW AKS Namespace Bootstrapper",
    "description": "Read and write Kubernetes namespace objects in an approved AKS cluster; no namespace deletion or RBAC management.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
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

### 10.5 deploy受约束角色分配清单

目标RG和外部模型账号的条件授权只应包含实际模板需要的角色：

| 角色 | Role Definition ID | 用途 |
| --- | --- | --- |
| Network Contributor | `4d97b98b-1d4f-4787-a291-c67834d212e7` | AKS控制面使用目标VNet |
| AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | AKS kubelet拉取目标ACR镜像 |
| Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | 应用Workload Identity调用模型 |
| Key Vault Secrets User | `4633458b-17de-408a-b874-0445c86b69e6` | 应用/代理读取各自Vault Secret |
| Key Vault Secrets Officer | `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` | 获批初始化身份写入对应Vault |

条件应同时覆盖角色分配的新增和删除动作。无法安全配置条件时，不授予无条件广域管理员角色，由IAM管理员在部署窗口代执行。

### 10.6 Stage与登录方式速查

| Stage/步骤 | 客户现场 | 实施方验证 |
| --- | --- | --- |
| Stage2决策、Stage3源检查 | 当前账号 | 无Azure身份或operator |
| Stage3–4 | 当前账号 | 单一operator UAMI |
| Stage5 platform | 当前部署账号 | 单一operator UAMI |
| Stage5 database-roles | PG管理员用户/组成员，或独立database UAMI | PG管理员组内operator，或独立database UAMI |
| Stage5 secrets/schema；migration restore | migrationPrincipalId对应服务身份 | 单一operator UAMI |
| Stage6 application | 当前账号或客户runtime身份 | 单一operator UAMI |
| Stage7代理镜像/foundation/runtime | 当前账号 | 单一operator UAMI |
| Stage7 entra-apps/admin-credentials | 客户专项服务身份 | entra-bootstrap UAMI |
| Stage7 entra-access/revoke | 客户专项服务身份 | entra-access UAMI |
| Stage8–9非Entra步骤 | 当前账号 | 单一operator UAMI |

参考：

- [Microsoft Entra licensing](https://learn.microsoft.com/entra/fundamentals/licensing)
- [Microsoft Entra Security Defaults](https://learn.microsoft.com/entra/fundamentals/security-defaults)
