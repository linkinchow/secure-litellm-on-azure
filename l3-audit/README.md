# 可选 L3 原文审计服务

使用 TypeScript 实现的 LiteLLM 原生网关透明审计代理。客户完成 Stage2–9 后，
可按独立 runbook 启用；**不执行 L3 部署步骤时，基础版 Spend Logs / PostgreSQL 审计路径不变。**

**部署与运维入口：[L3 原文审计 Runbook](../docs/litellm-optional-l3-runbook-zh.md)。**
该文档包含 Azure 参数、权限、私网、镜像推送、入口切换、查询、保全、验收及回退。
本 README 是模块说明，不替代部署审批或云端验收。

## 架构与职责

```text
API 私有 Traefik → l3-audit → LiteLLM → 模型
                     │
                     └→ 异步磁盘队列（每副本独立 PVC）
                          ├→ Blob：原文及内容哈希
                          └→ LAW：无正文索引、运行风险与心跳

IT 管理员 CLI → LAW 查询 → Blob 原文下载 / 案件保全 / 解除保全
```

- 捕获 Chat/Responses HTTP JSON、SSE 和 Responses WSS 的轮次及工具往返。
- Embeddings 只保存请求输入，不保存响应向量。
- 原文 body/WSS 消息以 base64 写入 Blob 封装；不复制认证 Header、Cookie 或明文 Key。
  **正文自身仍可能含敏感信息，本模块不提供内容 DLP。**
- LAW 保存模型、Key 哈希、关联标识、字节数、Blob 引用和缺口等结构化信息，不复制正文。
- 先保存 Blob，再交付 LAW 索引；重试可能产生重复索引，查询按记录 ID 去重。
- Admin 登录链路不经过此代理，不新增公网审计页面；IT 管理员使用 CLI 查询。
- 启用 L3 后，专用激活工具关闭 PG 正文副本，保留费用/调用元数据；回退恢复基础版策略。

基础设施模板位于 [infra/optional-l3/](../infra/optional-l3/)，不会被基础部署自动执行。
生成的工作负载使用两个副本、Workload Identity、独立 PVC 和 ClusterIP Service。

## 开发与验证

要求 **Node.js 24** 和 npm。从仓库根目录执行：

```bash
npm ci --prefix l3-audit --ignore-scripts --no-fund
npm run build --prefix l3-audit
npm test --prefix l3-audit
```

`npm test` 会先编译 TypeScript，再运行单元测试、loopback HTTP/SSE/WSS 协议测试、
队列恢复和管理员操作测试；Azure 调用使用替身，不访问真实云端或产生模型费用。
不要将离线测试通过解释为客户 Azure 私网、权限、真实模型及保全功能已验收。

完整的相关 Python 回归和 Bicep 编译命令见 runbook。
安装依赖提示漏洞时先核对公告与锁定版本，不运行 `npm audit fix --force` 批量升级。

## 镜像构建

在仓库根目录执行：

```bash
docker build --platform linux/amd64 \
  -f l3-audit/Dockerfile \
  -t litellm-optional-l3:local \
  l3-audit
```

构建上下文是 `l3-audit`，不是仓库根目录。镜像锁定基础镜像 digest 和 npm 依赖，
以非 root 用户运行。推送至客户私有 ACR 前，按客户供应链要求完成扫描、SBOM 和签名；
部署引用必须是批准的 `ACR/repository@sha256:...`，不能使用本地标签或浮动标签。
具体 ACR 登录、推送及 digest 获取步骤见 runbook。

## 运行配置

服务监听 `8080`；配置必须在启动前提供，不能仅运行 `npm start` 就完成部署。

| 环境变量 | 用途 |
| --- | --- |
| `L3_STORAGE_URL` | 批准的 Azure Blob 账号 HTTPS 地址 |
| `L3_DCE_URL` | Logs Ingestion 的 DCE HTTPS 地址 |
| `L3_DCR_ID` | DCR immutable ID，格式 `dcr-...`，不是 ARM resource ID |
| `L3_SPOOL` | 可写的持久化缓冲目录，默认 `/spool` |
| `L3_UPSTREAM` | 固定内部 HTTP 上游，默认 `http://litellm.litellm.svc.cluster.local:4000` |
| `AZURE_CLIENT_ID`、`AZURE_TENANT_ID`、`AZURE_FEDERATED_TOKEN_FILE` | AKS Workload Identity 注入的身份配置 |

运行服务显式使用 `WorkloadIdentityCredential`，不使用 Storage account key。
管理员 CLI 使用 `DefaultAzureCredential`，在获批操作机以管理员身份执行；
保全操作另外需要 `L3_SUBSCRIPTION`、`L3_RESOURCE_GROUP` 和 `L3_STORAGE_ACCOUNT`。
管理员参数和完整命令见 runbook，不把管理员登录凭据注入服务 Pod。

健康端点为 `GET /l3/health`，返回 `spoolReady` 及交付/丢失/失败/磁盘计数。
**HTTP 200 或 Pod Ready 不代表审计正常**；应同时检查缓冲状态、LAW 心跳及实际告警通知。
仅通过批准的私网或本机 `port-forward` 查看，不公开健康端点或正文。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| [src/server.ts](src/server.ts) | 服务启动、工作负载身份、交付调度和心跳 |
| [src/proxy.ts](src/proxy.ts) | HTTP/SSE 转发及 WebSocket 升级 |
| [src/websocket.ts](src/websocket.ts) | 被动帧解析、WSS 轮次及响应关联 |
| [src/record.ts](src/record.ts) | 原文封装、元数据提取、哈希及采集预算 |
| [src/queue.ts](src/queue.ts) | 磁盘缓冲、交付检查点、重试和损坏记录隔离 |
| [src/azure.ts](src/azure.ts) | Blob 原文与 LAW 索引交付 |
| [src/render.ts](src/render.ts) | 生成可审查的 Kubernetes 工作负载 |
| [src/private-endpoints.ts](src/private-endpoints.ts) | 从部署输出校验 PE/NIC 并自动发现私有 IP |
| [src/activation.ts](src/activation.ts) | 带审批哈希和漂移校验的 PG 正文开关发布 |
| [src/admin.ts](src/admin.ts) | `query`、`get`、`hold`、`release` 管理员操作 |
| [test/](test/) | 合成协议、队列、Azure 接口及激活 CLI 回归 |

## 可靠性与留存边界

- **业务优先，审计故障不主动阻断推理**；但代理本身是业务跳点，进程不可用仍可能影响业务，
  不提供自动旁路或模型请求重放。
- 每次 HTTP 请求或 WSS 轮次原文累计上限 **16 MiB**；每 Pod 磁盘队列最多 **1 GiB /
  4096 条**，最长重试 **24 小时**。另有活动采集、待写队列、分片和 WSS 并发预算，
  详细阈值见 runbook；不能把原始内容预算当作进程总内存上限。
- 请求结束前或 fsync 前退出可能丢失记录，PVC 丢失也无法保证恢复；**不承诺零丢失或
  exactly-once**。超限、断连、无法关联或交付失败必须保留缺口，不能冒充完整审计。
- 普通 Blob 使用 **30 天**生命周期阈值，实际删除有延迟；保全副本进入独立案件容器并设置
  legal hold，不受普通内容生命周期规则删除。
- `release` 会在显式确认后解除整个案件的 hold 并删除案件容器，必须按批准流程执行。
- 网关无法记录客户端未发送的本地工具活动；`previous_response_id` 只做前后轮关联，
  不保证前序正文到期后仍可还原完整会话。

不要将原文、密钥、客户配置、缓冲文件或管理员下载结果提交 Git、上传普通 artifact 或粘贴到聊天。
