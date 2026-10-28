# LiteLLM Kustomize 应用层

本目录是[安全增强客户方案](../README_ZH.md)的Kubernetes组件，不是旧部署脚本的替代执行命令。请按[客户迁移指南](../docs/customer-migration-guide-zh.md)完成前置基础设施、客户配置与阶段验收，再生成专用overlay；验证目录不等于可直接上线的部署清单。

> **当前路径与静态骨架分开。** 2026-10-09的West US 3 greenfield测试采用`1.104.0`固定源码构建、Workload Identity Azure模型认证、Entra-only PostgreSQL/Redis及分离私有backend/certificate Vault；API和Admin经独立Front Door Premium endpoint/PLS回源到隔离native ingress。Admin为原生用户名/密码fallback登录，不是Entra SSO；用户已验证登录和virtual key的Codex Responses推理。下文stage7–9代理组件是可选Entra分支，不是当前native拓扑；按[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)生成/发布当前清单，不直接应用validation overlay。

## Stage 9边缘入口准备

`components/stage9-edge`在API ingress中只保留6个推理Exact路径及`/readyz`，API和Admin代理都增加`FRONT_DOOR_ID`补充检查；Admin仍使用独立ingress，并由独立Front Door endpoint、WAF Prevention来源IP白名单和内层登录保护。`validation/stage9`组合前序组件，未加入环境overlay。运行`make validate-stage9`和域名生成器`--stage 9`验证；占位ID、镜像与IngressClass必须经批准替换。

Front Door/PLS模板位于`infra/edge`和`infra/edge-origin`；默认关闭且不创建DNS。此静态组件不会安装controller、创建内部LB或解禁阶段7关闭协议。当前私有入口安装、native发布、证书分层、启流量与回退见[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)；Actions顺序见[迁移指南](../docs/customer-migration-guide-zh.md)。

## Stage 8原生留痕与可选增强组件

第一阶段选择原生Spend Logs，将获批正文留在私有PostgreSQL；自建L3与collector不作为保存Prompt的必备组件。Base正文仍关闭，但原生发布生成器、受控核心查询及Stage8/9模式化证据检查已实现；真实留存/权限/容量/故障仍须验收，不要直接改静态值为true或跳过阶段。基础版边界见[部署指南](../docs/customer-deployment-workflows-zh.md)。

`components/stage8-audit`与`validation/stage8`包含L3/OTLP/输入Content Safety配置、审批/保全只读挂载、暂停的独立清理CronJob、Collector和网络策略。所有功能默认关闭，未加入环境overlay。执行`make validate-stage8`回归；域名生成可用`scripts/render_stage7_domain.py --stage 8`。

只有选择增强L3时，才先通过`infra/audit-storage`建设独立私有CMK存储并验证各身份权限，再通过批准配置开启采集及清理。`/audit`读取独立Blob，不是原生Spend Logs查询或Admin UI解禁。配置/接口/维护窗口与增强限制见[部署与验收指南](../docs/customer-deployment-workflows-zh.md)及[可选代理说明](../auth-proxy/README_ZH.md)。

本目录静态组件及离线渲染不会自动部署到当前集群；受控运行时生成/发布入口另见当前指南。

## 安全基线

Base清单包含：

- 两副本和滚动更新；
- ClusterIP Service，不创建公网LoadBalancer、NodePort或Ingress；
- 专用ServiceAccount且默认不自动挂载Kubernetes API Token；
- UID/GID `10001`、非Root、RuntimeDefault seccomp；
- 禁止提权、drop ALL capabilities、只读根文件系统；
- `/tmp`受限emptyDir；
- startup/readiness/liveness probes；
- requests/limits；
- PDB和topology spread；
- 静态Spend Logs配置保留7天且Prompt/Response正文关闭；获批启用与保留策略由原生生成/门禁路径处理，7天不是替客户确定的永久期限；
- Secret逐项引用，不使用`envFrom`。

## 环境覆盖

- `dev`：1副本，仅用于开发验证；
- `test`：2副本；
- `prod`：2副本。

静态Base固定官方LiteLLM `1.104.8` digest；当前运行时路径使用`1.104.8`固定源码构建，发布生成器要求批准的私有ACR digest。静态清单只用于渲染/验证，不能冒充当前构建的部署清单。镜像扫描、SBOM和签名需走受控供应链；版本、漏洞、GPT-6和数据库兼容的有界证据见[1.104.0升级验证](../docs/litellm-1.104.0-upgrade-validation-2026-10-07.md)及[1.104.8升级验证](../docs/litellm-1.104.8-upgrade-validation-2026-10-28.md)。

## 尚未投入环境Overlay

以下内容尚未进入提交的dev/test/prod静态环境Overlay，不能以占位符假装已部署；这不是当前受控生成路径缺少这些模块的声明：

- Workload Identity ServiceAccount注解和Pod标签已有Stage4组件；
- Key Vault CSI `SecretProviderClass`和逐项Secret同步已有Stage5组件；
- NetworkPolicy已有Stage4组件；
- Internal Load Balancer/Private Link Service；
- Front Door/WAF；
- PostgreSQL Flexible Server和Managed Redis目标连接已有Stage5组件，但未执行迁移及协议验收；
- HPA/KEDA指标；
- 生产模型列表、Router目标配置、客户自有Entra认证代理和Guardrail。

## 验证

从仓库根目录运行：

```bash
kubectl kustomize deploy/overlays/dev
kubectl kustomize deploy/overlays/test
kubectl kustomize deploy/overlays/prod
```

不要直接将阶段3清单应用到当前生产集群。目标新环境完成Private AKS、Workload Identity、Key Vault、PG和Redis后，才能生成可部署overlay。

## Stage 4 NetworkPolicy组件

`deploy/components/stage4-network`提供：

- namespace Default Deny；
- 仅允许ingress-nginx到LiteLLM 4000；
- 显式DNS出口；
- 到Private Endpoint subnet的443/5432/6380/10000；
- 受Firewall进一步约束的公网HTTPS出口；
- 明确排除IMDS地址，防止继续依赖节点身份；
- Workload Identity ServiceAccount和Pod标签patch。

ServiceAccount Client ID保持占位符，必须由受保护的Bicep部署输出注入，禁止把真实Client ID提交到仓库。

该静态组件尚未加入prod overlay；其占位Vault、PG、Redis和DNS/IP依赖必须由目标环境输出解析，不代表当前测试资源未建设。未完成协议和故障测试前不得应用到生产。

## Stage 5数据组件

`deploy/components/stage5-data`提供：

- Key Vault Secrets Store CSI `SecretProviderClass`；
- Master Key、永久Salt和`DATABASE_URL`逐项同步到`litellm-runtime-secrets`；
- LiteLLM Pod只读CSI挂载；
- Azure Managed Redis主机、端口和Workload Identity Object ID注入；
- `azure_redis_ad_token=true`、TLS、证书链和主机名校验。

由于LiteLLM当前以环境变量读取上述三个Secret，CSI同步仍会在Kubernetes中产生Secret对象。这是明确记录的过渡补偿方案，不是“Secret不落etcd”。启用前必须确认etcd静态加密、Secret最小RBAC、namespace隔离、轮换行为和未授权读取失败。

上段仅描述静态Stage5组件。当前生成的backend清单改用只读CSI文件和启动器，PG使用Entra令牌连接模板，Redis刷新Entra令牌；不要据此在当前网关引入数据库密码、Access Key或旧Secret同步流程。backend和certificate Vault权限必须分离。

`deploy/validation/stage5`仅用于同时渲染Stage4和Stage5组件。其中所有`REPLACE_*`值必须由受保护部署输出替换；不得直接应用带占位符的清单。Redis端口固定为10000，并使用Workload Identity Object ID作为Entra用户名。PostgreSQL连接串必须包含`sslmode=verify-full`并作为单个Key Vault Secret引导。

## Stage 6高可用与路由组件

`deploy/components/stage6-ha`提供：

- 固定双副本、零不可用滚动更新和30秒稳定Ready窗口；
- 600秒终止窗口及30秒Endpoint摘除等待；
- CPU/内存HPA，2至6副本，600秒缩容稳定窗口；
- Git管理的模型组、稳定`model_info.id`、Router和日志安全策略模板；
- `simple-shuffle`、四类affinity及Redis Entra/TLS共享状态；
- 明确禁止未经A/B批准的`usage-based-routing-v2`。

`deploy/validation/stage6`组合Base和Stage4/5/6组件，仅用于渲染与静态验证。真实模型名称、端点和稳定ID仍是`REPLACE_*`，不能直接应用。`preStop sleep 30`只为Endpoint传播预留时间，不是LiteLLM drain证明；WebSocket/SSE、节点drain、HPA和故障行为必须在新环境实测。

运行`make validate-stage6`执行全部前序回归和Stage6门禁。

## Stage 7双子域与认证代理（可选Entra分支）

`components/stage7-identity`与`validation/stage7`保留`llm-api.example.com`、`llm-admin.example.com`通用模板。客户workflow从`CUSTOMER_CONFIG_JSON`的`baseDomain`生成域名；本地单独预览可运行`./.venv/bin/python scripts/render_stage7_domain.py --base-domain <客户域名>`。生成内容只写忽略的`temp/`，同步代理Host、Ingress/TLS与OIDC回调，不部署资源，身份和镜像等占位符仍需补齐。两个入口对应独立Deployment、ServiceAccount和NetworkPolicy；仅管理代理挂载内部凭据CSI，API采用企业Token与客户端vkey双凭据，不再挂载内部Key。后端只允许代理访问，不再允许ingress直达；模型与预算由LiteLLM统一管理。

源码和配置契约见`auth-proxy/README_ZH.md`；Node 24，首次执行`npm ci --prefix auth-proxy --ignore-scripts`，再运行`make validate-stage7`。尚未加入任何环境overlay。

管理面是OIDC保护的有限管理API，不是已经完成无感登录的原生Admin UI。WebSocket、Files、MCP、原生UI与Responses对象引用/加密多轮上下文默认拒绝，必须补齐授权验收才可放行。两个独立Vault/UAMI、私有ingress controller、DNS、证书、真实Entra角色和MFA均未部署。
