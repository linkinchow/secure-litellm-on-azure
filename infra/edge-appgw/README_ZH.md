# Stage 9 可选公网入口：Application Gateway WAF_v2

`parameters.edge.type=applicationGateway`时，`stage9-edge-prepare`/`stage9-edge-release`使用本模板替代[Front Door模板](../edge/main.bicep)。两种入口二选一；缺省仍是Front Door。执行步骤见[Stage2–9指南8.4](../../local_execution/stage2-9-guide-zh.md)。

## 何时选择

| 需求 | Front Door Premium | Application Gateway WAF_v2 |
| --- | --- | --- |
| 首字节等待 | 固定上限240秒（`originResponseTimeoutSeconds`最大值），超出返回504 | 后端超时缺省900秒，最长86400秒；无数据等待受公网IP空闲超时30分钟限制 |
| WAF请求体检查 | 前128KB | 最多2MB（`requestBodyInspectLimitInKB=2048`），超出部分放行不拒绝 |
| SSE | 官方未明确说明（实测未缓冲） | 关闭响应缓冲后逐条转发（官方有配置说明） |
| 部署范围 | 全球边缘 | 单区域，跨可用区 |
| 回源 | Private Link Service | 目标VNet内直连私有Traefik LB |

用户分布于多个国家/地区且确需全球就近接入时选Front Door；需要长首字节等待或大请求体检查、用户集中在单一区域时选Application Gateway。

## 资源与依赖

模板只在`deployEdge=true`时创建，名称按环境确定（`agw-llm-<env>`等）：

- 专用子网与NSG：放行Internet到443、`GatewayManager`到65200–65535、`AzureLoadBalancer`；不挂路由表；网络隔离已注册时委派给`Microsoft.Network/applicationGateways`。
- 公网IP：Standard、跨可用区、空闲超时30分钟、DNS标签作用域`ResourceGroupReuse`。
- 网关证书Vault（`kvagw-<env>-<hash>`）：只存监听器使用的PKCS#12副本`api-tls-pfx`/`admin-tls-pfx`；RBAC、清除保护、90天软删除、公网关闭、私有端点（复用现有`privatelink.vaultcore.azure.net`区域）、AuditEvent诊断。Application Gateway读取Key Vault证书依赖“允许受信任的Microsoft服务”例外（仅有私有端点不够），因此该Vault设`bypass=AzureServices`；存PEM和ACME账户的证书Vault保持`bypass=None`不变。
- 用户分配身份在网关证书Vault上获Key Vault Secrets User；已批准的证书导入人/组（`certificateImporterPrincipalId`）获Key Vault Secrets Officer，用于写入PFX。
- 三个WAF策略：API（模式随release，DRS 2.1 + BotManager，2MB检查，方法/WebSocket规则与[API协议](../../scripts/api_protocol.py)一致，按客户端IP限速；`RateLimitModelList`（优先级15，`ClientAddr`分组，`OneMin`）另对`/models`、`/v1/models`限速，阈值`modelListRateLimitPerMinute`缺省120即旧ingress每秒2次）、Admin（固定Prevention，`RemoteAddr`白名单、TRACE/TRACK拦截、限速）、闸门（固定Prevention，全部拦截）。
- 网关：WAF_v2、关闭HTTP/2、关闭响应缓冲、TLS策略`AppGwSslPolicy20220101S`、API/Admin两个SNI监听器、`/readyz`健康探测、`X-LLMGW-Edge-Id`重写、API路径白名单：6个推理路径及Codex模型发现`/models`、`/v1/models`，均为精确匹配，`/v1/models/{id}`等其余路径进入空池返回502；模型列表只允许GET（WAF `BlockModelListNonGet`与Traefik双重限制），说明见[Front Door模板“模型列表路由”](../edge/README_ZH.md#模型列表路由)。
- 诊断设置：全部日志与指标写入现有工作区（资源专用表，例如`AGWFirewallLogs`）。

部署前置：Stage4私有入口回执（提供两个LB地址）、`privatelink.vaultcore.azure.net`已链接到目标VNet、子网已加入两个`privateIngress.<plane>.allowedCidrs`。

部署分两次：`deployGateway`由准备阶段只读推导，不写在客户配置中。网关证书Vault中两个PFX都存在之前，`stage9-edge-prepare`只创建Vault、私有端点、角色分配和WAF策略等（`deployGateway=false`）；执行`stage9-edge-certificates`写入PFX后，再次`stage9-edge-prepare`才创建网关和网关诊断设置。

## 已知限制

- 本期仅native认证、本地执行；Admin白名单增量工具、API WSS WAF更新工具、模型同步edge证据、WAF安全探针尚不支持本模式，调用时报错。
- 子网大小创建后不可更改。
- Detection模式下方法类自定义规则和限流只记录；路径白名单与Traefik路由不受WAF模式影响。
- 限流按`ClientAddr`计数，共享NAT出口的所有Codex客户端共用模型列表限额；限流是近似的分钟窗口，不等同旧ingress的令牌桶突发上限。
- 经网关后Traefik/LiteLLM看到的来源地址为网关实例，真实客户端地址在`X-Forwarded-For`。
- 切换入口类型时Front Door与Application Gateway资源同时存在、同时计费，下线不再使用的入口须另立变更单。
