# LiteLLM 运行配置受控更新

本流程修改已部署网关的 LiteLLM 配置，不重新部署 Azure 基础设施，不更新镜像、模型映射、凭据、数据库或身份认证模式。
适用于本项目固定的 LiteLLM `1.104.0` 构建和已有私有 AKS 工作负载；在能够访问私有集群的 Runner 上运行。

## 1. 配置来源与更新方式

实际配置文件是 Pod 内的 `/app/config/config.yaml`。它来自 ConfigMap，以只读 `subPath` 挂载；
直接修改旧 ConfigMap 不会可靠地刷新现有 Pod，也不是本流程的发布方式。

入口为 `python -m local_execution.runtime_config`，依次执行：

1. 校验私有输入、现有镜像、工作负载身份、配置和 HPA 基线。
2. 生成只读 plan，并执行 Kubernetes server dry-run。
3. execute 重新读取全部输入和基线，必须匹配审批的 SHA-256。
4. 创建按配置内容命名的 immutable ConfigMap，仅替换 Deployment 的配置卷引用，等待滚动更新。
5. 校验 Deployment 的其他字段未变、HPA 未变、每个 Ready Pod 挂载的新配置一致。
6. native 模式在每个 Ready Pod 内通过只读本地 API 核对实际环境凭据登录开关。
7. 验证成功后，原子写入 `$CFG` 的 `application.runtimeSettings`，保留其他配置及 `localExecution`。

这不是热加载：滚动期间新旧 Pod 可以暂时同时存在。HPA 可在既有批准范围内调整副本数。
后续完整应用部署也读取 `application.runtimeSettings`，不会恢复已关闭的环境凭据登录。
计划会列出受影响的 Stage 6–9 配置指纹；本操作不自动刷新阶段验收、下游回执或推理证据。

## 2. 支持的字段

patch 仅合并明确提供的叶子字段。省略表示保留，不支持 `null` 删除或任意 YAML 覆盖。
未知字段、非法类型、凭据字面量和未批准的设置都会被拒绝。

| 配置段 | 字段 | 约束 |
| --- | --- | --- |
| `general_settings` | `disable_env_credential_login` | 布尔；仅 native 模式 |
| `general_settings` | `ui_access_mode` | `all` / `admin_only` |
| `litellm_settings` | `drop_params`、`modify_params`、`force_ipv4` | 布尔 |
| `litellm_settings` | `set_verbose` | 只允许 `false`，不能开启敏感调试日志 |
| `litellm_settings` | `request_timeout` | 有限正数 |
| `router_settings` | `enable_pre_call_checks` | 布尔 |
| `router_settings` | `num_retries`、`allowed_fails` | 非负整数 |
| `router_settings` | `cooldown_time`、`retry_after` | 有限非负数 |
| `router_settings` | `timeout`、`stream_timeout` | 有限正数 |
| `router_settings` | `routing_strategy` | 见下列值 |

路由策略只接受 `simple-shuffle`、`least-busy`、`usage-based-routing`、
`usage-based-routing-v2`、`latency-based-routing`、`cost-based-routing`。
允许设置不代表该策略在客户实际负载、配额和部署组合上已经完成验收。

不支持修改模型列表、模型别名/回退、Master Key、UI_PASSWORD、数据库 URL、Redis 连接、
Workload Identity、token refresh、文件/数据库模型存储模式、正文日志/留存、任意 hook 或外部 endpoint。
Stage 6 生成的配置固定注册镜像内置的 Codex Responses 兼容回调（现象、原因与边界见[部署指南](customer-deployment-workflows-zh.md)的“Codex Responses兼容回调”），本流程保留该回调，不能增删。

## 3. 关闭环境凭据登录的前提

先创建独立的 `proxy_admin` 用户，为其设置自己的密码，并完成必要的首次密码重置。
必须在新开的浏览器无痕窗口中，使用该用户的邮箱和密码成功登录，而不是复用旧会话、
bootstrap 用户、`UI_USERNAME` / `UI_PASSWORD` 或 Master Key。
在 Users 页面记录该用户的 **User ID**；不要把密码作为脚本参数或写入 patch。

关闭开关时必须同时提供：

- `--verified-admin-user-id`：上述独立用户的 User ID。
- `--admin-password-login-verified`：运维人员对独立密码登录成功的明确确认。

脚本通过 `/v2/user/info` 核对用户 ID、`proxy_admin` 角色、邮箱登录身份，并排除已知 bootstrap 身份。
该 API 不提供密码就绪证明，所以脚本不把账号存在当作密码登录成功；
新无痕窗口的人工验证和确认参数不可省略。它们绑定在审批计划中。
已有 `$CFG` 声明关闭环境凭据登录时，后续配置更新仍要求这两个参数。

这只关闭共享环境凭据的 **UI 登录入口**，不会删除独立管理员、旋转 Secret，
也不会关闭 Master Key 的管理 API 能力。Admin WAF 来源白名单仍生效。

## 4. 准备私有输入

从仓库根目录运行，使用当前已有 `$CFG`、实际变更单和满足客户单人/双人策略的 approver object ID。
`$CFG` 和 patch 必须位于本 checkout 的 Git-ignored 路径，并设为 `chmod 600`。

```bash
mkdir -p temp/runtime-config-inputs
chmod 700 temp/runtime-config-inputs
PATCH=temp/runtime-config-inputs/disable-env-login.local.json
cat > "$PATCH" <<'JSON'
{
  "schema_version": 1,
  "general_settings": {
    "disable_env_credential_login": true
  }
}
JSON
chmod 600 "$CFG" "$PATCH"
```

也接受等价的 UTF-8 YAML。可以在同一 patch 中包含多个支持的字段；不能混入模型或凭据。

## 5. Plan 与 execute

将 `$INDEPENDENT_ADMIN_USER_ID` 设置为已实际验证的独立管理员 User ID。
以下展示单个 approver 参数；双人策略重复 `--approved-by`，提供两个实际独立获批身份。

```bash
.venv/bin/python -m local_execution.runtime_config \
  --config "$CFG" \
  --patch "$PATCH" \
  --operation plan \
  --change-ticket "$CHANGE_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID" \
  --verified-admin-user-id "$INDEPENDENT_ADMIN_USER_ID" \
  --admin-password-login-verified
```

检查输出目录中的 `plan.json`：字段前后值、变更范围、管理员确认、受影响的阶段指纹和写操作声明。
审批成功 plan 的 `planSha256`，然后执行：

```bash
.venv/bin/python -m local_execution.runtime_config \
  --config "$CFG" \
  --patch "$PATCH" \
  --operation execute \
  --change-ticket "$CHANGE_TICKET" \
  --approved-by "$APPROVER_OBJECT_ID" \
  --verified-admin-user-id "$INDEPENDENT_ADMIN_USER_ID" \
  --admin-password-login-verified \
  --approved-plan-sha256 "$APPROVED_RUNTIME_PLAN_SHA256"
```

代码、输入、审批身份、管理员确认或实时基线变化时，旧审批不能继续使用，需重新 plan。
只接受所选输出目录中最新成功生成的 plan。默认输出为 `temp/runtime-config/`；
`--output-dir` 也必须位于本仓库的 `temp/` 内，plan 与 execute 使用相同目录。

非登录开关 patch：若 `$CFG` 尚未声明关闭环境凭据登录，无需两个管理员确认参数。
若新字段值已在实时配置中生效，execute 不创建新的配置卷更新，但仍验证工作负载并持久化批准的声明。

## 6. 生效确认与证据

成功 execute 应返回 `status=completed`、`rolloutVerified=true`、`configInstalled=true`。
native 模式还应有 `effectiveAuthenticationVerified=true`。
关闭环境凭据登录后，各 Ready Pod 的 `/health/readiness/details` 必须返回
`show_env_credential_login_warning=false`，独立管理员身份也必须仍有效。

Probe 只在已批准 Deployment 的 Ready Pod 内访问 `127.0.0.1:4000`，不走公网、不更改 WAF；
Master Key 只在 Pod 内从 CSI 挂载读取，不输出或写入证据，禁止 HTTP redirect。
它不发起 UI 登录、创建新会话或签发 token。
其他受控参数通过配置字节哈希、完整挂载校验和固定版本进程重启验证交付；
路由效果、超时行为、模型推理和客户应用验收需要另行测试。

UI 对 readiness details 有缓存，更新后可以重新登录或等待缓存刷新；
仅隐藏红色 tag 或清理 localStorage 不等于关闭环境凭据登录。
最后再用独立管理员新开无痕窗口验证实际 UI 登录。

输出目录保存私有计划、状态、前后 customer JSON、前后运行 YAML、原始基线及生效检查。
前态 YAML 是语义等价快照；原 ConfigMap 的名称和内容指纹记录在基线中。
原子安装另外保存 `previous-release.json`，此处是复用安装器生成的 customer JSON 备份，不代表新的发布回执。
不得提交这些运行数据。

## 7. 失败与回退

失败不会自动回滚，也不会自动安装未经验证的 `$CFG`。先检查 `state.json` 的 phase、
`applicationPatchAttempted`、`applicationPatched`、`rolloutVerified` 和 `configInstalled`；
失败状态不代表 Kubernetes 从未修改。

如果实际 patch/rollout 已发生而 `$CFG` 未安装，不要直接重试旧审批、删除共享资源或编辑失败操作的证据。
先核对实时 Deployment、每个 Ready Pod 的配置及独立管理员登录能力，再申请单独恢复审批：

- 向前恢复：核对 `customer-config.desired.json` 与原审批、实时挂载完全一致，批准后原子安装正确的声明，再重新 plan。
- 向后回退：存在配置卷变化时，会生成 `recovery-rollback-patch.json`。核对原 ConfigMap 仍存在，
  在执行前补入实时 `/metadata/resourceVersion` 的 `test`，并保留 Deployment UID、当前配置卷名称的 `test`；
  只回退配置卷，等待 rollout 并复核全部挂载和登录状态，再按审批恢复前态 customer JSON。

回退关闭登录的变更会重新开启共享环境凭据入口，应明确审查该安全影响。
不自动删除历史 ConfigMap、不旋转 Master Key/Salt/UI_PASSWORD、不触碰 Private Endpoint、DNS、角色或 WAF。
