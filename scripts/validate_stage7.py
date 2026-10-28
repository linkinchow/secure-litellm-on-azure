#!/usr/bin/env python3
"""Validate the rendered Stage 7 trust boundary offline."""

import json
import sys
from pathlib import Path

import yaml


def validate(path: Path) -> None:
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    by_kind = {}
    for document in documents:
        by_kind.setdefault(document["kind"], {})[document["metadata"]["name"]] = document
    deployments = by_kind["Deployment"]
    assert set(deployments) == {"litellm", "llm-api-proxy", "llm-admin-proxy"}
    ingresses = by_kind["Ingress"]
    assert set(ingresses) == {"llm-api", "llm-admin"}
    assert "Secret" not in by_kind
    assert all(item["spec"]["type"] == "ClusterIP" for item in by_kind["Service"].values())
    policies = by_kind["NetworkPolicy"]
    assert set(policies) == {"default-deny", "allow-litellm-required-traffic", "llm-api-proxy-ingress", "llm-admin-proxy-ingress", "llm-auth-proxy-egress"}
    assert policies["default-deny"]["spec"]["podSelector"] == {}
    assert not policies["default-deny"]["spec"].get("ingress")
    assert policies["allow-litellm-required-traffic"]["spec"]["ingress"] == [{
        "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "entra-auth-proxy"}}}],
        "ports": [{"protocol": "TCP", "port": 4000}],
    }]
    for plane in ("api", "admin"):
        name = f"llm-{plane}-proxy"
        ingress = ingresses[f"llm-{plane}"]["spec"]
        assert ingress["ingressClassName"] == f"REPLACE_PRIVATE_{plane.upper()}_INGRESS_CLASS"
        rule = ingress["rules"][0]
        assert rule["host"] == f"llm-{plane}.example.com"
        assert ingress["tls"][0]["hosts"] == [rule["host"]]
        assert rule["http"]["paths"][0]["backend"]["service"]["name"] == name
        deployment = deployments[name]["spec"]
        assert deployment["replicas"] == 2
        pod = deployment["template"]["spec"]
        assert pod["serviceAccountName"] == name
        assert pod["nodeSelector"] == {"workload": "litellm"}
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        container = pod["containers"][0]
        assert container["image"] == "REPLACE_APPROVED_ACR_AUTH_PROXY_DIGEST"
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert not container.get("envFrom")
        assert container["env"] == [{"name": "PROXY_PLANE", "value": plane}]
        assert all(probe in container for probe in ("startupProbe", "readinessProbe", "livenessProbe"))
        assert by_kind["PodDisruptionBudget"][name]["spec"]["minAvailable"] == 1
        if plane == "api":
            assert "llm-api-auth" not in by_kind["SecretProviderClass"]
            assert not any("csi" in volume or "secret" in volume for volume in pod.get("volumes", []))
            assert not any(item["mountPath"] == "/mnt/auth-secrets" for item in container.get("volumeMounts", []))
        else:
            provider = by_kind["SecretProviderClass"][f"llm-{plane}-auth"]["spec"]
            assert "secretObjects" not in provider
            assert provider["parameters"]["clientID"] == f"REPLACE_{plane.upper()}_PROXY_CLIENT_ID"
            assert provider["parameters"]["keyvaultName"] == f"REPLACE_{plane.upper()}_AUTH_VAULT_NAME"
            objects = yaml.safe_load(provider["parameters"]["objects"])["array"]
            assert all(yaml.safe_load(item)["filePermission"] == "0444" for item in objects)
        ingress_policy = policies[f"llm-{plane}-proxy-ingress"]["spec"]
        assert ingress_policy["podSelector"]["matchLabels"]["plane"] == plane
        assert ingress_policy["ingress"][0]["from"] == [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"llm-{plane}-ingress"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "controller"}},
        }]
    assert deployments["litellm"]["spec"]["template"]["spec"]["nodeSelector"] == {"workload": "litellm"}
    backend_env = deployments["litellm"]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert not any(item["name"] == "AZURE_CLIENT_ID" for item in backend_env)
    policy_map = next(item for item in by_kind["ConfigMap"].values() if "policy.json" in item.get("data", {}))
    policy = json.loads(policy_map["data"]["policy.json"])
    assert policy["apiHost"] == "llm-api.example.com"
    assert policy["adminHost"] == "llm-admin.example.com"
    assert policy["bindings"] == [], "No identities may be pre-authorized in a public template"
    print("Stage 7 domains, identity separation and proxy-only backend checks passed.")


if __name__ == "__main__":
    validate(Path(sys.argv[1]))