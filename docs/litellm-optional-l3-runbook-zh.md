# Stage2–9 完成后：可选 TypeScript L3 原文审计 Runbook

## 1. 适用范围与冻结基线

先按 [Stage2–9 runbook](../local_execution/stage2-9-guide-zh.md) 部署并验收原生 LiteLLM 集群。
**不执行本文、不添加 `l3Audit`，行为不变：获批正文仍由基础版 Spend Logs 存入 PG。**
本功能不替换原有 Entra 代理增强 L3；本期仅接入 native 模式，不要求重新部署 Entra 或购买商业日志连接器。

本代码已包含离线协议/故障测试；客户 Azure 私网、RBAC、真实模型、告警送达及保全必须另行实测。
ARM/rollout 成功不等于内容审计验收通过，不伪造 Stage8 验收证据。

```text
API Front Door → 原有 API 私有 Traefik
  → 可选 l3-audit TypeScript 透明代理（两副本）
    → 原有 LiteLLM → 模型
    └→ 异步持久化磁盘缓冲（每副本独立 PVC）
       ├→ 私有 Blob：原文；普通容器30天生命周期
       └→ LAW L3Audit_CL：无正文结构化索引、风险和心跳

IT管理员 CLI → LAW 查索引 → 私网读取 Blob
                         └→ 独立案件容器 legal hold / 明确解除并删除
Admin Front Door / Admin 登录链路保持直连 LiteLLM，不经过新代理。
```

当前选择 Blob Storage（非 HNS），无需同时创建 ADLS 或 ADX。使用微软托管密钥、
基础设施双重加密、禁用共享密钥、PE、Workload Identity。未来有查询规模需求再评估 ADX。
不设置独立调查员角色或原文审批体系，已有 IT 管理员是查询和保全操作者。
运行服务仍使用单独最小范围工作负载身份，不使用管理员凭据。

### 1.1 采集内容和明确边界

- Chat/Responses：HTTP JSON、SSE；Responses WSS 同连接的请求轮次、返回事件、
  模型函数调用与客户端回传的工具结果。
- Embeddings：记录输入，不保存返回向量，避免不必要的体积增长。
- HTTP 请求/响应 body 原始字节以 base64 保存在 JSON 中，WSS 按消息记录；
  不复制 Authorization、Cookie、Token、HTTP Header 或明文 Key。
  `bodyEncoding`只保存identity/gzip/deflate/br等白名单标记，压缩body保留原始字节；
  不自动解压或推断压缩SSE的终态，未知编码明确标记缺口。
- LAW 仅存调用 ID、时间、模型、Key SHA-256、轮次关联哈希、结果状态、字节数、
  内容哈希、Blob 相对路径及运行风险。无正文预览、无模型语义摘要。
- 请求体里客户自己写入的代码、PII、Secret 仍属于原文；本期**没有正文 DLP、内容分类
  或 Prompt Injection 检测**。客户必须批准允许采集的数据类别。
- Key 哈希表示凭据关联，不代表员工的已验证 Entra 身份；不信任客户端自填 user/metadata 作为审计主体。
- 网关看不到未发送给模型的本地工具活动。只有 `previous_response_id` 时保存本轮
  并关联前轮，不重建或重复展开历史上下文；旧轮次到期后不保证还能展开完整会话。
- 不是逐网络字节取证；`completed` 是观测到的响应终态，不证明客户端完整接收。
  WSS 非文本、异常分帧、未知扩展、无法关联的事件/并发轮次均标记缺口，不冒充完整记录。

### 1.2 业务优先与资源上限

| 项目 | 第一版固定上限/行为 |
| --- | --- |
| 单次 HTTP 或 WSS 轮次 | 请求＋响应累计16MiB，超限保留已捕获前缀并标记 `gap` |
| 活动采集预算 | 共享64MiB原始内容预算；JSON/base64、帧重组等有额外开销，不代表进程总内存上限 |
| WSS 采集并发 | 每Pod最多32个被采集连接、每连接最多32个未完成轮次；超限业务仍转发，输出缺口 |
| 分片数量 | 每个采集/帧重组最多4096个片段，防止碎片元数据耗尽内存 |
| 待交付缓冲 | 每Pod最多1GiB或4096条记录；内存待写队列最多64MiB |
| 重试 | 每5秒启动一次交付；先Blob再LAW；最长24小时后放弃并报告缺口 |
| 持久化 | StatefulSet独立2Gi PVC；原文入队后fsync，故障恢复继续交付；不自动删除PVC |
| 无法写盘/缓冲满/Azure故障 | **业务继续**，日志和心跳标记丢失/退化；不能宣称零丢失 |

进程在请求结束前、fsync前退出会丢失活动/待写记录；PVC/节点永久丢失也会丢数据。
损坏的队列文件被隔离为`.corrupt`并告警，不阻塞后续记录；仍占用磁盘预算，
须由管理员在批准窗口检查和处理，不自动删除可能可恢复的原文。
集群整体停机时不采集、不交付，24小时重试窗口不会暂停。
LAW至多一次查询语义不是交付承诺：导出成功但确认丢失时可能重复，以 `Record.id`
去重。内容Blob按唯一ID及哈希处理重复；不保证 exactly-once。

审计存储失败不主动拒绝推理；**代理进程本身不可用仍可能影响业务**，两副本不是自动旁路。
不允许失败时偷偷重试非幂等模型调用或重放WSS帧；故障时按第8节审核回退。

## 2. 操作前准备

执行者应有：目标资源部署/What-if权限、批准的作用域内角色分配权限、目标AKS命名空间
发布权限，以及已有管理员组。不得为方便授予整个订阅Owner。
Runner具备Node 24、npm、Azure CLI/Bicep、jq、kubectl、kubelogin、Docker及原项目依赖。
固定当前审核代码版本，安排可能涉及 ingress/backend rollout 的维护窗口。

```bash
set -euo pipefail
set +x
umask 077
CFG="local_execution/customer.json"
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' "$CFG")"
TENANT_ID="$(jq -er '.azure.tenantId' "$CFG")"
TARGET_RG="$(jq -er '.target.resourceGroup' "$CFG")"
AKS_NAME="$(jq -er '.parameters.platform.stage4Aks.name' "$CFG")"
ACR_NAME="$(jq -er '.parameters.platform.containerRegistryName' "$CFG")"
WORKSPACE_NAME="$(jq -er '.parameters.platform.logAnalyticsWorkspaceName' "$CFG")"
jq -e '.application.authentication.mode == "native" and .contentAudit.mode == "native"' "$CFG" >/dev/null
az account show --query '{id:id,tenant:tenantId}' --output json \
  | jq -e --arg sub "$SUBSCRIPTION_ID" --arg tenant "$TENANT_ID" \
      '.id == $sub and .tenant == $tenant' >/dev/null
mkdir -p temp
L3_DIR="$(mktemp -d "$PWD/temp/optional-l3.XXXXXXXX")"
cp "$CFG" "$L3_DIR/customer-before.json"
chmod 600 "$L3_DIR/customer-before.json"
npm ci --prefix l3-audit --ignore-scripts --no-fund
npm test --prefix l3-audit
.venv/bin/python -m unittest \
  tests.test_optional_l3 tests.test_private_ingress \
  tests.test_backend_manifest tests.test_local_release_report
az bicep build --file infra/optional-l3/main.bicep --stdout >/dev/null
```

以上三条验证命令等价于`make validate-optional-l3`，不要求Runner安装make；
已有make时可以使用该快捷目标。验证失败时停止，不继续创建资源或切换入口。
依赖安装提示漏洞时核对具体公告和锁定版本，不运行`npm audit fix --force`擅自批量升级。

这里只备份本次客户配置，不备份或输出任何数据库/运行Secret。
选择已有L3与本方案迁移时停止，先审核已有存储、正文所有权及回退，不混用两套采集。

### 2.1 防止旧配置与新环境混用

`customer.json`不是固定文件名契约：迁移后请将`CFG`指向当前实际部署使用的文件，
不要将旧West US配置用于West US 3。只改infra参数不能修复CFG，后面的Stage4仍会操作CFG中的集群。
先输出并人工核对以下非敏感摘要；区域、目标RG、AKS、ACR、LAW、网络和证书Vault必须一致指向目标，
如有`westus`/`westus3`、`wus1`/`wus3`等混用则停止，不批量字符串替换来重建配置。

```bash
jq '{location,target,aks:.parameters.platform.stage4Aks.name,
     acr:.parameters.platform.containerRegistryName,
     law:.parameters.platform.logAnalyticsWorkspaceName,
     vnet:.parameters.platform.stage4Network.virtualNetworkName,
     certificates:.privateIngress,backendImage:.application.backendImage}' "$CFG"
AKS_FACTS="$(az aks show --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" -n "$AKS_NAME" \
  --query '{location:location,issuer:oidcIssuerProfile.issuerUrl,workloadIdentity:securityProfile.workloadIdentity.enabled,subnets:agentPoolProfiles[].vnetSubnetId}' \
  -o json --only-show-errors)"
printf '%s' "$AKS_FACTS" | jq -e --arg location "$(jq -er '.location' "$CFG")" \
  '.location == $location and .workloadIdentity == true and (.issuer | startswith("https://"))' >/dev/null
printf '%s\n' "$AKS_FACTS" | jq '.'
```

上述检查核对当前CFG所指资源，并不自动知道你“想部署”的环境；必须人工确认摘要。

## 3. 部署独立 Azure 资源

模板：[infra/optional-l3/main.bicep](../infra/optional-l3/main.bicep)。
**该模板不被 Stage2–9 自动调用。** 不使用旧 `audit-foundation/audit-storage` 组件替代它。

在 `$L3_DIR/infra.parameters.json` 创建批准的ARM参数文件：

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "location": {"value": "REPLACE_TARGET_LOCATION"},
    "storageAccountName": {"value": "REPLACE_NEW_GLOBALLY_UNIQUE_STORAGE_NAME"},
    "workspaceName": {"value": "REPLACE_EXISTING_WORKSPACE_NAME"},
    "virtualNetworkName": {"value": "REPLACE_TARGET_VNET"},
    "privateEndpointSubnetName": {"value": "REPLACE_PRIVATE_ENDPOINT_SUBNET"},
    "aksOidcIssuer": {"value": "REPLACE_ACTUAL_AKS_OIDC_ISSUER"},
    "administratorPrincipalId": {"value": "REPLACE_EXISTING_IT_ADMIN_GROUP_OBJECT_ID"},
    "administratorPrincipalType": {"value": "Group"},
    "alertEmail": {"value": "AUTO_FROM_CUSTOMER_CONFIG"},
    "existingMonitorScopeName": {"value": ""},
    "workspaceAlreadyLinkedToMonitorScope": {"value": false},
    "privateDnsZonesToCreate": {"value": []},
    "privateDnsZonesToLink": {"value": []}
  }
}
```

### 3.1 自动填写客户告警邮箱及已知环境参数

**不需要手工创建Action Group**。同一Bicep会创建`l3-<storageAccountName>-alerts`，
两条告警直接引用它。邮箱优先取当前客户CFG的`ownerEmail`，不写死任何个人或示例客户地址。
Runner使用Managed Identity/服务主体时没有“登录用户邮箱”，不能将其Client ID或显示名作为接收者。
仅CFG缺少邮箱且CLI确为个人用户登录时才查询Microsoft Graph的`mail`；查询失败会显式停止，
不改用可能是访客标识、服务账号或不可投递UPN的字符串。

创建上面的参数文件后，在同一终端执行以下命令，自动填入邮箱、区域、LAW、VNet、子网和实际Issuer；
新Storage名称、管理员Object ID及DNS/AMPLS决定仍需按第3.2节核对。

```bash
ALERT_EMAIL="$(jq -r '.ownerEmail // empty' "$CFG")"
if [[ -z "$ALERT_EMAIL" ]]; then
  LOGIN_TYPE="$(az account show --query user.type -o tsv --only-show-errors)"
  if [[ "$LOGIN_TYPE" != "user" ]]; then
    printf 'Cannot discover a human email from managed identity/service principal. Set approved CFG ownerEmail.\n' >&2
    exit 1
  fi
  ALERT_EMAIL="$(az ad signed-in-user show --query mail -o tsv --only-show-errors)"
fi
if [[ ! "$ALERT_EMAIL" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
  printf 'Missing or invalid customer notification email; verify CFG ownerEmail or signed-in user mail.\n' >&2
  exit 1
fi
printf 'L3 alert recipient from this customer configuration/login: %s\n' "$ALERT_EMAIL"
AKS_ISSUER="$(printf '%s' "$AKS_FACTS" | jq -er '.issuer')"
PARAMETERS_TMP="$(mktemp "$L3_DIR/infra.parameters.XXXXXXXX")"
jq --arg email "$ALERT_EMAIL" --arg issuer "$AKS_ISSUER" \
  --slurpfile config "$CFG" '
  .parameters.alertEmail.value = $email |
  .parameters.location.value = $config[0].location |
  .parameters.workspaceName.value = $config[0].parameters.platform.logAnalyticsWorkspaceName |
  .parameters.virtualNetworkName.value = $config[0].parameters.platform.stage4Network.virtualNetworkName |
  .parameters.privateEndpointSubnetName.value = $config[0].parameters.platform.stage4Network.privateEndpointSubnetName |
  .parameters.aksOidcIssuer.value = $issuer
' "$L3_DIR/infra.parameters.json" > "$PARAMETERS_TMP"
mv "$PARAMETERS_TMP" "$L3_DIR/infra.parameters.json"
chmod 600 "$L3_DIR/infra.parameters.json"
```

无需再维护一份邮箱；但必须在What-if审核时确认接收者确实是客户希望通知的人。
`ownerEmail`有效但指向其他负责人时不静默替换为操作员邮箱，按客户配置管理流程更正。

### 3.2 参数取值与审核

参数取值与审核：

1. workspace/VNet/subnet与CFG一致且位于本目标RG；新Storage必须是独立空账号，
   不接管备份库、应用库或旧增强L3账号。
2. `aksOidcIssuer` 从 `az aks show ... --query oidcIssuerProfile.issuerUrl -o tsv` 获取，
   同时确认AKS OIDC及Workload Identity启用，不手填Issuer。
3. 管理员填已有IT组的Object ID，不是Client ID。模板只在审计账号、目标workspace授权，
   管理员可读写审计正文并管理legal hold，**不是新的独立调查员权限体系**。
4. `alertEmail`由第3.1节自动填入；部署身份须有目标RG内Action Group写入权限。
   Bicep创建专用Action Group，不复用Smart Detection或旧环境接收者。告警在部署后生效，
   初期无心跳告警属于预期；服务就绪后必须验证告警恢复及真实通知送达。
5. **先盘点现有AMPLS。一个VNet不能关联不同AMPLS。** 已有时填写其实际名称，
   不创建另一个scope；如果workspace已在该scope内，`workspaceAlreadyLinkedToMonitorScope=true`。
   为空只适用于当前VNet没有AMPLS的情况。此模板支持同RG资源，跨RG DNS/AMPLS交网络Owner
   另行准备，不擅自重复创建或修改现有scope模式。
6. 核对以下五个私有DNS Zone：`privatelink.blob.core.windows.net`、
   `privatelink.monitor.azure.com`、`privatelink.oms.opinsights.azure.com`、
   `privatelink.ods.opinsights.azure.com`、`privatelink.agentsvc.azure-automation.net`。
   `privateDnsZonesToCreate`仅列本RG确实缺少的名称；`privateDnsZonesToLink`仅列没有
   目标VNet等效链接的Zone。已有链接不同名时复用，不重复链接同一VNet。
7. Runner/管理员操作机也须经获批私网DNS/转发访问Blob、DCE和LAW查询端点；
   模板不会自动修改其他VNet的链接/路由。防火墙允许Workload Identity的Entra token exchange，
   不开放Storage/DCE公网，不加全网放行来解决问题。
8. 30天为从Blob最后修改时间起算的普通生命周期删除阈值，不按模型调用时间精确计时；
   Azure执行有延迟，不是第30天精确擦除保证。
   LAW表保留30天；工作区计费/现有权限保持按实际配置审核。

只读盘点示例，输出均为元数据：

```bash
az aks show --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" -n "$AKS_NAME" \
  --query '{issuer:oidcIssuerProfile.issuerUrl,workloadIdentity:securityProfile.workloadIdentity.enabled}' -o json
az network private-dns zone list --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" \
  --query '[].name' -o json
az monitor private-link-scope list --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" -o table
az network private-endpoint list --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" \
  --query '[].{name:name,connections:privateLinkServiceConnections[].privateLinkServiceId}' -o json
az deployment group what-if --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" \
  --name optional-l3 --template-file infra/optional-l3/main.bicep \
  --parameters "@$L3_DIR/infra.parameters.json"
```

审核What-if：只允许新增或明确批准的L3对象（含专用Action Group和两条告警）、目标LAW自定义表/角色及DNS链接，
**不得修改/删除AKS、节点池、业务PG、Front Door/WAF、现有访问策略或其他DNS记录**。
现有同名表、scope关联或角色冲突时停止，不强制覆盖。按客户变更审批留存参数文件哈希及输出，
执行前确认文件未改且没有并行基础设施变更；执行命令本身不是自动What-if范围校验器。

批准后：

```bash
az deployment group create --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" \
  --name optional-l3 --template-file infra/optional-l3/main.bicep \
  --parameters "@$L3_DIR/infra.parameters.json" \
  --query properties.outputs -o json > "$L3_DIR/outputs.json"
jq '{deploymentSettings,workspaceId,privateEndpoints,alertActionGroupId}' "$L3_DIR/outputs.json"
```

输出不含密钥。记录workspaceId、DCE URL、DCR immutable ID、writer Client ID、两个PE。
逐项检查Storage和DCE公网Disabled、PE Approved、DNS解析到对应PE NIC IP。
发现私网不可达、RBAC拒绝或角色尚未传播时停止，不将业务切到代理。

## 4. 构建、扫描并部署 TypeScript 代理

不复用未审核的浮动tag或旧auth-proxy镜像。按现有供应链流程执行SBOM、签名、扫描及ACR私网
推送；下面是构建/推送命令，不替代上述门禁。镜像构建使用已锁定Node基础digest和npm lockfile。

```bash
set -euo pipefail
L3_TAG="l3-audit:$(git rev-parse --short=12 HEAD)"
docker build --platform linux/amd64 -f l3-audit/Dockerfile -t "${ACR_NAME}.azurecr.io/${L3_TAG}" l3-audit
```

完成客户批准的扫描、SBOM及签名流程后，使用已有获批ACR推送身份，在同一用户、
同一Docker配置环境下显式登录ACR。**Azure CLI已登录不代表Docker已登录ACR**，
旧Docker凭据也可能过期；不用`sudo docker push`切到另一个凭据目录。
下面的`&&`确保登录或推送失败后不查询digest；先清空旧引用，防止错误沿用：

```bash
unset L3_DIGEST L3_IMAGE
az acr login --subscription "$SUBSCRIPTION_ID" --name "$ACR_NAME" --only-show-errors &&
docker push "${ACR_NAME}.azurecr.io/${L3_TAG}" &&
L3_DIGEST="$(az acr repository show --subscription "$SUBSCRIPTION_ID" \
  --name "$ACR_NAME" --image "$L3_TAG" --query digest -o tsv --only-show-errors)" &&
if [[ "$L3_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  L3_IMAGE="${ACR_NAME}.azurecr.io/l3-audit@${L3_DIGEST}"
  printf 'Approved image reference candidate: %s\n' "$L3_IMAGE"
else
  unset L3_DIGEST L3_IMAGE
  printf 'Invalid or empty ACR digest; stop before workload rendering.\n' >&2
  exit 1
fi
```

若build已成功、push报`authentication required`，不必重新构建；完成上述登录后重试原tag。
若push失败，紧接着出现`specified tag does not exist`只是尚未上传该tag的连带结果，
不能将其解释为构建失败，也不能继续用空`L3_IMAGE`发布。

登录成功但push仍被拒绝时，核对当前Azure身份、ACR作用域的数据面推送权限及传播：
传统`LegacyRegistryPermissions`模式需要`AcrPush`或等效受限角色；
ABAC仓库权限模式需相应的Repository Writer/Reader及条件，不能机械套用`AcrPush`。
仍须保持ACR公网Disabled、admin账号Disabled；网络失败按实际PE/DNS/路由排查，
不要打开公网、启用admin密码或打印/复制访问Token来绕过问题。

建立独立kubeconfig，不覆盖默认context，不使用`--admin`：

```bash
KCFG="$L3_DIR/kubeconfig"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" -g "$TARGET_RG" -n "$AKS_NAME" \
  --file "$KCFG" --overwrite-existing --only-show-errors
chmod 600 "$KCFG"
kubelogin convert-kubeconfig --kubeconfig "$KCFG" -l azurecli
EXPECTED_SERVER="https://$(az aks show --subscription "$SUBSCRIPTION_ID" \
  -g "$TARGET_RG" -n "$AKS_NAME" --query privateFqdn -o tsv)"
```

准备渲染设置：从本次部署`outputs.json`的两个PE资源ID自动读取**实际NIC的全部私有IPv4**，
无需手工填写IP。工具核对CFG订阅/RG、目标子网、Blob账号/Monitor连接、
Succeeded/Approved及NIC归属，缺少或不匹配时停止，不猜公网DNS或示例地址。
执行身份需要对目标PE及其NIC的管理面读取权限。此处不创建/修改PE或writer身份，
writer由第3节Bicep创建，只能访问普通内容容器和DCR，不能管理保全容器。

```bash
npm run build --prefix l3-audit &&
PE_IPS="$(node l3-audit/dist/src/private-endpoints.js "$CFG" "$L3_DIR/outputs.json")" &&
jq --arg image "$L3_IMAGE" --argjson ips "$PE_IPS" \
  '.deploymentSettings.value + {image:$image,privateEndpointIps:$ips}' \
  "$L3_DIR/outputs.json" > "$L3_DIR/deployment-settings.json" &&
node l3-audit/dist/src/render.js "$L3_DIR/deployment-settings.json" "$L3_DIR/workload.json" &&
kubectl --kubeconfig "$KCFG" -n litellm apply --server-side \
  --field-manager=llmgw-l3 --dry-run=server -f "$L3_DIR/workload.json"
```

上述命令链任一步失败都停止，不继续diff/apply。`Invalid L3 deployment field`会指出
具体字段；占位符IP、空镜像digest或缺少部署输出都不能继续发布。
`workload.json`为独占创建，已有文件时先核对/归档旧计划或使用新的明确文件名，
不要为了重试而删除整个`L3_DIR`（其中有配置备份与kubeconfig）。
发现实际NIC地址不等于Runner/Pod的DNS和路由已通过验证，仍需执行网络及业务验收。

成功生成且server dry-run通过后，单独审查diff：

```bash
kubectl --kubeconfig "$KCFG" -n litellm diff --server-side \
  --field-manager=llmgw-l3 -f "$L3_DIR/workload.json"
```

`kubectl diff`有差异返回1是正常行为；不要把它和后续apply串成自动执行。
人工审查manifest和diff，记录文件SHA-256及变更批准，只新增命名的L3 StatefulSet/Service/SA/PDB/
NetworkPolicy和PVC，不修改原业务Deployment或ingress。批准后确认同一文件未变：

```bash
kubectl --kubeconfig "$KCFG" -n litellm apply --server-side \
  --field-manager=llmgw-l3 -f "$L3_DIR/workload.json"
kubectl --kubeconfig "$KCFG" -n litellm rollout status statefulset/l3-audit --timeout=15m
kubectl --kubeconfig "$KCFG" -n litellm get pods,pvc -l app.kubernetes.io/name=l3-audit
```

两个Pod须Ready，两个PVC须Bound。每副本缓冲独立，PVC保留，不缩容/删除StatefulSet来“清积压”。
健康端点HTTP200只说明业务代理可响应；必须检查其`spoolReady=true`及计数，不能只看Ready。

## 5. 切换 API 路由与 PG 正文所有权

### 5.1 加入显式可选配置

使用客户配置管理流程，将CFG增加：

```json
{"l3Audit":{"enabled":true}}
```

这是新增顶层字段，**保留原 `contentAudit` 配置作为基础版回退策略**，不要把整个CFG替换成这个片段。
不得同时加入旧 `auditRuntime/auditGovernance`。本期不存在隐式自动启用。

在Runner同一个终端运行下面的合并命令，不需要手工编辑完整JSON。
先确认输出的CFG路径、区域、目标RG及集群是实际想操作的环境；West US 3执行者须使用
`local_execution/customer.westus3.local.json`等实际文件，不沿用旧West US配置。
命令备份当前CFG，仅增加顶层布尔开关，不改变其他字段：

```bash
(
  set -euo pipefail
  umask 077
  printf 'CFG=%s\n' "$CFG"
  jq '{location,target,aks:.parameters.platform.stage4Aks.name,
       authenticationMode:.application.authentication.mode,l3Audit}' "$CFG"
  jq -e '.application.authentication.mode == "native" and .contentAudit.mode == "native"
    and (has("auditRuntime") | not) and (has("auditGovernance") | not)
    and ((has("l3Audit") | not) or .l3Audit == {"enabled":true})' "$CFG" >/dev/null
  test -f "$CFG"
  test ! -L "$CFG"
  test -d "$L3_DIR"
  CFG_BACKUP="$(mktemp "$L3_DIR/customer-before-l3.XXXXXXXX.json")"
  cp "$CFG" "$CFG_BACKUP"
  chmod 600 "$CFG_BACKUP"
  CFG_TMP="$(mktemp "${CFG}.l3-merge.XXXXXXXX")"
  trap 'rm -f -- "$CFG_TMP"' EXIT
  jq '.l3Audit = {"enabled":true}' "$CFG" > "$CFG_TMP"
  jq -e '.l3Audit.enabled == true and (.l3Audit.enabled | type) == "boolean"' "$CFG_TMP" >/dev/null
  mv "$CFG_TMP" "$CFG"
  chmod 600 "$CFG"
  printf 'CFG backup: %s\n' "$CFG_BACKUP"
)
```

执行后再确认，然后才能为L3重新生成Stage4计划。旧的基础版Stage4成功回执不能替代新配置的计划：

```bash
jq -e '.application.authentication.mode == "native" and .l3Audit.enabled == true
  and (.l3Audit.enabled | type) == "boolean"' "$CFG"
```

Stage0–3及Stage5–7基础设施/应用指纹不因该开关改变；Stage4入口、Stage8审计、
Stage9绑定会改变。代码revision变化仍遵循原runbook的证据复核要求，不伪造旧验收。

先切API入口到已Ready的代理；Admin保持原上游。Stage4仍保留Host/FDID/TLS及WSS方法约束，
不会生成WAF Allow、修改DNS或重新开放流量：

```bash
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage4-private-ingress --operation plan
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage4-private-ingress --operation execute \
  --approved-plan-sha256 "REPLACE_THIS_L3_INGRESS_PLAN_SHA256"
```

### 5.2 验证 L3，再关闭 PG 正文副本

先使用受限Key和合成内容跑第7节协议矩阵，确认Blob与LAW内容/索引均正确。
这段短验证窗口PG仍保留正文，属于明确的临时双写；不把真实客户敏感数据用于验证。
验证失败先回退入口，不关闭PG正文，避免没有任何内容记录。

验证成功后，使用专用的**仅改变PG正文开关**的计划/执行工具，不为此重跑全量
`stage8-application` 或重部署Stage5/6。这避免覆盖模型、运行设置或依赖旧基础设施revision的全量发布。

```bash
node l3-audit/dist/src/activation.js plan "$CFG" "$KCFG" "$EXPECTED_SERVER" \
  "$L3_DIR/pg-enable-plan.json" enable
node l3-audit/dist/src/activation.js execute "$CFG" "$KCFG" "$EXPECTED_SERVER" \
  "$L3_DIR/pg-enable-plan.json" enable "REPLACE_PG_PLAN_SHA256"
```

工具绑定计划文件哈希、代码、CFG、实际集群FQDN、Deployment UID/template和当前ConfigMap。
AKS kubeconfig常显式包含`:443`，而ARM的`privateFqdn`没有端口；激活工具按规范化后的HTTPS
origin核对二者，允许默认443端口和根路径`/`的等价写法，不放宽主机、端口、凭据或路径范围。
旧版本若因此报`Kubeconfig does not target the approved AKS API`，先比对ARM实际FQDN，
更新修复并重新`npm run build --prefix l3-audit`后生成新计划，不修改kubeconfig或绕过目标检查，
也不重新部署基础设施、镜像或Stage4。
若提示`Enable requires top-level l3Audit.enabled=true`，说明当前CFG尚未按5.1节启用，
PG开关没有改变；完成合并后重新运行Stage4 plan/execute和协议验收，再运行PG激活计划，
不复用之前基础版Stage4的哈希。
执行仅创建内容寻址的不可变配置，再CAS替换该配置卷引用并等待rollout。
只把 `store_prompts_in_spend_logs` 变为false，保留 `disable_spend_logs=false`、模型、
预算、身份、费用日志及其他设置；不删除旧配置。冲突/漂移时重新计划，不强制接管。
现有PG正文不会被该开关追溯删除，仍按原清理/备份策略处理。

**所有激活步骤完成后**刷新Stage9绑定回执及发布报告：

```bash
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage9-edge-bind --operation plan
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage9-edge-bind --operation execute \
  --approved-plan-sha256 "REPLACE_THIS_L3_BIND_PLAN_SHA256"
.venv/bin/python -m local_execution.release_report --config "$CFG" \
  --change-ticket "$CHANGE_TICKET" --approved-by "$APPROVER_OBJECT_ID" --replace
```

报告会记录`optionalL3Enabled=true`，但它不是L3采集验收报告。双人模式保留两位实际批准人。
`release_report`生成器仅适用已发布dev/test canary；生产按原发布流程准备真实检查证据，
不得伪造canary报告或切换WAF模式。已发布入口不重跑edge-prepare、edge-release、origin或DNS。

## 6. IT管理员 CLI 查询、读取、保全和解除

在能访问Blob PE、LAW查询端点的批准操作机，以现有管理员Azure身份执行。
不要在服务Pod里挂载管理员身份；可用独立Azure CLI配置目录隔离登录。
将以下变量从第3节实际输出及批准参数取得，不放任何连接串/账户Key：

```bash
export L3_STORAGE_URL="$(jq -er '.deploymentSettings.value.storageUrl' "$L3_DIR/outputs.json")"
export L3_DCE_URL="$(jq -er '.deploymentSettings.value.dce' "$L3_DIR/outputs.json")"
export L3_DCR_ID="$(jq -er '.deploymentSettings.value.dcr' "$L3_DIR/outputs.json")"
export L3_SUBSCRIPTION="$SUBSCRIPTION_ID"
export L3_RESOURCE_GROUP="$TARGET_RG"
export L3_STORAGE_ACCOUNT="REPLACE_ACTUAL_STORAGE_ACCOUNT"
LAW_WORKSPACE_ID="$(jq -er '.workspaceId.value' "$L3_DIR/outputs.json")"

node l3-audit/dist/src/admin.js query --workspace "$LAW_WORKSPACE_ID" --hours 24
node l3-audit/dist/src/admin.js query --workspace "$LAW_WORKSPACE_ID" --hours 24 \
  --model "REPLACE_ACTUAL_LITELLM_ALIAS"
node l3-audit/dist/src/admin.js query --workspace "$LAW_WORKSPACE_ID" --hours 24 \
  --id "REPLACE_CALL_UUID"
```

可加`--key-hash`；时间范围1–720小时，每次最多100条最新记录，以ID去重，不一次下载全库正文。
查询返回索引，不返回prompt/response。更多统计用LAW KQL，不把JSON索引看作原文。

根据索引`Record.blob`获取原文到独占0600文件：

```bash
node l3-audit/dist/src/admin.js get \
  --blob "content/YYYY/MM/DD/REPLACE_CALL_UUID.json" \
  --output "$L3_DIR/private-call.json"
```

默认不在终端打印原文。文件中`parts[].direction`区分请求/响应，
`parts[].data`按base64解码是实际body/WSS消息；`gap`非空时只有部分内容或缺口，
即使`outcome=completed`也不能认定审计完整。读取会核对存储版本ETag、封装哈希和内容哈希。
已存在的输出文件不会被覆盖。不要将文件放到Git、公共工单、聊天或普通日志。

按调用ID保全：从LAW先取得Blob引用，选择一个案件ID（小写字母开头，仅字母数字，3–31位）。
本期每个案件一个独立容器，避免解除一个案件的hold影响其他案件。

```bash
node l3-audit/dist/src/admin.js hold \
  --case case20261016 \
  --blob "content/YYYY/MM/DD/REPLACE_CALL_UUID.json"
```

CLI读取并校验源对象，在案件容器设置legal hold后上传独立副本，再核对hold。
普通原文30天生命周期不覆盖案件容器；源对象已过期时不能补救。
失败/中断后可能保留空的已保全容器或已上传副本，查明状态后重跑，不当作原子事务。
记录案件ID、调用ID及成功结果；LAW索引到期不删除保全副本。

普通记录到期后仍可读取已保全对象：

```bash
node l3-audit/dist/src/admin.js get --case case20261016 \
  --id "REPLACE_CALL_UUID" --output "$L3_DIR/private-held-call.json"
```

仅在已批准解除整个案件时执行，**解除后会删除该案件容器中的全部保全副本**：

```bash
node l3-audit/dist/src/admin.js release --case case20261016 --confirm case20261016
```

解除hold与删除容器不是事务；中途失败时必须核对实际hold/对象状态，不能把失败当成功。
IT管理员本身可以解除保全，这是本期简化权限边界，不是双人审批或不可撤销证据库。
Azure管理操作有Activity Log；本期不建设独立逐条原文读取审批/审计服务。

## 7. 上线验收清单

保留元数据证据，不将真实原文放进验收artifact。测试使用获批合成内容与低预算Key。

1. HTTP Chat/Responses JSON和SSE成功/失败各一次：响应、流式事件和状态不被代理改写；
   查到请求/响应原文及对应LAW索引，Key/Header不进入内容封装。
2. 用实际批准别名（不要照抄`coding`）执行已有WSS工具往返探针：

```bash
.venv/bin/python -m tests.responses_wss_probe \
  --api-url "https://llm-api.REPLACE_BASE_DOMAIN" \
  --model "REPLACE_APPROVED_ALIAS" \
  --confirm-authorized-target --direct --tool-roundtrip \
  --output-json "$L3_DIR/wss-probe.json"
```

3. 探针`accepted=true`只证明协议；还须在L3查到两轮及工具回传，
   第一轮`responseHash`与第二轮`previousResponseHash`相同。客户端断连不得标成完整。
4. Embeddings输入可查，响应向量不保存；同一连接多轮独立关联，不重复展开历史。
5. PG费用/调用记录继续存在；新调用的正文列不再保存完整输入输出。历史PG正文及备份另行处理。
6. 在隔离测试环境临时阻断代理到Blob/LAW的批准测试路径：推理仍成功，
   PVC积压/失败计数上升；恢复后Blob及LAW补齐，重复记录按ID去重。
   不在生产擅改防火墙；资源/时间上限用离线测试验证，禁止压垮生产缓冲。
7. 用受控Pod重启验证PVC重连及已入队恢复；活动/fsync前记录允许明确丢失，不声称零丢失。
8. 查询/下载、重复保全、普通原文到期后的保全读取、解除案件均实测；不同案件不相互解除。
9. 验证两类告警实际通知到批准接收人：风险事件、超过10分钟无心跳。
   Azure Monitor本身中断时告警也可能延迟；结合平台可用性告警，不称绝对实时。
10. HTTP原路径、原生Admin登录、WAF/FDID/私网隔离回归；不新增公网管理页面。

运维可临时转发健康端点，仅本机访问，在独立终端执行：

```bash
kubectl --kubeconfig "$KCFG" -n litellm port-forward \
  --address 127.0.0.1 service/l3-audit 18080:8080
```

另一个终端只读`curl http://127.0.0.1:18080/l3/health`，结束后Ctrl-C。
看`spoolReady`、`diskBytes`、`dropped`、`failures`，不要只看HTTP200。
这些计数是每Pod/进程累计值，重启清零；不同Pod分别核对，LAW心跳按ID去重。

## 8. 回退与后续运行

1. 若仅Blob/LAW故障，按业务优先策略继续转发并排障；不要清PVC来消除告警。
2. 若代理影响业务，按客户批准应急流程立即将CFG移除`l3Audit`，重新执行第5.1节
   Stage4入口plan/审核/execute，恢复直连LiteLLM；Admin不变。应急窗口可能只有费用元数据，
   如实记录缺口，不声称自动旁路。
3. 业务稳定后恢复PG正文：使用**移除l3Audit后的CFG**生成新的PG计划并执行：

```bash
node l3-audit/dist/src/activation.js plan "$CFG" "$KCFG" "$EXPECTED_SERVER" \
  "$L3_DIR/pg-disable-plan.json" disable
node l3-audit/dist/src/activation.js execute "$CFG" "$KCFG" "$EXPECTED_SERVER" \
  "$L3_DIR/pg-disable-plan.json" disable "REPLACE_ROLLBACK_PG_PLAN_SHA256"
```

非紧急回退可先恢复PG正文再恢复直连，将短暂双写作为批准的过渡，减少留痕空窗。
不要直接恢复整个旧CFG覆盖期间新增的模型、白名单或其他运行设置。
4. 按第5.2节重新生成Stage9绑定回执及适用的发布报告，去掉optionalL3Enabled；
   **不重跑edge-prepare/edge-release/DNS**。
5. 让代理继续运行，排空已有队列；观察两个副本PVC和LAW索引一致后再审核停用工作负载。
   不自动删除Storage、LAW表、PVC、保全容器或身份，防止误删证据。
6. 保留30天普通原文策略和案件保全责任人，确认全部案件解除/过期策略后再做独立退役审批。

## 9. 开发验证与当前能力边界

新采集、透明转发、缓冲、Azure交付、CLI及激活工具均为TypeScript；
现有Python仅增加可选配置校验、路由渲染、PG原生发布策略和绑定门禁的桥接，不复制采集逻辑。
离线执行第2节的npm、Python及Bicep三条验证命令；已有make时也可执行
`make validate-optional-l3`；CI有独立Node任务。
合成协议测试没有真实模型费用，也不证明客户云端IAM/网络/保全策略已验收。

本期不包括：独立审批读者UI、原文内容DLP、模型摘要、跨区域灾备、Exactly-once、
所有协议/任意WSS扩展零缺口、所有本地工具活动或全链路mTLS。
新增功能不能被描述成故障时保证每条原文不丢失；有更强合规需求时另行设计并批准。
