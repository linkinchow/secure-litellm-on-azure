# 验证 Codex 通过 WebSocket 调用 LiteLLM 管理的模型

> **历史旧网关/隔离环境WebSocket验证，不是当前入口的协议验收。** 2026-10-09用户已验证West US 3 private native网关的virtual key Codex Responses推理；这不能证明使用WebSocket或HTTP 101。当前API为独立Front Door/PLS及native ingress，不能将本文旧ingress结果当作已开放WS，也不能修改私有源站绕过路由门禁。当前协议、发布与拒绝测试按[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)执行。

## 验证目标

当前native 1.104.0链路已接入精确Responses WSS入口契约及API WAF方法规则；
现有West US 3需先按[WSS升级执行单](../local_execution/native-wss-upgrade-zh.md)更新ingress/WAF/绑定，
并执行两轮和工具回传验收。本页下方1.95.0、ingress-nginx及公开`/v1/models`步骤仅是历史记录，
不可用于当前Front Door/private native环境；本次也未扩展Entra代理协议。

本文验证以下完整链路：

```text
Codex CLI
  -> HTTPS/WSS
  -> ingress-nginx
  -> LiteLLM /v1/responses
  -> LiteLLM model_name
  -> Azure OpenAI / Foundry Deployment
```

一次完整验证必须同时满足：

1. LiteLLM 的 `/v1/models` 能看到目标模型别名。
2. 相同 Key 和模型通过 HTTP Responses API 调用成功。
3. Codex 正常完成一次真实推理。
4. 同一时间窗内，ingress access log 出现 Codex 对 `/v1/responses` 的 HTTP `101` Upgrade。

只看到 Codex 输出成功不能证明使用了 WebSocket，因为 Codex 可以在 WebSocket 失败后回退到 HTTP。只看到 `101` 也不能证明模型推理成功，因为握手后仍可能发生模型不存在、权限不足或上游调用失败。

## 已验证版本

测试日期：2026-08-10

| 组件 | 已验证版本或状态 |
|---|---|
| Codex CLI | `0.144.5` |
| Node.js | `24.11.1` |
| LiteLLM | `docker.litellm.ai/berriai/litellm:1.95.0` |
| LiteLLM Deployment | `1/1 Ready` |
| Ingress | ingress-nginx + HTTPS |
| API | Responses API |
| 目标模型 | LiteLLM model group `gpt-5.6-sol` |

旧的 `micl/litellm:mi-fix-image-gen` 镜像不支持该 Responses WebSocket 路由，不适用于本文验证。

## 前置条件

### LiteLLM 模型配置

目标模型必须已经通过 `litellm.config.yaml`、Admin UI 或 Model API 加入 LiteLLM。Codex 使用的是 LiteLLM 的 `model_name`，不是 Azure 后端 Deployment 名称。

示例：

```yaml
model_list:
  - model_name: gpt-5.6-sol
    litellm_params:
      model: azure/gpt-5.6-sol
      api_base: https://example.openai.azure.com/
      api_version: 2025-04-01-preview
```

目标模型需要支持 Responses API。Virtual Key 的模型白名单也必须包含该 `model_name`。

如果模型通过 Admin UI 或 Model API 管理，应确保 Pod 中已经启用：

```text
STORE_MODEL_IN_DB=true
```

不要同时用数据库和 ConfigMap 管理同一条模型 Deployment，以免产生重复路由或配置漂移。

### 网络与 TLS

- LiteLLM 必须通过有效 HTTPS 域名访问。
- WebSocket URL 由 Codex 从 `base_url` 推导，本文对应 `wss://litellm.example.com/v1/responses`。
- ingress-nginx 应允许长连接；本项目默认关闭响应缓冲，并将读写超时设为 600 秒。
- 当前 ingress-nginx 原生支持 WebSocket Upgrade，不需要额外添加 `Upgrade`/`Connection` annotation。

### 客户端工具

```powershell
codex --version
kubectl version --client
```

本文命令面向 PowerShell。

## 第一步：设置 Virtual Key

使用普通用户或 Team 的 LiteLLM Virtual Key，不要向终端输出 Key，也不要把 Master Key 分发给用户。

```powershell
$env:LITELLM_API_KEY = Read-Host "LiteLLM Virtual Key"
$litellmBaseUrl = "https://litellm.example.com/v1"
$model = "gpt-5.6-sol"
```

所有验证完成后删除环境变量：

```powershell
Remove-Item Env:LITELLM_API_KEY -ErrorAction SilentlyContinue
```

## 第二步：确认模型由 LiteLLM 暴露

```powershell
$headers = @{
  Authorization = "Bearer $env:LITELLM_API_KEY"
}

$models = Invoke-RestMethod `
  -Uri "$litellmBaseUrl/models" `
  -Headers $headers `
  -Method Get

$models.data.id | Sort-Object
```

输出必须包含目标 LiteLLM 模型别名，例如：

```text
gpt-5.6-sol
```

如果列表中没有目标模型，先检查：

- 模型是否已经写入 ConfigMap 或 LiteLLM 数据库。
- Virtual Key 的模型白名单是否允许该模型。
- UI 管理模型时，`STORE_MODEL_IN_DB` 是否为 `true`。
- 模型别名是否与 Codex 的 `-m` 参数完全一致。

## 第三步：验证 HTTP Responses 基线

该步骤先排除模型、Key 和 Responses API 本身的问题。

```powershell
$body = @{
  model = $model
  input = "Reply with exactly: HTTP-RESPONSES-OK"
  store = $false
  max_output_tokens = 64
} | ConvertTo-Json -Depth 10

$response = Invoke-RestMethod `
  -Uri "$litellmBaseUrl/responses" `
  -Headers $headers `
  -ContentType "application/json" `
  -Method Post `
  -Body $body

$response.output |
  Where-Object type -eq "message" |
  ForEach-Object { $_.content.text }
```

预期输出包含：

```text
HTTP-RESPONSES-OK
```

HTTP 基线失败时不要继续判断 WebSocket。先修复模型路由、Key 权限、Azure Managed Identity 或上游 Deployment。

## 第四步：配置 Codex Provider

在 Codex `config.toml` 中配置 LiteLLM Provider：

```toml
[model_providers.litellm]
name = "LiteLLM"
base_url = "https://litellm.example.com/v1"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
supports_websockets = true
```

关键项：

| 配置 | 作用 |
|---|---|
| `base_url` | 必须包含 `/v1`，Codex 会连接 `/v1/responses` |
| `env_key` | 从环境变量读取 Virtual Key |
| `wire_api = "responses"` | 使用 Responses API，而不是 Chat Completions |
| `supports_websockets = true` | 允许 Codex 优先使用 Responses WebSocket |

只设置 `wire_api = "responses"` 并不等于启用了 WebSocket。

可用以下 PowerShell 命令检查配置中的非敏感字段：

```powershell
$lines = Get-Content (Join-Path $HOME ".codex/config.toml")
$inSection = $false

foreach ($line in $lines) {
  if ($line -match '^\[model_providers\.litellm\]') {
    $inSection = $true
    $line
    continue
  }
  if ($inSection -and $line -match '^\[') { break }
  if ($inSection -and $line -match `
    '^(name|base_url|env_key|wire_api|supports_websockets)\s*=') {
    $line
  }
}
```

## 第五步：运行真实 Codex 推理

先记录 UTC 时间，随后立即运行 Codex：

```powershell
$startedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

codex exec `
  -c 'model_provider="litellm"' `
  -m $model `
  --sandbox read-only `
  --json `
  'Reply with exactly: CODEX-WS-E2E-OK'

$codexExitCode = $LASTEXITCODE
"Codex exit code: $codexExitCode"
```

成功结果应包含：

```json
{"type":"item.completed","item":{"type":"agent_message","text":"CODEX-WS-E2E-OK"}}
{"type":"turn.completed","usage":{}}
```

并且：

```text
Codex exit code: 0
```

## 第六步：从 ingress 日志证明使用了 WebSocket

执行：

```powershell
kubectl logs `
  -n ingress-nginx `
  -l app.kubernetes.io/component=controller `
  --all-containers=true `
  --prefix `
  --since-time=$startedAt |
  Select-String -Pattern '"GET /v1/responses HTTP/1.1" 101' -Context 0,2
```

若集群中只有一个 ingress controller Deployment，也可使用：

```powershell
kubectl logs `
  -n ingress-nginx `
  deployment/ingress-nginx-controller `
  --since-time=$startedAt |
  Select-String -Pattern '"GET /v1/responses HTTP/1.1" 101' -Context 0,2
```

真实成功样例：

```text
"GET /v1/responses HTTP/1.1" 101
"codex_exec/0.144.5 (Windows ...; aarch64) ..."
[litellm-litellm-mi-proxy-4000] ... 101
```

该记录同时证明：

- 请求来自真实 Codex CLI，而不是浏览器或独立测试工具。
- 请求路径是 `/v1/responses`。
- ingress 接受 WebSocket Upgrade 并返回 `101 Switching Protocols`。
- 连接被转发到 LiteLLM Service 的 `4000` 端口。

## 通过标准

完整通过必须同时满足：

| 检查 | 预期结果 |
|---|---|
| `GET /v1/models` | `200`，列表包含目标 `model_name` |
| HTTP `POST /v1/responses` | `200`，返回预期文本 |
| `codex exec` | Exit Code `0`，出现 `turn.completed` |
| ingress access log | Codex User-Agent 对 `/v1/responses` 返回 `101` |

本项目于 2026-08-10 得到以下实测结果：

```text
LiteLLM image: docker.litellm.ai/berriai/litellm:1.95.0
Codex: 0.144.5
Model group: gpt-5.6-sol
Codex result: CODEX-WS-E2E-OK
Codex exit code: 0
Ingress: GET /v1/responses HTTP/1.1 -> 101
Upstream: litellm-mi-proxy:4000
```

因此，该版本组合已经验证 Codex 能通过 WebSocket 调用 LiteLLM 管理的模型。

## 握手探针与真实推理的区别

低层探针可以只验证：

```text
HTTPS authentication: HTTP 200
WebSocket handshake: HTTP 101
```

如果探针故意发送不存在的模型，服务器可能在返回文本事件前关闭连接。这种结果仍证明 HTTPS 鉴权和 Upgrade 成功，但不能证明真实模型推理成功。

建议最终始终使用本文的真实 Codex 推理加 ingress `101` 日志完成闭环。

## 常见失败与判断

### Codex 成功，但日志没有 `101`

Codex 很可能回退到了 HTTP。检查：

- `supports_websockets` 是否确实为 `true`。
- `wire_api` 是否为 `responses`。
- 是否查询了正确的 ingress controller 和时间窗。
- Codex 是否复用了验证开始前已经建立的长连接。

最后一种情况可先结束现有 Codex 进程，再启动新的 `codex exec` 重测。

### WebSocket 返回 `404` 或 `405`

通常表示当前 LiteLLM 镜像没有注册 Responses WebSocket 路由。确认运行镜像：

```powershell
kubectl get deployment -n litellm litellm-mi-proxy `
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

本项目要求使用已验证的 LiteLLM `1.95.0` 或后续经过回归的版本。

### WebSocket 返回 `401` 或 `403`

检查：

- `LITELLM_API_KEY` 是否传给 Codex 子进程。
- Virtual Key 是否有效或已过期。
- ingress/WAF 是否允许带 Authorization header 的 Upgrade 请求。
- 是否错误地把 Master Key 或 Virtual Key 前后加了引号或空格。

先重新执行 `/v1/models` HTTP 鉴权控制。如果 HTTP 也失败，应先处理 Key，而不是 WebSocket。

### Codex 显示 WebSocket 重连后回退 HTTP

典型表现：

```text
Reconnecting... 5/5
Falling back to HTTP
```

检查 LiteLLM Pod、ingress controller 和 Azure Load Balancer 在该时间窗的日志，并确认：

- Upgrade 是否到达 ingress。
- ingress 是否返回 `101`。
- 长连接是否被代理超时中断。
- LiteLLM Pod 是否重启或被重新调度。

### `model_not_found` 或模型权限错误

这说明 WebSocket 可能已经连接成功，但 LiteLLM 无法路由目标模型。重新检查 `/v1/models`、Virtual Key 模型白名单和模型别名。

### 要求 `x-ms-oai-image-generation-deployment`

这是 Azure 内置 `image_generation` 工具缺少图像模型 Deployment 的错误，与 Responses WebSocket 支持无关。

## 升级后的回归要求

以下变更后应重新执行完整验证：

- LiteLLM 镜像升级。
- Codex CLI 升级。
- ingress-nginx、WAF、Load Balancer 或 TLS 配置变更。
- LiteLLM Virtual Key、Team 或模型白名单调整。
- 模型从 ConfigMap 迁移到数据库，或从数据库迁移回 ConfigMap。

不要只保留一次 `101` 截图。建议记录测试时间、Codex 版本、LiteLLM 镜像、模型别名、Codex Exit Code 和对应 ingress request ID。