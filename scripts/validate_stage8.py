#!/usr/bin/env python3
"""Validate Stage 8's offline configuration and trust boundaries."""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate(path: Path) -> None:
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    objects = {(item["kind"], item["metadata"]["name"]): item for item in documents}
    deployments = [item for item in documents if item["kind"] == "Deployment"]
    assert len(deployments) == 4
    for item in deployments:
        pod = item["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["nodeSelector"] == {"workload": "litellm"}
        for container in pod["containers"]:
            assert container["securityContext"]["allowPrivilegeEscalation"] is False
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
            assert "envFrom" not in container
    assert "Secret" not in {item["kind"] for item in documents}
    assert all(item["spec"]["type"] == "ClusterIP" for item in documents if item["kind"] == "Service")
    backend = objects[("NetworkPolicy", "allow-litellm-required-traffic")]
    assert backend["spec"]["ingress"] == [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "entra-auth-proxy"}}}], "ports": [{"protocol": "TCP", "port": 4000}]}]
    assert objects[("Ingress", "llm-admin")]["spec"]["ingressClassName"] == "REPLACE_PRIVATE_ADMIN_INGRESS_CLASS"
    config_maps = [item for item in documents if item["kind"] == "ConfigMap"]
    config = json.loads(next(item["data"]["config.json"] for item in config_maps if "config.json" in item.get("data", {})))
    assert config["l3"]["enabled"] is False
    assert config["l3"]["retentionDays"] == 7
    assert config["guardrail"]["enabled"] is False and config["guardrail"]["mode"] == "observe"
    assert config["telemetry"]["enabled"] is False
    approvals = next(item["data"] for item in config_maps if "approvals.json" in item.get("data", {}))
    assert json.loads(approvals["approvals.json"]) == []
    assert json.loads(approvals["holds.json"]) == []
    for plane in ("api", "admin"):
        pod = objects[("Deployment", f"llm-{plane}-proxy")]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert {entry["name"]: entry.get("value") for entry in container["env"]}["STAGE8_CONFIG"] == "/etc/stage8/config.json"
        mounts = {entry["name"] for entry in container["volumeMounts"]}
        assert ("approvals" in mounts) == (plane == "admin")
    job = objects[("CronJob", "l3-retention")]["spec"]
    assert job["suspend"] is True and job["concurrencyPolicy"] == "Forbid"
    worker = job["jobTemplate"]["spec"]["template"]["spec"]
    assert worker["serviceAccountName"] == "l3-retention"
    assert worker["nodeSelector"] == {"workload": "litellm"}
    assert worker["automountServiceAccountToken"] is False
    assert worker["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    collector = yaml.safe_load(next(item["data"]["collector-config.yaml"] for item in config_maps if "collector-config.yaml" in item.get("data", {})))
    assert set(collector["service"]["pipelines"]) == {"traces"}
    assert set(collector["exporters"]) == {"azuremonitor"}
    assert collector["exporters"]["azuremonitor"]["auth"]["authenticator"] == "azure_auth"
    assert "transform/allowlist" in collector["service"]["pipelines"]["traces"]["processors"]
    rules = json.loads((ROOT / "infra/audit-detection/rules.json").read_text())
    assert len(rules) >= 5
    assert all("ContainerLogV2" in rule["query"] and "PodNamespace" in rule["query"] for rule in rules)
    contracts = json.loads((ROOT / "infra/audit-detection/response-contracts.json").read_text())
    assert len(contracts) == 3 and all(item["enabled"] is False for item in contracts)
    print("Stage 8 offline configuration and authorization boundaries passed.")


if __name__ == "__main__":
    validate(Path(sys.argv[1]))