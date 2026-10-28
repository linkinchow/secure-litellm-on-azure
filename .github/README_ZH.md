# GitHub Actions与客户迁移门禁

客户从[部署与验收工作流指南](../docs/customer-deployment-workflows-zh.md)开始。主要迁移入口支持公开fork和私有仓库，只能手动触发受保护默认分支；`CUSTOMER_CONFIG_JSON`及`WORKFLOW_ARTIFACT_KEY`必须配置为Environment Secret。不在源码内填写客户配置或凭据；账本从加密artifact自动读取。

当前West US 3 greenfield测试采用[本地Stage2–9 native-auth路径](../local_execution/stage2-9-guide-zh.md)：原生LiteLLM Admin用户名/密码及API virtual key，不是用户Entra SSO。下列Actions入口仍是受控客户交付方式，不代表当前测试通过Actions部署；管理面资源成功与workflow作业成功均不能替代真实rollout/协议验收。

## 实际执行与验收入口

- `customer-deploy.yml`：受保护默认分支、Environment；bootstrap初始化RG/日志库，各阶段plan/deploy，绑定计划run ID。阶段9release另需发布报告及托管API edge-bind回执。
- `customer-runtime.yml`：专属Linux私网runner、独立runtime OIDC身份；阶段0备份/隔离恢复、阶段1探针与监控、阶段4命名空间与监控、阶段5目标空库恢复、阶段6~8成品清单发布。
- `customer-acceptance.yml`：draft生成pending清单，confirm支持已批准GitHub login的单人逐项人工确认；record保留外部报告兼容。人工结果不冒充自动技术验收。
- `customer-runner-checks.yml`：在客户runner上实际核对工具、身份范围、新旧集群只读访问，不改变资源。

主要入口仅上传AES-GCM加密的`sealed-artifact.json`，计划/部署输出/验收数据通常保留7天，原始stdout/stderr和详细Summary不公开；dump、kubeconfig不进入artifact。非秘密workflow输入和运行元数据仍可能公开，不在输入中填写敏感信息。具体本机解密审核方法见指南，部署成功不等于可以生产切流。增强L3治理workflow仍保留独立私有/双审批要求。

可选Entra代理分支的API认证采用企业Token与客户端vkey双凭据，模型/预算只由LiteLLM管理。该分支的客户API bindings不再填写models，proxy-credentials动作仅初始化管理端Key，application不再发布API内部Key挂载。旧配置/镜像需配套迁移并刷新计划与回执，客户端Token自动续期和真实权限仍需验收；不自动清理旧Key/Vault/RBAC。当前native分支不使用该双凭据代理。见[认证契约及迁移步骤](../auth-proxy/README_ZH.md)。

## `customer-migration.yml`

第一阶段选择原生Spend Logs，不默认执行自建L3的audit-foundation/audit部署及audit治理/恢复workflow。原生Stage8发布、observability和模式化证据门禁已实现；增强L3的必审计binding及治理要求不会被原生配置自动满足。基础版仍需正文、查询权限、清理/备份、容量和故障验收，不能手填passed跳过，见[部署状态表](../docs/customer-deployment-workflows-zh.md)。本文不启用客户正文日志或修改workflow行为。

选择dev/test/prod、阶段0至9、guide/config-check/preflight/what-if以及组件。config-check允许无前序证据检查后续配置；preflight/what-if保持前序门禁。实际部署入口独立，不修改此workflow的只读行为。

原始配置/计划不作为artifact上传。详细变量、Secret、权限、每阶段准备和回退见迁移指南。应用Secret继续由客户Key Vault/CSI提供，不放入JSON非秘密参数。

## `ci.yml`

在Pull Request和`main` push时执行：

- Python单元测试、客户配置/证据门禁及README导航检查；
- 所有Bicep和Bicep参数编译；
- dev/test/prod Kustomize渲染；
- manifest安全策略检查；
- LiteLLM方案范围检查、个人信息和明显凭据扫描；
- Node认证/审计测试及固定版本OSS回调矩阵；
- Pull Request依赖审查。

CI不登录Azure，不部署资源。

## `iac-what-if.yml`

旧入口不再直接登录Azure或预览默认关闭的模板，运行时明确失败并引导至`customer-migration.yml`，防止绕过阶段门禁。新入口需要`AZURE_CLIENT_ID`、`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`及上述配置/证据，均在客户Environment管理。

## `promote-litellm-image.yml`

仅手动触发且绑定`prod` Environment审批：

1. 拒绝未固定SHA-256 digest的源镜像；
2. 使用`az acr import`导入目标ACR；
3. 解析目标不可变digest；
4. 生成SPDX JSON SBOM；
5. 使用Trivy阻断未修复Critical漏洞；
6. 使用GitHub OIDC/Cosign签名digest；构建镜像时附加构建commit（`llmgw.revision`）和运行时修订（`llmgw.runtimeRevision`，由`python -m scripts.runtime_revision --require-clean`对镜像输入文件的Git内容计算），部署只校验后者，因此无关commit不触发重建；
7. 上传30天SBOM证据；
8. 不直接部署Kubernetes，overlay变更必须走Pull Request。

该入口使用MIGRATION_PRIVATE_RUNNER_LABELS指定的私网runner，允许公开fork的受保护默认分支手动执行，并核对目标ACR与Secret配置一致。SBOM加密保存，Docker凭据使用作业独立临时目录并清理。首次使用前验证私网路径、Docker、OIDC最小权限和Cosign签名验证策略，不得开放生产ACR公网。镜像引用和Cosign透明度元数据不是秘密，应预期公开。

## Action版本治理

当前LiteLLM使用`1.104.8`固定源码供应链；`source-image-checks.yml`用于源码镜像检查，`promote-litellm-image.yml`保留受控digest导入/晋级用途。模型同步的唯一操作入口说明见[专用runbook](../docs/litellm-model-sync-runbook-zh.md)，不能用CI成功推断已在客户环境执行同步。

工作流中的所有第三方Action均固定到已验证的完整commit SHA，行尾保留对应版本标签注释。Dependabot每月提出升级，升级必须经过Dependency Review和Pull Request审批，不直接跟随可移动标签。
