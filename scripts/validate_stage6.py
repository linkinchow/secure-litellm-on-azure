#!/usr/bin/env python3
"""Static safety checks for the Stage 6 LiteLLM HA and routing component."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
STAGE6 = ROOT / "deploy/components/stage6-ha"
APPROVED_DIGEST = "sha256:6cc08e60be3e4ebf0cfb41ef23aed97e9ba4f61702d1e95e533f47ee6d67865f"
AFFINITY_CHECKS = {
    "encrypted_content_affinity",
    "responses_api_deployment_check",
    "session_affinity",
    "deployment_affinity",
}


def load_documents(path: Path) -> list[dict[str, Any]]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def one(documents: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [item for item in documents if item.get("kind") == kind]
    assert len(matches) == 1, f"expected one {kind}, found {len(matches)}"
    return matches[0]


def validate_component_sources() -> None:
    config_patch = one(load_documents(STAGE6 / "config-patch.yaml"), "ConfigMap")
    config = yaml.safe_load(config_patch["data"]["config.yaml"])

    models = config["model_list"]
    assert len(models) == 2, "render-only routing baseline needs two deployment templates"
    model_ids = [model["model_info"]["id"] for model in models]
    assert len(set(model_ids)) == len(model_ids), "model_info.id values must be unique"
    assert all(value.startswith("REPLACE_STABLE_DEPLOYMENT_ID_") for value in model_ids)
    assert all("rpm" not in model["litellm_params"] for model in models)
    assert all("tpm" not in model["litellm_params"] for model in models)
    assert all("max_parallel_requests" not in model["litellm_params"] for model in models)

    router = config["router_settings"]
    assert router["routing_strategy"] == "simple-shuffle"
    assert "routing_strategy_args" not in router
    assert set(router["optional_pre_call_checks"]) == AFFINITY_CHECKS
    assert set(router["model_group_affinity_config"]["REPLACE_MODEL_GROUP"]) == AFFINITY_CHECKS
    assert router["deployment_affinity_ttl_seconds"] == 3600
    assert router["allowed_fails"] == 2
    assert router["cooldown_time"] == 30
    assert router["cache_kwargs"]["azure_redis_ad_token"] is True
    assert router["cache_kwargs"]["ssl_cert_reqs"] == "required"
    assert router["cache_kwargs"]["ssl_check_hostname"] is True

    general = config["general_settings"]
    assert general["store_prompts_in_spend_logs"] is False
    assert general["maximum_spend_logs_retention_period"] == "7d"

    hpa = one(load_documents(STAGE6 / "hpa.yaml"), "HorizontalPodAutoscaler")
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["spec"]["minReplicas"] == 2
    assert hpa["spec"]["maxReplicas"] >= 4
    assert hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] >= 300
    metrics = {item["resource"]["name"] for item in hpa["spec"]["metrics"]}
    assert metrics == {"cpu", "memory"}


def validate_rendered(path: Path) -> None:
    documents = load_documents(path)
    deployment = one(documents, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    pod_spec = deployment["spec"]["template"]["spec"]

    assert deployment["spec"]["replicas"] == 2
    assert deployment["spec"]["minReadySeconds"] >= 30
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {
        "maxSurge": 1,
        "maxUnavailable": 0,
    }
    assert pod_spec["terminationGracePeriodSeconds"] >= 600
    assert pod_spec["topologySpreadConstraints"][0]["whenUnsatisfiable"] == "DoNotSchedule"
    assert pod_spec["nodeSelector"] == {"workload": "litellm"}, "backend must stay on the user node pool"
    assert f"@{APPROVED_DIGEST}" in container["image"]
    assert container["lifecycle"]["preStop"]["exec"]["command"][-1] == "sleep 30"
    assert all(name in container for name in ("startupProbe", "readinessProbe", "livenessProbe"))
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert "envFrom" not in container
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["STORE_MODEL_IN_DB"] == "false"

    hpa = one(documents, "HorizontalPodAutoscaler")
    assert hpa["spec"]["minReplicas"] == 2
    pdb = one(documents, "PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] == 1
    service = one(documents, "Service")
    assert service["spec"]["type"] == "ClusterIP", "public LoadBalancer/NodePort is forbidden"
    one(documents, "SecretProviderClass")

    config_map = one(documents, "ConfigMap")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    assert config["router_settings"]["routing_strategy"] == "simple-shuffle"
    assert set(config["router_settings"]["optional_pre_call_checks"]) == AFFINITY_CHECKS
    assert "store_model_in_db" not in config["general_settings"]

    serialized = path.read_text(encoding="utf-8")
    for marker in ("postgresql://", "redis://", "rediss://", "sk-"):
        assert marker not in serialized, f"credential marker found: {marker}"


if __name__ == "__main__":
    validate_component_sources()
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_stage6.py <rendered-stage6.yaml>")
    validate_rendered(Path(sys.argv[1]))
    print("Stage 6 HA and routing safety checks passed.")
