# 客户 API/Admin 证书导入 Key Vault Runbook

> 适用：客户已经提供两套服务器证书，目标环境已完成 Stage4 certificate-vault。
> 与当前 Traefik 私有入口、原生及延期 Entra 模式共用的证书契约一致。
> 本文是人工操作说明，不表示已在客户环境上传或发布。所有操作使用客户自己的租户、订阅和配置。

## 1. 上传什么，上传到哪里

阿里云 Nginx 格式下载包通常是四个文件；文件名前可能有编号，以实际文件为准：

```text
llm-api.<baseDomain>.pem
llm-api.<baseDomain>.key
llm-admin.<baseDomain>.pem
llm-admin.<baseDomain>.key
```

`.pem` 是服务器叶证书及可能附带的中间链，`.key` 是匹配的叶私钥。
文件名和后缀不证明证书正确，也不证明 `.pem` 已包含完整链。

| 平面 | 合并后的内容，按顺序排列 | 证书专用 Vault 中的对象 |
| --- | --- | --- |
| API | API 叶证书 → 所需中间链 → API 未加密叶私钥 | **Secret** `api-tls` |
| Admin | Admin 叶证书 → 所需中间链 → Admin 未加密叶私钥 | **Secret** `admin-tls` |

**四个输入文件最终生成两个 Secret，不是四个 Secret，也不是 Key Vault Certificates 对象。**
不要用 `az keyvault certificate import` 替代本流程，不上传 CSR、CA 私钥或只有证书的文件。
仅提供 `.pem` 而没有匹配叶私钥时停止，请客户补齐；不能从证书恢复私钥。

两份源站证书都须覆盖实际域名 `llm-api.<baseDomain>` / `llm-admin.<baseDomain>`，
由 Front Door 信任的公有 CA 签发，根须在 Microsoft Trusted CA List 中。
内部 CA、自签名和仅某 CDN 信任的 Origin 证书不适用；不要关闭回源名称或证书验证。
域名可由合法 SAN 通配符覆盖，但须通过后面的实际主机名校验。

这两份是 **Front Door 到 Traefik 的源站证书**。客户端到 Front Door 的证书仍由 Azure 托管，
本操作不将阿里云证书上传为 Front Door BYOC，也不修改 DNS、WAF、FDID 或业务流量开关。
完整设计见 [TLS 证书设计](litellm-ingress-tls-certificate-design-zh.md)。

## 2. 开始前的检查

准备以下条件：

- 一份当前、获批且不包含明文凭据的客户 CFG；证书 Vault、域名和目标 RG 都从该配置及实际部署输出核对。
- Azure CLI、jq、OpenSSL 3、仓库 `.venv` 及项目依赖；从仓库根目录的 Bash 终端执行。
- 获批的人工导入身份：`certificateImporterPrincipalId` 对应 **User 或 Group 的 Object ID**，
  在证书专用 Vault 上拥有 **Key Vault Secrets Officer**。
- 发布身份 `ingressReaderPrincipalId` 是另一个主体，使用 **Key Vault Secrets User** 读取材料；
  不为导入方便将部署 UAMI、应用 Pod 或 Traefik 提升为证书写入者。
- 管理面读取部署输出的身份；Secrets Officer 不等于目标 RG Reader。没有读取权限时，
  由已有权限的部署人员提供并共同核对以下输出，不临时扩大导入人权限。
- 获批网络路径、证书交付/保管方式和变更窗口；已存在同名 Secret 时，另有证书轮换审批及旧版本记录。

### 2.1 核对目标环境与实际 Vault

以下管理面只读查询使用已有部署会话。`CFG` 换成客户正在部署的文件，不套用其他客户的 Vault 名称。

```bash
set -euo pipefail
set +x
umask 077
CFG="local_execution/customer.json"
TENANT_ID="$(jq -er '.azure.tenantId' "$CFG")"
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' "$CFG")"
TARGET_RG="$(jq -er '.target.resourceGroup' "$CFG")"
ENVIRONMENT_NAME="$(jq -er '.environment' "$CFG")"
BASE_DOMAIN="$(jq -er '.baseDomain' "$CFG")"
CONFIGURED_VAULT="$(jq -er '.parameters["certificate-vault"].vaultName' "$CFG")"
IMPORTER_OBJECT_ID="$(jq -er '.parameters["certificate-vault"].certificateImporterPrincipalId' "$CFG")"
IMPORTER_TYPE="$(jq -er '.parameters["certificate-vault"].certificateImporterPrincipalType' "$CFG")"

CERT_OUTPUT="$(az deployment group show \
  --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "llmgw-${ENVIRONMENT_NAME}-s4-certificate-vault" \
  --query '{state:properties.provisioningState,vault:properties.outputs.certificateVault.value}' \
  --output json --only-show-errors)"
EXPECTED_VAULT_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${TARGET_RG}/providers/Microsoft.KeyVault/vaults/${CONFIGURED_VAULT}"
printf '%s' "$CERT_OUTPUT" | jq -e \
  --arg name "$CONFIGURED_VAULT" --arg id "$EXPECTED_VAULT_ID" '
    .state == "Succeeded" and .vault.name == $name
    and ((.vault.id | ascii_downcase) == ($id | ascii_downcase))
    and .vault.apiTlsSecretId == ("https://" + $name + ".vault.azure.net/secrets/api-tls")
    and .vault.adminTlsSecretId == ("https://" + $name + ".vault.azure.net/secrets/admin-tls")
  ' >/dev/null
CERT_VAULT="$(printf '%s' "$CERT_OUTPUT" | jq -er '.vault.name')"
API_SECRET_BASE="$(printf '%s' "$CERT_OUTPUT" | jq -er '.vault.apiTlsSecretId')"
ADMIN_SECRET_BASE="$(printf '%s' "$CERT_OUTPUT" | jq -er '.vault.adminTlsSecretId')"
printf 'environment=%s tenant=%s subscription=%s rg=%s vault=%s\n' \
  "$ENVIRONMENT_NAME" "$TENANT_ID" "$SUBSCRIPTION_ID" "$TARGET_RG" "$CERT_VAULT"
```

核对 `privateIngress.api.tlsSecretId` / `privateIngress.admin.tlsSecretId` 分别指向上述两个地址，
或其获批的固定版本；不能指向 `/certificates/`、另一个 Vault、同一个 Secret 或后台应用 Vault。
固定版本的处理见第 6 节。`certificateMaterialsImported=false` 是基础设施固定输出，
不随人工上传改变，不需要重跑 certificate-vault 来更新这个布尔值。

没有本组件的实际输出时，停止套用这段命令，先由 Owner 核对外部证书 Vault 的等效配置、
权限及私网接入；不要按相似名称猜目标。

### 2.2 选择已批准的上传网络路径

| 场景 | 操作方式 | 网络完成条件 |
| --- | --- | --- |
| 已有获批私网操作机或 Runner，能到证书 Vault PE | **优先保持 Vault 公网关闭**。证书经客户批准的加密传输到受控私网机器，在该机器上传；人工导入人登录，不使用部署 UAMI 写证书 | Vault 正常域名解析到实际 PE 的私有 IP，TLS/路由及数据权限正常 |
| 证书在客户本机，且已批准短时受限公网导入 | 按 [迁移指南 4-C](customer-migration-guide-zh.md#4-c-部署后手动导入两个secret) 由网络管理员开启 Selected networks，只批准本机实际公网出口 IPv4 `/32`；本机上传 | 无论成功、失败或中断，都立即关闭公网、清除临时规则并复核 |
| Policy/NSP 禁止公网例外，且本机不能到 PE | 使用获批的私网导入机器；当前没有可达路径则停止 | 不绕过 Policy，不开放 All networks，不改 hosts 或关闭 TLS 验证 |

临时例外只作用于**证书专用 Vault**，不是 API/Admin 入口或其他业务 Vault。
保持 `defaultAction=Deny`、`bypass=None`，保留 PE/DNS、RBAC、软删除、清除保护和删除锁；
不修改 IaC 来长期保留公网，窗口内不并行重部署 certificate-vault。
私网路径的“能解析到 10.x”不是充分证明，还须与实际 Vault PE 地址核对。

在证书文件所在、可达 Vault 的受控机器继续第 3–5 节。同机操作时保持变量；
换机器时只传递获批配置、目标元数据及证书材料，不分享 Azure 登录缓存。

## 3. 按阿里云四文件格式整理并校验

原始材料保持不变。先创建独占、权限 0700 的临时目录，再合并为权限 0600 的两个文件。
不在 Cloud Shell、GitHub artifact、Git、聊天或共享目录中保存私钥。
若下载文件带编号，下面四个输入路径按实际文件名修改。

```bash
set -euo pipefail
set +x
umask 077
CERT_DIR="REPLACE_ABSOLUTE_DIRECTORY_WITH_FOUR_FILES"
API_CHAIN_FILE="$CERT_DIR/llm-api.${BASE_DOMAIN}.pem"
API_LEAF_KEY_FILE="$CERT_DIR/llm-api.${BASE_DOMAIN}.key"
ADMIN_CHAIN_FILE="$CERT_DIR/llm-admin.${BASE_DOMAIN}.pem"
ADMIN_LEAF_KEY_FILE="$CERT_DIR/llm-admin.${BASE_DOMAIN}.key"
for INPUT_FILE in "$API_CHAIN_FILE" "$API_LEAF_KEY_FILE" "$ADMIN_CHAIN_FILE" "$ADMIN_LEAF_KEY_FILE"; do
  if [[ ! -f "$INPUT_FILE" || -L "$INPUT_FILE" ]]; then
    printf 'Missing or symlinked certificate input: %s\n' "$INPUT_FILE" >&2
    exit 1
  fi
done
IMPORT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/llmgw-certificate-import.XXXXXXXX")"
API_PEM_FILE="$IMPORT_DIR/api.pem"
ADMIN_PEM_FILE="$IMPORT_DIR/admin.pem"
{ cat "$API_CHAIN_FILE"; printf '\n'; cat "$API_LEAF_KEY_FILE"; printf '\n'; } > "$API_PEM_FILE"
{ cat "$ADMIN_CHAIN_FILE"; printf '\n'; cat "$ADMIN_LEAF_KEY_FILE"; printf '\n'; } > "$ADMIN_PEM_FILE"
chmod 600 "$API_PEM_FILE" "$ADMIN_PEM_FILE"
printf 'Private import directory: %s\n' "$IMPORT_DIR"
```

如叶私钥有密码，先由客户在该受控机器解密到另一个权限 0600 的临时文件，例如
`openssl pkey -in "$API_LEAF_KEY_FILE" -out "$IMPORT_DIR/api-unencrypted.key"`。
OpenSSL 交互输入密码；不要用命令行 `-passin pass:...` 或打印密码。随后以解密文件重新合并，
Admin 同理。本流程的合并文件必须含未加密叶私钥，不能直接上传加密 `.key`。

使用与发布器相同的 `certificate_material` 校验，再使用项目 certifi 公有根执行信任链检查：

```bash
.venv/bin/python - "$BASE_DOMAIN" "$API_PEM_FILE" "$ADMIN_PEM_FILE" "$IMPORT_DIR" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

import certifi
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from service_identity.exceptions import CertificateError, VerificationError

from scripts.private_ingress import certificate_material

rows = []
try:
    for plane, filename in zip(("api", "admin"), sys.argv[2:4], strict=True):
        raw = Path(filename).read_bytes()
        if len(raw) > 25 * 1024:
            raise ValueError("Key Vault Secret size limit")
        host = f"llm-{plane}.{sys.argv[1]}"
        material = certificate_material(raw.decode("utf-8"), host)
        chain = Path(sys.argv[4]) / f"{plane}-chain.pem"
        chain.write_text(material["certificate"], encoding="utf-8")
        verified = subprocess.run(
            ["openssl", "verify", "-CAfile", certifi.where(), "-purpose", "sslserver",
             "-verify_hostname", host, "-untrusted", str(chain), str(chain)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if verified.returncode != 0:
            raise ValueError("Public certificate chain verification failed")
        rows.append({"plane": plane, "sha256": material["sha256"],
                     "expiresAt": material["expiresAt"], "publicChainVerified": True})
except (OSError, ValueError, TypeError, UnsupportedAlgorithm, x509.ExtensionNotFound,
        CertificateError, VerificationError, subprocess.TimeoutExpired):
    raise SystemExit("Certificate validation failed; no private material printed.") from None
print(json.dumps(rows, ensure_ascii=False))
PY
```

只有两份全部通过才继续。检查覆盖：

- SAN 与实际主机名匹配，证书与叶私钥一致，serverAuth 用途满足发布器要求；
- 当前有效，至少还剩 **7 天**；实际运维宜保留更长续期缓冲；
- **每个合并值不超过 25KB**，符合 Key Vault Secret 限制；
- 所需中间链齐全，使用 certifi 公有根可以构建有效链。

缺链时向客户/CA补齐正确中间证书，叶证书在前，通常不需附根证书；
不要用任意 CA 文件掩盖错误。该预检不替代 Front Door 的 Microsoft 信任列表和实际回源验证。
保留输出的两份指纹和到期时间，不保存或粘贴 PEM 内容。

## 4. 使用获批人工身份登录

在导入专用终端用独立 Azure CLI 配置目录，避免覆盖同一 Runner 的部署身份登录。
租户、订阅及 Object ID 仍用第 2 节已核对的值，不使用 GitHub Client ID 代替人工 Object ID。

```bash
set -euo pipefail
PREVIOUS_AZURE_CONFIG_DIR="${AZURE_CONFIG_DIR-}"
export AZURE_CONFIG_DIR="$IMPORT_DIR/azure-cli"
az login --tenant "$TENANT_ID" --use-device-code --output none --only-show-errors
az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{tenant:tenantId,subscription:id,type:user.type}' \
  --output json --only-show-errors \
  | jq -e --arg tenant "$TENANT_ID" --arg sub "$SUBSCRIPTION_ID" '
      .tenant == $tenant and .subscription == $sub and .type == "user"
    ' >/dev/null
SIGNED_IN_OBJECT_ID="$(az ad signed-in-user show --query id --output tsv --only-show-errors)"
if [[ "$IMPORTER_TYPE" == "User" ]]; then
  if [[ "${SIGNED_IN_OBJECT_ID,,}" != "${IMPORTER_OBJECT_ID,,}" ]]; then
    printf 'Signed-in user is not the approved certificate importer.\n' >&2
    exit 1
  fi
elif [[ "$IMPORTER_TYPE" == "Group" ]]; then
  printf 'Owner must verify user %s belongs to approved importer group %s before upload.\n' \
    "$SIGNED_IN_OBJECT_ID" "$IMPORTER_OBJECT_ID"
  read -r -p 'Owner已核对本用户的组成员资格及有效权限，输入approved: ' GROUP_APPROVAL
  if [[ "$GROUP_APPROVAL" != "approved" ]]; then
    printf 'Importer group membership is not approved.\n' >&2
    exit 1
  fi
else
  printf 'Manual importer must be a User or Group.\n' >&2
  exit 1
fi
az keyvault secret list --subscription "$SUBSCRIPTION_ID" --vault-name "$CERT_VAULT" \
  --query "[?name=='api-tls' || name=='admin-tls'].{name:name,id:id,enabled:attributes.enabled}" \
  --output json --only-show-errors > "$IMPORT_DIR/before-secrets.json"
jq '.' "$IMPORT_DIR/before-secrets.json"
if jq -e 'length > 0' "$IMPORT_DIR/before-secrets.json" >/dev/null; then
  printf 'Existing TLS secrets found: obtain rotation approval and record old version IDs.\n'
  read -r -p '已批准轮换并记录旧版本的实际变更编号: ' ROTATION_TICKET
  : "${ROTATION_TICKET:?Existing secrets require an approved rotation ticket}"
fi
```

列表只输出元数据，不返回 Secret value，也不证明写权限。Group 的成员资格及继承/条件授权
由 Owner 核对；上面的确认输入不是自动授权或新增角色。读取/登录失败时停止，不换 Owner
账号掩盖错误，不授予应用身份写权限。若在受限公网窗口中，失败或中断也须立即关闭窗口。

## 5. 上传两个 Secret，并只保留元数据

在获批窗口、两份本地校验及身份核对完成后，执行：

```bash
set -euo pipefail
az keyvault secret set --subscription "$SUBSCRIPTION_ID" --vault-name "$CERT_VAULT" \
  --name api-tls --file "$API_PEM_FILE" --encoding utf-8 \
  --content-type application/x-pem-file \
  --query '{id:id,enabled:attributes.enabled}' --output json --only-show-errors \
  > "$IMPORT_DIR/api-upload.json"
jq -e --arg base "$API_SECRET_BASE/" '
  .enabled == true and (.id | startswith($base))
' "$IMPORT_DIR/api-upload.json" >/dev/null
jq '.' "$IMPORT_DIR/api-upload.json"

az keyvault secret set --subscription "$SUBSCRIPTION_ID" --vault-name "$CERT_VAULT" \
  --name admin-tls --file "$ADMIN_PEM_FILE" --encoding utf-8 \
  --content-type application/x-pem-file \
  --query '{id:id,enabled:attributes.enabled}' --output json --only-show-errors \
  > "$IMPORT_DIR/admin-upload.json"
jq -e --arg base "$ADMIN_SECRET_BASE/" '
  .enabled == true and (.id | startswith($base))
' "$IMPORT_DIR/admin-upload.json" >/dev/null
jq '.' "$IMPORT_DIR/admin-upload.json"
API_VERSION_ID="$(jq -er '.id' "$IMPORT_DIR/api-upload.json")"
ADMIN_VERSION_ID="$(jq -er '.id' "$IMPORT_DIR/admin-upload.json")"
```

不启用 `--debug` 或 shell trace，不使用 `--value` 把私钥放到进程命令行。
CLI 默认返回可能含 Secret value，必须保留上面的元数据 `--query`，不将原始 CLI 输出写日志。

同名 Secret 的 `set` 会创建新版本；旧版本不删除、不 purge。两次写入**不是原子事务**：
API 成功、Admin 失败时只能记为部分完成，不开始入口发布；核对已生成版本后按批准方案补齐，
不要盲目重跑两条命令制造更多版本。已删除保留中的同名 Secret 由 Owner 处理，不擅自 purge。

**如使用了临时公网，立即由网络管理员关闭，不能等入口发布成功后再关。**
按 [4-C 第 6 步](customer-migration-guide-zh.md#4-c-部署后手动导入两个secret) 清除本次规则并确认
`publicNetworkAccess=Disabled`、`defaultAction=Deny`、`bypass=None`、`ipRules=[]`，
PE 仍 Approved。关闭窗口的责任人须在上传开始前确定，终端中断不能解除该责任。
一直走私网时不需要开启或关闭公网。

## 6. 让入口真正使用新证书

**Secret 上传成功 ≠ 运行中的 Traefik 已换证。** Traefik 不直接监听 Key Vault 新版本。

1. 保存两个完整带版本 ID、两份叶证书指纹/到期时间、变更编号及网络窗口关闭证据。
2. CFG 使用无版本引用时，Stage4 plan 会解析到当时最新具体版本；执行前若版本变化，旧计划失效。
   固定版本引用时，必须将 `privateIngress.api/admin.tlsSecretId` 更新为本次对应完整版本 ID，
   按客户配置管理流程同步；不要修改生成的 manifest 或复用旧哈希。
3. 两份由客户手动提供时，跳过 API ACME `certificate-renew`，续期由客户供证流程负责；
   不并行运行自动签发覆盖本次 API Secret，不清空 ACME 状态。
4. 退出导入身份，回到原来获批的部署终端。下面本地执行步骤使用部署/runtime身份及已有阶段门禁，
   **不拿人工 Secrets Officer 身份直接操作 AKS**：

```bash
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage4-private-ingress --operation plan
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage4-private-ingress --operation execute \
  --approved-plan-sha256 "REPLACE_THIS_CERTIFICATE_PLAN_SHA256"
```

审核 plan 中 `certificates.api/admin.secretId` 与本次版本 ID、指纹、到期时间；
计划本身包括私网读取、公有链预检和 server-side dry-run，不是纯离线文件校验。
execute 成功后确认 `applied=true`、`verified=true`，实际两个私有入口提供预期指纹。
发布可能触发 ingress 滚动更新，须安排维护窗口。

首次部署接着走正常 Stage9，不因为上传证书而提前开流量。
已发布环境换证后，若 ingress Pod template/绑定状态、客户配置或代码版本变化，
按当前 [本地执行指南](../local_execution/stage2-9-guide-zh.md) 重新 plan/execute `stage9-edge-bind`
并刷新发布报告；这不要求重跑 origin、edge-prepare、edge-release 或 DNS。
继续验证两条 Front Door 私有源站健康、实际 HTTPS/WSS 推理及 Admin 登录，
不能只用浏览器看到的 Front Door 托管证书证明源站已换证。

## 7. 收尾与失败处理

在导入专用终端退出临时登录，恢复原 Azure CLI 配置选择：

```bash
az logout
az account clear
if [[ -n "$PREVIOUS_AZURE_CONFIG_DIR" ]]; then
  export AZURE_CONFIG_DIR="$PREVIOUS_AZURE_CONFIG_DIR"
else
  unset AZURE_CONFIG_DIR
fi
```

按客户保管规则清理**本次明确生成的**合并 PEM、解密私钥、临时登录缓存及目录，
不删除原始证书或唯一恢复副本，不递归删除整个 `/tmp`、仓库或用户目录。
仅将获批元数据存入受控变更记录，不上传临时目录到 Git/GitHub artifact。

| 现象 | 处理 |
| --- | --- |
| 合并材料校验失败 | 检查 fullchain 顺序、缺中间证书、私钥匹配/加密状态、SAN、有效期及 25KB；不上传 |
| Vault 超时或 `ForbiddenByFirewall` | 按所选路径核对实际 PE/DNS/路由，或获批公网出口及窗口；不扩大到 All networks |
| RBAC `Forbidden` | 核对租户、实际导入主体、Secrets Officer 作用域、组/条件及角色传播；不临时改成 Owner |
| 只有一个 Secret 上传成功 | 记为部分完成，立即关闭任何公网窗口；保留成功版本，按批准方案补齐另一个 |
| 上传成功但 Stage4 读取/信任校验失败 | 上传与发布身份分开排查；保留新版本，不关闭 TLS/名称验证，不声称入口已更新 |
| 新版本导致发布或回源失败 | 确认旧版本仍有效、未泄露/未撤销，再固定旧版本重新 plan/审核/execute；没有自动回退或原子换证 |

最终闭环：两份版本/指纹已核对、无临时公网遗留、实际发布身份私网读取成功、
两个 ingress 实际证书一致、Front Door 回源与业务验收通过。

参考：[Key Vault Secret CLI](https://learn.microsoft.com/en-us/cli/azure/keyvault/secret)、
[Key Vault RBAC](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide)、
[Key Vault 网络安全](https://learn.microsoft.com/en-us/azure/key-vault/general/network-security)、
[Front Door 端到端 TLS](https://learn.microsoft.com/en-us/azure/frontdoor/end-to-end-tls)。
