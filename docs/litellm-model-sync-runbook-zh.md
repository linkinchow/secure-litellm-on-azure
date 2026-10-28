# LiteLLM 模型目录批量更新 Runbook

本文是通过 JSON 配置接入和更新已有模型部署的独立操作手册。流程、字段、
审批、验收和恢复说明集中在本文，不属于首次部署 Stage0–9 的执行步骤。

## 1. 范围与前提

适用于已经正常运行的私有 LiteLLM 网关。客户可以在同一 Entra 租户的多个
Azure 订阅中选择多个 Foundry/Azure OpenAI 账号，并为每个账号明确指定已有部署。
当前连接器只支持提供标准 Azure OpenAI endpoint 的 OpenAI 模型部署；不是
任意 Foundry 模型或第三方 endpoint 的通用连接器。

默认策略是 `replace`：`MODEL_CATALOG` 是整个网关的完整模型清单，
未列出的 LiteLLM 模型映射会移除，包括其他资源中的模型和同一部署的旧客户端别名。
命令不会创建、修改或删除 Azure 模型部署，不删除 PE/DNS/角色或已有连接记录，
也不自动修改 vkey、预算、日志和数据库。
同一客户端 `model_name` 可以在多个账号配置不同 deployment 名，接入同一模型组。
不要把不同用途或不兼容的模型混到同一个组；同组真实模型家族必须一致。

需要准备：

- 正常运行的 AKS、LiteLLM Deployment、现有 workload identity 和私网基础设施。
- 能访问 AKS 私有 API 的受控 Runner，整个操作期间保持 AKS 运行。
- 真实客户配置 `CFG`，包含现有 deployment、connection 和模型映射。
- 实际获批的变更编号、批准人以及变更窗口。
- 每个目标模型支持的推理 API version；不根据文件名或发布日期猜测。

不使用模型 API Key 或 Master Key，不改变账号公网访问开关、TLS 校验、
用户认证、Front Door、WAF 白名单、APIM 或数据库 schema。

**现状边界（2026-10-09）：** West US 3已有 greenfield native dev/test 网关运行，
Admin原生登录和virtual key的Responses调用已由客户验证。只读管理面核对了
私有AKS运行、Front Door双入口启用、PostgreSQL Entra-only认证、Managed Redis
访问密钥认证禁用及私有Vault；这不等于production验收或本工具已在客户环境
执行通过。以下仍是独立的受审批变更流程，不需要重新部署整个网关。

## 2. JSON 配置结构

完整通用示例见
[azure-openai.catalog.example.json](../local_execution/azure-openai.catalog.example.json)。
结构为 **订阅 → 资源账号 → 模型部署**，不是对所有资源套用一份全局部署名单。

```json
{
  "schema_version": 1,
  "subscriptions": [
    {
      "subscription_id": "11111111-1111-4111-8111-111111111111",
      "resources": [
        {
          "resource_group": "rg-models-east",
          "name": "example-foundry-east",
          "endpoint": "https://example-openai-east.openai.azure.com/",
          "models": [
            {
              "model_name": "customer-chat",
              "deployment_name": "chat-east",
              "litellm_params": {
                "api_version": "v1",
                "rpm": 120,
                "tpm": 60000
              },
              "model_info": {
                "input_cost_per_token": 0.000002,
                "output_cost_per_token": 0.000008
              }
            }
          ]
        }
      ]
    },
    {
      "subscription_id": "22222222-2222-4222-8222-222222222222",
      "resources": [
        {
          "resource_group": "rg-models-west",
          "name": "example-foundry-west",
          "models": [
            {
              "model_name": "customer-chat",
              "deployment_name": "chat-west",
              "litellm_params": {
                "api_version": "v1"
              }
            }
          ]
        }
      ]
    }
  ]
}
```

示例中的资源、UUID、价格和 API version 仅用于展示格式，不是实际客户资源、
有效批准记录、价格建议或所有模型均支持的版本。实际账号名、部署名、价格和
API version 必须由客户确认。

### 2.1 身份字段

| 层级 | 字段 | 含义 |
| --- | --- | --- |
| 顶层 | `schema_version` | 当前结构版本，必须为 `1` |
| 订阅 | `subscription_id` | 目标订阅 UUID；逐订阅验证租户和读取权限 |
| 资源 | `resource_group` | 该 Foundry/Azure OpenAI 账号所在 RG |
| 资源 | `name` | Azure 门户资源概览中的资源名称，对应 ARM 的 `name`；不是 Foundry project、deployment 或模型名 |
| 资源 | `endpoint` | 可选的预期 Azure OpenAI endpoint；填写时必须与 Azure 管理面查询结果一致 |
| 模型 | `model_name` | 客户端调用 LiteLLM 时使用的别名，例如 `coding` |
| 模型 | `deployment_name` | **当前资源账号内**已经存在的 Azure deployment 名 |
| 模型 | `litellm_params` | 可选的模型级调用参数和限流配置 |
| 模型 | `model_info` | 可选的模型成本和元数据覆盖 |

模型属于其所在资源，不再跨全部账号搜索“任意一个匹配就成功”。
指定资源缺少指定 deployment、部署尚未就绪、资源身份不一致或发现不完整时停止。
不允许重复订阅、资源或精确模型映射，未知结构字段明确报错。

无需填写 APIM、region、账号 alias 或内部模型 ID。连接器从 Azure 管理面核验
账号 hostname 和 deployment 信息，生成稳定 connection alias、资源 ID 和内部
模型 ID。旧的 `azure-openai-list` / `deployment_list` 格式不再接收；按照本文
迁移到明确的资源级映射。此前示例的 `account_name` 改为 `name`，旧字段明确
报错，不同时接收两个容易混淆的名字。

`name` 用于在指定订阅和资源组中定位 Azure 资源。资源名与 endpoint 的
hostname 不要求相同；上例特意展示了不同名字，实际值仍须由客户核实。
不能从 endpoint 猜测资源名，也不能把模型 deployment 名填入资源 `name`。
字段名称与 Azure 的
[Microsoft.CognitiveServices/accounts 资源定义](https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/accounts)
中的 `name` 保持一致。

`endpoint` 是可选的**预期值核验**，不是自由覆盖连接地址：

- 省略时，从 Azure 管理面发现实际 Azure OpenAI endpoint。
- 填写时，核验 HTTPS 账号根地址，并在规范化后与实际 endpoint 比较；
  scheme/hostname 大小写和单个末尾 `/` 不影响相等判断。
  显式标准端口 `:443` 可填写，规范化时移除；`:0443` 等非标准写法拒绝。
- 不接受用户信息、查询参数、fragment、任意 API 路径、非 HTTPS 地址、
  非标准端口或其他服务类型的 endpoint。显式填写空字符串、`null` 或非字符串
  会失败，不等同于省略；空格、控制字符和百分号编码也不接受。
- 资源或 endpoint 不匹配时，在任何实际写入前失败，不改连客户输入的地址。
- Azure 元数据缺失或有歧义时也失败，不把手填 endpoint 当作绕过发现的兜底。

私网访问仍使用标准 `<hostname>.openai.azure.com`，由私有 DNS 解析到 PE；
不要填写 `privatelink.openai.azure.com` 地址。可选 endpoint 不扩展连接器的
服务类型、区域云或认证支持范围。

### 2.2 与官方 config.yaml 的对应

参考 LiteLLM 官方说明：

- [模型配置与别名](https://docs.litellm.ai/docs/proxy/configs)
- [配置字段参考](https://docs.litellm.ai/docs/proxy/config_settings)
- [自定义价格与 base_model](https://docs.litellm.ai/docs/proxy/custom_pricing)

JSON 中的 `model_name`、`litellm_params`、`model_info` 对应官方 `model_list` 条目。
工具生成并维护以下字段：

```yaml
model_list:
  - model_name: customer-chat
    litellm_params:
      model: azure/chat-east
      api_base: https://<verified-account-host>.openai.azure.com
      api_version: "v1"
      rpm: 120
      tpm: 60000
    model_info:
      id: <stable-generated-id>
      base_model: azure/<verified-or-explicit-model-family>
      input_cost_per_token: 0.000002
      output_cost_per_token: 0.000008
```

这是受控的模型级配置接口，**不是任意 config.yaml 字段透传**。全局
`general_settings`、`router_settings`、`litellm_settings`、
`environment_variables` 和认证/网络字段不能放进目录。
每个可配置字段均需通过类型及范围校验，不支持的字段明确失败，不静默丢弃。

连接器禁止覆盖 `litellm_params.model`、`api_base`、API Key、Azure token、
token provider、credential、认证 header、TLS 绕过和内部 `model_info.id`。
这些值依赖已经核验的部署及 workload identity，不能通过 JSON 改走其他连接路径。

### 2.3 手工成本配置

新模型还未进入 LiteLLM 内置价格表时，可以在该 deployment 的 `model_info`
提供客户核验过的成本。例如报价为输入 `$2/百万 token`、输出 `$8/百万 token`，
应填写：

```json
{
  "model_info": {
    "input_cost_per_token": 0.000002,
    "output_cost_per_token": 0.000008
  }
}
```

**单位是 USD/token，不是 USD/百万 token。** 不要直接填 `2`、`8`。
客户可以按实际模型、区域和合同填写不同后端价格；不要求同模型组成本相同。
费用必须是有限、非负 JSON 数字，不接受字符串、布尔值、NaN 或 Infinity。
工具不从空字段推断免费，也不在价格未知时填 `0`。

官方还定义了缓存 token、音频 token、图像、推理 token、长上下文等成本字段。
这些字段必须由当前工具支持，并受到运行中 LiteLLM 版本支持；字段被写入配置
不等于该模型/接口实际会使用该计价维度。
`base_model` 用于映射真实模型家族，不应填客户端别名或虚构 Azure deployment。

**零成本风险：** 官方说明显式将输入和输出 token 成本都设为 `0` 可以绕过
该模型的预算检查。只在客户确认真实零成本并明确批准后使用，不能把它当作
“价格未知”的兜底值。支持某个成本字段不等于改变实际 Azure 账单。

官方最新文档的 `cost_per_second` 需要 LiteLLM 1.105.0 或更高版本。
不能因为网页列出了字段，就认为当前固定的 1.104.8 镜像已支持；不支持的字段
会导致校验失败，而不是声称定价已生效。当前拒绝统一的 `cost_per_second`，
但支持 1.104.8 已有的 `input_cost_per_second` / `output_cost_per_second`。

### 2.4 支持的模型级字段

以下是当前工具的完整允许列表，不代表官方 config.yaml 的全部字段。
数字必须是字面 JSON 数字；整数不接受布尔值或小数。
所有配置字段均不接受 `null`、环境变量表达式或任意额外字段。
字段类型校验通过也不表示 Azure 部署支持该调用参数；客户仍须按部署协议实测。

**`litellm_params`（21 个字段）：**

| 字段 | 类型与限制 |
| --- | --- |
| `api_version` | 显式 API version 字符串；模型级值优先于 CLI fallback |
| `max_tokens`, `max_completion_tokens`, `n`, `rpm`, `tpm`, `dimensions` | 正整数，最大 `2147483647` |
| `seed` | 有符号 64 位整数 |
| `num_retries` | 整数，`0`–`2147483647` |
| `top_logprobs` | 整数，`0`–`20` |
| `temperature` | 有限数字，`0`–`2` |
| `top_p` | 有限数字，`0`–`1` |
| `presence_penalty`, `frequency_penalty` | 有限数字，`-2`–`2` |
| `timeout` | 大于 `0` 的有限数字，单位秒 |
| `stream`, `logprobs` | JSON 布尔值 |
| `stop` | 非空字符串，或包含 1–4 个非空字符串的数组；每项最多 1024 字符 |
| `reasoning_effort` | `low`、`medium` 或 `high` |
| `encoding_format` | `float` 或 `base64` |
| `response_format` | 仅 `{"type":"text"}` 或 `{"type":"json_object"}` |

`max_tokens` 与 `max_completion_tokens` 不能同时存在，合并后的现有配置也受
此限制。工具不会为换用另一个字段自动清除旧字段；发生冲突应先解决配置，
不能通过 `null` 假装删除。`json_schema`、`extra_body`、`tools` 等未列出的
配置不透传；这些选项可以在兼容的实际推理请求中按业务需要使用。

**`model_info` 非价格字段（5 个）：**

| 字段 | 类型与限制 |
| --- | --- |
| `base_model` | 真实模型家族字符串；接受普通家族名或 `azure/<family>`，统一为 Azure 形式 |
| `mode` | `chat`、`embedding` 或 `completion` |
| `max_tokens`, `max_input_tokens`, `max_output_tokens` | 正整数，最大 `2147483647`；模型能力元数据，不是请求的输出上限参数 |

省略 `base_model` 时，已有精确映射保留原值；新映射从实际 deployment 的模型
信息推导。客户显式指定时覆盖该映射的原值。`supports_*` 能力标记及内部 `id`
不在允许列表中，不能自行加入。

**`model_info` 价格字段（28 个）：**

全部为有限、非负数字。以下按计价单位分组，长上下文、priority、flex 和 batch
字段只在 LiteLLM 支持的相应计价路径中生效，不自动启用 Azure 的服务层级。

| 计价维度 | 允许字段 | 单位 |
| --- | --- | --- |
| 普通输入/输出 | `input_cost_per_token`, `output_cost_per_token` | USD/token |
| 缓存写入/读取 | `cache_creation_input_token_cost`, `cache_read_input_token_cost` | USD/token |
| 超过 128k 的输入/输出 | `input_cost_per_token_above_128k_tokens`, `output_cost_per_token_above_128k_tokens` | USD/token |
| 超过 200k 的输入/输出 | `input_cost_per_token_above_200k_tokens`, `output_cost_per_token_above_200k_tokens` | USD/token |
| 超过 200k 的缓存 | `cache_creation_input_token_cost_above_200k_tokens`, `cache_read_input_token_cost_above_200k_tokens` | USD/token |
| 图像 | `input_cost_per_image` | USD/图像 |
| 图像 token | `input_cost_per_image_token` | USD/token |
| 音频 token | `input_cost_per_audio_token`, `output_cost_per_audio_token` | USD/token |
| 输出推理 token | `output_cost_per_reasoning_token` | USD/token |
| 字符 | `input_cost_per_character`, `output_cost_per_character` | USD/字符 |
| 音频时长 | `input_cost_per_audio_per_second` | USD/秒 |
| 视频时长 | `input_cost_per_video_per_second`, `input_cost_per_video_per_second_above_128k_tokens` | USD/秒 |
| 输入/输出时长 | `input_cost_per_second`, `output_cost_per_second` | USD/秒 |
| priority 输入/输出 | `input_cost_per_token_priority`, `output_cost_per_token_priority` | USD/token |
| flex 输入/输出 | `input_cost_per_token_flex`, `output_cost_per_token_flex` | USD/token |
| batch 输入/输出 | `input_cost_per_token_batches`, `output_cost_per_token_batches` | USD/token |

当合并结果中输入和输出 token 成本都显式为 `0` 时，工具在 stderr、review 和
执行状态中输出 `pricingWarnings`。这只是提示，不能替代客户对预算风险的审批。

### 2.5 常规接入的最小配置

`litellm_params`、`model_info` 和资源级 `endpoint` 都可以省略，不需要为使用
示例而复制 temperature、限流、模型能力或价格覆盖。新映射不添加这些自定义
值；已有精确映射保留原来的设置。没有客户价格覆盖时，成本按 LiteLLM 自身
逻辑计算；新模型缺少内置价格时不能保证成本准确，应补充真实价格并验证。

```json
{
  "schema_version": 1,
  "subscriptions": [
    {
      "subscription_id": "11111111-1111-4111-8111-111111111111",
      "resources": [
        {
          "resource_group": "rg-models-east",
          "name": "example-foundry-east",
          "models": [
            {
              "model_name": "customer-chat",
              "deployment_name": "chat-east"
            }
          ]
        }
      ]
    }
  ]
}
```

省略 `litellm_params.api_version` 时，必须在第 5、6 节的 plan 和 execute
命令中提供相同的显式 fallback。本文命令已添加 `--api-version v1`，适用于
第 2.6 节说明的 Azure OpenAI Responses 场景；其他协议须先确认版本再同时修改
两条命令。工具不会猜测 API version；不要把上述最小文件当作无需该参数就能
执行的配置。

### 2.6 确认推理 API version：v1 与日期版本

**API version 不一定是日期。** 必须区分：

| 值 | 含义 | 是否能直接用作本工具的 `api_version` |
| --- | --- | --- |
| `v1` | Azure OpenAI 新版推理接口，由 LiteLLM 选择 v1 路径 | 可用于已确认支持的协议与部署 |
| `2025-04-01-preview` 等日期版本 | 旧版日期化推理接口的 API version | 只有对应接口文档或成功请求明确使用时才填写 |
| 模型版本，例如 `2026-09-03` | 具体模型的发布版本 | 不能据此推断 API version |
| ARM 查询使用的 `api-version` | Azure 管理接口版本 | 不能作为推理版本 |

微软的 [Responses API 文档](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses)
列出了 `gpt-6-astra`、`gpt-5.6-terra`、`gpt-5.6-luna` 等受支持模型。
对于这些模型的 Codex / Responses 接入，优先明确选择 `v1`。
微软的 [v1 API 生命周期说明](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle)
说明新接口不再要求每月更换日期化 `api-version`；不能据此忽略部署区域、
协议、SDK 和模型功能的兼容性。

当前固定的 [LiteLLM 1.104.8 Azure URL 适配器](https://github.com/BerriAI/litellm/blob/v1.104.8/litellm/llms/azure/common_utils.py)
识别 `api_version="v1"`，其
[Responses 适配器](https://github.com/BerriAI/litellm/blob/v1.104.8/litellm/llms/azure/responses/transformation.py)
据此选择 `/openai/v1/responses`。本目录校验也接受 `v1`；这不是工具的隐式默认，
仍须在模型配置或 CLI 中明确提供。资源级 `endpoint` 保持账号根地址，
**不要添加 `/openai/v1/` 或 `/responses` 路径**。

客户确认步骤：

1. 在 Foundry 选择具体部署与所需协议，查看 Playground 的“查看代码 / View code”。
   若示例使用 `OpenAI` 且 `base_url` 以 `/openai/v1/` 结尾，本工具填写 `v1`；
   若示例使用 `AzureOpenAI(..., api_version="...")`，核对后使用其明确版本。
2. 可参考同一部署、同一协议已成功调用的配置，但不能把旧模型的日期版本
   自动推广给全部新模型。`az ... deployment show` 中的模型版本不是推理版本，
   管理面发现不会给出所有可用推理 API version。
3. 完成审批和同步后，按第 6 节用批准的 vkey 对各客户端模型别名做实际推理，
   再验证所需的流式、tool 调用和费用。文档/源码/目录校验通过或 plan 成功，
   **不等于具体 deployment 已完成真实推理验证**。

不要将 Responses 场景的 `v1` 建议直接视为 Chat Completions、Embedding 或全部
模型功能均已验收。不同协议需单独确认；通用 JSON 示例中的日期版本也只是格式
示例，不是新推理模型的通用建议。

## 3. 同步与合并规则

精确映射身份为客户端模型名、账号资源 ID 和 deployment 名。

- 默认 `replace` 以 catalog 为完整清单，只保留文件中列出的精确映射；
  同一 Foundry deployment 的 `coding`、`gpt-6-astra` 是不同客户端名称。
  若文件只列 `gpt-6-astra`，旧 `coding` 映射会移除。
- 只有显式传入 `--model-policy merge` 才使用增量合并，保留未列出的模型组及后端，
  包括同一组中未列出的旧后端。本文的 plan/execute 显式使用 `replace` 以便审核，
  不传该参数时行为相同。
- 新映射生成稳定内部 ID；更新已有映射保留其原 ID。
- 成本或参数更新也属于实际配置变更，要经过计划审核和 rollout。
- 输入文件省略的已有配置保留；明确提供的字段覆盖该映射的相应值。
- 不通过 `null` 猜测删除或恢复默认；不支持的值明确失败。
- 请求默认值是否被客户端请求覆盖，遵循实际 LiteLLM/模型接口行为。
- 完全相同的期望目录为 no-op，但仍核验真实私网、工作负载和挂载状态。

正常更新只修改 `MODEL_CATALOG`，不要提前手工修改 `$CFG`；它是当前配置基线，
execute 验证 rollout/挂载后自动更新。replace 的删除列表记录在 review/state 的
`removedModels` 和 `removedModelGroups` 中；移除旧名称会使仍使用该名称的请求失效，
需要单独核对客户端和 vkey 的允许模型范围，不会自动改名或扩大 vkey 权限。
已移除模型组对应的 router affinity 项同步移除，其余非模型配置保留。

应用渲染、当前 baseline 比较、review hash、CSI/ConfigMap 挂载验证和后续
重验证使用同一模型配置，不能只让价格出现在客户 JSON 而漏掉实际运行 YAML。

## 4. 权限与私网准备

沿用客户配置的 `localExecution.authentication.deploy/runtime`。所有订阅须属于
获批同一租户；多订阅不是自动授权或跨租户凭据切换。

| 身份/范围 | 必须具备的能力 |
| --- | --- |
| deploy / 各模型资源 | 读取账号、deployment、PE connection 和 role assignment |
| deploy / 网关 RG | 读写本次 deployment、缺失 PE 和必要 DNS binding/link |
| deploy / 外部模型 RG | 写受审 nested deployment |
| deploy / 模型账号 | 给现有 workload identity 分配最小调用角色 |
| runtime / AKS | 获取凭据，读取工作负载及 ConfigMap，apply ConfigMap，patch Deployment，验证 rollout 和执行受限 Pod 检查 |

最小模型调用角色为 account scope 的 Cognitive Services OpenAI User
(`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`)。Contributor 不自动包含角色分配权限；
应使用受约束的 IAM 授权，不为方便增加无条件订阅级 Owner。

使用既有 VNet、专用 PE subnet、`privatelink.openai.azure.com` zone 和 workload
identity。只补齐计划中明确允许的 PE、DNS 和调用权限；已有合规资源直接复用。
执行后再次验证 PE 为 Approved、NIC IP 属于私有子网、A record 与 endpoint
匹配，以及后台 Pod 中 DNS/TLS 正常。Pending/Rejected、DNS 冲突、租户错误、
权限不足均明确失败，不自动批准外部连接或开放公网。

## 5. 准备输入与生成计划

在 Runner 的仓库根目录执行。实际客户文件必须放在 checkout 内的 Git-ignored
普通文件，路径不能包含 symlink。不要提交客户配置或修改附件快照。

```bash
CFG="local_execution/customer.local.json"
MODEL_CATALOG="temp/customer-models.local.json"

chmod 600 "$CFG" "$MODEL_CATALOG"
read -r -p "本次实际获批的变更编号: " CHANGE_TICKET
read -r -p "本次实际批准人的 Entra Object ID: " APPROVER_OBJECT_ID

.venv/bin/python -m local_execution.model_sync \
  --config "$CFG" \
  --catalog "$MODEL_CATALOG" \
  --api-version v1 \
  --model-policy replace \
  --operation plan \
  --change-ticket "$CHANGE_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID"
```

上例适用于已有明确风险接受的单操作员 governance。双人审批配置必须重复
`--approved-by` 提供两个真实且合格的批准人。所有调用均使用一致的审批参数。

上述命令针对第 2.6 节的 Responses 场景提供显式 `v1` fallback，所以可省略
`litellm_params`。也可以在各模型填写 `litellm_params.api_version`；模型级值
优先，不会被 CLI 的 `v1` 覆盖。若使用了其他协议或不兼容的模型，先核验并调整
对应版本。没有模型级值也没有 fallback 时失败，没有默认 API version。

plan 进行管理面发现、FullResourcePayloads What-if、Kubernetes server dry-run
和有限现场检查，不部署资源、不更新客户配置、不发送推理请求。
结果写入 `temp/model-sync/<UTC时间>-model-sync-plan-<后缀>/`，
目录 0700、文件 0600。不要开启 shell trace、debug 或输出环境变量。

必须审阅：

1. `discovery.json`：每个订阅/资源的身份、实际 deployment 和家族映射。
2. `model-sync-review.json`：批准范围、baseline、输入/代码/现场 hash 和期望配置。
   核对 `modelPolicy=replace`、`removedModels` 和 `removedModelGroups`，确认删除范围
   包括所有未列出的模型，不只是本次列出的 Foundry 资源。
3. What-if：仅允许计划中的 PE/DNS/调用权限和必要 nested deployment。
4. `desired-customer.json`、ConfigMap YAML 和 JSON Patch：模型及成本正确、
   完整模型清单与 catalog 一致，删除列表符合批准范围、非模型设置不变。
5. 确认没有账号/模型 deployment、数据库、镜像、env、CSI、ingress 或流量变更。

## 6. 审核后执行

```bash
read -r -p "本次已审核的 model-sync planSha256: " MODEL_SYNC_PLAN_SHA256

.venv/bin/python -m local_execution.model_sync \
  --config "$CFG" \
  --catalog "$MODEL_CATALOG" \
  --api-version v1 \
  --model-policy replace \
  --operation execute \
  --change-ticket "$CHANGE_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID" \
  --approved-plan-sha256 "$MODEL_SYNC_PLAN_SHA256"
```

execute 重新发现并计算计划，任何实际写入前必须匹配已批准 SHA。输入、代码、
现场或审批参数变化后重新 plan，不复用旧 hash。需要 fallback API version 时，
plan 和 execute 均传相同的 `--api-version` 和 `--model-policy`；切换 merge/replace
也会改变批准 hash，不能沿用另一模式的计划。

先部署缺失的私网/权限并重验证，再生成 immutable/hash-named ConfigMap，
通过带 UID/resourceVersion/test 的 optimistic patch 仅替换配置 volume 的引用。
保持镜像、Pod env、CSI、身份、Secret、数据库和非模型配置不变。
rollout 后读取工作负载和 Ready Pod 的实际挂载 YAML，核验成本及参数没有遗漏。
最后原子安装客户配置，并保存旧配置和受保护回执。

`status=completed` 和 rollout/挂载验证只证明配置发布完成，不证明模型推理、
价格计算、vkey 权限和业务验收成功。工具明确保留 `inferenceVerified=false`。
随后使用批准的 vkey 验证客户端别名、Responses/Chat/Embedding 协议、实际
token usage 和 Spend Logs 成本；不要把 vkey 或正文写进部署证据/公开日志。
新模型要核对 vkey 的模型访问范围，不能把 Azure deployment 名直接当客户端别名。

## 7. 按需执行：下游回执重验证

本节不是模型发布生效或 UI 显示更新的前提。开发/测试中仅更新模型并做实际推理
验证时，可以暂不执行；不执行不会回滚配置或影响已经生效的模型。
若要继续依赖这些回执的阶段部署、受控发布或正式验收，则需要重验证相应证据，
不能用旧回执证明新配置已经验收。UI 可见也不代表推理、成本或 vkey 权限已验证。

连接、模型、成本或参数变化可能改变阶段 fingerprint。以本次 review/state 的
`staleStageFingerprints` 为准，不手算/复制旧 hash 或伪造历史阶段验收。
新增连接可影响 Stage4 private-ingress 回执；模型配置可影响 Stage6 backend-route
回执；单独生成 release report 或刷新 edge-bind 不能修复这两项。

对于已有正常且已绑定 Front Door 的 native 私有入口，使用独立重验证命令。
`SYNC_OUTPUT` 必须取自**成功 execute**的输出目录，不是 plan 目录：

```bash
SYNC_OUTPUT="temp/model-sync/REPLACE_SUCCESSFUL_EXECUTE_DIRECTORY"
read -r -p "本次回执重验证实际获批的变更编号: " REVALIDATION_TICKET

.venv/bin/python -m local_execution.model_sync_evidence \
  --config "$CFG" --sync-output "$SYNC_OUTPUT" --operation plan \
  --change-ticket "$REVALIDATION_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID"

read -r -p "本次已审核的 evidence planSha256: " EVIDENCE_PLAN_SHA256
.venv/bin/python -m local_execution.model_sync_evidence \
  --config "$CFG" --sync-output "$SYNC_OUTPUT" --operation execute \
  --change-ticket "$REVALIDATION_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID" \
  --approved-plan-sha256 "$EVIDENCE_PLAN_SHA256"
```

审阅 `evidence-review.json`：两个 ARM template 的 `resources` 必须为空。
该流程重新核验现有模型挂载、PE/DNS/role/TLS、API/Admin Service/LB、UID、
路由/模板/证书、真实 Front Door FDID 的正负探测，仅保存 Stage4/6 回执。
不写 Kubernetes、不改变 ingress，不重发 TLS Secret、不重新部署基础设施。
新记录保留新批准且明确 `stageAccepted=false`、`inferenceVerified=false`。

之后在支持 native dev/test canary 的配置下，刷新报告：

```bash
.venv/bin/python -m local_execution.release_report \
  --config "$CFG" --replace \
  --change-ticket "$REVALIDATION_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID"

.venv/bin/python -m local_execution \
  --config "$CFG" --step stage9-edge-bind --operation plan
```

必须审阅 binding 的 `runtime-review.json`，API/Admin 各自的
`currentRouteConfigSha256` 与 `desiredRouteConfigSha256`、以及
`currentPodTemplateSha256` 与 `desiredPodTemplateSha256` 必须相等。
不相等表示真实 ingress 变更，停止，不把它当模型更新的回执刷新。

```bash
read -r -p "本次已审核的 edge-bind planSha256: " BIND_PLAN_SHA256
.venv/bin/python -m local_execution \
  --config "$CFG" --step stage9-edge-bind --operation execute \
  --approved-plan-sha256 "$BIND_PLAN_SHA256"
```

回执缺失、真实 drift、非 native、证书/网络/权限不可验证或其他配置变化均阻塞
重验证，不绕过。Entra/production 验收按其原审批策略单独进行。
其余整阶段验收、Stage5 schema/platform 等旧回执**不会**被这些命令自动刷新，
不能用旧 Stage5 回执渲染全应用，也不能为了刷新 hash 重跑基础设施或数据库迁移。
