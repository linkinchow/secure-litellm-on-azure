# Stage 9边缘入口

独立资源组级Bicep入口，不自动纳入阶段4/5编排，不部署Kubernetes。默认`deployEdge=false`、`enableApiTraffic=false`、`enableAdminTraffic=false`。API WAF默认Detection；Admin WAF固定Prevention，确保来源IP白名单不会只记录而不阻断。

当前West US 3 greenfield测试使用本模块的Front Door Premium双平面设计，API/Admin分别通过PLS进入隔离native ingress；原生Admin用户名/密码登录和virtual key的Codex Responses推理已有用户验证，不是Entra SSO。2026-10-09批准出口为三个精确公网IPv4 `/32`及一个IPv6 `/128`，不是仅IPv4策略；资源管理状态不替代未批准来源、错误密码、协议和回退的独立验收。

`main.bicep`创建一个Front Door Premium profile，但API和Admin使用不同endpoint、自定义域、origin group、Private Link origin、route和WAF policy。API route只有8个精确路径：6个推理路径及模型发现`/models`、`/v1/models`（见下文“模型列表路由”）；Admin route为完整UI路径，Admin WAF用`SocketAddr`匹配实际连接源，并阻断不在`adminAllowedCidrs`中的来源，内层继续执行Entra或LiteLLM原生登录。两条route均无缓存、禁用默认`azurefd.net`域路由且仅支持HTTPS。

Admin域不启用客户端证书认证，不创建Front Door Secret或边缘CA信任Vault，也不依赖Preview API。外层IP白名单不是用户身份认证：共享NAT后的所有用户都能通过该门禁，仍必须使用内层登录、强密码、限流和源站FDID绑定。公网出口不稳定时应先选择可控企业代理/VPN出口或其他身份感知边缘方案，而不是放宽CIDR。

## Native Responses WebSocket

native ingress保留六个POST推理路径，并仅对`/responses`和`/v1/responses`接受合法GET WebSocket Upgrade；两种协议都受同一Host、TLS和FDID门禁约束。Front Door原生支持WSS且route已禁用缓存，不需额外协议开关。API WAF以五条Block方法规则区分推理POST、模型列表GET、合法Responses握手及其他方法/GET，不用Allow短路managed rules/限流；WebSocket头检查只作用于Responses路径；Admin不变。LiteLLM固定1.104.0已含原生Responses WS和Azure上游路径，本次不更换后端镜像。

首次部署按原Stage顺序自动取得该契约。已发布native dev/test canary按[WSS升级执行单](../../local_execution/native-wss-upgrade-zh.md)更新ingress、单资源API WAF及绑定/报告，**不要重跑禁流量edge-prepare**。WAF只检查握手、不检查后续帧，HTTP限流不是per-turn限流；真实两轮、工具回传、Key权限/预算及Spend Logs须独立验收。Entra代理不因本次变更自动支持WSS。

## 模型列表路由

Codex Desktop/CLI/VS Code启动和刷新时`GET /v1/models`（带`client_version`等查询串）做模型发现；缺少该路由时客户端不可用。API平面因此只额外开放`/models`和`/v1/models`两个精确路径，且只允许GET：

| 层 | 控制 |
| --- | --- |
| Front Door route | `patternsToMatch`增加两个精确路径，无通配；`/v1/models/{id}`、`/model/info`等管理路径仍不路由 |
| API WAF | 优先级11只放行Responses和模型列表的GET；优先级14 `BlockModelListNonGet`阻断两路径上的非GET；优先级15 `RateLimitModelList`按`SocketAddr`每分钟限流（`groupBy=SocketAddr`，RegEx匹配路径及可选查询串），先于全局`RateLimitApi`（20） |
| Traefik | 仅`Method(GET) && Path(/models或/v1/models)`进入LiteLLM，其他方法和子路径返回404；同样要求Host与FDID |

`modelListRateLimitPerMinute`缺省120，对应旧ingress的`limit-rps=2`；允许1–10000，可在`parameters.edge`中调整，按共享NAT后的Codex客户端数和刷新频率评估。Front Door以1分钟窗口计数，不是令牌桶：旧ingress的突发上限约6个请求，这里同一分钟内可一次用完120个；共享NAT出口的所有用户共用一个计数。Detection模式只记录不阻断。Entra代理overlay不开放该路由。

## 依赖顺序

1. 在新Private AKS建设经过支持周期/安全评审的两个独立私有ingress controller，分别使用`llm-api-ingress`和`llm-admin-ingress` namespace；本模块不安装controller。
2. controller owner创建API和Admin两个不同的Standard internal LB frontend，HTTPS证书分别覆盖`llm-api.<baseDomain>`和`llm-admin.<baseDomain>`；配置SSE不缓冲及适当超时。
3. `../edge-origin/main.bicep`分别引用两个已审查frontend并创建`pls-llm-api`和`pls-llm-admin`。NAT subnet需`privateLinkServiceNetworkPolicies=Disabled`，Stage4的`snet-private-ingress`已有此目标设置。
4. 将两个PLS ID/location输入本模块；部署edge后、执行edge-bind前，两条Front Door托管Private Endpoint请求都必须手工批准并核对profile、origin及目标frontend，不能只凭`requestMessage`。edge-bind会读取live origin和PLS，要求每条PLS恰有一个Approved且没有Pending连接。
5. 取得管理员浏览器实际使用路径的稳定公网出口CIDR，填入`adminAllowedCidrs`。不得填写Laptop的RFC1918地址、任意公网宽网段或`0.0.0.0/0`；企业代理/VPN有多个批准出口时逐项填写。
6. 完成两个域的`_dnsauth`所有权、边缘托管证书、两份源站证书、Admin WAF规则与内层登录检查后，再评审启用流量。Microsoft托管边缘证书不安装到ingress。

PLS的`visibility=['*']`允许Front Door跨订阅发现，不代表数据访问授权；`autoApproval=[]`，不得自动批准未知连接。PLS绑定frontend只靠ID不能证明平面归属，必须核验LB后端池、rule、端口、selector与controller所有权。API和Admin不得引用同一PLS或同一frontend，也不得把旧公网gateway放入origin。

## Admin来源IP参数

| 字段 | 标识内容 | 如何取得与验证 |
| --- | --- | --- |
| `adminAllowedCidrs` | 允许访问Admin域的1–32个公网出口CIDR | 在管理员使用的实际公司网络/VPN/代理路径查询公网出口；必须显式CIDR，IPv4不得宽于`/24`、IPv6不得宽于`/64`，且必须全局可路由、无重复 |
| `adminRateLimitPerMinute` | 单一源地址的Admin边缘每分钟限流阈值 | 依据管理员人数、企业NAT汇聚及UI实际请求基线确定；Admin WAF固定Prevention，过低会直接阻断共享出口用户 |
Admin WAF固定包含三条自定义规则：优先级5的`BlockUnapprovedAdminSources`使用`SocketAddr`、`IPMatch`和`negateCondition=true`阻断白名单外来源；优先级10阻断TRACE/TRACK；优先级20执行一分钟限流。使用`SocketAddr`是为了不信任可伪造的`X-Forwarded-For`。部署及release会把实际CIDR、规则、模式和阈值绑定到回执并检查live漂移。

固定IPv4出口按`<批准公网IPv4>/32`填写，固定IPv6出口按`<批准公网IPv6>/128`填写。当前路径使用精确地址，表中的`/24`及`/64`只是校验上限，不是建议扩大的网段。双栈浏览器可能使用IPv6，须在实际访问路径核验并分别批准；不能只查询IPv4后假定Admin可用。Laptop私网地址（如`10.x`、`172.16/12`或`192.168/16`）不会到达Front Door，不能作为白名单值。出口动态变化时，先确定稳定企业出口和更新流程，不得扩大到不受控网络。受控更新命令见[本地Stage2–9指南](../../local_execution/stage2-9-guide-zh.md)，不要手改WAF规则造成配置/回执漂移。

## 只读工具

```bash
make validate-stage9

# 默认关闭的预览；不会创建资源
./.venv/bin/python scripts/preview_stage9.py --resource-group <目标资源组> --layer edge
./.venv/bin/python scripts/preview_stage9.py --resource-group <目标资源组> --layer edge-origin

# 本地域名组合预览，仍含待填的身份、镜像和controller占位符
./.venv/bin/python scripts/render_stage7_domain.py --stage 9
kubectl kustomize temp/stage9-domain

# 审批/证据填写于被Git忽略的环境文件，不修改公共模板
./.venv/bin/python scripts/stage9_release.py render --config temp/<环境>/release.json --output-dir temp/<环境>/stage9
./.venv/bin/python scripts/preview_stage9.py --resource-group <目标资源组> --config temp/<环境>/release.json
```

`release.example.json`是失败关闭的结构样例，不是一份批准书。`phase`支持prepare（两个endpoint均关闭）、canary（两个endpoint启用、API WAF Detection）和production（API WAF Prevention）；Admin WAF在全部phase都保持Prevention。开启流量需要实际Front Door ID、两套PLS、`adminAllowedCidrs`、来源门禁/内层登录证据和回退批准。2026-09-10第一阶段改为原生Spend Logs，需验原生记录、权限、留存、容量及故障；只有选增强方案才需L3治理/恢复。协议矩阵与数据库恢复仍为必验，见[当前部署指南](../../docs/customer-deployment-workflows-zh.md)。

本地native dev/test canary可使用`python -m local_execution.release_report`从当前配置和live资源生成报告，仅需显式提供真实变更编号和批准人；参见[本地生成步骤](../../local_execution/stage2-9-guide-zh.md#自动生成native-devtest-canary发布报告)。生成命令不部署或释放流量，edge-bind回执也不会自动生成报告。

`checks`每项包含`passed=true`、`report`、近7天带时区的`observedAt`；changeTicket及两个不同approvedBy对象ID也必须存在。脚本只校验attestation结构/时效，不访问工单验证签名，不代替GitHub Environment审批、Azure RBAC或人工报告评审。

生成物在忽略的`temp/`子目录：`edge.parameters.json`、Stage9 Kustomize overlay、代理policy、admin App Registration回调模板、配置摘要hash。默认域名脚本的输出不带已批准Front Door ID，应用时API和Admin代理都无法启动，不能直接用于生产。

What-if详细结果和diagnostics以0600权限保存在忽略目录，只输出计数。检查拒绝任何Modify/Delete/Unsupported等未知计划；已部署资源的正常后续变更也会被拒绝，需另行审阅，不可自动当成误报跳过。

`stage9_release.py check-origin`可检查ARM GET导出的LB与subnet快照：

```bash
./.venv/bin/python scripts/stage9_release.py check-origin \
  --load-balancer temp/<环境>/lb.json \
  --frontend-id <完整frontend资源ID> \
  --subnet temp/<环境>/subnet.json
```

它不证明快照新鲜度、controller端口规则或网络可达性。需要实际维护者补充来源和拒绝路径测试。

当前native入口、证书、发布与回退步骤见[本地Stage2–9指南](../../local_execution/stage2-9-guide-zh.md)；Actions发布/验收及增强分支边界见[客户迁移指南](../../docs/customer-migration-guide-zh.md)。模型同步另见[专用runbook](../../docs/litellm-model-sync-runbook-zh.md)，不由edge部署自动完成。