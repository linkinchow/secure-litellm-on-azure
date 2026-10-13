import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

from scripts.admin_credentials import OIDC_SECRET, SESSION_SECRET
from scripts.migration_deploy import group_id
from scripts.proxy_credentials import binding_contract, credential_bindings
from scripts.proxy_manifest import prepare_proxy_documents, render_proxy_documents
from scripts.backend_manifest import render_backend_manifest
from scripts.migration_runtime import check_application, publish
from scripts.customer_migration import ROOT, stage_fingerprint
from scripts.runtime_secrets import BACKEND_SECRETS
from scripts.private_ingress import NATIVE_API_PATHS, render_ingress
from tests.test_proxy_config import APPS, proxy_customer


def edge_deployment_output(config, identifier, profile):
    configured = config["parameters"]["edge"]
    origins = {}
    for plane, parameter in (("api", "privateOrigin"), ("admin", "adminPrivateOrigin")):
        origin = copy.deepcopy(configured[parameter])
        if origin["privateLinkServiceId"] == "auto":
            origin["privateLinkServiceId"] = group_id(config) + "/providers/Microsoft.Network/privateLinkServices/" + plane
        origins[parameter] = origin
    return {"provisioned": True, "apiTrafficEnabled": False, "adminTrafficEnabled": False, "apiHost": "llm-api." + config["baseDomain"], "adminHost": "llm-admin." + config["baseDomain"], "endpointHost": "api.azurefd.net", "adminEndpointHost": "admin.azurefd.net", "routeId": profile + "/afdEndpoints/api/routes/api", "adminRouteId": profile + "/afdEndpoints/admin/routes/admin", "adminWafId": group_id(config) + "/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/admin", "adminAccessMode": "SourceIpAllowlistAndNativeLogin", "privateOrigin": origins["privateOrigin"], "adminPrivateOrigin": origins["adminPrivateOrigin"], "adminAllowedCidrs": copy.deepcopy(configured["adminAllowedCidrs"]), "adminRateLimitPerMinute": configured.get("adminRateLimitPerMinute", 120), "profileId": identifier}


def live_edge_resource(config, identifier, profile, arguments, connection_status="Approved", edge_output=None):
    if arguments[0] != "resource":
        return None
    resource_id = arguments[arguments.index("--ids") + 1]
    if resource_id == profile:
        return {"id": profile, "properties": {"frontDoorId": identifier}}
    edge = edge_output or edge_deployment_output(config, identifier, profile)
    for plane, route_key, traffic_key in (("api", "routeId", "apiTrafficEnabled"), ("admin", "adminRouteId", "adminTrafficEnabled")):
        if resource_id == edge[route_key]:
            patterns = list(NATIVE_API_PATHS) if plane == "api" else ["/*"]
            return {"id": resource_id, "properties": {"provisioningState": "Succeeded", "deploymentStatus": "Succeeded" if edge[traffic_key] else "NotStarted", "enabledState": "Enabled" if edge[traffic_key] else "Disabled", "supportedProtocols": ["Https"], "forwardingProtocol": "HttpsOnly", "httpsRedirect": "Enabled", "linkToDefaultDomain": "Disabled", "patternsToMatch": patterns, "ruleSets": [], "customDomains": [{"id": profile + f"/customDomains/llm-{plane}"}], "originGroup": {"id": profile + f"/originGroups/private-{plane}"}}}
    if resource_id == edge["routeId"].rsplit("/routes/", 1)[0]:
        return {"id": resource_id, "properties": {"provisioningState": "Succeeded", "enabledState": "Disabled"}}
    if resource_id == edge["adminRouteId"].rsplit("/routes/", 1)[0]:
        return {"id": resource_id, "properties": {"provisioningState": "Succeeded", "enabledState": "Disabled"}}
    for plane, parameter in (("api", "privateOrigin"), ("admin", "adminPrivateOrigin")):
        origin_id = profile + f"/originGroups/private-{plane}/origins/private-{plane}"
        if resource_id == origin_id:
            host = f"llm-{plane}." + config["baseDomain"]
            origin = edge[parameter]
            return {"id": resource_id, "properties": {"provisioningState": "Succeeded", "enabledState": "Enabled", "hostName": host, "originHostHeader": host, "enforceCertificateNameCheck": True, "sharedPrivateLinkResource": {"privateLink": {"id": origin["privateLinkServiceId"]}, "privateLinkLocation": origin["privateLinkLocation"], "status": "Approved" if edge[f"{plane}TrafficEnabled"] else None}}}
        if resource_id == edge[parameter]["privateLinkServiceId"]:
            return {"id": resource_id, "properties": {"autoApproval": {"subscriptions": []}, "privateEndpointConnections": [{"properties": {"privateEndpoint": {"id": "/synthetic/front-door-private-endpoint/" + plane}, "privateLinkServiceConnectionState": {"status": connection_status}}}]}}
    for plane in ("api", "admin"):
        if resource_id == profile + f"/customDomains/llm-{plane}":
            return {"id": resource_id, "properties": {"provisioningState": "Succeeded", "deploymentStatus": "Succeeded", "domainValidationState": "Approved", "hostName": f"llm-{plane}." + config["baseDomain"], "tlsSettings": {"certificateType": "ManagedCertificate", "minimumTlsVersion": "TLS12"}}}
    if resource_id == edge["adminWafId"]:
        return {"id": resource_id, "properties": {"provisioningState": "Succeeded", "policySettings": {"enabledState": "Enabled", "mode": "Prevention", "requestBodyCheck": "Enabled", "logScrubbing": {"state": "Enabled"}}, "managedRules": {"managedRuleSets": [{"ruleSetType": "Microsoft_DefaultRuleSet", "ruleSetVersion": "2.1", "ruleSetAction": "Block"}, {"ruleSetType": "Microsoft_BotManagerRuleSet", "ruleSetVersion": "1.1", "ruleSetAction": "Block"}]}, "customRules": {"rules": [
            {"name": "BlockUnapprovedAdminSources", "priority": 5, "enabledState": "Enabled", "ruleType": "MatchRule", "action": "Block", "matchConditions": [{"matchVariable": "SocketAddr", "operator": "IPMatch", "negateCondition": True, "matchValue": edge["adminAllowedCidrs"]}]},
            {"name": "BlockUnsafeMethods", "priority": 10, "enabledState": "Enabled", "ruleType": "MatchRule", "action": "Block", "matchConditions": [{"matchVariable": "RequestMethod", "operator": "Equal", "negateCondition": False, "matchValue": ["TRACE", "TRACK"]}]},
            {"name": "RateLimitAdmin", "priority": 20, "enabledState": "Enabled", "ruleType": "RateLimitRule", "rateLimitDurationInMinutes": 1, "rateLimitThreshold": edge["adminRateLimitPerMinute"], "action": "Block", "matchConditions": [{"matchVariable": "RequestUri", "operator": "BeginsWith", "negateCondition": False, "matchValue": ["/"]}]},
        ]}}}
    raise AssertionError("Unexpected live edge resource: " + resource_id)


class ProxyManifestTests(unittest.TestCase):
    def test_native_admin_ingress_binding_applies_only_to_business_route(self):
        from scripts.edge_binding import bind_native_ingress, native_ingress_document_state
        config = proxy_customer()
        config.pop("proxy")
        config.pop("entra", None)
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
        config["privateIngress"] = {plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.30.0.0/16"]} for plane in ("api", "admin")}
        documents = render_ingress(config, "admin", "registry.invalid/traefik@sha256:" + "a" * 64, ["10.30.0.0/16"])
        resources = {item["kind"].lower(): item for item in documents}
        for item in resources.values():
            item.setdefault("metadata", {}).setdefault("uid", item["metadata"]["name"] + "-uid")
        client = Mock()
        client.get.side_effect = lambda kind, name: copy.deepcopy(resources[kind])
        identifier = "11111111-1111-4111-8111-111111111111"
        _current_config, _current_deployment, desired_config, desired_deployment, _current, desired = bind_native_ingress(config, client, identifier, plane="admin")
        dynamic = yaml.safe_load(desired_config["data"]["routes.yaml"])
        self.assertIn(identifier, dynamic["http"]["routers"]["admin"]["rule"])
        self.assertNotIn("X-Azure-FDID", dynamic["http"]["routers"]["admin-health"]["rule"])
        self.assertEqual(desired["frontDoorHeaderBound"], identifier)
        native_ingress_document_state(config, desired_config, desired_deployment, resources["service"], identifier, plane="admin")

    def test_native_edge_binding_verifies_both_private_ingresses_without_proxy(self):
        from scripts.edge_binding import bind_edge, native_ingress_document_state, require_edge_binding
        config = proxy_customer()
        config.pop("proxy")
        config.pop("entra", None)
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
        config["parameters"]["edge"]["privateOrigin"]["privateLinkServiceId"] = "auto"
        config["parameters"]["edge"]["adminPrivateOrigin"]["privateLinkServiceId"] = "auto"
        config["privateIngress"] = {plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.30.0.0/16"]} for plane in ("api", "admin")}
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge_output = edge_deployment_output(config, identifier, profile)
        resources, clients = {}, {}
        for plane in ("api", "admin"):
            documents = render_ingress(config, plane, "registry.invalid/traefik@sha256:" + "a" * 64, ["10.30.0.0/16"])
            resources[plane] = {item["kind"].lower(): item for item in documents}
            for item in resources[plane].values():
                item.setdefault("metadata", {}).setdefault("uid", item["metadata"]["name"] + "-uid")
            resources[plane]["deployment"]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["defaultMode"] = 420
            clients[plane] = Mock()
            clients[plane].get.side_effect = lambda kind, name, selected=plane: copy.deepcopy(resources[selected][kind])
            def change(kind, name, operations, selected=plane):
                self.assertEqual(name, f"llm-{selected}-ingress")
                self.assertEqual(operations[0]["value"], resources[selected][kind]["metadata"]["uid"])
                for operation in operations:
                    parts = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].split("/")[1:]]
                    target = resources[selected][kind]
                    for part in parts[:-1]:
                        target = target[part]
                    if operation["op"] == "test":
                        self.assertEqual(target[parts[-1]], operation["value"])
                    else:
                        self.assertNotIn(operation["path"], {"/spec", "/data"})
                        target[parts[-1]] = copy.deepcopy(operation["value"])
            clients[plane].patch.side_effect = change
        azure = Mock()
        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "edge": edge_output}
            resource = live_edge_resource(config, identifier, profile, arguments, edge_output=edge_output)
            if resource is not None:
                return resource
            return {"properties": {"provisioningState": "Succeeded"}}
        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            plan = bind_edge(config, "plan", "a" * 40, directory, "", azure, clients)
            self.assertEqual(plan["bindingMode"], "native-private-ingress")
            result = bind_edge(config, "execute", "a" * 40, directory, plan["planSha256"], azure, clients)
            self.assertTrue(result["applied"])
            receipt = json.loads((directory / "edge-binding-receipt.json").read_text())["outputs"]["edgeBinding"]["value"]
            self.assertEqual(receipt["frontDoorId"], identifier)
            self.assertEqual(receipt["bindingMode"], "native-private-ingress")
            self.assertEqual(set(receipt["planes"]), {"api", "admin"})
            for plane in ("api", "admin"):
                self.assertEqual(clients[plane].patch.call_count, 2)
                dynamic = yaml.safe_load(resources[plane]["configmap"]["data"]["routes.yaml"])
                self.assertIn(f"HeaderRegexp(`X-Azure-FDID`, `(?i)^{identifier}$`)", dynamic["http"]["routers"][plane]["rule"])
                self.assertNotIn("X-Azure-FDID", dynamic["http"]["routers"][f"{plane}-health"]["rule"])
            def release_cloud(arguments):
                if arguments[:3] == ["deployment", "group", "show"] and arguments[arguments.index("--name") + 1].endswith("edge-bind"):
                    return {"state": "Succeeded", "binding": receipt}
                return cloud(arguments)
            azure.scoped.side_effect = release_cloud
            require_edge_binding(config, "a" * 40, identifier, azure, clients)
            resources["admin"]["configmap"]["data"]["routes.yaml"] = resources["admin"]["configmap"]["data"]["routes.yaml"].replace(identifier, "22222222-2222-4222-8222-222222222222")
            with self.assertRaisesRegex(ValueError, "another or no Front Door|differs from"):
                require_edge_binding(config, "a" * 40, identifier, azure, clients)
            resources["admin"]["configmap"]["data"]["routes.yaml"] = resources["admin"]["configmap"]["data"]["routes.yaml"].replace("22222222-2222-4222-8222-222222222222", identifier)
            changed_edge = copy.deepcopy(edge_output)
            changed_edge["adminPrivateOrigin"]["privateLinkServiceId"] = group_id(config) + "/providers/Microsoft.Network/privateLinkServices/replaced-admin"
            def changed_cloud(arguments):
                if arguments[:3] == ["deployment", "group", "show"]:
                    name = arguments[arguments.index("--name") + 1]
                    return {"state": "Succeeded", "binding": receipt} if name.endswith("edge-bind") else {"state": "Succeeded", "edge": changed_edge}
                return live_edge_resource(config, identifier, profile, arguments, edge_output=changed_edge)
            azure.scoped.side_effect = changed_cloud
            with self.assertRaisesRegex(ValueError, "receipt differs"):
                require_edge_binding(config, "a" * 40, identifier, azure, clients)
            broadened = copy.deepcopy(resources["api"]["configmap"])
            broadened_dynamic = yaml.safe_load(broadened["data"]["routes.yaml"])
            broadened_dynamic["http"]["routers"]["management-bypass"] = copy.deepcopy(broadened_dynamic["http"]["routers"]["api"])
            broadened["data"]["routes.yaml"] = yaml.safe_dump(broadened_dynamic, sort_keys=False)
            with self.assertRaisesRegex(ValueError, "unexpected router"):
                native_ingress_document_state(config, broadened, resources["api"]["deployment"], resources["api"]["service"])
            wrong_service = copy.deepcopy(resources["api"]["service"])
            wrong_service["spec"]["selector"]["plane"] = "admin"
            with self.assertRaisesRegex(ValueError, "private routing contract"):
                native_ingress_document_state(config, resources["api"]["configmap"], resources["api"]["deployment"], wrong_service)
            wrong_volume = copy.deepcopy(resources["api"]["deployment"])
            wrong_volume["spec"]["template"]["spec"]["volumes"][0]["configMap"]["defaultMode"] = 511
            with self.assertRaisesRegex(ValueError, "reviewed route ConfigMap"):
                native_ingress_document_state(config, resources["api"]["configmap"], wrong_volume, resources["api"]["service"])

    def test_front_door_binding_survives_generated_application_publish(self):
        from scripts.edge_binding import bind_deployment, preserve_binding
        identifier = "11111111-1111-4111-8111-111111111111"
        api = {"kind": "Deployment", "metadata": {"name": "llm-api-proxy"}, "spec": {"template": {"spec": {"containers": [{"name": "auth-proxy", "env": [{"name": "PROXY_PLANE", "value": "api"}]}]}}}}
        admin = copy.deepcopy(api)
        admin["metadata"]["name"] = "llm-admin-proxy"
        live = bind_deployment(api, identifier)
        result = preserve_binding([api, admin], live)
        self.assertEqual(result[0], live)
        self.assertEqual(result[1], admin)
        self.assertNotIn("FRONT_DOOR_ID", json.dumps(api))
        with self.assertRaisesRegex(ValueError, "another"):
            bind_deployment(live, "22222222-2222-4222-8222-222222222222")

    def test_edge_binding_plans_then_applies_both_proxies_and_saves_receipt(self):
        from scripts.edge_binding import bind_edge
        config = proxy_customer()
        config["proxy"]["image"] = "customerregistry.azurecr.io/auth-proxy@sha256:" + "a" * 64
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge_output = edge_deployment_output(config, identifier, profile)
        azure, client = Mock(), Mock()
        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "edge": edge_output}
            resource = live_edge_resource(config, identifier, profile, arguments, edge_output=edge_output)
            if resource is not None:
                return resource
            return {"properties": {"provisioningState": "Succeeded"}}
        azure.scoped.side_effect = cloud
        api = {"metadata": {"name": "llm-api-proxy", "uid": "api-original"}, "spec": {"template": {"spec": {"serviceAccountName": "llm-api-proxy", "containers": [{"name": "auth-proxy", "image": config["proxy"]["image"]}]}}}}
        admin = copy.deepcopy(api)
        admin["metadata"]["name"] = "llm-admin-proxy"
        admin["metadata"]["uid"] = "admin-original"
        admin["spec"]["template"]["spec"]["serviceAccountName"] = "llm-admin-proxy"
        workloads = {"api": api, "admin": admin}
        client.get.side_effect = lambda kind, name: copy.deepcopy(workloads["api" if name == "llm-api-proxy" else "admin"])
        def change(kind, name, operations):
            plane = "api" if name == "llm-api-proxy" else "admin"
            self.assertEqual(operations[0]["value"], workloads[plane]["metadata"]["uid"])
            self.assertEqual(operations[1]["value"], workloads[plane]["spec"])
            workloads[plane]["spec"] = copy.deepcopy(operations[2]["value"])
        client.patch.side_effect = change
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            plan = bind_edge(config, "plan", "a" * 40, directory, "", azure, client)
            client.patch.assert_not_called()
            with self.assertRaisesRegex(ValueError, "not approved"):
                bind_edge(config, "execute", "a" * 40, directory, "f" * 64, azure, client)
            result = bind_edge(config, "execute", "a" * 40, directory, plan["planSha256"], azure, client)
            self.assertTrue(result["applied"])
            self.assertFalse(result["trafficVerified"])
            receipt = json.loads((directory / "edge-binding-receipt.json").read_text())["outputs"]["edgeBinding"]["value"]
            self.assertEqual(receipt["frontDoorId"], identifier)
            self.assertEqual(set(receipt["planes"]), {"api", "admin"})
            self.assertEqual(client.patch.call_count, 2)
            for workload in workloads.values():
                self.assertIn("FRONT_DOOR_ID", json.dumps(workload))
            azure.scoped.side_effect = lambda arguments: {"state": "Succeeded", "edge": {"provisioned": True, "apiHost": "other.invalid"}}
            with self.assertRaisesRegex(ValueError, "domains|matching"):
                bind_edge(config, "plan", "a" * 40, directory, "", azure, client)

    def test_edge_binding_rejects_pending_private_link_connection(self):
        from scripts.edge_binding import deployed_edge
        config = proxy_customer()
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge = edge_deployment_output(config, identifier, profile)
        azure = Mock()
        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "edge": edge}
            return live_edge_resource(config, identifier, profile, arguments, connection_status="Pending", edge_output=edge)
        azure.scoped.side_effect = cloud
        with self.assertRaisesRegex(ValueError, "exactly one approved"):
            deployed_edge(config, azure)

    def test_deployed_edge_live_checks_enabled_route_with_stale_status(self):
        from scripts.edge_binding import validate_private_edge_resources
        config = proxy_customer()
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge = edge_deployment_output(config, identifier, profile)
        edge.update(apiTrafficEnabled=True, adminTrafficEnabled=True)
        azure = Mock()
        def cloud(arguments):
            resource = live_edge_resource(config, identifier, profile, arguments, edge_output=edge)
            if resource["id"] in {edge["routeId"], edge["adminRouteId"]}:
                resource["properties"]["deploymentStatus"] = "NotStarted"
            return resource
        azure.scoped.side_effect = cloud
        probe = Mock()
        validate_private_edge_resources(config, azure, profile, {"api": edge["privateOrigin"], "admin": edge["adminPrivateOrigin"]}, edge, route_probe=probe)
        self.assertEqual(probe.call_args_list, [
            unittest.mock.call("llm-api." + config["baseDomain"], edge["endpointHost"], "api"),
            unittest.mock.call("llm-admin." + config["baseDomain"], edge["adminEndpointHost"], "admin"),
        ])
        probe.side_effect = ValueError("public route probe failed")
        with self.assertRaisesRegex(ValueError, "public route probe failed"):
            validate_private_edge_resources(config, azure, profile, {"api": edge["privateOrigin"], "admin": edge["adminPrivateOrigin"]}, edge, route_probe=probe)

    def test_null_origin_status_requires_pls_approval_and_public_probe(self):
        from scripts.edge_binding import validate_private_edge_resources
        config = proxy_customer()
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge = edge_deployment_output(config, identifier, profile)
        edge.update(apiTrafficEnabled=True, adminTrafficEnabled=True)
        azure = Mock()
        probe = Mock()
        shared_status = None
        connection_status = "Approved"
        extra_connection = False

        def cloud(arguments):
            resource = live_edge_resource(config, identifier, profile, arguments,
                                          connection_status=connection_status, edge_output=edge)
            properties = resource["properties"]
            if "sharedPrivateLinkResource" in properties:
                properties["sharedPrivateLinkResource"]["status"] = shared_status
            if extra_connection and "privateEndpointConnections" in properties:
                properties["privateEndpointConnections"].append(copy.deepcopy(
                    properties["privateEndpointConnections"][0]))
            return resource

        azure.scoped.side_effect = cloud
        origins = {"api": edge["privateOrigin"], "admin": edge["adminPrivateOrigin"]}
        validate_private_edge_resources(config, azure, profile, origins, edge, route_probe=probe)
        self.assertEqual(probe.call_args_list, [
            unittest.mock.call("llm-api." + config["baseDomain"], edge["endpointHost"], "api"),
            unittest.mock.call("llm-admin." + config["baseDomain"], edge["adminEndpointHost"], "admin"),
        ])
        probe.side_effect = ValueError("HTTPS verification failed")
        with self.assertRaisesRegex(ValueError, "HTTPS verification failed"):
            validate_private_edge_resources(config, azure, profile, origins, edge, route_probe=probe)
        probe.side_effect = None
        for connection_status in ("Pending", "Rejected", "Disconnected"):
            probe.reset_mock()
            with self.subTest(pls=connection_status), self.assertRaisesRegex(ValueError, "exactly one approved"):
                validate_private_edge_resources(config, azure, profile, origins, edge, route_probe=probe)
            probe.assert_not_called()
        connection_status = "Approved"
        extra_connection = True
        with self.assertRaisesRegex(ValueError, "exactly one approved"):
            validate_private_edge_resources(config, azure, profile, origins, edge, route_probe=probe)
        extra_connection = False
        for shared_status in ("Pending", "Rejected", "Disconnected", "unexpected"):
            probe.reset_mock()
            with self.subTest(origin=shared_status), self.assertRaisesRegex(ValueError, "private connection"):
                validate_private_edge_resources(config, azure, profile, origins, edge, route_probe=probe)
            probe.assert_not_called()

    def test_live_route_probe_rejects_front_door_fallback(self):
        from scripts.edge_binding import probe_enabled_route
        connection = MagicMock()
        connection.__enter__.return_value = connection
        secured = MagicMock()
        secured.__enter__.return_value = secured
        context = Mock()
        context.wrap_socket.return_value = secured
        response = Mock(status=401)
        response.getheaders.return_value = [("x-azure-ref", "synthetic")]
        address = [(2, 1, 6, "", ("192.0.2.10", 443))]
        with patch("scripts.edge_binding.socket.getaddrinfo", return_value=address), patch("scripts.edge_binding.socket.create_connection", return_value=connection), patch("scripts.edge_binding.ssl.create_default_context", return_value=context), patch("scripts.edge_binding.http.client.HTTPResponse", return_value=response):
            probe_enabled_route("llm-api.customer.invalid", "api.azurefd.net", "api")
            secured.sendall.assert_called_once()
            response.status = 404
            with self.assertRaisesRegex(ValueError, "did not reach"):
                probe_enabled_route("llm-api.customer.invalid", "api.azurefd.net", "api")
            response.status = 401
            response.getheaders.return_value = []
            with self.assertRaisesRegex(ValueError, "did not reach"):
                probe_enabled_route("llm-api.customer.invalid", "api.azurefd.net", "api")

    def test_deployed_edge_resolves_auto_private_origins_from_outputs(self):
        from scripts.edge_binding import deployed_edge
        config = proxy_customer()
        config["parameters"]["edge"]["privateOrigin"]["privateLinkServiceId"] = "auto"
        config["parameters"]["edge"]["adminPrivateOrigin"]["privateLinkServiceId"] = "auto"
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge = edge_deployment_output(config, identifier, profile)
        azure = Mock()
        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "edge": edge}
            return live_edge_resource(config, identifier, profile, arguments, edge_output=edge)
        azure.scoped.side_effect = cloud
        result = deployed_edge(config, azure)
        self.assertNotEqual(result["privateOrigin"]["privateLinkServiceId"], "auto")
        self.assertNotEqual(result["adminPrivateOrigin"]["privateLinkServiceId"], "auto")
        self.assertEqual(config["parameters"]["edge"]["privateOrigin"]["privateLinkServiceId"], "auto")

    def test_deployed_edge_rejects_live_admin_allowlist_drift(self):
        from scripts.edge_binding import deployed_edge
        config = proxy_customer()
        identifier = "11111111-1111-4111-8111-111111111111"
        profile = group_id(config) + "/providers/Microsoft.Cdn/profiles/synthetic"
        edge = edge_deployment_output(config, identifier, profile)
        azure = Mock()
        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "edge": edge}
            resource = live_edge_resource(config, identifier, profile, arguments, edge_output=edge)
            if resource["id"] == edge["adminWafId"]:
                resource["properties"]["customRules"]["rules"][0]["matchConditions"][0]["matchValue"] = ["21.31.41.51/32"]
            return resource
        azure.scoped.side_effect = cloud
        with self.assertRaisesRegex(ValueError, "allowlist differs"):
            deployed_edge(config, azure)

    def test_preparation_requires_access_receipt_for_same_applications(self):
        config = proxy_customer()
        config["privateIngress"] = {}
        config["proxy"]["image"] = "customerregistry.azurecr.io/auth-proxy@sha256:" + "a" * 64
        revision = "a" * 40
        common = {"revision": revision, "configSha256": stage_fingerprint(config, 7)}
        receipts = {
            "entra-apps": {**common, "applications": APPS},
            "entra-access": {**common, "applications": APPS, "directoryVerified": True, "loginVerified": False},
            "admin-credentials": common,
            "proxy-credentials": common,
        }

        def cloud(arguments):
            name = arguments[arguments.index("--name") + 1]
            if name.endswith("proxy-foundation"):
                return {"state": "Succeeded", "foundation": {}}
            key = next(key for key in receipts if name.endswith(key))
            return {"state": "Succeeded", "receipt": receipts[key]}

        azure = Mock()
        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.proxy_manifest.verify_runtime_image") as image, patch("scripts.proxy_manifest.render_proxy_documents", return_value=[]) as render:
            path = Path(directory)
            prepare_proxy_documents(config, revision, path, azure)
            image.assert_called_once()
            render.assert_called_once()
            receipts["entra-access"] = {**receipts["entra-access"], "applications": {}}
            with self.assertRaisesRegex(ValueError, "Access grant receipt"):
                prepare_proxy_documents(config, revision, path, azure)
            receipts["entra-access"] = {**common, "applications": APPS, "directoryVerified": False}
            with self.assertRaisesRegex(ValueError, "Access grant receipt"):
                prepare_proxy_documents(config, revision, path, azure)
            self.assertEqual(image.call_count, 1)

    def test_manifests_isolate_plane_secrets_and_have_no_placeholder(self):
        config = proxy_customer()
        config["proxy"]["image"] = "customerregistry.azurecr.io/auth-proxy@sha256:" + "a" * 64
        foundation = {plane: {"identity": {"clientId": APPS[plane]["appId"], "serviceAccountName": f"llm-{plane}-proxy", "kubernetesNamespace": "litellm"}, "vault": {"name": "synthetic-" + plane, "id": group_id(config) + "/providers/Microsoft.KeyVault/vaults/synthetic-" + plane}} for plane in ("api", "admin")}

        def secret(plane, name):
            return {"id": f"https://synthetic-{plane}.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32, "expiresAt": "2099-01-01T00:00:00+00:00" if name == OIDC_SECRET else None}

        applications = {plane: {"id": value["appId"], **value} for plane, value in APPS.items()}
        admin = {"application": applications["admin"], "vaultId": foundation["admin"]["vault"]["id"], "secrets": {name: secret("admin", name) for name in (OIDC_SECRET, SESSION_SECRET)}}
        credentials = {"bindings": []}
        for binding in credential_bindings(config):
            contract = binding_contract(config, binding)
            credentials["bindings"].append({"contract": contract, "secret": {**secret(binding["plane"], contract["secretName"]), "state": "ready"}, "userExists": True, "keyExists": True})
        documents = render_proxy_documents(config, foundation, applications, admin, credentials)
        rotated = copy.deepcopy(admin)
        rotated["secrets"][OIDC_SECRET].update(version="b" * 32, id=f"https://synthetic-admin.vault.azure.net/secrets/{OIDC_SECRET}/" + "b" * 32)
        replacement = render_proxy_documents(config, foundation, applications, rotated, credentials)
        before_pods = {item["metadata"]["name"]: item["spec"]["template"] for item in documents if item["kind"] == "Deployment"}
        after_pods = {item["metadata"]["name"]: item["spec"]["template"] for item in replacement if item["kind"] == "Deployment"}
        self.assertEqual(before_pods["llm-api-proxy"], after_pods["llm-api-proxy"])
        self.assertNotEqual(before_pods["llm-admin-proxy"]["metadata"]["annotations"], after_pods["llm-admin-proxy"]["metadata"]["annotations"])
        rotated_session = copy.deepcopy(admin)
        rotated_session["secrets"][SESSION_SECRET].update(version="c" * 32, id=f"https://synthetic-admin.vault.azure.net/secrets/{SESSION_SECRET}/" + "c" * 32)
        session_documents = render_proxy_documents(config, foundation, applications, rotated_session, credentials)
        session_pods = {item["metadata"]["name"]: item["spec"]["template"] for item in session_documents if item["kind"] == "Deployment"}
        self.assertEqual(before_pods["llm-api-proxy"], session_pods["llm-api-proxy"])
        self.assertNotEqual(before_pods["llm-admin-proxy"], session_pods["llm-admin-proxy"])
        backend = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "55555555-5555-4555-8555-555555555555", "workloadIdentityPrincipalId": "66666666-6666-4666-8666-666666666666", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        backend_versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        backend_documents = render_backend_manifest(config, backend, backend_versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        combined = backend_documents + documents
        check_application(combined, 7, config)
        invalid_mount = copy.deepcopy(combined)
        invalid_api = next(item for item in invalid_mount if item["kind"] == "Deployment" and item["metadata"]["name"] == "llm-api-proxy")
        invalid_api["spec"]["template"]["spec"]["volumes"].append({"name": "old-key", "secret": {"secretName": "old-key"}})
        with self.assertRaisesRegex(ValueError, "must not mount internal credentials"):
            check_application(invalid_mount, 7, config)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {}, clear=True), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=backend_documents), patch("scripts.proxy_manifest.prepare_proxy_documents", return_value=documents) as generated, patch("scripts.migration_runtime.run_command", side_effect=lambda arguments, path, label: "{}" if label == "server-dry-run" else "") as command:
            path = Path(directory)
            publish(config, 7, "application", "plan", "a" * 40, path, "")
            generated.assert_called_once()
            self.assertTrue(all("--dry-run=server" in call.args[0] for call in command.call_args_list if "apply" in call.args[0]))
            digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            publish(config, 7, "application", "execute", "a" * 40, path, digest)
            self.assertTrue(json.loads((path / "runtime-summary.json").read_text())["applied"])
            self.assertEqual(len([call for call in command.call_args_list if "rollout" in call.args[0]]), 3)
            with self.assertRaisesRegex(ValueError, "approved authentication and audit configuration"):
                publish(config, 8, "application", "plan", "a" * 40, path, "")
            from scripts.native_audit import prepare_native_audit
            native = copy.deepcopy(config)
            native["contentAudit"] = {"mode": "native", "retentionDays": 7, "contentPolicyAccepted": True}
            for binding in native["proxy"]["bindings"]:
                binding.pop("auditTeamId", None)
            native["proxy"]["bindings"] = [binding for binding in native["proxy"]["bindings"] if binding["role"] != "audit_reader"]
            native_documents = render_proxy_documents(native, foundation, applications, admin, credentials)
            with patch("scripts.audit_runtime.AuditCluster.get", return_value=None) as live, patch("scripts.proxy_manifest.prepare_proxy_documents", return_value=native_documents), patch("scripts.audit_manifest.prepare_audit_documents") as enhanced:
                native_result = prepare_native_audit(native, backend_documents + native_documents, path, Mock(), ["kubectl"])
                check_application(native_result, 8, native)
                command.reset_mock()
                publish(native, 8, "application", "plan", "a" * 40, path, "")
                digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
                publish(native, 8, "application", "execute", "a" * 40, path, digest)
                enhanced.assert_not_called()
                self.assertEqual(len([call for call in command.call_args_list if "rollout" in call.args[0]]), 3)
                self.assertFalse(any(item["kind"] == "CronJob" for item in native_result))
                native_settings = yaml.safe_load(next(item for item in native_result if item["kind"] == "ConfigMap" and "config.yaml" in item.get("data", {}))["data"]["config.yaml"])
                self.assertTrue(native_settings["general_settings"]["store_prompts_in_spend_logs"])
                live.side_effect = [{"spec": {"template": {"spec": {"volumes": [{"name": "stage8", "configMap": {"name": "existing-l3"}}]}}}}, {"data": {"config.json": json.dumps({"l3": {"enabled": True}})}}]
                with self.assertRaisesRegex(ValueError, "Existing L3"):
                    prepare_native_audit(native, backend_documents + native_documents, path, Mock(), ["kubectl"])
            from scripts.audit_manifest import render_audit_documents
            from tests.test_audit_manifest import fixture
            _, _, audit_foundation, storage = fixture()
            config["auditRuntime"] = {"retentionDays": 7, "captureEnabled": True, "retentionEnabled": False, "deliveryPolicyAccepted": True}
            audited = render_audit_documents(config, combined, audit_foundation, storage, "10.30.8.0/24")
            check_application(audited, 8, config)
            providers_before = [item for item in combined if item["kind"] == "SecretProviderClass"]
            providers_after = [item for item in audited if item["kind"] == "SecretProviderClass"]
            self.assertEqual(providers_before, providers_after)
            with patch("scripts.audit_manifest.prepare_audit_documents", return_value=audited) as audit_render:
                command.reset_mock()
                publish(config, 8, "application", "plan", "a" * 40, path, "")
                self.assertTrue(all("--dry-run=server" in call.args[0] for call in command.call_args_list if "apply" in call.args[0]))
                digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
                publish(config, 8, "application", "execute", "a" * 40, path, digest)
                self.assertEqual(audit_render.call_count, 2)
                summary = json.loads((path / "runtime-summary.json").read_text())
                self.assertTrue(summary["applied"])
                self.assertFalse(summary["stageAccepted"])
                self.assertEqual(len([call for call in command.call_args_list if "rollout" in call.args[0]]), 3)
        self.assertNotIn("REPLACE", json.dumps(documents))
        self.assertNotIn("litellm-master-key", json.dumps(documents))
        self.assertNotIn("secretObjects", json.dumps(documents))
        self.assertNotIn("Ingress", [item["kind"] for item in documents])
        providers = [item for item in documents if item["kind"] == "SecretProviderClass"]
        self.assertEqual([item["metadata"]["name"] for item in providers], ["llm-admin-auth"])
        api_pod = before_pods["llm-api-proxy"]["spec"]
        self.assertNotIn("csi", json.dumps(api_pod))
        self.assertNotIn("/mnt/auth-secrets", json.dumps(api_pod))
        for provider in providers:
            for entry in yaml.safe_load(provider["spec"]["parameters"]["objects"])["array"]:
                secret_object = yaml.safe_load(entry)
                self.assertEqual(secret_object["objectVersion"], "a" * 32)
                self.assertEqual(secret_object["filePermission"], "0444")
        invalid = copy.deepcopy(credentials)
        invalid["bindings"][0]["secret"]["id"] = secret("api", invalid["bindings"][0]["contract"]["secretName"])["id"]
        with self.assertRaisesRegex(ValueError, "crosses its approved plane"):
            render_proxy_documents(config, foundation, applications, admin, invalid)
        expired = copy.deepcopy(admin)
        expired["secrets"][OIDC_SECRET]["expiresAt"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "Rotate the admin"):
            render_proxy_documents(config, foundation, applications, expired, credentials)