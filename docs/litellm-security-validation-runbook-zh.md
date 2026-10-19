# 加固版 LiteLLM：WAF 与安全功能验证 Runbook

## 1. 目的和边界

用于客户已部署、已获准测试的原生 LiteLLM 网关。本文组织两份安全脚本，
并补充现有 WSS 探针及本机 Codex CLI 验收：

| 工具 | 用途 | 不能证明什么 |
| --- | --- | --- |
| [edge_security_probe.py](../tests/edge_security_probe.py) | 旧的低频入口观察：Key 拒绝、路径隔离、合成样本、可选一次合法推理 | 状态码不能定位拦截层，不证明WAF或限流 |
| [waf_security_probe.py](../tests/waf_security_probe.py) | 新的配置检查、方法规则和托管规则主动样本、WAF日志关联、后端安全配置核验 | 不证明完整托管规则覆盖、DDoS容量、内容DLP或应用预算行为 |
| [responses_wss_probe.py](../tests/responses_wss_probe.py) | 授权101＋同连接两轮及工具回传 | 不证明长连接中逐帧WAF或撤销即时生效 |
| Codex CLI（手动可选） | 真正coding agent冷启动和合成只读任务 | 最终有回答不等于没有HTTP回退，也不等于安全验收完成 |

**不自动改变任何Azure/Kubernetes配置，不自动调用Codex，不读取其配置或凭据。**
主动WAF用例不使用真实Key，只使用无效合成凭据及不存在的模型名，不请求真实模型生成。
新脚本默认7个API请求，可选Admin后9个，未批准来源时另加1个伪造转发头负例；
串行、间隔2秒、每次超时15秒、无并发无重试；遇网络/TLS失败、429或5xx停止。
少量SQLi/XSS字符串只是无副作用合成检测信号，不提取数据、不执行脚本或工具、不扫描其他目标。

限流是独立二次授权子命令，不在默认套件中执行。禁止临时降低线上阈值以方便测试，
禁止将本手册用于未授权域名、压力测试、DDoS或绕过已有防护。
测试可能产生安全告警、日志摄取费用和L3原文；提前通知SOC，保留变更编号、窗口与实际出口。

## 2. 准备与目标核对

在仓库根目录使用现有项目虚拟环境。新脚本复用仓库Python模块，
所以使用`python -m tests...`，不是从任意目录直接执行文件。
需要Azure CLI、Reader或等效只读管理权限，以及批准LAW的日志查询权限；
这不需要Storage正文读取权限或修改WAF权限。无法访问私网的操作机应通过获批路径接入。
LAW查询需要Azure CLI的`log-analytics`扩展。在执行测试的同一用户、同一
`AZURE_CONFIG_DIR`环境中先运行`az extension show --name log-analytics`；
若明确提示未安装，再执行`az extension add --name log-analytics --only-show-errors`，
然后重新核对扩展。不要依赖捕获输出的脚本内动态安装提示。
脚本将动态安装禁用并关闭交互stdin，避免在不可见的`(Y/n)`提示上等到超时。

```bash
set -euo pipefail
set +x
umask 077
CFG="local_execution/customer.westus3.local.json"
# 客户使用自己实际部署的CFG；上面路径仅为West US 3测试环境示例。
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' "$CFG")"
API_URL="https://llm-api.$(jq -er '.baseDomain' "$CFG")"
ADMIN_URL="https://llm-admin.$(jq -er '.baseDomain' "$CFG")"
jq '{environment,location,target,authentication:.application.authentication.mode,
     adminAllowedCidrs:.parameters.edge.adminAllowedCidrs,
     apiRate:.parameters.edge.rateLimitPerMinute,
     adminRate:.parameters.edge.adminRateLimitPerMinute}' "$CFG"
az account show --subscription "$SUBSCRIPTION_ID" \
  --query '{subscription:id,tenant:tenantId,identityType:user.type}' -o json --only-show-errors
mkdir -p temp
SECURITY_DIR="$(mktemp -d "$PWD/temp/security-verification.XXXXXXXX")"
.venv/bin/python -m unittest tests.test_edge_security_probe tests.test_waf_security_probe
```

不复制私钥、真实v-key、Codex认证文件、完整客户配置到测试报告。
变量必须与当前环境一致，不使用旧West US的CFG操作West US 3。
Azure CLI登录身份按客户现有流程准备；Managed Identity没有个人邮箱或交互用户，
不要为了查询日志关闭条件访问策略或直接改成Owner。

`--direct`只忽略HTTP环境代理，不绕过VPN/TUN；实际SocketAddr出口以网络Owner确认和WAF日志为准。
Admin允许来源与不允许来源需要两台/两种获批出口分别执行，不自动代理到第三方IP，
不通过伪造X-Forwarded-For模拟真实源地址改变。

## 3. 先做只读安全配置核验

```bash
.venv/bin/python -m tests.waf_security_probe run \
  --config "$CFG" --api-url "$API_URL" \
  --configuration-only --confirm-authorized-target \
  --output-json "$SECURITY_DIR/configuration.json"
```

不发送业务HTTP请求。报告检查：

- 从实际Stage9部署输出定位Front Door，再从**实际securityPolicies关联**定位两份WAF；
  核对域名关联，不仅检查“有一个WAF资源”。
- WAF启用、API实际Detection/Prevention模式，Admin必须Prevention；不自动切模式。
- 四条API方法/握手规则的匹配条件与仓库契约一致；Admin使用SocketAddr来源门禁、
  TRACE/TRACK规则；自定义Allow旁路、托管规则排除/覆盖会使基线检查失败，需单独审核。
- 两个rate rule阈值与CFG一致。这里只是配置证据，**不是实际限流通过**。
- 请求体检查和日志scrubbing开关、指定LAW的WAF诊断启用；scrubbing开关不是完整脱敏证明。
- 两条route的HTTPS、默认域名链接关闭；边缘托管TLS1.2配置、源站名称验证及PLS目标一致。
  Private Link状态返回Approved才计pass，ARM省略/null时标记unverified，
  不能因此认定连接未批准或自动当成通过；再用现有Stage9私有源站检查核验。
- 同RG内AKS的私有API、Azure RBAC、禁用本地账户、OIDC/WI；ACR关闭公网及admin；
  Vault私网/RBAC/清除保护；PG私网/Entra-only；Redis禁用access key且使用加密连接。

每条结果含`scope=configuration-only`，不会宣称实际Pod数据权限或网络旁路已验证。
Redis ARM未返回publicNetworkAccess时标记`unverified`，不把null解释为公网关闭。
缺少预期资源、错误设置标记`fail`。目标RG存在其他同类资源也会被检查，
应核对实际资源名称，不因“测试环境还有旧资源”就忽略失败。

配置不符合时停止主动测试，先确定差异是否获批；脚本不会擅自修复或重部署。

## 4. 旧脚本：入口和鉴权低频基线

默认无需Key，无真实模型调用：

```bash
.venv/bin/python -m tests.edge_security_probe \
  --api-url "$API_URL" --admin-url "$ADMIN_URL" \
  --confirm-authorized-target --direct \
  --output-json "$SECURITY_DIR/edge-observations.json"
```

报告只说明状态是否在预期集合，403可能来自WAF、路由、应用鉴权或其他层。
API管理路径拒绝、无效Key拒绝需要保留，但不能据此声称SQLi/XSS已被WAF识别。
这个旧报告与新脚本报告不能混用；新脚本的`verify`只接受其自己的报告。

可选一次合法HTTP基线：另选文件名，在上面命令追加
`--baseline-model "实际获准模型别名" --prompt-key`。有少量模型费用，
隐藏输入低预算/限定模型/短有效期v-key，绝不输入Master Key。
旧脚本模型输出上限64 token，推理模型可能因预算/参数兼容性失败，应按结果排查，
不自动放大预算或重试。

## 5. 新脚本：WAF规则实测

### 5.1 API方法、握手规则、管理隔离与托管样本

```bash
.venv/bin/python -m tests.waf_security_probe run \
  --config "$CFG" --api-url "$API_URL" \
  --confirm-authorized-target --direct \
  --output-json "$SECURITY_DIR/waf-api.json"
```

| 用例 | 预期日志证据 |
| --- | --- |
| PUT Responses | `BlockUnsupportedApiMethods` |
| GET Chat | `BlockNonResponsesGet` |
| GET Responses但无Upgrade | `BlockInvalidWebSocketUpgrade` |
| GET Responses有Upgrade但Connection=close | `BlockInvalidWebSocketConnection` |
| POST Responses携带合成XSS query | DefaultRuleSet的XSS类别；不能用Bot规则命中代替 |
| POST Responses携带合成SQLi query | DefaultRuleSet的SQLI类别；不能用通用403代替 |
| POST `/user/info` | API管理查询路径拒绝观察；不发送有效凭据，不调用创建/删除接口 |

部分非法Upgrade请求可能先被Front Door协议解析器拒绝；无对应规则日志时保持
`inconclusive`，不靠修改请求、放宽规则或判定所有403都成功来掩盖这一边界。
托管规则可能因实际规则集、阈值、排除或数据格式未匹配，样本失败也不等于全部WAF失效。
API为Detection时，方法用例允许观察到400/401/403/404/405/426等下游/协议拒绝，
不要求WAF返回403；之后仍必须以日志判定，Detection命中最多记`detection-match-only`。

### 5.2 Admin允许出口与不允许出口

在**已确认允许**的真实出口执行：

```bash
.venv/bin/python -m tests.waf_security_probe run \
  --config "$CFG" --api-url "$API_URL" --admin-url "$ADMIN_URL" \
  --admin-source approved --confirm-authorized-target --direct \
  --output-json "$SECURITY_DIR/waf-admin-approved.json"
```

允许出口可访问登录页不代表已登录或获得管理权限；TRACE应命中`BlockUnsafeMethods`。
在**已确认不允许**的获批测试出口，将参数改成`--admin-source unapproved`，
使用新文件名。预期普通登录页、TRACE及伪造转发头的请求都被
`BlockUnapprovedAdminSources`拒绝；后者验证不能靠自报Header替代SocketAddr。
该测试不尝试破解登录、不提交密码、不创建Key。

## 6. 等日志到达后关联证据，不重复发请求

先确认上述`log-analytics`扩展已安装；`verify`会预检查扩展。
主动脚本不会为了等日志自动重放样本。等待Azure日志摄取后，在能查询LAW的机器执行：

```bash
.venv/bin/python -m tests.waf_security_probe verify \
  --config "$CFG" --report "$SECURITY_DIR/waf-api.json" \
  --output-json "$SECURITY_DIR/waf-api-evidence.json"
```

每份报告分别验证，Admin报告对应自己的输出路径。工具会重读实时配置并检查指纹，
若测试后模式/规则/关联等变化则要求重新审核，不将新配置解释为旧请求的证据。
新版使用`fingerprintVersion=2`的分组件配置指纹，保留WAF规则/模式、关联、路由、
源站、TLS及诊断设置，不把顶层ETag/systemData、资源部署状态、域名DNS验证token/到期时间
等ARM运行元数据当作配置。错误会指出`api.policy`、`admin.route`等变化组件，
不打印完整资源对象或潜在敏感值。
AzureDiagnostics查询按原请求时间窗、profile resource ID、tracking reference限定，
只投影rule/action/mode/policy/host，不导出匹配正文、Header或完整URI。
当前自动查询适配仓库Front Door的AzureDiagnostics诊断路径；客户使用不同资源专用表时
应先适配并测试查询，不把“表不兼容/无权限”当成规则通过。

| 验证结果 | 含义 |
| --- | --- |
| `prevention-block-verified` | tracking reference、域名、实际policy和预期规则类别关联，Prevention日志确认Block且HTTP403 |
| `detection-match-only` | Detection模式下看到了预期规则命中，**未证明阻断** |
| `inconclusive` | 无日志、日志延迟、类别/策略不符、协议层先拒绝等，需排查 |
| `http-observation-only` | 仅状态观察，不宣称WAF负责 |
| `fail` | HTTP观察不符合预期 |

托管规则可能先累计anomaly score再由另一条规则最终Block；工具允许同一policy、
host、tracking reference下组合这两类日志，自定义规则则要求其自身有Block记录。
后续日志补齐时可对同一原报告重新verify，**输出必须使用新文件名**，不要覆盖旧证据。
报告中的`wafQuery`可在批准的LAW终端手动执行；缺少`X-Azure-Ref`时不能自动关联。

### 6.1 `Edge state changed since probe`如何处理

旧版对Azure完整资源响应做哈希，ETag、系统时间、域名验证等元数据改变也会触发此错误。
报错发生在日志查询前，并不等于WAF测试未命中。先确认测试期间是否确实修改过策略、
模式、白名单、路由、诊断设置或CFG，不直接跳过检查，也不自动重发请求。

旧报告只保存总哈希，没有可用于重建的分组件基线；如果原始总哈希仍匹配，新工具可继续验证。
若不匹配则明确拒绝，不能安全区分元数据变化和真实配置变化，**不能把它就地升级或判为通过**。
保留原报告与其中`wafQuery`，可手动查询历史日志作为观察证据；如需自动完整核验，
更新修复代码后，在批准窗口仅重新执行一次第5节有界套件，使用新文件名：

```bash
.venv/bin/python -m tests.waf_security_probe run \
  --config "$CFG" --api-url "$API_URL" --confirm-authorized-target --direct \
  --output-json "$SECURITY_DIR/waf-api-v2.json"
# 等日志摄取，再执行；不要循环重复run来等待日志。
.venv/bin/python -m tests.waf_security_probe verify \
  --config "$CFG" --report "$SECURITY_DIR/waf-api-v2.json" \
  --output-json "$SECURITY_DIR/waf-api-v2-evidence.json"
```

无需更改WAF、重部署Stage4/9或修改LAW。如果v2仍提示某个组件变化，
应按组件检查真实变更，不能直接重写报告哈希或将当前快照替换成测试时快照。

### 6.2 Azure CLI超时、扩展提示和空workspace变量

`Azure read failed or timed out`不等于权限不足。`verify`先读取edge，再查询LAW；
旧版若缺少扩展，会在捕获输出的进程里等待动态安装确认，最终超时。
新版预检查扩展并给出明确安装指引，不会后台安装或要求放宽Azure角色。
安装完成后只需重新`verify`，无需重新发主动样本。

手工排查时，`--workspace`需要**workspace customerId GUID**，不是名称、ARM ID或空变量。
`verify`自己从已核对的LAW管理面读取GUID，不依赖`LAW_WORKSPACE_ID`环境变量；
手动CLI查询和其他runbook可能依赖该变量，使用前自动获取并校验：

```bash
LAW_WORKSPACE_ID="$(az monitor log-analytics workspace show \
  --subscription "$SUBSCRIPTION_ID" -g "$(jq -er '.target.resourceGroup' "$CFG")" \
  -n "$(jq -er '.parameters.edge.logAnalyticsWorkspaceName' "$CFG")" \
  --query customerId -o tsv --only-show-errors)" &&
[[ "$LAW_WORKSPACE_ID" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]] &&
az monitor log-analytics query --subscription "$SUBSCRIPTION_ID" \
  --workspace "$LAW_WORKSPACE_ID" --analytics-query 'print connectivity=1' \
  --timespan P1D -o json --only-show-errors
```

此查询不读取业务日志。返回`connectivity=1`证明该身份能完成此基础查询，
不自动证明它有权读取全部表；之后按原报告重新`verify`检验实际WAF表查询。
`PathNotFoundError`先核对workspace GUID；`AuthorizationFailed`/`Forbidden`再核对实际身份和
表/资源范围授权。管理面可读不等于数据面可查，DNS/TLS超时也不能靠增加Owner权限解决。

退出码：`run=0`只代表检查/请求完成且没有发现失败，可能仍`pendingWafEvidence=true`；
`verify=0`只表示所执行用例满足当前验证条件，不是整体安全合规通过。
`verify=1`含Detection-only或未决/失败项；`2`是配置、权限、报告或执行错误。
没有自动结论的能力保持未验证，不把“不支持”映射成pass。

## 7. 限流：独立窗口和二次授权

不建议在当前API 600/min阈值上为了验限流放大流量。此子命令硬限制：
dev/test、最多100次、最多2次/秒、循环55秒预算、串行无重试；单请求最多15秒，
所以总墙钟时间可能多出最后一次网络等待。只有已批准的现有阈值加5不超过请求预算
才执行，超过预算会在发请求前明确拒绝。

```bash
# 仅在独立获批的低阈值dev/test目标、确认共享出口影响后执行。
.venv/bin/python -m tests.waf_security_probe rate-limit \
  --config "$CFG" --api-url "$API_URL" \
  --confirm-authorized-target --confirm-rate-limit-impact \
  --request-budget 100 --direct \
  --output-json "$SECURITY_DIR/waf-rate.json"
```

600/min目标会返回“exceeds bounded budget”，表示**未测试**，不是防护失败。
不要为通过测试改生产规则或提高本脚本上限。测试不是容量/负载/DDoS工具。
首次403/429即停止并保留证据；可能是鉴权/Bot/应用限额，不自动当成RateLimitApi。
随后使用第6节verify，只在关联`RateLimitApi`的Prevention Block日志后算该用例通过。
Front Door分布式计数、窗口边界、共享NAT流量和请求到达路径会影响结果，
有界预算内未观察到限流只能标记未决，不能承诺精确第N次必拦。

## 8. WSS与本机Codex CLI手动验收

### 8.1 有界WSS工具往返

这是额外的真实模型测试，需要另行批准费用和数据采集范围：

```bash
.venv/bin/python -m tests.responses_wss_probe \
  --api-url "$API_URL" --model "REPLACE_ACTUAL_APPROVED_ALIAS" \
  --confirm-authorized-target --direct --tool-roundtrip \
  --output-json "$SECURITY_DIR/wss-tool-roundtrip.json"
```

隐藏输入受限Key。必须`accepted=true`、合法101、两轮completed及函数回传。
**WAF只检查握手，不检查之后的WebSocket帧**；内容DLP、每轮限流、预算、
Key撤销和正文审计须单独验收，不能由这个结果推导。

### 8.2 Codex冷启动（脚本不会代跑）

先执行`codex --version`、`codex exec --help`，核对本机版本支持下列选项。
由管理员按现有流程将Codex provider指向批准API，使用`wire_api="responses"`、
正确模型别名及支持WSS的配置。不要把Key写进命令、报告或本仓库。
先审核本机已有MCP、插件、hooks和规则，关闭未经批准的外部工具或网络集成；
`--sandbox read-only`不等于关闭这些集成，也不是零外部动作保证。

在新建、仅含合成文件的目录执行一次，**不要以客户代码仓库作为工作目录**：

```bash
CODEX_TEST_DIR="$(mktemp -d "$SECURITY_DIR/codex-synthetic.XXXXXXXX")"
printf 'Synthetic test: the expected answer is BLUE.\n' > "$CODEX_TEST_DIR/fixture.txt"
# MODEL_ALIAS填实际获批LiteLLM别名；litellm须是已审核的本机provider名称。
MODEL_ALIAS="REPLACE_ACTUAL_APPROVED_ALIAS"
timeout --signal=TERM 120s codex exec \
  --sandbox read-only --skip-git-repo-check --ephemeral \
  --cd "$CODEX_TEST_DIR" --model "$MODEL_ALIAS" \
  -c 'model_provider="litellm"' \
  'Read only fixture.txt and report its expected answer. Do not modify files, use external tools, or access the network.'
```

本步骤一次CLI调用仍可能包含多轮模型请求和客户端重试，使用专用低预算Key，
到时停止不表示已取消所有上游计费。若本机版本不支持选项，先按其帮助调整并审核，
不要使用dangerously-bypass或扩大sandbox绕过。
`timeout`124表示达到时间预算，不是通过；避免直接导出可能含配置/正文的完整debug日志。

验收：冷启动无原来的404重连，合成只读任务完成，无文件写入；
同时通过批准的客户端传输证据或L3记录`transport=wss`确认没有仅靠HTTP回退。
若只有最终答案而没有传输证据，只记录功能成功、WSS未验证。

## 9. 其他安全feature如何验收

| 功能 | 本次证据与后续动作 |
| --- | --- |
| TLS | HTTP探针保持证书/主机名验证，无`-k`；配置核对TLS1.2，不测试弱密码套件穷举 |
| API/Admin隔离 | 负例状态＋Admin实际来源/WAF日志，登录/角色仍需正常业务权限验收 |
| 私有源站与FDID | 本脚本检查Private Link和源站TLS配置；私网缺失/错误FDID及跨Host拒绝复用原Stage4/6检查，不从公网403推导旁路安全 |
| 虚拟Key模型ACL、预算、到期/撤销 | 另用专用低预算Key手工验允许/拒绝模型和撤销；不消费到客户预算耗尽，不撤销业务Key |
| AKS/PG/Redis/Vault/ACR | 本次仅配置检查；WI实际允许/拒绝、数据库最小权限、Secret/证书轮换仍遵循部署runbook |
| L3原文、LAW与保全 | 按[L3 runbook](litellm-optional-l3-runbook-zh.md)验证采集、交付、读取、hold及缺口 |
| 告警 | SOC核对本测试固定时间窗/追踪ID的WAF日志及通知，Action Group测试送达，不制造生产故障 |

## 10. 收尾与报告

- 保存Git revision、变更编号、批准窗口、操作机出口、两份脚本各自报告、WAF关联证据及未决项。
- 报告采用独占0600文件；客户环境名称和tracking reference仍属内部信息，不公开发布。
- 不将截图中的真实prompt、Key或Azure原始错误复制进报告；不用“WAF通过”概括只有Detection的结果。
- 默认不创建任何云资源，无需执行删除/回放Stage2–9；不为测试重跑edge-prepare或改变流量。
- 试验Key按批准流程到期/回收；合成目录和证据按客户留存策略处理，不递归清空仓库、temp根或用户目录。
- 本脚本没有检测所有攻击、没有进行渗透测试或容量证明；客户策略不允许主动样本时只执行第3节。

参考：[Front Door WAF日志与TrackingReference](https://learn.microsoft.com/en-us/azure/web-application-firewall/afds/waf-front-door-monitor)、
[原生部署指南](../local_execution/stage2-9-guide-zh.md)。
