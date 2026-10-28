import json
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
READMES = (
    "l3-audit/README.md",
    "README.md", "README_ZH.md", "LiteLLM/README.md", "LiteLLM/README_ZH.md",
    "tests/README.md", "tests/README_ZH.md", "infra/README_ZH.md",
    "deploy/README_ZH.md", "auth-proxy/README_ZH.md", ".github/README_ZH.md",
    "docs/customer-deployment-workflows-zh.md",
    "docs/customer-private-runner-preparation-zh.md",
    "docs/litellm-content-audit-phase1-customer-brief-zh.md",
    "docs/litellm-ingress-tls-certificate-design-zh.md",
    "docs/litellm-ingress-certificate-import-runbook-zh.md",
    "docs/litellm-optional-l3-runbook-zh.md",
    "docs/litellm-security-validation-runbook-zh.md",
    "docs/litellm-bom-cost-comparison-zh.md",
    "docs/litellm-azure-security-hardening-zh.md",
    "docs/customer-migration-guide-zh.md",
    "local_execution/README_ZH.md",
    "local_execution/stage2-9-guide-zh.md",
    "docs/litellm-1.104.0-upgrade-validation-2026-10-07.md",
    "docs/litellm-1.104.8-upgrade-validation-2026-10-28.md",
    "docs/litellm-model-sync-runbook-zh.md",
    "docs/litellm-security-hardening-implementation-roadmap-zh.md",
    "docs/litellm-security-hardening-change-list-zh.md",
    "docs/litellm-code-completion-backlog-2026-09-07.md",
    "infra/edge/README_ZH.md",
)


class ProjectDocumentationTests(unittest.TestCase):
    def test_security_validation_runbook_has_bounded_commands_and_explicit_evidence(self):
        import subprocess
        text = (ROOT / "docs/litellm-security-validation-runbook-zh.md").read_text()
        self.assertIn("!docs/litellm-security-validation-runbook-zh.md",
                      (ROOT / ".gitignore").read_text().splitlines())
        for token in ("edge_security_probe", "waf_security_probe", "TrackingReference",
                      "detection-match-only", "inconclusive", "--confirm-rate-limit-impact",
                      "--configuration-only", "--sandbox read-only", "--request-budget 100"):
            self.assertIn(token, text)
        self.assertNotIn("--dangerously-bypass", text)
        for source in re.findall(r"```bash\n(.*?)\n```", text, re.S):
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_l3_runbook_is_opt_in_and_commands_are_valid(self):
        import subprocess
        text = (ROOT / "docs/litellm-optional-l3-runbook-zh.md").read_text()
        self.assertIn("!docs/litellm-optional-l3-runbook-zh.md",
                      (ROOT / ".gitignore").read_text().splitlines())
        for token in ("不执行本文", "16MiB", "1GiB", "24小时", "30天", "legal hold",
                      "CLI", "不能宣称零丢失", "activation.js", "Stage4入口", "Stage9绑定",
                      "不保存返回向量", "无正文", "微软托管密钥"):
            self.assertIn(token, text)
        for source in re.findall(r"```bash\n(.*?)\n```", text, re.S):
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for source in re.findall(r"```json\n(.*?)\n```", text, re.S):
            self.assertIsInstance(json.loads(source), dict)
        self.assertIn("litellm-optional-l3-runbook-zh.md",
                      (ROOT / "local_execution/stage2-9-guide-zh.md").read_text())

    def test_root_mermaid_architecture_preserves_separate_ingress_planes(self):
        for name in ("README.md", "README_ZH.md"):
            text = (ROOT / name).read_text()
            diagrams = re.findall(r"```mermaid\n(.*?)\n```", text, re.S)
            with self.subTest(document=name):
                self.assertEqual(len(diagrams), 1)
                diagram = diagrams[0]
                self.assertTrue(diagram.startswith("flowchart TB\n"))
                for path in (
                    "apiClients --> apiEdge --> apiPls --> apiIngress",
                    "admins --> adminEdge --> adminPls --> adminIngress",
                    "apiIngress --> litellm", "adminIngress --> litellm",
                    'subgraph aks["Private AKS"]',
                    'backendVault[', 'certificateVault[', 'models[', 'postgres[', 'redis[',
                    'acr[', 'monitoring[', 'l3[',
                ):
                    self.assertIn(path, diagram)
                self.assertNotIn("```text", text)

    def test_root_architecture_images_are_tracked_and_embedded_in_readmes(self):
        import subprocess

        images = (
            "images/litellm-azure-security-architecture.png",
            "images/litellm-content-audit-phase1-architecture.jpg",
        )
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", *images], cwd=ROOT,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((ROOT / "docs/images").exists())
        for path in images:
            self.assertTrue((ROOT / path).is_file())
        with (ROOT / images[0]).open("rb") as stream:
            self.assertEqual(stream.read(8), b"\x89PNG\r\n\x1a\n")
        with (ROOT / images[1]).open("rb") as stream:
            self.assertEqual(stream.read(3), b"\xff\xd8\xff")
        for name in ("README.md", "README_ZH.md"):
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                self.assertRegex(text, r"!\[[^\]]+\]\(images/litellm-azure-security-architecture\.png\)")
                self.assertIn("[images](images/)", text)
                self.assertNotIn("docs/images/", text)

    def test_root_readmes_record_bounded_current_native_baseline(self):
        for name in ("README.md", "README_ZH.md"):
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                self.assertIn("2026-10-09", text)
                self.assertRegex(text, r"(?i)west\s*us\s*3")
                for token in ("native", "1.104.8", "Balanced_B0", "10000", "SocketAddr"):
                    self.assertIn(token, text)

    def test_retired_stage_documents_have_no_remaining_references(self):
        import subprocess

        for pattern in ("litellm-stage[0-9]-*.md", "customer-stage[0-9]-acceptance-checklist-zh.md"):
            self.assertEqual(list((ROOT / "docs").glob(pattern)), [])
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.split("\0")
        retired = r"(?:litellm-stage[0-9]-[^\s)\"<>]+\.md|customer-stage[0-9]-acceptance-checklist-zh\.md)"
        for name in filter(None, tracked):
            path = ROOT / name
            if path.is_file():
                with self.subTest(document=name):
                    self.assertNotRegex(path.read_text(), retired)

    def test_model_sync_has_one_standalone_runbook(self):
        import subprocess
        from scripts.model_configuration import INFO_FIELDS, PARAM_FIELDS

        path = ROOT / "docs/litellm-model-sync-runbook-zh.md"
        runbook = path.read_text()
        self.assertIn("!docs/litellm-model-sync-runbook-zh.md", (ROOT / ".gitignore").read_text().splitlines())
        for token in ("schema_version", "subscriptions", "subscription_id", "resources",
                      "resource_group", "name", "endpoint", "models", "model_name", "deployment_name",
                      "litellm_params", "model_info", "input_cost_per_token", "output_cost_per_token",
                      "model_sync_evidence", "inferenceVerified=false", "预算检查",
                      "https://docs.litellm.ai/docs/proxy/configs"):
            self.assertIn(token, runbook)
        for field in PARAM_FIELDS | INFO_FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", runbook)
        for directory in (ROOT / "docs", ROOT / "local_execution"):
            for document in directory.rglob("*.md"):
                if document == path:
                    continue
                with self.subTest(document=document.relative_to(ROOT)):
                    self.assertNotRegex(document.read_text(), r"model[_-]sync|azure-openai\.catalog")
        for source in re.findall(r"```bash\n(.*?)\n```", runbook, re.S):
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for source in re.findall(r"```json\n(.*?)\n```", runbook, re.S):
            example = json.loads(source)
            self.assertIsInstance(example, dict)
            for subscription in example.get("subscriptions", []):
                for resource in subscription["resources"]:
                    self.assertIn("name", resource)
                    self.assertNotIn("account_name", resource)
        self.assertIn("预期值核验", runbook)
        self.assertIn("不把手填 endpoint 当作绕过发现的兜底", runbook)
        self.assertIn("默认策略是 `replace`：`MODEL_CATALOG` 是整个网关的完整模型清单", runbook)
        self.assertIn("## 7. 按需执行：下游回执重验证", runbook)
        self.assertIn("本节不是模型发布生效或 UI 显示更新的前提", runbook)

    def test_model_sync_runbook_uses_matching_explicit_v1_fallback(self):
        import shlex
        from scripts.model_sync_catalog import parse_catalog

        runbook = (ROOT / "docs/litellm-model-sync-runbook-zh.md").read_text()
        self.assertIn("### 2.6 确认推理 API version：v1 与日期版本", runbook)
        self.assertIn("不等于具体 deployment 已完成真实推理验证", runbook)
        versions = {}
        for source in re.findall(r"```bash\n(.*?)\n```", runbook, re.S):
            command = ".venv/bin/python -m local_execution.model_sync "
            if command in source:
                args = shlex.split(source.split(command, 1)[1])
                operation = args[args.index("--operation") + 1]
                versions[operation] = args[args.index("--api-version") + 1]
                self.assertEqual(args[args.index("--model-policy") + 1], "replace")
        self.assertEqual(versions, {"plan": "v1", "execute": "v1"})
        catalogs = [
            json.loads(source) for source in re.findall(r"```json\n(.*?)\n```", runbook, re.S)
            if '"schema_version"' in source
        ]
        self.assertEqual(len(catalogs), 2)
        for catalog in catalogs:
            parse_catalog(catalog, api_version="v1")

    def test_runtime_kubernetes_authorization_is_identity_and_scope_specific(self):
        import subprocess

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-B2. runtime身份的Kubernetes预授权", 1)[1].split("#### 4-C.", 1)[0]
        self.assertIn("#4-b2-runtime身份的kubernetes预授权", guide)
        for value in ("AZURE_RUNTIME_CLIENT_ID", "RUNTIME_OBJECT_ID", "不是`AZURE_CLIENT_ID`", "aadProfile.enableAzureRbac=true", "roleDefinitions/write", "roleAssignments/write", "所有Namespace", "Pod Security", "ServiceAccount", "SecretProviderClass", "不能创建Namespace", "不会收紧", "个人管理员kubectl成功不能代替", "不要求重部署platform"):
            self.assertIn(value, section)
        definition = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S)[1])["properties"]
        self.assertEqual(definition["roleName"], "LLMGW AKS Namespace Bootstrapper")
        self.assertEqual(definition["assignableScopes"], ["/subscriptions/REPLACE_TARGET_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"])
        self.assertEqual(definition["permissions"], [{
            "actions": [], "notActions": [], "dataActions": [
                "Microsoft.ContainerService/managedClusters/namespaces/read",
                "Microsoft.ContainerService/managedClusters/namespaces/write",
            ], "notDataActions": [],
        }])
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        self.assertEqual(len(snippets), 4)
        for source in snippets:
            self.assertTrue(source.startswith("set -euo pipefail\n"))
            self.assertNotIn("|| break", source)
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("--admin", source)
            self.assertNotIn("get-access-token", source)
            self.assertNotIn("role assignment delete", source)
        self.assertIn('--id "$RUNTIME_CLIENT_ID"', snippets[0])
        self.assertIn('"${AKS_ID}/namespaces/litellm"', snippets[1])
        self.assertIn('"Azure Kubernetes Service RBAC Reader"', snippets[1])
        self.assertIn("for NAMESPACE in llm-api-ingress llm-admin-ingress; do", snippets[2])
        self.assertIn('--scope "${AKS_ID}/namespaces/${NAMESPACE}"', snippets[2])
        self.assertIn('"Azure Kubernetes Service RBAC Writer"', snippets[2])
        for source in snippets[1:3]:
            self.assertIn('--assignee-object-id "$RUNTIME_OBJECT_ID" --assignee-principal-type ServicePrincipal', source)
        self.assertIn("--include-inherited", snippets[3])
        self.assertIn("--fill-principal-name false", snippets[3])

    def test_target_connectivity_workflow_and_upgrade_instructions_match(self):
        import subprocess
        from scripts.customer_migration import COMPONENTS

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-B1. Runner到新AKS的DNS连接", 1)[1].split("#### 4-B2.", 1)[0]
        example = json.loads((ROOT / "config/customer.example.json").read_text())
        self.assertEqual(set(example["parameters"]["runner-target-connectivity"]), COMPONENTS["runner-target-connectivity"][2])
        self.assertEqual(COMPONENTS["runner-target-connectivity"][0], 4)
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("runner-target-connectivity", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        self.assertIn("connectivity-review.json", (ROOT / ".github/workflows/customer-deploy.yml").read_text())
        self.assertNotIn("runner-target-connectivity", (ROOT / ".github/workflows/customer-runner-checks.yml").read_text())
        for value in ("AZURE_CLIENT_ID", "节点RG", "join/action", "manageDnsLink", "privateFqdn", "nodeResourceGroup", "privatelink.azurecr.io", "acrPrivateEndpointIps", "acrManagement", "S4-04A", "S4-04B", "关闭自动注册", "不用重跑platform或certificate-vault", "无需增加字段", "ACR登录端点解析到公网并返回403", "single-operator", "旧SHA Stage0–3账本", "当前SHA的匹配plan", "配置指纹匹配", "双人模式", "源镜像", "7天", "实际结论变化时仍须实测重验", "旧platform回执SHA", "不自动覆盖A记录", "只允许", "reused", "external"):
            self.assertIn(value, section)
        self.assertLess(guide.index("| S4-04B |"), guide.index("| S4-05 |"))
        settings = json.loads("{" + re.search(r"```json\n(.*?)\n```", section, re.S)[1] + "}")
        self.assertEqual(settings["runner-target-connectivity"], example["parameters"]["runner-target-connectivity"])
        for source in re.findall(r"```bash\n(.*?)\n```", section, re.S):
            self.assertEqual(subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True).returncode, 0)

    def test_aks_ingress_role_recovery_is_narrow_and_requires_a_new_runtime_plan(self):
        from scripts.customer_migration import COMPONENTS

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("**入口Service一直Pending、`lb-ready-api`超时的升级恢复：**", 1)[1].split("#### 4-B2.", 1)[0]
        self.assertEqual(COMPONENTS["aks-ingress-role"][:2], (4, "aks-ingress-role"))
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("aks-ingress-role", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        for value in (
            "identity.principalId", "不是kubelet身份", "CUSTOMER_CONFIG_JSON`不新增字段",
            "component=aks-ingress-role", "Microsoft.Authorization/roleAssignments", "Network Contributor",
            "专用目标VNet下的一条", "不能为通过What-if扩大", "新建S4-11", "当前部分落地状态",
            "不能复用旧S4-11", "无需重跑S4-01/02 platform", "Service仍Pending",
        ):
            self.assertIn(value, section)
        self.assertLess(section.index("operation=plan"), section.index("operation=deploy"))
        self.assertLess(section.index("operation=deploy"), section.index("新建S4-11"))

    def test_certificate_lock_permissions_are_explicit_and_target_scoped(self):
        import subprocess

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-A1. 证书Vault防删除锁权限", 1)[1].split("#### 4-B.", 1)[0]
        self.assertIn("#4-a1-证书vault防删除锁权限", guide)
        self.assertLess(guide.index("#### 4-A1."), guide.index("| S4-03 |"))
        definition = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))["properties"]
        self.assertEqual(definition["roleName"], "LLMGW Resource Lock Writer")
        self.assertEqual(definition["assignableScopes"], ["/subscriptions/REPLACE_TARGET_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"])
        self.assertEqual(definition["permissions"], [{
            "actions": ["Microsoft.Authorization/locks/read", "Microsoft.Authorization/locks/write"],
            "notActions": [], "dataActions": [], "notDataActions": [],
        }])
        template = (ROOT / "infra/certificate-vault/main.bicep").read_text()
        self.assertIn("resource deleteLock 'Microsoft.Authorization/locks@", template)
        for value in ("protect-key-vault-from-deletion", "CanNotDelete"):
            self.assertIn(value, template)
            self.assertIn(value, section)
        for required in ("AZURE_CLIENT_ID", "DEPLOY_OBJECT_ID", "不是Client ID本身", "不是模型所在RG", "roleDefinitions/write", "locks/delete", "不是只允许某一个Vault锁", "权限传播", "新建S4-03", "新SHA", "不需重跑已成功的platform", "唯一原因"):
            self.assertIn(required, section)
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        self.assertEqual(len(snippets), 1)
        source = snippets[0]
        result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for required in ("--subscription", '--assignee-object-id "$DEPLOY_OBJECT_ID"', '--scope "$TARGET_RG_SCOPE"', "--include-inherited", "--fill-principal-name false"):
            self.assertIn(required, source)
        for forbidden in ("role assignment create", "role definition create", "az lock", "get-access-token", "--debug"):
            self.assertNotIn(forbidden, source)

    def test_stage4_partial_network_failure_requires_replanning(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("**S4-02部分失败时：**", 1)[1].split("**S4-04之后", 1)[0]
        for required in ("Operation details", "AnotherOperationInProgress", "dependsOn", "冲突操作结束", "产生费用", "不能声称自动回滚", "新建S4-01", "新的成功plan ID", "不要回放Stage0", "新SHA"):
            self.assertIn(required, section)

    def test_stage4_model_authorization_matches_templates_and_safe_instructions(self):
        import subprocess

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 4-0. 模型账号的一次性授权", 1)[1].split("#### 4-A.", 1)[0]
        self.assertLess(guide.index("#### 4-0."), guide.index("| S4-01 |"))
        self.assertIn("#4-0-模型账号的一次性授权", guide)
        definition = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))["properties"]
        self.assertEqual(definition["roleName"], "LLMGW Model Deployment Operator")
        self.assertEqual(len(definition["permissions"]), 1)
        permissions = definition["permissions"][0]
        self.assertEqual(set(permissions["actions"]), {
            "*/read", "Microsoft.Resources/deployments/*",
            "Microsoft.CognitiveServices/accounts/privateEndpointConnectionsApproval/action",
        })
        for field in ("notActions", "dataActions", "notDataActions"):
            self.assertEqual(permissions[field], [])
        self.assertEqual(len(definition["assignableScopes"]), 2)
        for scope in definition["assignableScopes"]:
            self.assertRegex(scope, r"^/subscriptions/REPLACE_MODEL_SUBSCRIPTION_ID_[12]/resourceGroups/REPLACE_MODEL_RG_[12]$")
        role_id = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
        self.assertIn(role_id, (ROOT / "infra/modules/model-access-role/main.bicep").read_text())
        self.assertIn(role_id, section)
        for required in (
            "AZURE_CLIENT_ID", "不是`AZURE_RUNTIME_CLIENT_ID`", "Enterprise applications", "az ad sp show",
            "Assignable scopes", "roleDefinitions/write", "JSON页", "数据面", "同一Entra租户",
            "Constrain roles", "不限制接收者", "Constrain roles and principal types", "Service principals",
            "@Request", "@Resource", "roleAssignments/write", "roleAssignments/delete", "同时限制新增和删除",
            "顶层`principalType=ServicePrincipal`", "仅补Azure授权", "新SHA", "plan本身不要求前序验收",
        ):
            self.assertIn(required, section)
        workflow = yaml.load((ROOT / ".github/workflows/customer-deploy.yml").read_text(), Loader=yaml.BaseLoader)
        login = next(step for step in workflow["jobs"]["infrastructure"]["steps"] if step.get("uses", "").startswith("azure/login@"))
        self.assertEqual(login["with"]["client-id"], "${{ vars.AZURE_CLIENT_ID }}")
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        self.assertEqual(len(snippets), 2)
        for source in snippets:
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            for forbidden in ("role assignment create", "role definition create", "get-access-token", "list-keys", "--debug"):
                self.assertNotIn(forbidden, source)
        for value in ("--include-inherited", "--assignee-object-id", "--fill-principal-name false", "conditionVersion:conditionVersion"):
            self.assertIn(value, snippets[-1])

    def test_certificate_workflow_example_and_manual_import_stay_aligned(self):
        import subprocess
        from scripts.customer_migration import COMPONENTS

        example = json.loads((ROOT / "config/customer.example.json").read_text())
        settings = example["parameters"]["certificate-vault"]
        self.assertEqual(set(settings), COMPONENTS["certificate-vault"][2])
        self.assertFalse(example["parameters"]["platform"]["createStage5KeyVaultPrivateDnsZone"])
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("certificate-vault", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("### 阶段4：", 1)[1].split("### 阶段5：", 1)[0]
        for value in (*settings, "Secrets User", "Secrets Officer", "certificateMaterialsImported=false", "25KB", "S4-03", "S4-04", "S4-11", "S4-12", "Object ID", "不打开Vault公网", "CA私钥独立保管"):
            self.assertIn(value, section)
        snippets = re.findall(r"```bash\n(.*?)\n```", section.split("#### 4-C.", 1)[1], re.S)
        self.assertEqual(len(snippets), 6)
        for source in snippets:
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        for value in ("两个Secret，不是两张新证书", "Microsoft Trusted CA List", "Stage9来源准入由Front Door WAF", "上传不需要登录Runner", "客户本机上传", "公有CA签发", "不是已签发证书", "不返回值", "Secret Identifier"):
            self.assertIn(value, section)
        for value in ("specific virtual networks and IP addresses", "实际公网出口IPv4", "/32", "Microsoft.KeyVault/vaults/write", "无论上传成功、失败或中断", "Disable public access", "ipRules=[]", "bypass=None", "网络策略拒绝", "certificates.api/admin.secretId", "applied=true", "verified=true"):
            self.assertIn(value, section)
        self.assertLess(section.index("**6. 立即关闭公网"), section.index("**7. 验证上传材料"))
        commands = "\n".join(snippets)
        for forbidden in ("az network bastion", "scp -P", "AZURE_CONFIG_DIR", "az keyvault update", "az keyvault secret show"):
            self.assertNotIn(forbidden, commands)
        login = snippets[3]
        self.assertTrue(login.startswith("set -euo pipefail\n"))
        for value in ("az login --tenant", "az ad signed-in-user show", 'API_PEM_FILE="temp/certificate-import/api.pem"', "az keyvault secret list"):
            self.assertIn(value, login)
        upload = snippets[4]
        self.assertEqual(upload.count("az keyvault secret set"), 2)
        self.assertEqual(upload.count("--query '{id:id,enabled:attributes.enabled}'"), 2)
        self.assertNotIn("--value", upload)
        self.assertNotIn("certificate import", upload)
        self.assertIn("az keyvault show", snippets[5])
        self.assertIn("publicNetworkAccess:properties.publicNetworkAccess", snippets[5])
        reference = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        self.assertIn("customer-migration-guide-zh.md#4-a-共用证书vault的配置与取值", reference)
        self.assertIn("customer-migration-guide-zh.md#4-c-部署后手动导入两个secret", reference)
        self.assertIn("立即关闭公网并清除临时IP规则", reference)
        template = (ROOT / "infra/certificate-vault/main.bicep").read_text()
        self.assertIn("publicNetworkAccess: 'Disabled'", template)
        self.assertIn("bypass: 'None'", template)
        self.assertIn("infra/certificate-vault/main.bicep", (ROOT / "scripts/validate-stage4.sh").read_text())

    def test_documented_certificate_creation_produces_valid_api_csr_and_rejects_self_signed_admin_origin(self):
        import subprocess
        import tempfile
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        section = (ROOT / "docs/customer-migration-guide-zh.md").read_text().split("#### 4-C.", 1)[1]
        snippets = re.findall(r"```bash\n(.*?)\n```", section, re.S)
        api_source = snippets[1].replace("REPLACE_BASE_DOMAIN_FROM_CUSTOMER_JSON", "customer.invalid")
        self.assertNotIn("openssl req -x509 -newkey", section)
        self.assertIn("自签名或企业内部CA", section)
        self.assertIn("Microsoft Trusted CA List", section)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(["bash"], input=api_source, cwd=directory, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            api_directory = next((Path(directory) / "temp").glob("api-csr.*"))
            csr = x509.load_pem_x509_csr((api_directory / "api.csr").read_bytes())
            self.assertTrue(csr.is_signature_valid)
            self.assertEqual(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), ["llm-api.customer.invalid"])
            api_key = serialization.load_pem_private_key((api_directory / "api.key").read_bytes(), password=None)
            self.assertEqual(api_key.public_key().public_numbers(), csr.public_key().public_numbers())

    def test_manual_certificate_validation_outputs_metadata_only_and_rejects_oversize(self):
        import io
        from unittest.mock import patch

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text().split("#### 4-C.", 1)[1]
        source = re.search(r"<<'PY'\n(.*?)\nPY", guide, re.S).group(1)
        material = {"certificate": "private-value-sentinel", "key": "private-value-sentinel", "sha256": "a" * 64, "expiresAt": "synthetic-expiry"}
        cases = ((b"synthetic-pem", None, True), (b"x" * (25 * 1024 + 1), None, False), (b"invalid", ValueError("private-value-sentinel"), False))
        for raw, error, success in cases:
            with self.subTest(success=success, size=len(raw)), patch("sys.argv", ["-", "customer.invalid", "api.pem", "admin.pem"]), patch("pathlib.Path.read_bytes", return_value=raw), patch("scripts.private_ingress.certificate_material", return_value=material, side_effect=error) as validate, patch("sys.stdout", new_callable=io.StringIO) as output:
                if success:
                    exec(compile(source, "manual-certificate-validation", "exec"), {})
                    self.assertEqual(validate.call_count, 2)
                    self.assertEqual([call.args[1] for call in validate.call_args_list], ["llm-api.customer.invalid", "llm-admin.customer.invalid"])
                    self.assertIn("sha256=" + "a" * 64, output.getvalue())
                else:
                    with self.assertRaisesRegex(SystemExit, "no private material printed") as failure:
                        exec(compile(source, "manual-certificate-validation", "exec"), {})
                    self.assertNotIn("private-value-sentinel", str(failure.exception))
                    self.assertEqual(output.getvalue(), "")
                self.assertNotIn("private-value-sentinel", output.getvalue())

    def test_customer_certificate_import_runbook_uses_two_file_backed_secrets(self):
        import subprocess

        name = "docs/litellm-ingress-certificate-import-runbook-zh.md"
        runbook = (ROOT / name).read_text()
        self.assertIn("!" + name, (ROOT / ".gitignore").read_text().splitlines())
        for token in ("llm-api.<baseDomain>.pem", "llm-api.<baseDomain>.key",
                      "llm-admin.<baseDomain>.pem", "llm-admin.<baseDomain>.key",
                      "api-tls", "admin-tls", "Key Vault Secrets Officer",
                      "Key Vault Secrets User", "certificateImporterPrincipalId",
                      "privateIngress.api.tlsSecretId", "25KB", "7 天",
                      "publicNetworkAccess=Disabled", "不是原子事务",
                      "applied=true", "verified=true", "Stage4 plan",
                      "PREVIOUS_AZURE_CONFIG_DIR", "不是四个 Secret"):
            with self.subTest(token=token):
                self.assertIn(token, runbook)
        snippets = re.findall(r"```bash\n(.*?)\n```", runbook, re.S)
        for source in snippets:
            result = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        commands = "\n".join(snippets)
        self.assertEqual(commands.count("az keyvault secret set"), 2)
        self.assertEqual(commands.count("--query '{id:id,enabled:attributes.enabled}'"), 2)
        self.assertIn("--name api-tls --file \"$API_PEM_FILE\" --encoding utf-8", commands)
        self.assertIn("--name admin-tls --file \"$ADMIN_PEM_FILE\" --encoding utf-8", commands)
        for forbidden in ("--value", "--debug", "az keyvault certificate import",
                          "az role assignment create", "az keyvault update"):
            self.assertNotIn(forbidden, commands)
        for document in ("docs/customer-migration-guide-zh.md",
                         "docs/litellm-ingress-tls-certificate-design-zh.md",
                         "local_execution/stage2-9-guide-zh.md"):
            with self.subTest(document=document):
                self.assertIn(Path(name).name, (ROOT / document).read_text())

    def test_customer_certificate_import_preflight_checks_chain_and_private_report_boundary(self):
        import io
        import subprocess
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        runbook = (ROOT / "docs/litellm-ingress-certificate-import-runbook-zh.md").read_text()
        source = re.search(r"<<'PY'\n(.*?)\nPY", runbook, re.S).group(1)
        material = {"certificate": "public-chain", "key": "private-sentinel",
                    "sha256": "a" * 64, "expiresAt": "synthetic-expiry"}
        cases = (
            (b"x" * (25 * 1024), None, 0, None, True),
            (b"x" * (25 * 1024 + 1), None, 0, None, False),
            (b"invalid", ValueError("private-sentinel"), 0, None, False),
            (b"encrypted-pem", TypeError("private-sentinel"), 0, None, False),
            (b"synthetic-pem", None, 1, None, False),
            (b"synthetic-pem", None, 0, subprocess.TimeoutExpired("private-sentinel", 30), False),
        )
        for raw, validation_error, returncode, verify_error, success in cases:
            with self.subTest(size=len(raw), returncode=returncode, success=success), \
                    tempfile.TemporaryDirectory() as directory, \
                    patch("sys.argv", ["-", "customer.invalid", "api.pem", "admin.pem", directory]), \
                    patch("pathlib.Path.read_bytes", return_value=raw), \
                    patch("scripts.private_ingress.certificate_material", return_value=material,
                          side_effect=validation_error) as validate, \
                    patch("subprocess.run", return_value=SimpleNamespace(
                        returncode=returncode, stdout="private-sentinel", stderr="private-sentinel"),
                          side_effect=verify_error) as verify, \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                if success:
                    exec(compile(source, "certificate-import-preflight", "exec"), {})
                    rows = json.loads(output.getvalue())
                    self.assertEqual([row["plane"] for row in rows], ["api", "admin"])
                    self.assertTrue(all(row["publicChainVerified"] for row in rows))
                    self.assertEqual(validate.call_count, 2)
                    self.assertEqual([call.args[1] for call in validate.call_args_list],
                                     ["llm-api.customer.invalid", "llm-admin.customer.invalid"])
                    self.assertEqual(verify.call_count, 2)
                    for call, plane in zip(verify.call_args_list, ("api", "admin"), strict=True):
                        args = call.args[0]
                        self.assertEqual(args[:3], ["openssl", "verify", "-CAfile"])
                        self.assertEqual(args[4:8], ["-purpose", "sslserver", "-verify_hostname",
                                                   f"llm-{plane}.customer.invalid"])
                        self.assertEqual(args[8:], ["-untrusted",
                                                   str(Path(directory) / f"{plane}-chain.pem"),
                                                   str(Path(directory) / f"{plane}-chain.pem")])
                        self.assertTrue(call.kwargs["capture_output"])
                        self.assertEqual(call.kwargs["timeout"], 30)
                else:
                    with self.assertRaisesRegex(SystemExit, "no private material printed") as failure:
                        exec(compile(source, "certificate-import-preflight", "exec"), {})
                    self.assertNotIn("private-sentinel", str(failure.exception))
                    self.assertEqual(output.getvalue(), "")
                self.assertNotIn("private-sentinel", output.getvalue())

    def test_litellm_1104_runtime_upgrade_contract_is_aligned(self):
        from scripts.source_supply_chain import RUNTIME_CONTRACT, source_image
        from scripts.validate_manifests import EXPECTED_DIGEST
        from scripts.validate_stage6 import APPROVED_DIGEST

        digest = "sha256:6cc08e60be3e4ebf0cfb41ef23aed97e9ba4f61702d1e95e533f47ee6d67865f"
        self.assertEqual(source_image(), "docker.litellm.ai/berriai/litellm@" + digest)
        self.assertEqual(EXPECTED_DIGEST, digest)
        self.assertEqual(APPROVED_DIGEST, digest)
        self.assertEqual(RUNTIME_CONTRACT["versions"], {"litellm": "1.104.8", "anyio": "4.14.2", "PyJWT": "2.15.0"})
        self.assertTrue(all(RUNTIME_CONTRACT["gpt6"].values()))
        self.assertTrue(all(RUNTIME_CONTRACT["catalog"].values()))
        promotion = yaml.load((ROOT / ".github/workflows/promote-litellm-image.yml").read_text(), Loader=yaml.BaseLoader)
        inputs = promotion["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["source_image"]["default"], "docker.litellm.ai/berriai/litellm@" + digest)
        self.assertEqual(inputs["environment"]["default"], "test")
        self.assertEqual(inputs["target_tag"]["default"], "litellm-azure:1.104.8")
        self.assertEqual(inputs["build_azure_runtime"]["default"], "true")
        self.assertIn("docker.litellm.ai/berriai/litellm@" + digest, (ROOT / ".github/workflows/ci.yml").read_text())
        evidence = (ROOT / "docs/litellm-1.104.8-upgrade-validation-2026-10-28.md").read_text()
        for required in (digest, "CVE-2026-102268", "gpt-6.1", "189", "operator-managed", "e82a3c646908ba0a6ee0beddc4310d7a6cac4065"):
            self.assertIn(required, evidence)

    def test_runner_connectivity_workflow_example_and_runbook_stay_aligned(self):
        from scripts.customer_migration import COMPONENTS
        from scripts.runner_connectivity import BACKUP_PROBES
        example = json.loads((ROOT / "config/customer.example.json").read_text())
        settings = example["parameters"]["runner-connectivity"]
        self.assertEqual(set(settings), COMPONENTS["runner-connectivity"][2])
        self.assertTrue(settings["managePeering"])
        self.assertTrue(settings["manageBlobDnsLink"])
        self.assertTrue(settings["runnerVirtualNetworkId"].startswith("REPLACE_"))
        for filename in ("customer-deploy.yml", "customer-migration.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertIn("runner-connectivity", workflow["on"]["workflow_dispatch"]["inputs"]["component"]["options"])
        source = (ROOT / ".github/workflows/customer-runner-checks.yml").read_text()
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["check_backup"]["default"], "false")
        self.assertIn("${{ inputs.check_backup }}", source)
        self.assertIn("--check-backup", source)
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        for value in (*settings, *BACKUP_PROBES, "check_backup=true", "同订阅", "S0-08"):
            self.assertIn(value, guide)
        self.assertNotIn("人工网络/权限准备，不是workflow按钮", guide)

    def test_runbook_stage_tables_match_workflows_and_controllers(self):
        from scripts.customer_migration import COMPONENTS
        from scripts.migration_runtime import validate_action
        from tests.test_customer_migration import customer_config

        workflows = {}
        for filename in ("customer-deploy.yml", "customer-runtime.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            workflows[workflow["name"]] = workflow["on"]["workflow_dispatch"]["inputs"]
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        rows = re.findall(r"^\| (S\d+-\d+) \| (Customer infrastructure deployment|Customer private runtime operations) \| (\d) \| (component|action)=([a-z-]+) \| (plan|deploy|execute) \| ([^|]+) \| ([^|]+) \|$", guide, re.M)
        self.assertGreaterEqual(len(rows), 50)
        self.assertEqual({int(row[2]) for row in rows}, {0, 1, 3, 4, 5, 6, 7, 8, 9})
        self.assertEqual(len({row[0] for row in rows}), len(rows))
        plans = {}
        for step, workflow_name, stage, selector, action, operation, approval, confirmation in rows:
            with self.subTest(step=step):
                inputs = workflows[workflow_name]
                self.assertIn(stage, inputs["stage"]["options"])
                self.assertIn(action, inputs[selector]["options"])
                self.assertIn(operation, inputs["operation"]["options"])
                if selector == "component":
                    self.assertIn(int(stage), {3, 4, 5} if action == "platform" else {COMPONENTS[action][0]})
                else:
                    validate_action(customer_config(), int(stage), action)
                key = (workflow_name, stage, action)
                if operation == "plan":
                    self.assertIn("留空", approval)
                    self.assertEqual(confirmation.strip(), "留空")
                    self.assertNotEqual(action, "backup-restore")
                    plans[key] = step
                else:
                    self.assertEqual(confirmation.strip(), "test")
                    if action == "backup-restore":
                        self.assertEqual(operation, "execute")
                        self.assertEqual(approval.strip(), "留空")
                    else:
                        self.assertIn(plans[key], approval)

    def test_runbook_places_backup_requirements_next_to_execution(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        backup = guide.split("### 阶段0：", 1)[1].split("### 阶段1：", 1)[0]
        for required in ("POSTGRES_RESTORE_IMAGE", "Environment Variable", "docker pull --platform linux/amd64", ".RepoDigests", "backupAutomationPrincipalId", "Private DNS", "backupBlob", "backupSha256", "只支持execute"):
            self.assertIn(required, backup)
        for required in ("Legacy direct hash input; prefer approved_run_id", "Successful matching infrastructure plan run ID", "MIGRATION_RELEASE_JSON", "最终目标选择/接线待补齐", "当前无stop-legacy workflow按钮"):
            self.assertIn(required, guide)
        self.assertNotIn("Stage8/application仍要求auditRuntime", guide)
        self.assertNotIn("本仓库尚未自动编排该controller/LB", guide)

    def test_stage_zero_sequence_includes_truthful_acceptance_handoff(self):
        from scripts.customer_migration import stage_checks
        from tests.test_customer_migration import customer_config

        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("#### 0-C. 按顺序运行", 1)[1].split("#### 0-C1.", 1)[0]
        workflow = yaml.load((ROOT / ".github/workflows/customer-acceptance.yml").read_text(), Loader=yaml.BaseLoader)
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        for step, operation in (("S0-09", "draft"), ("S0-11", "confirm")):
            self.assertIn(operation, inputs["operation"]["options"])
            self.assertIn(f"| {step} | {workflow['name']} | {operation} |", section)
        self.assertIn(",".join(stage_checks(0, customer_config())), section)
        for field in ("reviewed_run_id", "checked_items", "evidence_notes", "confirm_environment"):
            self.assertIn(field, inputs)
            self.assertIn(field, section)
        for required in ("S0-10", "这八步未自动检查", "当前confirm不支持部分通过", "不是S0-08的备份ID", "没有可以如实提交的完整confirm填写值", "不能用artifact key替代"):
            self.assertIn(required, section)

    def test_backup_preflight_review_separates_network_identity_and_data_proofs(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        review = guide.split("#### 0-C1. S0-05逐项审查", 1)[1].split("### 阶段1：", 1)[0]
        for required in ("properties.parameters.backupAutomationPrincipalId.value", "properties.outputs.backupStorage.value", "networkInterfaces[0].id", "ipConfigurations[].privateIPAddress", "peeringState", "allowVirtualNetworkAccess", "FullyInSync", "az network private-dns link vnet list", "show-effective-route-table", "list-effective-nsg", 'getent ahostsv4 "$BLOB_HOST"', "--noproxy '*'", "remote_ip=%{remote_ip}", "--connect-timeout 5", "Storage Blob Data Contributor", "--auth-mode login", "--subresource=exec", "Blob只读探针已由check_backup覆盖", "不能证明Blob授权", "列举成功不能证明上传/下载成功"):
            with self.subTest(required=required):
                self.assertIn(required, review)
        commands = "\n".join(re.findall(r"```bash\n(.*?)\n```", review, re.S))
        self.assertEqual(commands.count("az network vnet peering list"), 2)
        self.assertNotRegex(commands, r"az\s+network\s+[^\n]*(?:\bcreate\b|\bdelete\b|\bupdate\b)")
        self.assertNotIn("role assignment create", commands)
        self.assertNotIn("curl -k", commands)
        self.assertNotIn("kubectl exec", commands)

    def test_backup_identity_example_and_value_discovery_match_runtime_contract(self):
        from scripts.customer_migration import COMPONENTS, parameters_for
        from tests.test_customer_migration import customer_config

        example = json.loads((ROOT / "config/customer.example.json").read_text())
        for name in ("backupOwnerPrincipalId", "backupAutomationPrincipalId"):
            with self.subTest(parameter=name):
                self.assertIn(name, COMPONENTS["backup"][2])
                self.assertRegex(example["parameters"]["backup"][name], r"^REPLACE_[A-Z_]+$")
        config = customer_config()
        principal = "55555555-5555-4555-8555-555555555555"
        config["parameters"]["backup"] = {
            **example["parameters"]["backup"],
            "logAnalyticsWorkspaceName": "customer-logs",
            "backupOwnerPrincipalId": "33333333-3333-4333-8333-333333333333",
            "backupAutomationPrincipalId": principal,
            "virtualNetworkName": "target-vnet",
        }
        _, document = parameters_for(config, 0, "backup")
        self.assertEqual(document["parameters"]["backupAutomationPrincipalId"]["value"], principal)
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        identity = guide.split("#### 0-A1. ", 1)[1].split("#### 0-B. ", 1)[0]
        for required in ("backupOwnerPrincipalId", "backupAutomationPrincipalId", "AZURE_RUNTIME_CLIENT_ID", "Enterprise applications", 'az ad sp show --id "$RUNTIME_CLIENT_ID"', "clientId:appId,objectId:id", "az identity show", "objectId:principalId", 'az ad user show --id "$BACKUP_OWNER_UPN"', "claims.appid", "claims/objectidentifier", "CUSTOMER_CONFIG_JSON", "component=backup", "Storage Blob Data Contributor", "--assignee-object-id", "--include-inherited"):
            with self.subTest(required=required):
                self.assertIn(required, identity)

    def test_runbook_covers_configuration_for_all_primary_workflows(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        configuration = guide.split("### 2.1 Variables和Secrets", 1)[1].split("### 2.2", 1)[0]
        files = ("customer-deploy.yml", "customer-runtime.yml", "customer-migration.yml", "customer-acceptance.yml", "customer-runner-checks.yml", "customer-gateway-checks.yml", "promote-litellm-image.yml")
        for filename in files:
            workflow = (ROOT / ".github/workflows" / filename).read_text()
            for context, name in set(re.findall(r"\b(vars|secrets)\.([A-Z][A-Z0-9_]*)", workflow)):
                with self.subTest(workflow=filename, context=context, name=name):
                    self.assertIn("`" + name + "`", configuration)

    def test_migration_guide_documents_actual_runner_check_inputs(self):
        guide = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        section = guide.split("### 2.1 Variables和Secrets", 1)[1].split("### 2.2", 1)[0]
        rows = {
            columns[0].strip("`"): columns[1]
            for line in section.splitlines() if line.startswith("| `")
            for columns in ([cell.strip() for cell in line.strip("|").split("|")],)
        }
        workflow = (ROOT / ".github/workflows/customer-runner-checks.yml").read_text()
        inputs = set(re.findall(r"\$\{\{\s*(vars|secrets)\.([A-Z][A-Z0-9_]*)\s*\}\}", workflow))
        self.assertIn(("vars", "AZURE_RUNTIME_CLIENT_ID"), inputs)
        for context, name in inputs:
            expected = "Environment Secret" if context == "secrets" else "Environment Variable"
            if name == "MIGRATION_PRIVATE_RUNNER_LABELS":
                expected = "Repository Variable"
            with self.subTest(name=name):
                self.assertEqual(rows.get(name), expected)
        self.assertEqual(rows.get("AZURE_CLIENT_ID"), "Environment Variable")
        self.assertIn("Runner检查不读取它", section)
        self.assertNotIn("兼容Variable", section)

    def test_public_fork_setup_explains_secret_and_manual_confirmation_contract(self):
        for name in ("README.md", "README_ZH.md", "docs/customer-deployment-workflows-zh.md", "docs/customer-private-runner-preparation-zh.md"):
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                self.assertIn("fork", text)
                self.assertIn("WORKFLOW_ARTIFACT_KEY", text)
                self.assertIn("Customer private runner checks", text)
        guide = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        self.assertIn("scripts.workflow_security open", guide)
        self.assertIn("independentlyVerified=false", guide)
        self.assertIn("legacy-access-restore", guide)
        self.assertIn("edge-bind", guide)

    def test_current_guides_select_native_audit_without_claiming_production_acceptance(self):
        for name in ("README.md", "README_ZH.md", "docs/customer-migration-guide-zh.md", "docs/customer-deployment-workflows-zh.md"):
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                self.assertIn("Spend Logs", text)
                self.assertIn("2026-09-10", text)
                self.assertNotIn("L3是首发必需", text)
                self.assertNotIn("L3 is a required release capability", text)
        guide = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        self.assertIn("2026-10-09", guide)
        self.assertNotIn("不是原生模式已上线", guide)
        self.assertIn("不跳过Stage8进入Stage9", guide)
        self.assertIn("原生模式的observability不再依赖auditRuntime", guide)
        self.assertIn("readyForCustomerMigration", guide)
        self.assertIn("原生UI/API受控查询", guide)

    def test_active_admin_edge_docs_use_source_ip_allowlist_not_mtls(self):
        active = (
            "README.md", "README_ZH.md", "auth-proxy/README_ZH.md", "deploy/README_ZH.md",
            "infra/README_ZH.md", "infra/edge/README_ZH.md", "tests/README.md", "tests/README_ZH.md",
            "docs/customer-deployment-workflows-zh.md", "docs/customer-migration-guide-zh.md",
            "docs/customer-private-runner-preparation-zh.md",
            "docs/litellm-ingress-tls-certificate-design-zh.md", "local_execution/stage2-9-guide-zh.md",
        )
        forbidden = (
            "adminMtls", "admin_mtls_enforcement", "admin_ca_revocation_rotation",
            "ClientCertificateRequiredAndValidated", "strict-mTLS", "严格mTLS", "Admin客户端CA",
            "trustedClientCaSecrets", "allowedCertificateFqdns", "2026-08-01-preview",
        )
        for name in active:
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                for value in forbidden:
                    self.assertNotIn(value, text)
        for name in ("infra/edge/README_ZH.md", "docs/customer-migration-guide-zh.md", "local_execution/stage2-9-guide-zh.md"):
            text = (ROOT / name).read_text()
            with self.subTest(current_contract=name):
                self.assertIn("adminAllowedCidrs", text)
                self.assertIn("Prevention", text)
                self.assertIn("公网出口", text)

    def test_stage9_guides_separate_parallel_canary_from_optional_final_migration(self):
        guides = (
            "docs/customer-migration-guide-zh.md",
            "local_execution/stage2-9-guide-zh.md",
        )
        for name in guides:
            text = (ROOT / name).read_text()
            with self.subTest(document=name):
                for required in (
                    "并行canary", "不要求旧系统停写", "只在备份时点一致", "不会持续同步",
                    "独立数据库", "virtual key", "可选最终迁移", "旧环境退役", "第三方DNS",
                    "dev/test", "checks", "实时", "prod",
                ):
                    self.assertIn(required, text)
                self.assertNotIn("第5节未完成时不得启用业务流量", text)
                self.assertNotIn("未完成人工最终迁移方案时，只能执行", text)

        delivery = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        for required in ("dev/test native canary", "可省略人工checks", "其他canary/production继续要求完整证据"):
            self.assertIn(required, delivery)

    def test_local_execution_documents_greenfield_without_legacy_operations(self):
        local = (ROOT / "local_execution/README_ZH.md").read_text()
        main = (ROOT / "docs/customer-migration-guide-zh.md").read_text()
        later = (ROOT / "local_execution/stage2-9-guide-zh.md").read_text()
        for required in ("deploymentMode=greenfield", "manageBlobDnsLink=false", "--step network", "不得运行`backup`", "stage5-restore-target"):
            self.assertIn(required, local)
        self.assertIn("migration与greenfield均可", main)
        self.assertNotIn("当前不支持greenfield", main)
        self.assertIn("greenfield已完成Stage0 bootstrap/network/Runner Peering", later)

    def test_audit_costs_describe_native_storage_and_optional_enhancement(self):
        text = (ROOT / "docs/litellm-bom-cost-comparison-zh.md").read_text()
        section = text.split("## 6. 员工与 Agent 上下文审计成本影响", 1)[1].split("## 7.", 1)[0]
        for required in ("原生 Spend Logs", "PostgreSQL", "WAL", "备份", "自建 L3", "本轮不生成未经询价和压测的新总价"):
            self.assertIn(required, section)
        self.assertNotIn("L3通过异步 callback输出到独立存储", section)
        self.assertNotIn("`store_prompts_in_spend_logs` 保持 `false`", section)

    def test_customer_api_example_uses_admission_only_bindings(self):
        text = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        block = next(block for block in re.findall(r"```json\n(.*?)\n```", text, re.S) if '"proxy":' in block)
        example = json.loads("{" + block + "}")
        api = next(item for item in example["proxy"]["bindings"] if item["plane"] == "api")
        self.assertNotIn("models", api)
        self.assertNotIn("keyFile", api)
        for name in ("auth-proxy/README_ZH.md", "docs/customer-deployment-workflows-zh.md", "docs/litellm-content-audit-phase1-customer-brief-zh.md"):
            with self.subTest(document=name):
                self.assertIn("X-LiteLLM-API-Key", (ROOT / name).read_text())

    def test_customer_readme_local_links_exist(self):
        for name in READMES:
            document = ROOT / name
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                link = urlsplit(target)
                if link.scheme or link.netloc or not link.path:
                    continue
                with self.subTest(document=name, target=target):
                    self.assertTrue((document.parent / unquote(link.path)).exists())

    def test_root_readmes_point_to_customer_solution(self):
        for name in ("README.md", "README_ZH.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertTrue(text.startswith("# "))
                self.assertIn("LiteLLM on Azure", text.splitlines()[0])
                self.assertIn("docs/customer-migration-guide-zh.md", text)
                self.assertIn("CUSTOMER_CONFIG_JSON", text)
                self.assertIn("docs/litellm-code-completion-backlog-2026-09-07.md", text)

    def test_removed_gateway_has_no_entrypoint_or_sdk_dependency(self):
        for name in ("APIM/deploy_mi_apim.py", "APIM/azure-openai.json", "APIM/README.md", "APIM/README_ZH.md", "tests/test_apim_deploy.py"):
            with self.subTest(path=name):
                self.assertFalse((ROOT / name).exists())
        self.assertNotIn("azure-mgmt-apimanagement", (ROOT / "requirements.txt").read_text())

    def test_full_plan_covers_both_paths_and_runtime_acceptance(self):
        text = (ROOT / "docs/customer-deployment-workflows-zh.md").read_text()
        for number in range(1, 13):
            self.assertIn(f"| A{number:02d} ", text)
        for required in ("greenfield", "RTO/RPO", "Prisma", "OIDC", "Python", "Go", "MIGRATION_MANIFEST_YAML"):
            self.assertIn(required, text)