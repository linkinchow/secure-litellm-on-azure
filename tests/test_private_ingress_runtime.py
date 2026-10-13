import json
import copy
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import yaml

from scripts.customer_migration import ROOT, MigrationError, private_write, stage_fingerprint
from scripts.migration_deploy import group_id, resolve_origin
from scripts.migration_runtime import validate_action
from scripts.private_ingress_runtime import deploy_private_ingress, ingress_ownership_handoff, read_certificate, require_private_ingress_backends, require_private_registry_dns, scan_image, verify_private_ingress_backends
from scripts.private_ingress import ingress_image, native_api_rule, native_admin_rule, render_ingress
from tests.test_customer_migration import customer_config


def ingress_config():
    config = customer_config()
    config["privateIngress"] = {plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.30.0.0/16"]} for plane in ("api", "admin")}
    config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
    return config


class PrivateIngressRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = ingress_config()
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "temp")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("scripts.private_ingress_runtime.connect_cluster", return_value=["kubectl", "--namespace", "litellm"]))
        self.azure = Mock()
        self.azure.scoped.side_effect = self.cloud
        self.stack.enter_context(patch("scripts.private_ingress_runtime.AzureCommands", return_value=self.azure))
        self.command = self.stack.enter_context(patch("scripts.private_ingress_runtime.run_command", side_effect=self.commands))
        self.cert = self.stack.enter_context(patch("scripts.private_ingress_runtime.read_certificate", side_effect=self.certificate))
        self.promote = self.stack.enter_context(patch("scripts.private_ingress_runtime.promote_image"))
        self.verify = self.stack.enter_context(patch("scripts.private_ingress_runtime.verify_endpoint"))
        self.event_reason = "SyncLoadBalancerFailed"

    def certificate(self, config, secret_id, host):
        return {"certificate": "synthetic-public-chain", "key": "synthetic-private-material", "sha256": ("a" if "api" in host else "b") * 64, "expiresAt": "2099-01-01T00:00:00Z", "secretId": secret_id + "/" + "c" * 32}

    def commands(self, arguments, directory, label, **kwargs):
        if label.startswith("before-"):
            self.assertIn("--show-managed-fields", arguments)
            return ""
        if label.startswith("endpoint-slices-"):
            return json.dumps({"items": [{"metadata": {"name": "synthetic-endpoints"}, "addressType": "IPv4", "ports": [{"port": 8443}], "endpoints": [{"nodeName": "synthetic-node", "conditions": {"ready": True}}]}]})
        if label.startswith("service-events-"):
            return json.dumps({"items": [{"type": "Warning", "reason": self.event_reason, "count": 1, "message": "synthetic bounded diagnostic"}]})
        if label.startswith("service-"):
            return json.dumps({"status": {"loadBalancer": {"ingress": [{"ip": "10.30.4.10" if label.endswith("api") else "10.30.4.11"}]}}})
        return "verified"

    def cloud(self, arguments):
        if arguments[:2] == ["aks", "show"]:
            return {"id": group_id(self.config) + "/providers/Microsoft.ContainerService/managedClusters/new-aks", "nodeResourceGroup": "rg-nodes", "private": True}
        if arguments[:4] == ["network", "vnet", "subnet", "show"]:
            return {"prefix": "10.30.4.0/24", "plsPolicy": "Disabled"}
        if arguments[:3] == ["network", "lb", "list"]:
            subnet = group_id(self.config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet/subnets/snet-ingress"
            return [{"name": "kubernetes-internal", "sku": {"name": "Standard"}, "frontendIPConfigurations": [{"name": plane, "privateIPAddress": address, "subnet": {"id": subnet}} for plane, address in (("api", "10.30.4.10"), ("admin", "10.30.4.11"))]}]
        return {"properties": {"provisioningState": "Succeeded"}}

    def plan(self):
        return deploy_private_ingress(self.config, "plan", "a" * 40, self.path, "")

    def test_plan_contains_no_private_key_and_never_promotes_or_applies(self):
        result = self.plan()
        self.assertFalse(result["stageAccepted"])
        self.assertEqual(result["ingressObservation"]["api"]["readyEndpointCount"], 1)
        self.assertEqual(result["ingressObservation"]["api"]["eventReasons"], ["SyncLoadBalancerFailed"])
        review = (self.path / "runtime-review.json").read_text()
        self.assertIn("currentIngressObservationNotPlanBound", review)
        self.assertNotIn("synthetic-private-material", review)
        self.assertNotIn("synthetic-public-chain", review)
        self.promote.assert_not_called()
        self.verify.assert_not_called()
        trust_commands = [call.args[0] for call in self.command.call_args_list if call.args[2].startswith("certificate-trust-")]
        self.assertEqual(len(trust_commands), 2)
        for arguments in trust_commands:
            self.assertEqual(arguments[:3], ["openssl", "verify", "-CAfile"])
            self.assertEqual(Path(arguments[3]).name, "cacert.pem")
        for call in self.command.call_args_list:
            if "apply" in call.args[0]:
                self.assertIn("--dry-run=server", call.args[0])
        self.assertFalse(any("create" in call.args[0] for call in self.azure.scoped.call_args_list))

    def test_dynamic_observations_do_not_change_the_approved_plan_hash(self):
        first = self.plan()
        self.event_reason = "EnsuredLoadBalancer"
        second = self.plan()
        self.assertEqual(first["planSha256"], second["planSha256"])
        self.assertNotEqual(first["ingressObservation"], second["ingressObservation"])

    def test_bound_native_ingress_migrates_only_known_legacy_owners(self):
        self.config["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        fdid = "11111111-1111-4111-8111-111111111111"
        resources = {}
        for plane in ("api", "admin"):
            documents = render_ingress(self.config, plane, ingress_image(self.config)[0], ["10.30.0.0/16"])
            for document in documents:
                if document["kind"] not in {"ConfigMap", "Deployment"}:
                    continue
                current = copy.deepcopy(document)
                root = "f:data" if current["kind"] == "ConfigMap" else "f:spec"
                current["metadata"].update(uid=plane + current["kind"], resourceVersion="1",
                                           managedFields=[{"manager": "kubectl-patch", "operation": "Update",
                                                           "fieldsV1": {root: {}}, "time": "first"}])
                if current["kind"] == "ConfigMap":
                    routes = yaml.safe_load(current["data"]["routes.yaml"])
                    renderer = native_api_rule if plane == "api" else native_admin_rule
                    routes["http"]["routers"][plane]["rule"] = renderer("llm-" + plane + "." + self.config["baseDomain"], fdid)
                    current["data"]["routes.yaml"] = yaml.safe_dump(routes, sort_keys=False)
                else:
                    current["spec"]["template"]["metadata"]["annotations"] = {
                        "llmgw/front-door-id": fdid, "llmgw/routes-sha256": "old",
                    }
                resources[(plane, current["kind"])] = current

        def commands(arguments, directory, label, **kwargs):
            for (plane, kind), current in resources.items():
                if label == f"before-binding-{plane}-configmap" and kind == "ConfigMap" or label == f"before-{plane}-{kind}":
                    self.assertIn("--show-managed-fields", arguments)
                    return json.dumps(current)
            return self.commands(arguments, directory, label, **kwargs)

        self.command.side_effect = commands
        first = self.plan()
        for current in resources.values():
            current["metadata"]["resourceVersion"] = "2"
            current["metadata"]["managedFields"][0]["time"] = "second"
        second = self.plan()
        self.assertEqual(first["planSha256"], second["planSha256"])
        review = json.loads((self.path / "runtime-review.json").read_text())
        for plane in ("api", "admin"):
            self.assertEqual(len(first["legacyOwnershipHandoffs"][plane]), 2)
            ordinary = list(yaml.safe_load_all((self.path / f"ingress-{plane}.yaml").read_text()))
            self.assertFalse(any(item["kind"] in {"ConfigMap", "Deployment"} for item in ordinary))
            deployment = next(item for item in review["documents"][plane] if item["kind"] == "Deployment")
            annotations = deployment["spec"]["template"]["metadata"]["annotations"]
            self.assertEqual(annotations["llmgw/front-door-id"], fdid)
            self.assertEqual(annotations["llmgw/certificate-sha256"], ("a" if plane == "api" else "b") * 64)
            self.assertNotEqual(annotations["llmgw/routes-sha256"], "old")
        forced = [call.args[0] for call in self.command.call_args_list if "--force-conflicts" in call.args[0]]
        self.assertTrue(forced)
        self.assertTrue(all("--dry-run=server" in command for command in forced))
        self.promote.assert_not_called()
        self.command.reset_mock()
        with self.assertRaisesRegex(MigrationError, "not approved"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, "f" * 64)
        self.assertFalse(any("--force-conflicts" in call.args[0] and "--dry-run=server" not in call.args[0]
                             for call in self.command.call_args_list))
        self.promote.assert_not_called()
        self.command.reset_mock()
        deploy_private_ingress(self.config, "execute", "a" * 40, self.path, first["planSha256"])
        forced = [call.args[0] for call in self.command.call_args_list if "--force-conflicts" in call.args[0] and "--dry-run=server" not in call.args[0]]
        self.assertEqual(len(forced), 4)
        for command in forced:
            payload = yaml.safe_load(Path(command[command.index("-f") + 1]).read_text())
            self.assertIn(payload["kind"], {"ConfigMap", "Deployment"})
            self.assertEqual(payload["metadata"]["resourceVersion"], "2")
            self.assertTrue(payload["metadata"]["uid"])

    def test_unknown_owner_blocks_handoff_before_any_force(self):
        document = {"kind": "Deployment", "metadata": {"labels": {"app.kubernetes.io/managed-by": "llmgw-workflow"}}}
        current = {"metadata": {"uid": "synthetic", "resourceVersion": "1",
                               "labels": {"app.kubernetes.io/managed-by": "llmgw-workflow"},
                               "managedFields": [{"manager": "kubectl-patch", "fieldsV1": {"f:spec": {}}},
                                                 {"manager": "external-operator", "fieldsV1": {"f:spec": {}}}]}}
        with self.assertRaisesRegex(MigrationError, "another manager"):
            ingress_ownership_handoff(document, current, True)
        self.assertFalse(ingress_ownership_handoff(document, current, False))
        current["metadata"]["managedFields"].pop()
        self.assertTrue(ingress_ownership_handoff(document, current, True))
        current["metadata"]["labels"] = {}
        with self.assertRaisesRegex(MigrationError, "workflow-managed"):
            ingress_ownership_handoff(document, current, True)

    def test_missing_managed_fields_are_not_treated_as_no_legacy_owner(self):
        document = {"kind": "ConfigMap", "metadata": {"labels": {}}}
        for value in (None, [], "invalid"):
            current = {"metadata": {"managedFields": value}}
            with self.subTest(value=value), self.assertRaisesRegex(MigrationError, "--show-managed-fields"):
                ingress_ownership_handoff(document, current, True)


    def test_unapproved_or_changed_certificate_never_deploys(self):
        result = self.plan()
        with self.assertRaisesRegex(ValueError, "plan changed"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, "f" * 64)
        original = self.certificate(self.config, self.config["privateIngress"]["api"]["tlsSecretId"], "llm-api.customer.invalid")
        self.cert.side_effect = None
        self.cert.return_value = {**original, "sha256": "d" * 64}
        with self.assertRaisesRegex(ValueError, "plan changed"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, result["planSha256"])
        self.promote.assert_not_called()

    def test_execute_verifies_both_planes_then_records_outputs(self):
        result = self.plan()
        outcome = deploy_private_ingress(self.config, "execute", "a" * 40, self.path, result["planSha256"])
        self.assertTrue(outcome["verified"])
        self.assertFalse(outcome["stageAccepted"])
        self.assertEqual(self.verify.call_count, 2)
        self.promote.assert_called_once()
        receipt = json.loads((self.path / "ingress-receipt-template.json").read_text())["outputs"]["privateIngress"]["value"]
        self.assertEqual(receipt["api"]["frontendName"], "api")
        self.assertEqual(receipt["admin"]["frontendName"], "admin")
        self.assertEqual(receipt["authenticationMode"], "entra")
        self.assertFalse(receipt["backendRoutesVerified"])
        self.assertTrue(all(call.kwargs.get("native") is None for call in self.verify.call_args_list))
        self.assertNotIn("synthetic-private-material", json.dumps(receipt))
        self.assertFalse((self.path / "ingress-tls.json").exists())

    def test_native_backend_routes_are_verified_after_stage6_and_bound_to_stage4(self):
        self.config["application"] = {"backendImage": "synthetic.azurecr.io/litellm@sha256:" + "a" * 64, "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "model", "id": "primary-coding", "apiVersion": "v1"}], "authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        ingress = {
            "revision": "a" * 40,
            "configSha256": stage_fingerprint(self.config, 4),
            "authenticationMode": "native",
            "backendRoutesVerified": False,
            "certificates": {"api": {"sha256": "a" * 64}, "admin": {"sha256": "b" * 64}},
            "api": {"privateIpAddress": "10.30.4.10"},
            "admin": {"privateIpAddress": "10.30.4.11"},
        }
        stage4 = {"state": "Succeeded", "ingress": ingress}
        self.azure.scoped.side_effect = [stage4, {"properties": {"provisioningState": "Succeeded"}}]
        result = verify_private_ingress_backends(self.config, "a" * 40, self.path, self.azure)
        self.assertTrue(result["backendRoutesVerified"])
        self.assertEqual(self.verify.call_count, 2)
        self.assertTrue(all(call.kwargs["native"] is True for call in self.verify.call_args_list))
        self.azure.scoped.side_effect = [stage4, {"state": "Succeeded", "verification": result}]
        require_private_ingress_backends(self.config, self.azure)
        result["revision"] = "b" * 40
        self.azure.scoped.side_effect = [stage4, {"state": "Succeeded", "verification": result}]
        require_private_ingress_backends(self.config, self.azure)
        self.assertEqual(self.verify.call_count, 6)
        self.azure.scoped.side_effect = [stage4, {"state": "Succeeded", "verification": result}]
        identifier = "11111111-1111-4111-8111-111111111111"
        require_private_ingress_backends(self.config, self.azure, front_door_id=identifier)
        self.assertTrue(all(call.kwargs["front_door_id"] == identifier
                            for call in self.verify.call_args_list[-2:]))
        result["revision"] = "invalid"
        self.azure.scoped.side_effect = [stage4, {"state": "Succeeded", "verification": result}]
        with self.assertRaisesRegex(ValueError, "Verify the current native private ingress"):
            require_private_ingress_backends(self.config, self.azure)
        result["revision"] = "b" * 40
        stale = {**ingress, "api": {"privateIpAddress": "10.30.4.12"}}
        self.verify.side_effect = ValueError("current ingress route verification failed")
        self.azure.scoped.side_effect = [{"state": "Succeeded", "ingress": stale}, {"state": "Succeeded", "verification": result}]
        with self.assertRaisesRegex(ValueError, "current ingress route verification failed"):
            require_private_ingress_backends(self.config, self.azure)

    def test_failed_tls_verification_never_records_success(self):
        result = self.plan()
        self.verify.side_effect = ValueError("TLS mismatch")
        with self.assertRaisesRegex(ValueError, "TLS mismatch"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, result["planSha256"])
        self.assertFalse((self.path / "ingress-receipt-template.json").exists())
        self.assertFalse(any("create" in call.args[0] for call in self.azure.scoped.call_args_list))

    def test_stage9_resolves_both_verified_private_frontends(self):
        automatic = {"resourceGroupName": "auto", "name": "auto", "frontendName": "auto"}
        self.config["parameters"]["origin"] = {"virtualNetworkName": "target-vnet", "ingressSubnetName": "snet-ingress", "apiLoadBalancer": automatic.copy(), "adminLoadBalancer": automatic.copy()}
        ingress = {"configSha256": stage_fingerprint(self.config, 4), "api": {"resourceGroupName": "rg-nodes", "name": "kubernetes-internal", "frontendName": "api", "privateIpAddress": "10.30.4.10"}, "admin": {"resourceGroupName": "rg-nodes", "name": "kubernetes-internal", "frontendName": "admin", "privateIpAddress": "10.30.4.11"}}

        def resolve(arguments):
            if arguments[:2] == ["aks", "show"]:
                return "rg-nodes"
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "ingress": ingress}
            return self.cloud(arguments)

        self.azure.scoped.side_effect = resolve
        resolved = resolve_origin(self.config, "origin", self.azure)
        self.assertEqual(resolved["parameters"]["origin"]["apiLoadBalancer"]["frontendName"], "api")
        self.assertEqual(resolved["parameters"]["origin"]["adminLoadBalancer"]["frontendName"], "admin")
        self.assertEqual(self.config["parameters"]["origin"]["apiLoadBalancer"]["name"], "auto")
        ingress["api"]["privateIpAddress"] = "10.30.4.12"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            resolve_origin(self.config, "origin", self.azure)
        ingress["configSha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "Stage 4 configuration"):
            resolve_origin(self.config, "origin", self.azure)
        ingress["configSha256"] = stage_fingerprint(self.config, 4)
        self.config["application"] = {"backendImage": "synthetic.azurecr.io/litellm@sha256:" + "a" * 64, "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "model", "id": "primary-coding", "apiVersion": "v1"}], "authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        ingress["configSha256"] = stage_fingerprint(self.config, 4)
        with self.assertRaisesRegex(ValueError, "authentication mode"):
            resolve_origin(self.config, "origin", self.azure)

    def test_private_ingress_only_runs_at_stage4_in_both_modes(self):
        validate_action(self.config, 4, "private-ingress")
        self.config["deploymentMode"] = "greenfield"
        self.config.pop("legacy")
        validate_action(self.config, 4, "private-ingress")
        with self.assertRaises(ValueError):
            validate_action(self.config, 3, "private-ingress")


@unittest.skipUnless(shutil.which("kubectl"), "kubectl required for real client serialization regression")
class KubectlOwnershipOutputTests(unittest.TestCase):
    def test_real_kubectl_hides_owners_unless_explicitly_requested(self):
        fixture = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "llm-api-ingress", "namespace": "llm-api-ingress",
                         "uid": "synthetic-uid", "resourceVersion": "1",
                         "labels": {"app.kubernetes.io/managed-by": "llmgw-workflow"},
                         "managedFields": [{"manager": "kubectl-patch", "operation": "Update",
                                            "apiVersion": "v1", "fieldsType": "FieldsV1",
                                            "fieldsV1": {"f:data": {"f:routes.yaml": {}}}}]},
            "data": {"routes.yaml": "synthetic-route"},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                responses = {
                    "/api": {"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"],
                             "serverAddressByClientCIDRs": []},
                    "/apis": {"kind": "APIGroupList", "apiVersion": "v1", "groups": []},
                    "/api/v1": {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "v1",
                                "resources": [{"name": "configmaps", "singularName": "configmap",
                                               "namespaced": True, "kind": "ConfigMap", "verbs": ["get"]}]},
                    "/api/v1/namespaces/llm-api-ingress/configmaps/llm-api-ingress": fixture,
                }
                if path not in responses:
                    self.send_error(404)
                    return
                body = json.dumps(responses[path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_arguments):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as name:
                private_write(Path(name) / "unused-kubeconfig",
                              yaml.safe_dump({"apiVersion": "v1", "kind": "Config",
                                              "clusters": [], "contexts": [], "users": [], "current-context": ""}))
                command = ["kubectl", "--server", f"http://127.0.0.1:{server.server_port}",
                           "--kubeconfig", str(Path(name) / "unused-kubeconfig"),
                           "--cache-dir", str(Path(name) / "cache"), "--request-timeout=5s",
                           "--namespace", "llm-api-ingress", "get", "configmap", "llm-api-ingress", "-o", "json"]
                def read(*options):
                    result = subprocess.run([*command, *options], capture_output=True, text=True,
                                            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost"},
                                            timeout=20, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)
                hidden = read()
                visible = read("--show-managed-fields")
                self.assertNotIn("managedFields", hidden["metadata"])
                self.assertEqual(visible["metadata"]["managedFields"], fixture["metadata"]["managedFields"])
                self.assertTrue(ingress_ownership_handoff(fixture, visible, True))
                with self.assertRaisesRegex(MigrationError, "ownership metadata is missing"):
                    ingress_ownership_handoff(fixture, hidden, True)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


class CertificateReadTests(unittest.TestCase):
    def test_read_binds_version_and_suppresses_secret_parser_errors(self):
        config = ingress_config()
        secret_id = config["privateIngress"]["api"]["tlsSecretId"]
        secret = {"id": secret_id + "/" + "a" * 32, "attributes": {"enabled": True}, "value": "synthetic-sensitive-value"}
        with patch("scripts.private_ingress_runtime.subprocess.run") as command, patch("scripts.private_ingress_runtime.certificate_material") as parse:
            command.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(secret))
            parse.return_value = {"sha256": "b" * 64}
            result = read_certificate(config, secret_id, "llm-api.customer.invalid")
            self.assertEqual(result["secretId"], secret["id"])
            self.assertNotIn(secret["value"], str(command.call_args))
            with self.assertRaisesRegex(ValueError, "version differs"):
                read_certificate(config, secret_id + "/" + "c" * 32, "llm-api.customer.invalid")
            parse.side_effect = ValueError(secret["value"])
            with self.assertRaisesRegex(ValueError, "TLS PEM failed") as caught:
                read_certificate(config, secret_id, "llm-api.customer.invalid")
            self.assertNotIn(secret["value"], str(caught.exception))


class IngressImageScanTests(unittest.TestCase):
    def test_registry_dns_requires_only_rfc1918_addresses(self):
        def resolve(address):
            return lambda *_args, **_kwargs: [(2, 1, 6, "", (address, 443))]

        require_private_registry_dns("syntheticregistry", resolve("10.30.8.9"))
        with self.assertRaisesRegex(MigrationError, "deploy runner-target-connectivity"):
            require_private_registry_dns("syntheticregistry", resolve("20.168.162.130"))

    def test_critical_findings_report_versions_without_raw_scanner_output(self):
        report = {"Results": [{"Target": "usr/local/bin/traefik", "Vulnerabilities": [{"VulnerabilityID": "CVE-2026-88007", "PkgName": "github.com/traefik/traefik/v3", "InstalledVersion": "v3.7.12", "FixedVersion": "3.7.13", "Severity": "CRITICAL"}]}]}
        result = SimpleNamespace(returncode=1, stdout=json.dumps(report), stderr="PRIVATE_SCANNER_PROGRESS")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            path = Path(directory)
            with self.assertRaisesRegex(MigrationError, r"vulnerability=CVE-2026-88007 package=github.com/traefik/traefik/v3 installed=v3.7.12 fixed=3.7.13") as raised:
                scan_image("docker.io/library/traefik@sha256:" + "a" * 64, path, Mock(return_value=result))
            self.assertNotIn("PRIVATE_SCANNER_PROGRESS", str(raised.exception))
            self.assertEqual(json.loads((path / "ingress-image-scan.json").read_text()), report)

    def test_secret_finding_is_not_ignored_or_exposed(self):
        report = {"Results": [{"Target": "image", "Secrets": [{"RuleID": "aws-access-key-id", "Category": "AWS", "Match": "PRIVATE_SECRET_VALUE"}]}]}
        result = SimpleNamespace(returncode=1, stdout=json.dumps(report), stderr="")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            with self.assertRaisesRegex(MigrationError, r"secret=aws-access-key-id category=AWS") as raised:
                scan_image("docker.io/library/traefik@sha256:" + "a" * 64, Path(directory), Mock(return_value=result))
            self.assertNotIn("PRIVATE_SECRET_VALUE", str(raised.exception))

    def test_clean_scan_uses_quiet_json_policy(self):
        command = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"Results": []}), stderr=""))
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            scan_image("docker.io/library/traefik@sha256:" + "a" * 64, Path(directory), command)
        arguments = command.call_args.args[0]
        self.assertIn("--quiet", arguments)
        self.assertEqual(arguments[arguments.index("--exit-code") + 1], "1")
        self.assertEqual(arguments[arguments.index("--severity") + 1], "CRITICAL")
        self.assertEqual(arguments[arguments.index("--format") + 1], "json")