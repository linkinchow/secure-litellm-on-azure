# 旧部署脚本参考：Foundry模型更新

> **当前同步请只使用[专用模型同步runbook](../docs/litellm-model-sync-runbook-zh.md)。** 下文保留旧`azure-openai.loc.json`/部署脚本的交叉组合与UI模型管理说明，不是当前West US 3 native私有网关的操作步骤。当前LiteLLM `1.104.8`固定源码路径禁用数据库模型存储，由受控配置管理模型；不要启用`STORE_MODEL_IN_DB`、重跑旧脚本或按旧VMSS身份授权方式绕过当前Workload Identity/私网校验。功能存在不代表当前环境已执行或通过模型同步验收。

本文历史说明：在Azure AI Foundry中为Azure OpenAI Resource新增deployment后，如何更新旧脚本部署的LiteLLM Proxy；全文“当前脚本/配置”均指该旧实现，不指当前客户网关。

## 1. 先确认你部署的类型

本项目当前支持的是：

```text
Azure AI Foundry / Azure OpenAI Resource
  └── Azure OpenAI deployment
        └── https://<resource-name>.openai.azure.com/
```

例如：

```text
Resource: your-azure-openai-resource
Endpoint: https://your-azure-openai-resource.openai.azure.com/
Deployment: gpt-5.4
```

这里的 `deployment_name` 是调用 Azure OpenAI API 时真正使用的名称。Azure OpenAI API 使用 deployment name，而不是只使用底层模型名称；两者可以相同，也可以不同。参考 [Microsoft Learn：Deploy a model](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/create-resource#deploy-a-model)。

当前仓库不直接支持下面这种 Foundry managed compute / online endpoint：

```text
Foundry Project
  └── Managed compute online endpoint
        └── ManagedOnlineDeployment
```

这类 endpoint 通常需要 Azure Machine Learning / Foundry 的 endpoint URL 和对应认证配置，不能直接填入当前 LiteLLM 的 `azure-openai-list`。

## 2. 在 Foundry 中获取三个关键值

在 Azure AI Foundry Portal 中：

1. 选择正确的 Subscription。
2. 选择 Azure OpenAI Resource，点击 **Use resource**。
3. 进入 **Management → Deployments**。
4. 找到新建的 deployment。
5. 记录以下信息：

```text
Azure OpenAI Resource name
Resource Group
Subscription ID
Endpoint
Deployment name
Underlying model name
```

其中最重要的是：

```text
Endpoint       → azure-openai-list[].endpoint
Resource name  → azure-openai-list[].name
Resource Group → azure-openai-list[].resource_group
Subscription   → azure-openai-list[].subscription_id
Deployment name → deployment_list[].deployment_name
Model name     → deployment_list[].model
```

可以使用 Azure CLI 查看某个 Resource 下的 deployments：

```powershell
az cognitiveservices account deployment list `
  --name <AZURE_OPENAI_RESOURCE_NAME> `
  --resource-group <RESOURCE_GROUP> `
  --subscription <SUBSCRIPTION_ID> `
  -o table
```

## 3. 判断是否需要修改 azure-openai-list

### 情况 A：现有 Azure OpenAI Resource 中新增 deployment

如果新模型部署在当前配置已经存在的 Resource 中，只需要修改 `deployment_list`。

例如当前已有：

```json
"azure-openai-list": [
  {
    "name": "your-azure-openai-resource",
    "endpoint": "https://your-azure-openai-resource.openai.azure.com/",
    "resource_group": "your-azure-openai-resource-group",
    "subscription_id": "<SUBSCRIPTION_ID>"
  }
]
```

只需新增 deployment：

```json
"deployment_list": [
  {
    "model": "gpt-5.4",
    "deployment_name": "gpt-5.4"
  },
  {
    "model": "gpt-5.6-terra",
    "deployment_name": "gpt-5.6-terra"
  }
]
```

### 情况 B：新 deployment 在新的 Azure OpenAI Resource 中

如果模型部署在另一个 Resource 中，需要同时新增 Resource：

```json
"azure-openai-list": [
  {
    "name": "aoai-eastus2-a",
    "endpoint": "https://aoai-eastus2-a.openai.azure.com/",
    "resource_group": "rg-aoai-eastus2-a",
    "subscription_id": "<SUBSCRIPTION_ID_A>"
  },
  {
    "name": "aoai-westus-b",
    "endpoint": "https://aoai-westus-b.openai.azure.com/",
    "resource_group": "rg-aoai-westus-b",
    "subscription_id": "<SUBSCRIPTION_ID_B>"
  }
]
```

并在 `deployment_list` 中加入要暴露的 deployment：

```json
"deployment_list": [
  {
    "model": "gpt-5.6-terra",
    "deployment_name": "gpt-5.6-terra"
  }
]
```

脚本会为每个 deployment 和每个 Resource 生成一个 LiteLLM model entry，从而实现多 Resource 之间的路由和重试。

## 4. deployment_list 的填写规则

当前配置格式如下：

```json
{
  "model": "gpt-5.6-terra",
  "deployment_name": "gpt-5.6-terra"
}
```

推荐新部署时让两个值保持一致：

```text
model             = 客户端在请求中使用的模型别名
 deployment_name  = Azure OpenAI 中真实的 deployment name
```

注意：JSON 中的实际键名没有前导空格，正确格式是：

```json
{
  "model": "gpt-5.6-terra",
  "deployment_name": "terra-prod"
}
```

如果使用：

```json
{
  "model": "gpt-5.6-terra",
  "deployment_name": "terra-prod"
}
```

当前 LiteLLM 生成器会把 `deployment_name` 作为 `model_name` 暴露给客户端，因此调用时应使用：

```json
{
  "model": "terra-prod"
}
```

为了避免LiteLLM路由配置与测试脚本之间出现模型名不一致，建议使用统一的客户模型别名。

## 5. 检查每个 Resource 是否都有该 deployment

当前脚本会对 `deployment_list × azure-openai-list` 做组合生成。例如：

```text
2 个 deployments × 3 个 Resources = 6 个 LiteLLM model entries
```

因此，如果把某个 deployment 放入 `deployment_list`，但它只存在于部分 Resource，调用到不存在该 deployment 的 Resource 时可能返回 404 或 deployment not found。

同步前建议确认：

```powershell
az cognitiveservices account deployment list `
  --name <RESOURCE_NAME> `
  --resource-group <RESOURCE_GROUP> `
  --subscription <SUBSCRIPTION_ID> `
  -o table
```

如果 deployment 只存在于一个 Resource，有两个选择：

1. 只把该 Resource 放入 `azure-openai-list`；
2. 扩展代码，让每个 deployment 显式声明可用 Resource。

当前仓库默认采用第一种简单模型，即同一个 `deployment_name` 应该存在于所有已配置 Resource 中。

## 6. 选择模型配置的权威来源

本方案支持两种模型管理方式，但建议只选择一种作为长期权威来源。

### 方式 A：GitOps / 部署脚本管理

继续维护 `azure-openai.loc.json`，然后运行部署脚本。优点是配置可审阅、可重复部署；缺点是每次新增模型都需要修改文件并触发 Pod 更新。

### 方式 B：LiteLLM Admin UI / 数据库管理

先通过部署环境变量开启：

```powershell
$env:STORE_MODEL_IN_DB = "true"
python .\deploy_mi_aks_litellm.py
```

之后可以在 Admin UI 的 Model Management 中直接新增、更新或删除模型，也可以修改 Router Settings。模型和路由设置保存在 PostgreSQL，并动态加入 Router，不需要为了已有 Foundry Resource 中的新 deployment 重新运行部署脚本。

UI 中添加 Azure OpenAI deployment 时至少填写：

```text
Model Name      = 客户端调用别名
LiteLLM Model   = azure/<Foundry deployment name>
API Base        = https://<resource-name>.openai.azure.com/
API Version     = 2025-04-01-preview（以模型实际要求为准）
Base Model      = 底层模型名称（用于成本和能力识别）
```

如果新 deployment 位于一个新的 Azure OpenAI Resource，UI 不会自动为 AKS Managed Identity 分配 Azure RBAC。必须先手工授予 `Cognitive Services OpenAI User`，或者把新 Resource 加入 JSON 后运行脚本完成授权，再由 UI 管理模型。

不要把同一条 deployment 同时写入 JSON 和 UI。LiteLLM 会合并 ConfigMap/YAML 模型与数据库模型，而不是自动去重或双向同步；混合管理可能形成重复后端。数据库中的同名 Router Settings 会覆盖 YAML 中的值。

## 7. 运行同步部署（GitOps 方式）

### 7.1 登录并选择 AKS 所在订阅

```powershell
az login
$env:AZURE_SUBSCRIPTION_ID = "<AKS_SUBSCRIPTION_ID>"
```

`AZURE_SUBSCRIPTION_ID` 用于选择 AKS、Resource Group 和 Managed Identity 的部署订阅；`azure-openai-list[].subscription_id` 用于指定每个 Azure OpenAI Resource 所在的订阅。

两者可以相同，也可以不同。

### 7.2 运行 LiteLLM 部署脚本

```powershell
cd C:\path\to\break-aoai-quota\LiteLLM
python .\deploy_mi_aks_litellm.py .\azure-openai.json
```

脚本会：

1. 读取并校验 `azure-openai.json`；
2. 为每个 Azure OpenAI Resource 检查或授予 Managed Identity 的 `Cognitive Services OpenAI User` 权限；
3. 重新生成 `litellm.config.yaml`；
4. 更新 Kubernetes ConfigMap，并根据配置 hash 自动触发 LiteLLM Pod 滚动更新；
5. 滚动更新 LiteLLM Deployment；
6. 重新运行 PostgreSQL 和 LiteLLM 的 Kubernetes 配置；
7. 输出新的 API Base URL。

`litellm.config.yaml` 是生成文件，不建议手工修改。下一次运行部署脚本时会被重新生成。

部署脚本会把配置文件的 SHA-256 hash 写入 LiteLLM Pod Template annotation。只要 deployment_list 或 zure-openai-list 变化，Kubernetes 就会自动创建新 Pod，加载最新模型列表。

## 8. 验证新模型是否同步成功

先获取 LoadBalancer 地址：

```powershell
kubectl get svc -n litellm
```

检查模型注册表：

```powershell
$headers = @{ Authorization = "Bearer <LITELLM_VIRTUAL_KEY>" }
Invoke-RestMethod `
  -Uri "http://<LOAD_BALANCER_IP>:4000/v1/models" `
  -Headers $headers
```

也可以直接发起 Chat 请求：

```powershell
$headers = @{
    Authorization = "Bearer <LITELLM_VIRTUAL_KEY>"
    "Content-Type" = "application/json"
}

$body = @{
    model = "gpt-5.6-terra"
    messages = @(
        @{
            role = "user"
            content = "请只回复 ok"
        }
    )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri "http://<LOAD_BALANCER_IP>:4000/v1/chat/completions" `
  -Method Post `
  -Headers $headers `
  -Body $body
```

运行仓库统一测试：

```powershell
cd C:\path\to\break-aoai-quota
python .\tests\test_all_deployments.py `
  --config .\LiteLLM\azure-openai.json `
  --base-url "http://<LOAD_BALANCER_IP>:4000" `
  --api-key "<LITELLM_VIRTUAL_KEY>" `
  --prompt ok
```

Windows 默认 `cp1252` 控制台可能无法输出中文 prompt，测试时使用 `--prompt ok` 可以避免输出编码问题。

## 9. 常见问题

### 9.1 返回 deployment not found

通常原因：

- `deployment_name` 写错；
- 写成了底层模型名，但 Azure 中实际 deployment name 不同；
- deployment 没有部署到 `azure-openai-list` 中的某个 Resource；
- `endpoint`、Resource Group 或 Subscription 对应错误。

先用 Azure CLI 查看真实 deployment name，再逐项对照 JSON。

### 9.2 新模型没有出现在 `/v1/models`

检查：

```text
1. deployment_list 是否保存了新模型；
2. deploy_mi_aks_litellm.py 是否重新运行；
3. litellm.config.yaml 是否包含新 model_name；
4. Kubernetes ConfigMap 是否更新；
5. LiteLLM Pod 是否完成滚动更新。
```

可以查看：

```powershell
kubectl get configmap litellm-config -n litellm -o yaml
kubectl get pods -n litellm
kubectl logs deployment/litellm-mi-proxy -n litellm --tail=100
```

### 9.3 Foundry Project 名称能不能直接填到 azure-openai-list

不能直接填。当前脚本需要的是 Azure OpenAI Resource 的 endpoint：

```text
https://<resource-name>.openai.azure.com/
```

Project 名称、Managed Compute endpoint 和 Azure OpenAI Resource 名称不是同一个维度。

### 9.4 新模型需要修改 api_version 吗

当前脚本对所有模型默认生成 `2025-04-01-preview`。如果新模型需要特定 API version，需要扩展 `generate_litellm_config()`，让每个 deployment 可以配置独立的 `api_version`，而不是只根据模型名称统一设置。

## 10. 删除或下线模型

从 `deployment_list` 删除模型并重新运行部署脚本，只会让 LiteLLM 不再生成该模型的路由；它不会自动删除 Azure AI Foundry 中已经存在的 deployment。

如果要彻底删除 Azure deployment，需要在 Foundry Portal 或 Azure CLI 中单独删除。