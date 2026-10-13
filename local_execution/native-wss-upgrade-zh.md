# Native Responses WSS：部署与已发布环境升级

适用链路：coding agent → Front Door Premium → API PLS → native Traefik → LiteLLM 1.104.0 → Azure Responses。
本次不升级后端镜像、不重建数据库、不修改 DNS，也不开放管理 API。
实现和离线测试不等于客户云上的协议验收。

## 1. 新客户按原有 Stage 顺序部署

使用包含本次改动的已审核代码版本，选择 Stage6 `native-auth`。
Stage4 `stage4-private-ingress`、Stage9 `stage9-edge-prepare`/`stage9-edge-bind`/`stage9-edge-release`
仍按[Stage2–9 指南](stage2-9-guide-zh.md)的首次部署顺序执行，不需要额外 Portal 配置或自定义 WebSocket 代理。

- POST 保留六个固定推理路径。
- GET 仅允许 `/responses` 和 `/v1/responses` 的 WebSocket Upgrade。
  `Upgrade: websocket` 和包含独立 `upgrade` token 的 `Connection` 必须存在；路径精确匹配。
  同一 Host/FDID 门禁覆盖 POST 和 WebSocket 两个分支。
- Front Door route 仍仅支持 HTTPS、没有缓存；WSS 使用同一 TLS 443 入口。
  Traefik 原生透传 Upgrade，不注入伪造的 Upgrade/Connection。
- API WAF 使用四条 **Block** 方法规则代替 `BlockNonPost`，不使用短路后续检查的 Allow 规则。
  现有 managed rules、API 限流及 Admin WAF 不因此取消。
  RequestUri 正则兼容路径形式及带 HTTPS authority 的完整 URI，路径部分仍精确锚定；
  不用只匹配 URI 后缀的方法，以免把其他路径的 query 误当作 Responses。
- 无 Key/错误 Key 的握手必须拒绝；合法 Key 握手后的首帧模型授权仍由 LiteLLM 执行。
  不能仅凭 HTTP 101 判断模型调用成功。

范围是 **native virtual-key** 路径，不是 Stage7 Entra 代理协议扩展。
Entra 代理继续执行自己的协议/对象授权契约，不能通过放宽 ingress 或绕过代理宣称 WSS 兼容。
需要该路径的客户须先完成代理层的独立实现与验收。

## 2. 已发布 native dev/test canary 升级

此流程适用于现有 West US 3 测试环境及等价的 native dev/test canary。
专用 API WAF 更新沿用具名审批和 native audit 门禁；不支持 production 或任意自定义方法规则。
当前 API mode 必须与批准的 dev/test canary Detection 报告一致，不自动切换模式。
生产存量变更须走单独批准的维护计划，不能修改 environment 或 phase 来绕过限制。

在能访问 AKS/Vault/ACR 私有端点的 **Runner** 仓库根目录执行部署；公网验收脚本可在 laptop/Linux 开发机执行。
先将本次代码纳入已审核版本并更新 Runner。不要在仍只有旧代码的 main 上开始执行。
保留既有 `CFG`；不要重新运行配置生成器、修改客户参数、删除 custom domain，
也不要重跑 `stage9-edge-prepare`（该步骤默认关闭两个入口）。

确认当前配置和真实审批信息：

```bash
set -euo pipefail
umask 077
test -n "${CFG:-}"
.venv/bin/python -m local_execution.api_wss --help
jq '{environment, location, resourceGroup: .target.resourceGroup,
     authentication: .application.authentication.mode,
     features: .localExecution.features,
     releaseReportPath: .localExecution.releaseReportPath}' "$CFG"
read -r -p '本次实际批准的变更编号: ' CHANGE_TICKET
read -r -p '本次实际批准人的 Entra 用户 Object ID: ' APPROVER_OBJECT_ID
```

原有 dual 策略需要第二位真实批准人：以下 API WAF 和报告命令均重复传入第二个 `--approved-by`。
不得临时改成 single-operator。plan 和 execute 使用相同审批信息，每个 execute 使用各自本次 plan 的哈希。

### 2.1 更新私有 ingress 契约

```bash
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage4-private-ingress --operation plan
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage4-private-ingress --operation execute \
  --approved-plan-sha256 "REPLACE_INGRESS_PLAN_SHA256"
```

审核 plan 中新增的 GET Responses Upgrade 分支及保留的实际 FDID。
该步骤会重新核验私有入口 TLS/映射并保存 Stage4 回执，可能涉及 ingress rollout；
安排维护窗口，不要承诺零中断。它不重跑 Stage4 platform 或 Stage6 应用/数据库部署。
如出现证书、私有 DNS 或 LB 地址漂移，停止并处理实际问题，不能禁用证书校验。

### 2.2 只更新已有 API WAF 的方法规则

```bash
.venv/bin/python -m local_execution.api_wss \
  --config "$CFG" --operation plan \
  --change-ticket "$CHANGE_TICKET" --approved-by "$APPROVER_OBJECT_ID"

.venv/bin/python -m local_execution.api_wss \
  --config "$CFG" --operation execute \
  --change-ticket "$CHANGE_TICKET" --approved-by "$APPROVER_OBJECT_ID" \
  --approved-plan-sha256 "REPLACE_API_WSS_PLAN_SHA256"
```

检查输出目录中的 `reviewed-plan.json`、`api-wss-template.json`、`api-waf-before.json`
及审批报告。模板仅包含 **现有 API WAF 一个资源**，以独立部署名称
`llmgw-<environment>-s9-api-wss` 执行 Incremental 更新，不覆盖原 Stage9 edge 输出。
What-if 只能修改 API custom rules；mode、managed rules、rate rule、tags 均按 live 快照保留。
实际 API 关联从 `api-waf` security policy 解析，不能猜名字，也不能误选 Admin WAF。
陌生规则、额外属性、计划/代码/配置变化或执行前 live 漂移都会阻止更新。
完成后再次读取 ARM 核验规则；失败看 `wss-summary.json`，不要把部署部分成功当作验收通过。

更新 **不切换 WAF Detection/Prevention**，不写 endpoint/route，不更改 Admin 白名单、DNS、
源站或 Private Link。没有用 Allow 规则绕过限流和 managed rules。
API WAF 为 Detection 时，规则匹配仍只是检测记录；不能据此称为 Prevention 验收。
执行前后均读取 live edge 核验既有发布状态。Azure API WAF 不保证提供 ETag；
本命令采用计划绑定、执行前重读及执行后核验，仍需客户维护窗口内避免并发 Portal/IaC 修改。

### 2.3 刷新当前代码版本的绑定和报告

```bash
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage9-edge-bind --operation plan
.venv/bin/python -m local_execution --config "$CFG" \
  --step stage9-edge-bind --operation execute \
  --approved-plan-sha256 "REPLACE_EDGE_BIND_PLAN_SHA256"

.venv/bin/python -m local_execution.release_report \
  --config "$CFG" \
  --change-ticket "$CHANGE_TICKET" --approved-by "$APPROVER_OBJECT_ID" \
  --replace
```

代码版本改变后必须生成新的绑定回执，不复制旧 revision/hash。
报告刷新会备份旧报告，但不重新释放流量。**已经 Enabled 的两个入口不需要再次
`stage9-edge-release`，不重跑 origin、DNS publish 或 Stage0–8 其他步骤。**
若实际环境不是已发布 canary，停止使用本节，回到首次部署门禁。

## 3. 公网 WSS 验收：不能只测 101

验收机更新到同一代码版本，并在其虚拟环境中按项目依赖安装：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m tests.responses_wss_probe \
  --api-url "https://llm-api.<customer-domain>" \
  --model "REPLACE_APPROVED_LITELLM_MODEL_ALIAS" \
  --confirm-authorized-target --direct --tool-roundtrip \
  --output-json "/tmp/responses-wss-$(date -u +%Y%m%dT%H%M%SZ).json"
```

`--model` 是 LiteLLM 别名，不是 Azure deployment；公网 `/v1/models` 仍未开放。
脚本交互隐藏输入受限 v-key，不在命令行、环境变量或报告中保存 Key。
`--direct` 不使用环境代理；客户必须通过企业代理时去掉它，并验证该代理支持 WSS。
输出文件独占创建，权限 0600，不记录原始请求/响应、continuation ID 或函数 call ID。
两次鉴权负例、一个合法连接、两轮生成，无并发或自动重试。
每轮最多 2048 输出 token，可能产生模型费用，使用专用低预算 Key 和合成内容。
仅做普通两轮对话时去掉 `--tool-roundtrip`；coding agent 场景保留该参数验工具往返。

通过条件：

1. 缺 Key 和错误 Key 均拒绝握手，通常为 401/403。
2. 合法 Key 返回 101；不是 HTTP 回退。
3. 同一连接上完成第一轮和带 `previous_response_id` 的第二轮。
4. 工具模式收到 `workshop_ping` 的 function call，然后回传固定 `OK`，
   第二轮也收到 `response.completed`。不会执行 shell、读客户文件或访问外部工具。
5. 报告 `accepted=true`；事件错误、超时、断开或 `response.incomplete` 均失败，
   不能把 101 后报错作为成功。

随后按客户实际客户端补充：

- 使用限定模型/预算/有效期的 v-key，验证未授权模型拒绝、预算耗尽/到期/撤销行为；
  包括长连接存续中的适用行为，不假定每帧重新鉴权。
- 冷启动 Codex/其他目标 agent，保留 `wire_api="responses"`，
  恢复或设置 `supports_websockets=true`，完成真实工具任务和多轮对话；
  确认不再经历最初的多次 404 重连，不把最终 HTTP 回退成功误记为 WSS。
- 对照同一时间窗的 Front Door、LiteLLM Spend Logs 和成本，验每轮记录、
  Key/用户归属、token 用量、模型/后端亲和及留存；不要假定 HTTP 审计覆盖自动等价于 WSS。
- 重跑既有 HTTP Responses、Chat、Embeddings、SSE 回归；
  普通 GET Responses、其他推理路径的 Upgrade、管理路径及缺失/错误 FDID 必须继续拒绝。
- 从批准/未批准公网出口回归 Admin 登录和来源门禁，检查业务健康与费用告警。

若 Azure 对应部署不支持 `/openai/v1/responses` 的 WebSocket 上游，或 Key ACL、
continuation、工具回传失败，应处理实际模型/上游兼容性，不通过开放全 GET、
绕过 FDID 或增加 WAF Allow 来掩盖错误。

## 4. 长连接限制与回退

[Microsoft 官方说明](https://learn.microsoft.com/en-us/azure/frontdoor/standard-premium/websocket)：
Front Door 原生支持 WSS，但 WAF 只检查 HTTP 握手，不检查建立后的帧。
闲置超时为 5 分钟，单连接最长 2 小时，维护可能更早断开；
目前每个 profile 的全球并发 WebSocket 上限为 3000。客户端需要重连/恢复，
容量与费用按真实并发另行评估。

边缘 HTTP rate rule 约束的是握手请求，不是同一连接内每次 `response.create`；
不得称为已验证的 per-turn 限流、正文防护、实时 Key 撤销或 DDoS 容量。
HTTP 和 WSS 的审计、预算、亲和和断线恢复都须单独观察。

上线前保存入口 plan、已知可用代码版本及本次 API WAF before 快照。
发生错误先停止新增 WSS 客户端，并将其临时恢复 HTTP；原 HTTP POST 契约未移除。
需要恢复旧入口时，使用已批准的旧代码版本对 `stage4-private-ingress`
重新 plan/审核/execute，并刷新该版本绑定；不要直接编辑 Pod/ConfigMap 绕过门禁。
剩余 WSS WAF 方法规则不使旧 POST-only ingress 放行 GET。
若还需恢复旧 WAF，由 Owner 从私有 before 快照生成只含可写字段的单资源模板，
审查 What-if 后执行；禁止直接 PUT 含只读链接的 ARM GET 快照、
关闭整个 edge 或未经审批修改 Detection/Prevention。
