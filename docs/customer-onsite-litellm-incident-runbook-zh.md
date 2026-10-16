# 客户现场 LiteLLM 503/429/OOM 排查与紧急处置手册

> 适用范围：Stage0-Stage1 旧 AKS 环境，LiteLLM `1.95.0`、单副本 LiteLLM、集群内 PostgreSQL、ingress-nginx。
>
> 原则：先取证、再变更；先备份、再清理；不输出 Master Key、数据库密码或 Authorization；不因 IP 属于云厂商 ASN 就直接加入白名单。

## 1. 现场目标

依次确认：

1. 503 是否由 LiteLLM `OOMKilled`、探针失败或无可用 Endpoint 导致；
2. 客户是否存在集中 401、不同无效 Key 激增或自动化扫描；
3. 429 来自 LiteLLM 限额、Azure OpenAI 配额还是入口层；
4. PostgreSQL 是否健康、容量是否充足；
5. 邮件和飞书告警是否实际接通；
6. 必要时通过提高 LiteLLM 内存、阻断已确认异常来源和重启恢复服务。

## 2. 变量与安全检查

在客户批准的私网 Runner 上执行。不要把真实 Key 写入命令历史或本文。

```bash
export NS="litellm"
export LITELLM_DEPLOYMENT="litellm-mi-proxy"
export POSTGRES_DEPLOYMENT="postgres"
export LITELLM_LABEL="app=litellm-mi-proxy"
export POSTGRES_LABEL="app=postgres"

# Azure 信息按客户环境填写
export SUBSCRIPTION_ID="REPLACE_CUSTOMER_SUBSCRIPTION_ID"
export RESOURCE_GROUP="REPLACE_CUSTOMER_LEGACY_RESOURCE_GROUP"
export AKS_NAME="REPLACE_CUSTOMER_AKS_NAME"
export WORKSPACE_NAME="REPLACE_CUSTOMER_LOG_ANALYTICS_WORKSPACE"

az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{subscription:id,user:user.name}' -o json
kubectl config current-context
kubectl cluster-info
```

确认当前 context、订阅、集群和客户变更单一致后再继续。

创建只用于本次事件的本地证据目录：

```bash
export STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE_DIR="$HOME/litellm-incident-$STAMP"
mkdir -p "$EVIDENCE_DIR"
chmod 700 "$EVIDENCE_DIR"
printf 'Evidence directory: %s\n' "$EVIDENCE_DIR"
```

## 3. 第一阶段：只读取证

### 3.1 工作负载、Endpoint 和资源状态

```bash
kubectl -n "$NS" get deployment,pod,svc,endpoints,pvc -o wide \
  | tee "$EVIDENCE_DIR/workloads.txt"

kubectl -n "$NS" get deployment "$LITELLM_DEPLOYMENT" -o yaml \
  > "$EVIDENCE_DIR/litellm-deployment-before.yaml"

kubectl -n "$NS" get deployment "$POSTGRES_DEPLOYMENT" -o yaml \
  > "$EVIDENCE_DIR/postgres-deployment-before.yaml"

kubectl -n "$NS" get ingress -o yaml \
  > "$EVIDENCE_DIR/ingress-before.yaml"

kubectl -n "$NS" get events --sort-by=.lastTimestamp \
  | tee "$EVIDENCE_DIR/events.txt"
```

检查重点：

- LiteLLM 是否只有 `1/1` 副本；
- Service Endpoint 是否为空；
- Pod 是否频繁重启；
- 是否出现 `OOMKilled`、`Unhealthy`、`Evicted` 或探针失败。

### 3.2 精确检查 OOM 和重启

```bash
kubectl -n "$NS" get pods -l "$LITELLM_LABEL" \
  -o jsonpath='{range .items[*]}pod={.metadata.name}{"\n"}ready={.status.containerStatuses[0].ready}{" restarts="}{.status.containerStatuses[0].restartCount}{"\n"}state={.status.containerStatuses[0].state}{"\n"}lastState={.status.containerStatuses[0].lastState}{"\n"}resources={.spec.containers[0].resources}{"\n\n"}{end}' \
  | tee "$EVIDENCE_DIR/litellm-container-state.txt"
```

若看到以下内容，即确认发生过容器内存终止：

```text
reason: OOMKilled
exitCode: 137
```

查看当前资源使用：

```bash
kubectl top pod -n "$NS"
kubectl top nodes
```

如 Metrics API 不可用，记录错误并继续，不要临时安装组件。

检查 cgroup 内存；重启后 `memory.events` 只代表当前容器实例：

```bash
LITELLM_POD="$(kubectl -n "$NS" get pod -l "$LITELLM_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" exec "$LITELLM_POD" -- sh -c '
  for f in memory.current memory.peak memory.max memory.events; do
    echo "== $f ==";
    cat "/sys/fs/cgroup/$f" 2>/dev/null || true;
  done
' | tee "$EVIDENCE_DIR/litellm-cgroup-memory.txt"kubectl -n "$NS" get events --sort-by=.lastTimestamp \
  | tail -n 100
```

### 3.3 LiteLLM 和 ingress-nginx 日志

当前容器：

```bash
kubectl -n "$NS" logs "$LITELLM_POD" --since=24h \
  > "$EVIDENCE_DIR/litellm-current.log"

grep -Ei '429|too many|rate.?limit|503|unavailable|timeout|prisma|database|memory|error|exception' \
  "$EVIDENCE_DIR/litellm-current.log" \
  | tail -n 300
```

如果发生过重启，读取上一个容器日志：

```bash
kubectl -n "$NS" logs "$LITELLM_POD" --previous \
  > "$EVIDENCE_DIR/litellm-previous.log" 2>&1 || true

grep -Ei '429|too many|rate.?limit|503|unavailable|timeout|prisma|database|memory|error|exception' \
  "$EVIDENCE_DIR/litellm-previous.log" \
  | tail -n 300
```

ingress-nginx：

```bash
INGRESS_POD="$(kubectl -n ingress-nginx get pod \
  -l app.kubernetes.io/component=controller \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n ingress-nginx logs "$INGRESS_POD" --since=24h \
  > "$EVIDENCE_DIR/ingress-nginx.log"

grep -E ' 401 | 429 | 502 | 503 | 504 |upstream|timed out|no live upstreams|connect\(\) failed' \
  "$EVIDENCE_DIR/ingress-nginx.log" \
  | tail -n 500
```

统计错误码：

```bash
awk '{status[$9]++} END {
  for (s in status)
    if (s==401 || s==429 || s==499 || s==500 || s==502 || s==503 || s==504)
      print s, status[s]
}' "$EVIDENCE_DIR/ingress-nginx.log" | sort -n
```

判断：

- `upstream prematurely closed connection` 后紧接 OOM：LiteLLM 进程被终止；
- upstream 列表为 `[]`：Service 当时没有 Ready Endpoint；
- 429 出现在 LiteLLM 响应中：继续检查 Budget/RPM/TPM或上游；
- 429 只出现在上游错误正文：检查 Azure OpenAI 配额。

### 3.4 PostgreSQL 健康、连接和容量

```bash
PG_POD="$(kubectl -n "$NS" get pod -l "$POSTGRES_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" exec "$PG_POD" -- sh -c \
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

kubectl -n "$NS" exec "$PG_POD" -- df -h /var/lib/postgresql/data
kubectl -n "$NS" exec "$PG_POD" -- df -i /var/lib/postgresql/data

kubectl -n "$NS" logs "$PG_POD" --since=24h \
  > "$EVIDENCE_DIR/postgres.log"

grep -Ei 'PANIC|FATAL|No space left|recovery|checkpoint|could not write|too many connections' \
  "$EVIDENCE_DIR/postgres.log" | tail -n 300
```

连接数：

```bash
kubectl -n "$NS" exec -i "$PG_POD" -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
show max_connections;
select count(*) from pg_stat_activity;
select coalesce(state, 'unknown'), count(*)
from pg_stat_activity
group by state
order by state;
SQL
```

### 3.5 Usage 数据规模与异常 Key 基数

以下命令只返回计数，不输出 Key：

```bash
kubectl -n "$NS" exec "$PG_POD" -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
    select
      date,
      count(*) as rows,
      count(distinct api_key) as distinct_keys,
      coalesce(sum(api_requests),0) as requests,
      coalesce(sum(successful_requests),0) as succeeded,
      coalesce(sum(failed_requests),0) as failed
    from \"LiteLLM_DailyUserSpend\"
    group by date
    order by date desc
    limit 30;
  "' | tee "$EVIDENCE_DIR/daily-user-spend-summary.txt"
```

表大小：

```bash
kubectl -n "$NS" exec "$PG_POD" -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
    select
      relname,
      n_live_tup,
      n_dead_tup,
      pg_size_pretty(pg_total_relation_size(relid))
    from pg_stat_user_tables
    order by pg_total_relation_size(relid) desc
    limit 20;
    select pg_size_pretty(pg_database_size(current_database()));
  "' | tee "$EVIDENCE_DIR/database-size-summary.txt"
```

若某一天 `distinct_keys` 接近 `failed`，说明很可能存在每次更换无效 Bearer 的自动化请求。

### 3.6 使用 Log Analytics 定位 401 来源

```bash
WORKSPACE_ID="$(az monitor log-analytics workspace show \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$WORKSPACE_NAME" \
  --query customerId -o tsv)"

export START_UTC="2026-09-22T00:00:00Z"
export END_UTC="2026-09-23T00:00:00Z"
```

按源IP、路径和User-Agent聚合，不读取 Authorization：

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query "
ContainerLogV2
| where TimeGenerated between (datetime($START_UTC) .. datetime($END_UTC))
| where PodNamespace == \"ingress-nginx\"
| extend Message=tostring(LogMessage)
| where Message matches regex @\"\\s401\\s\"
| extend ClientIP=tostring(split(Message,\" \")[0]), Quoted=split(Message,\"\\\"\")
| extend Request=tostring(Quoted[1]), UserAgent=tostring(Quoted[5])
| extend Method=tostring(split(Request,\" \")[0]), Path=tostring(split(Request,\" \")[1])
| summarize Requests=count(), FirstSeen=min(TimeGenerated), LastSeen=max(TimeGenerated)
  by ClientIP, Method, Path, UserAgent
| order by Requests desc
| take 50
" -o table | tee "$EVIDENCE_DIR/401-sources.txt"
```

每分钟速率：

```bash
az monitor log-analytics query --workspace "$WORKSPACE_ID" --analytics-query "
ContainerLogV2
| where TimeGenerated between (datetime($START_UTC) .. datetime($END_UTC))
| where PodNamespace == \"ingress-nginx\"
| extend Message=tostring(LogMessage)
| where Message matches regex @\"\\s401\\s\"
| summarize Requests=count() by bin(TimeGenerated, 1m)
| order by Requests desc
| take 50
" -o table | tee "$EVIDENCE_DIR/401-rate.txt"
```

注意：微软/Azure ASN 只表示 IP 托管于微软网络，不代表请求来自微软官方。先由客户安全团队核对授权扫描任务、NAT出口和源IP清单。

### 3.7 区分 429 来源

从 ingress 和 LiteLLM 日志提取同一时间段的429：

```bash
grep ' 429 ' "$EVIDENCE_DIR/ingress-nginx.log" | tail -n 200
grep -Ei '429|too many requests|rate.?limit|budget|rpm|tpm' \
  "$EVIDENCE_DIR/litellm-current.log" | tail -n 300
```

使用客户批准的测试 Key 重现时，只把 Key放入当前 shell 环境，不要粘贴到聊天或文档：

```bash
read -rsp "Approved test key: " LITELLM_TEST_KEY
export LITELLM_TEST_KEY
echo

# 按客户实际域名和获准测试模型填写
export LITELLM_BASE_URL="https://REPLACE_CUSTOMER_HOST"
export TEST_MODEL="REPLACE_APPROVED_TEST_MODEL"

curl --silent --show-error --include \
  --connect-timeout 10 --max-time 120 \
  -H "Authorization: Bearer $LITELLM_TEST_KEY" \
  -H "Content-Type: application/json" \
  "$LITELLM_BASE_URL/v1/chat/completions" \
  --data "{\"model\":\"$TEST_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"health check\"}],\"max_tokens\":16}" \
  > "$EVIDENCE_DIR/approved-test-response.txt"

grep -Ei 'HTTP/|retry-after|x-ratelimit|429|budget|rpm|tpm|too many' \
  "$EVIDENCE_DIR/approved-test-response.txt"

unset LITELLM_TEST_KEY
```

不要共享完整响应文件，先确认其中不含敏感正文或凭据。

## 4. 第二阶段：紧急修复

以下操作会改变生产环境，必须取得客户批准并记录执行时间。

### 4.1 首选止血：暂时避免大范围 Usage/Logs 查询

在完成修复前：

- 通知管理员暂时不要打开大日期范围的 Usage/Logs 页面；
- 不反复刷新503页面；
- API客户端对429/503使用指数退避，不立即高并发重试。

这是零变更止血方式，应先执行。

### 4.2 确认异常来源后，临时阻断单一源 IP

仅当客户确认该IP不是授权扫描器、办公NAT或业务出口时执行。不要封整个ASN或大网段。

```bash
export BLOCK_IP="REPLACE_CONFIRMED_ABUSIVE_IP/32"
export INGRESS_NAME="$(kubectl -n "$NS" get ingress \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" annotate ingress "$INGRESS_NAME" \
  nginx.ingress.kubernetes.io/denylist-source-range="$BLOCK_IP" \
  --overwrite

kubectl -n "$NS" get ingress "$INGRESS_NAME" \
  -o jsonpath='{.metadata.annotations.nginx\.ingress\.kubernetes\.io/denylist-source-range}{"\n"}'
```

从获准客户端验证正常访问。若误伤，立即回滚：

```bash
kubectl -n "$NS" annotate ingress "$INGRESS_NAME" \
  nginx.ingress.kubernetes.io/denylist-source-range-
```

如果该Ingress原来已有denylist，不能使用上述删除命令覆盖原值；应从 `$EVIDENCE_DIR/ingress-before.yaml` 恢复原注解。

### 4.3 OOM已确认时，临时提高LiteLLM内存

适用条件：

- 已确认 `OOMKilled/137`；
- 节点有足够可分配内存；
- 客户批准滚动重启和短暂中断；
- 该修复只是止血，不能替代版本升级和数据治理。

先检查节点和Pod资源：

```bash
kubectl top nodes
kubectl describe nodes | grep -A8 -E '^Name:|Allocated resources:'
kubectl -n "$NS" get deployment "$LITELLM_DEPLOYMENT" \
  -o jsonpath='requests={.spec.template.spec.containers[0].resources.requests}{"\n"}limits={.spec.template.spec.containers[0].resources.limits}{"\n"}'
```

若节点容量允许，将 limit 临时提高到 `4Gi`。保留原 `1Gi` request，避免仅因紧急提高调度请求而导致新Pod无法调度：

```bash
kubectl -n "$NS" set resources deployment "$LITELLM_DEPLOYMENT" \
  --containers=litellm \
  --requests=cpu=250m,memory=1Gi \
  --limits=cpu=1,memory=4Gi

kubectl -n "$NS" rollout status deployment "$LITELLM_DEPLOYMENT" \
  --timeout=10m
```

重新获取Pod并验证：

```bash
LITELLM_POD="$(kubectl -n "$NS" get pod -l "$LITELLM_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" get pod "$LITELLM_POD" -o wide
kubectl -n "$NS" exec "$LITELLM_POD" -- python -c \
  "import requests; r=requests.get('http://127.0.0.1:4000/health/liveliness',timeout=10); print(r.status_code, r.text); r.raise_for_status()"
```

若新Pod无法调度、启动失败或验证不通过，立即回滚刚才生成的Deployment revision：

```bash
kubectl -n "$NS" rollout undo deployment "$LITELLM_DEPLOYMENT"
kubectl -n "$NS" rollout status deployment "$LITELLM_DEPLOYMENT" \
  --timeout=10m
```

`$EVIDENCE_DIR/litellm-deployment-before.yaml`保留作审计和人工比对，不直接整份覆盖正在运行的Deployment。

不要在没有Redis共享状态和完整验证的旧环境中直接把LiteLLM扩成多副本。

### 4.4 进程已异常但未自动恢复时，重启LiteLLM

仅在健康检查失败、Endpoint异常或进程持续无响应时执行：

```bash
kubectl -n "$NS" rollout restart deployment "$LITELLM_DEPLOYMENT"
kubectl -n "$NS" rollout status deployment "$LITELLM_DEPLOYMENT" \
  --timeout=10m
```

重启不会解决聚合查询缺陷；未提高内存、未阻断异常流量或未处理异常数据时，问题可能再次发生。

### 4.5 PostgreSQL磁盘不足时

若明确出现 `No space left on device`，不要删除WAL、PVC、PV或数据库文件。

```bash
STORAGE_CLASS="$(kubectl -n "$NS" get pvc pg-data \
  -o jsonpath='{.spec.storageClassName}')"

kubectl get storageclass "$STORAGE_CLASS" \
  -o custom-columns='NAME:.metadata.name,ALLOW_EXPANSION:.allowVolumeExpansion,PROVISIONER:.provisioner'
```

只有 `ALLOW_EXPANSION=true` 且客户批准目标容量时，扩容原PVC。以下 `20Gi` 只是示例，必须大于当前申请值：

```bash
kubectl -n "$NS" patch pvc pg-data --type merge \
  -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'

kubectl -n "$NS" get pvc pg-data \
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,REQUESTED:.spec.resources.requests.storage,CAPACITY:.status.capacity.storage'
```

PVC不能缩容。若文件系统未自动扩展并显示 `FileSystemResizePending`，在维护窗口重启PostgreSQL：

```bash
kubectl -n "$NS" rollout restart deployment "$POSTGRES_DEPLOYMENT"
kubectl -n "$NS" rollout status deployment "$POSTGRES_DEPLOYMENT" \
  --timeout=5m
```

不要同时重启PostgreSQL和LiteLLM。

## 5. 数据清理前的强制备份

只有数据库健康时执行：

```bash
PG_POD="$(kubectl -n "$NS" get pod -l "$POSTGRES_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" exec "$PG_POD" -- sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/litellm-incident.dump'

kubectl -n "$NS" exec "$PG_POD" -- \
  pg_restore -l /tmp/litellm-incident.dump \
  > "$EVIDENCE_DIR/pg-restore-list.txt"

REMOTE_SHA256="$(kubectl -n "$NS" exec "$PG_POD" -- \
  sha256sum /tmp/litellm-incident.dump | awk '{print $1}')"

kubectl -n "$NS" exec "$PG_POD" -- \
  cat /tmp/litellm-incident.dump \
  > "$EVIDENCE_DIR/litellm-incident.dump"

test -s "$EVIDENCE_DIR/litellm-incident.dump"

LOCAL_SHA256="$(sha256sum "$EVIDENCE_DIR/litellm-incident.dump" \
  | awk '{print $1}')"

test "$LOCAL_SHA256" = "$REMOTE_SHA256"

printf '%s  %s\n' "$LOCAL_SHA256" "$EVIDENCE_DIR/litellm-incident.dump" \
  | tee "$EVIDENCE_DIR/litellm-incident.dump.sha256"

kubectl -n "$NS" exec "$PG_POD" -- rm /tmp/litellm-incident.dump
```

这里使用不带TTY的`kubectl exec ... cat`传输二进制dump，避免`kubectl cp`对容器内`tar`的额外依赖。若出现连接节点`10250`的502，先不要删除容器内dump，也不要重启PostgreSQL；按下面命令检查exec通道和Pod所在节点：

```bash
kubectl -n "$NS" get pod "$PG_POD" -o wide
kubectl get nodes -o wide
kubectl -n "$NS" exec "$PG_POD" -- true
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 100
```

若普通`kubectl exec ... -- true`也返回`proxy error ... :10250`，说明是API Server到目标节点kubelet的exec通道故障，不是dump文件错误。保留`/tmp/litellm-incident.dump`，等待通道恢复后重新执行远端SHA、流式复制和本地SHA校验；持续失败时由客户AKS平台团队检查目标节点、kubelet、网络/防火墙和控制面隧道。

必须确认：

- dump文件非空；
- `pg_restore -l`成功；
- 远端和本地SHA256一致且已记录；
- 备份已进入客户批准的受控存储。

## 6. 异常Usage数据清理边界

现场默认只做统计，不直接删除。执行删除前必须同时满足：

1. 客户确认异常日期、来源和业务影响；
2. 有已验证的数据库备份；
3. 客户数据Owner批准删除范围；
4. 明确需要处理的所有相关表和外键；
5. 在事务中先执行相同条件的 `SELECT count(*)`；
6. 维护窗口内执行并完成UI/API回归。

不要只删除一个Daily表，也不要使用仅按日期的宽泛删除。错误清理可能删除真实审计、预算和费用数据。

建议把以下信息交给后续修复变更，而不是现场即席拼接DELETE：

```bash
kubectl -n "$NS" exec "$PG_POD" -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
    select
      date,
      count(*) as rows,
      count(distinct api_key) as distinct_keys,
      sum(failed_requests) as failed,
      sum(successful_requests) as succeeded
    from \"LiteLLM_DailyUserSpend\"
    group by date
    order by rows desc
    limit 20;
  "'
```

## 7. 告警和通知检查

### 7.1 规则和Action Group

```bash
az monitor scheduled-query list --resource-group "$RESOURCE_GROUP" \
  --query '[].{name:name,enabled:enabled,severity:severity,actions:actions.actionGroups}' \
  -o table

az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" \
  --name ag-litellm-stage1-owner \
  --query '{enabled:enabled,emailReceivers:emailReceivers,logicAppReceivers:logicAppReceivers,webhookReceivers:webhookReceivers}' \
  -o json
```

判断：

- Email receiver必须为 `Enabled`；
- 飞书方案必须存在Logic App receiver；
- 只有Email、没有Logic App时，飞书不会收到通知；
- Action Group测试成功不代表具体KQL规则能覆盖OOM。

### 7.2 Portal测试飞书

在Azure Portal：

1. Monitor → Alerts → Action groups；
2. 打开 `ag-litellm-stage1-owner`；
3. 确认 `feishu-alert` Logic App Action存在且启用Common Alert Schema；
4. 使用 `Log Alert V2` 样本执行Test；
5. Logic App Runs history必须Succeeded；
6. 飞书HTTP返回 `code=0`；
7. 目标群实际收到消息。

重新部署monitoring后，必须重新检查Logic App绑定是否被IaC覆盖。

## 8. 修复后验证

### 8.1 Kubernetes

```bash
kubectl -n "$NS" get deployment,pod,svc,endpoints -o wide
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 100
kubectl top pod -n "$NS"
```

通过标准：

- LiteLLM和PostgreSQL均Ready；
- LiteLLM Service有Endpoint；
- restartCount不再增长；
- 没有新的OOMKilled或持续探针失败；
- 内存与limit保留合理余量。

### 8.2 健康检查

```bash
LITELLM_POD="$(kubectl -n "$NS" get pod -l "$LITELLM_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" exec "$LITELLM_POD" -- python -c \
  "import requests; p='/health/liveliness'; r=requests.get('http://127.0.0.1:4000'+p,timeout=10); print(p,r.status_code); r.raise_for_status()"

kubectl -n "$NS" exec "$LITELLM_POD" -- python -c \
  "import requests; p='/health/readiness'; r=requests.get('http://127.0.0.1:4000'+p,timeout=10); print(p,r.status_code); r.raise_for_status()"

PG_POD="$(kubectl -n "$NS" get pod -l "$POSTGRES_LABEL" \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl -n "$NS" exec "$PG_POD" -- sh -c \
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

### 8.3 外部访问

```bash
curl --silent --show-error --output /dev/null \
  --write-out 'HTTP %{http_code} total=%{time_total}s\n' \
  --connect-timeout 10 --max-time 30 \
  "$LITELLM_BASE_URL/health/liveliness"
```

再使用客户批准的测试Key完成：

- `/v1/models`；
- 一个最小Chat请求；
- Dashboard登录；
- Logs页面仅使用小日期范围；
- 观察15分钟内是否出现新的OOM、重启、429或503。

## 9. 现场判定速查

| 证据 | 判定 | 优先动作 |
|---|---|---|
| `OOMKilled`、exit 137、503时间一致 | LiteLLM内存终止 | 避免Usage大查询；确认节点容量后临时升至4Gi |
| 401高频且不同Key数接近失败数 | 疑似更换Bearer的自动化扫描 | 核对来源；按单IP阻断；准备数据治理 |
| PG `No space left on device` | 数据库磁盘耗尽 | 扩容原PVC；禁止删WAL/PVC |
| PG连接数接近上限 | 数据库连接压力 | 定位连接来源；不要仅重启掩盖 |
| NGINX 503且Endpoint为空 | 后端不Ready/重启 | 查OOM、探针和Pod事件 |
| Azure/OpenAI响应包含配额429 | 上游RPM/TPM | 查部署配额、重试和路由 |
| LiteLLM响应包含budget/RPM/TPM | LiteLLM限额 | 核对User/Key/Team配置 |
| 邮件正常、飞书无Logic App receiver | 飞书未接线 | 重新绑定Logic App并测试 |

## 10. 禁止事项

- 不因IP属于微软/Azure ASN就直接信任或放行；
- 不封禁整个云厂商ASN或大网段；
- 不输出、复制或记录Master Key、数据库密码和Bearer；
- 不删除 `pg_wal`、`pg_logical`、PVC、PV或Azure Disk；
- 不在未备份和未批准时删除Spend/Usage数据；
- 不同时重启PostgreSQL和LiteLLM；
- 不在旧环境中未经验证直接扩为多个LiteLLM副本；
- 不把临时提高内存当作根治；
- 不通过真实填满磁盘、制造OOM或删除AKS来测试告警。

## 11. 后续永久修复

1. 升级到包含Usage聚合有界处理修复的批准版LiteLLM；
2. 对401、不同无效Key基数、OOM、重启和Ingress 5xx增加告警；
3. 将飞书Logic App绑定纳入客户IaC；
4. 在入口增加WAF、Bot防护、来源控制和速率限制；
5. 经备份和审批后治理异常Usage数据；
6. 推进Redis、LiteLLM多副本、PDB和HPA，消除单副本故障。
