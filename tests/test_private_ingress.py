import unittest
import copy
import re
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import yaml

from scripts.private_ingress import NATIVE_API_PATHS, certificate_material, ingress_settings, native_api_rule, preserve_native_front_door_binding, render_ingress, source_ranges
from tests.test_customer_migration import customer_config


def traefik_matches(rule, method, path, host, headers=None):
    """Evaluate the Host/Method/Path/HeaderRegexp subset of a Traefik v3 rule for one request."""
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    expression = re.sub(r"Host\(`([^`]*)`\)", lambda item: repr(item[1] == host), rule)
    expression = re.sub(r"Method\(`([^`]*)`\)", lambda item: repr(item[1] == method), expression)
    expression = re.sub(r"Path\(`([^`]*)`\)", lambda item: repr(item[1] == path), expression)
    expression = re.sub(r"HeaderRegexp\(`([^`]*)`,\s*`([^`]*)`\)", lambda item: repr(re.search(item[2], headers.get(item[1].lower(), "")) is not None), expression)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    if re.fullmatch(r"[\sA-Za-z()]*", expression) is None or set(re.findall(r"[A-Za-z]+", expression)) - {"True", "False", "and", "or"}:
        raise AssertionError("Unsupported Traefik rule: " + rule)
    return eval(expression)


class PrivateIngressTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
        self.image = "registry.invalid/traefik@sha256:" + "a" * 64

    def test_private_planes_have_no_api_credentials_or_cross_plane_backend(self):
        for plane in ("api", "admin"):
            documents = render_ingress(self.config, plane, self.image, ["10.30.0.0/16"])
            objects = {document["kind"]: document for document in documents}
            self.assertFalse({"Secret", "Role", "ClusterRole"} & objects.keys())
            service = objects["Service"]
            self.assertEqual(service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"], "true")
            self.assertEqual([port["port"] for port in service["spec"]["ports"]], [443])
            self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
            pod = objects["Deployment"]["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["containers"][0]["image"], self.image)
            self.assertNotIn("providers.kubernetes", str(pod))
            dynamic = yaml.safe_load(objects["ConfigMap"]["data"]["routes.yaml"])
            self.assertEqual(dynamic["http"]["services"][plane]["loadBalancer"]["servers"], [{"url": f"http://llm-{plane}-proxy.litellm.svc.cluster.local:8080"}])
            self.assertTrue(dynamic["tls"]["options"]["default"]["sniStrict"])
            backend = objects["NetworkPolicy"]["spec"]["egress"][1]["to"][0]
            self.assertEqual(backend["podSelector"]["matchLabels"]["plane"], plane)

    def test_native_mode_routes_admin_privately_and_limits_api_to_inference(self):
        self.config["application"] = {
            "backendImage": "customerregistry.azurecr.io/litellm@sha256:" + "a" * 64,
            "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "model", "id": "primary-coding", "apiVersion": "v1"}],
            "authentication": {"mode": "native", "adminUsername": "gateway-admin"},
        }
        for plane in ("api", "admin"):
            documents = render_ingress(self.config, plane, self.image, ["10.30.0.0/16"])
            objects = {document["kind"]: document for document in documents}
            dynamic = yaml.safe_load(objects["ConfigMap"]["data"]["routes.yaml"])
            self.assertEqual(dynamic["http"]["services"][plane]["loadBalancer"]["servers"], [{"url": "http://litellm.litellm.svc.cluster.local:4000"}])
            policy = objects["NetworkPolicy"]["spec"]["egress"][1]
            self.assertEqual(policy["ports"], [{"protocol": "TCP", "port": 4000}])
            self.assertEqual(policy["to"][0]["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "litellm", "app.kubernetes.io/component": "gateway"})
            if plane == "api":
                rule = dynamic["http"]["routers"]["api"]["rule"]
                for path in ("/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings"):
                    self.assertIn(f"Path(`{path}`)", rule)
                self.assertIn("Method(`POST`)", rule)
                self.assertIn("(Method(`GET`) && (Path(`/models`) || Path(`/v1/models`)))", rule)
                self.assertEqual(dynamic["http"]["middlewares"]["api-health-path"]["replacePath"]["path"], "/health/readiness")
                self.assertIn("Path(`/readyz`)", dynamic["http"]["routers"]["api-health"]["rule"])
            else:
                self.assertEqual(set(dynamic["http"]["routers"]), {"admin", "admin-health"})
                self.assertEqual(dynamic["http"]["middlewares"]["admin-health-path"]["replacePath"]["path"], "/health/readiness")

    def test_native_api_rule_admits_model_list_get_only(self):
        host, edge = "llm-api.customer.invalid", "11111111-1111-4111-8111-111111111111"
        rule = native_api_rule(host, edge)
        bound = {"X-Azure-FDID": edge}
        self.assertEqual(sorted(NATIVE_API_PATHS), sorted(["/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings", "/models", "/v1/models"]))
        for path in ("/models", "/v1/models"):
            self.assertTrue(traefik_matches(rule, "GET", path, host, bound))
            self.assertFalse(traefik_matches(rule, "GET", path, host))
            self.assertFalse(traefik_matches(rule, "GET", path, "llm-admin.customer.invalid", bound))
            for method in ("POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"):
                self.assertFalse(traefik_matches(rule, method, path, host, bound), (method, path))
        for path in ("/v1/models/coding", "/models/coding", "/v1/models/", "/model/info", "/v1/model/info", "/key/generate", "/ui/"):
            for method in ("GET", "POST"):
                self.assertFalse(traefik_matches(rule, method, path, host, bound), (method, path))
        for path in ("/v1/chat/completions", "/v1/responses", "/embeddings"):
            self.assertTrue(traefik_matches(rule, "POST", path, host, bound))
            self.assertFalse(traefik_matches(rule, "GET", path, host, bound))
        handshake = {**bound, "Upgrade": "websocket", "Connection": "Upgrade"}
        self.assertTrue(traefik_matches(rule, "GET", "/v1/responses", host, handshake))
        self.assertFalse(traefik_matches(rule, "GET", "/v1/chat/completions", host, handshake))

    def test_each_plane_allows_only_configured_sources_and_private_link_nat_subnet(self):
        configured = ["10.60.0.0/16"]
        ingress_subnet = self.config["parameters"]["platform"]["stage4Network"]["ingressSubnetPrefix"]
        for plane in ("api", "admin"):
            documents = render_ingress(self.config, plane, self.image, configured)
            objects = {document["kind"]: document for document in documents}
            expected = sorted([*configured, ingress_subnet])
            self.assertEqual(objects["Service"]["spec"]["loadBalancerSourceRanges"], expected)
            policy_sources = [item["ipBlock"]["cidr"] for item in objects["NetworkPolicy"]["spec"]["ingress"][0]["from"]]
            self.assertEqual(policy_sources, expected)

    def test_native_ingress_redeploy_preserves_front_door_business_binding(self):
        self.config["application"] = {
            "backendImage": "customerregistry.azurecr.io/litellm@sha256:" + "a" * 64,
            "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "model", "id": "primary-coding", "apiVersion": "v1"}],
            "authentication": {"mode": "native", "adminUsername": "gateway-admin"},
        }
        documents = render_ingress(self.config, "api", self.image, ["10.30.0.0/16"])
        identifier = "11111111-1111-4111-8111-111111111111"
        current = copy.deepcopy(next(item for item in documents if item["kind"] == "ConfigMap"))
        dynamic = yaml.safe_load(current["data"]["routes.yaml"])
        dynamic["http"]["routers"]["api"]["rule"] += f" && HeaderRegexp(`X-Azure-FDID`, `(?i)^{identifier}$`)"
        current["data"]["routes.yaml"] = yaml.safe_dump(dynamic, sort_keys=False)
        result = preserve_native_front_door_binding(self.config, documents, current)
        rendered = yaml.safe_load(next(item for item in result if item["kind"] == "ConfigMap")["data"]["routes.yaml"])
        self.assertIn(identifier, rendered["http"]["routers"]["api"]["rule"])
        self.assertNotIn("X-Azure-FDID", rendered["http"]["routers"]["api-health"]["rule"])
        deployment = next(item for item in result if item["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["template"]["metadata"]["annotations"]["llmgw/front-door-id"], identifier)

    def test_public_and_missing_source_ranges_are_rejected(self):
        for ranges in ([], ["0.0.0.0/0"], ["8.8.8.0/24"], ["::/0"], ["10.30.1.1/24"]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                source_ranges(ranges)

    def test_unpinned_image_is_rejected(self):
        with self.assertRaises(ValueError):
            render_ingress(self.config, "api", "traefik:latest", ["10.0.0.0/8"])

    def test_settings_reject_arbitrary_secret_hosts_and_public_sources(self):
        self.config["privateIngress"] = {plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.30.0.0/16"]} for plane in ("api", "admin")}
        ingress_settings(self.config)
        for url in ("http://synthetic.vault.azure.net/secrets/tls", "https://attacker.invalid/secrets/tls", "https://synthetic.vault.azure.net/secrets/tls?sig=secret"):
            self.config["privateIngress"]["api"]["tlsSecretId"] = url
            with self.assertRaises(ValueError):
                ingress_settings(self.config)

    def test_certificate_domain_key_and_expiry_are_validated(self):
        now = datetime.now(timezone.utc)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "llm-api.customer.invalid")])
        cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.SubjectAlternativeName([x509.DNSName("llm-api.customer.invalid")]), critical=False).sign(key, hashes.SHA256())
        pem = cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        material = certificate_material(pem.decode(), "llm-api.customer.invalid", now)
        self.assertEqual(len(material["sha256"]), 64)
        with self.assertRaises(ValueError):
            certificate_material(pem.decode(), "llm-api.customer.invalid", now + timedelta(days=25))
        with self.assertRaises(Exception):
            certificate_material(pem.decode(), "llm-admin.customer.invalid", now)