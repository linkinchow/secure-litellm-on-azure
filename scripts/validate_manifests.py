#!/usr/bin/env python3
"""Validate rendered Stage 3 manifests without contacting a cluster."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

EXPECTED_DIGEST = "sha256:625981c83410a3ea68eb0697590a57ec1d764d634514d54fa5db0591077ee839"


def fail(message: str) -> None:
    raise AssertionError(message)


def get_container(documents: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    deployments = [item for item in documents if item.get("kind") == "Deployment"]
    if len(deployments) != 1:
        fail(f"expected one Deployment, found {len(deployments)}")
    deployment = deployments[0]
    containers = deployment["spec"]["template"]["spec"].get("containers", [])
    if len(containers) != 1:
        fail(f"expected one LiteLLM container, found {len(containers)}")
    return deployment, containers[0]


def validate(path: Path) -> None:
    documents = [item for item in yaml.safe_load_all(path.read_text()) if item]
    deployment, container = get_container(documents)
    pod_spec = deployment["spec"]["template"]["spec"]
    pod_security = pod_spec.get("securityContext", {})
    container_security = container.get("securityContext", {})

    image = container.get("image", "")
    if f"@{EXPECTED_DIGEST}" not in image:
        fail(f"{path}: LiteLLM image is not pinned to the approved digest")
    if container.get("imagePullPolicy") != "IfNotPresent":
        fail(f"{path}: imagePullPolicy must be IfNotPresent for a digest")
    if not pod_security.get("runAsNonRoot") or pod_security.get("runAsUser") != 10001:
        fail(f"{path}: Pod must run as non-root UID 10001")
    if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
        fail(f"{path}: RuntimeDefault seccomp is required")
    if container_security.get("allowPrivilegeEscalation") is not False:
        fail(f"{path}: privilege escalation must be disabled")
    if container_security.get("readOnlyRootFilesystem") is not True:
        fail(f"{path}: root filesystem must be read-only")
    if "ALL" not in container_security.get("capabilities", {}).get("drop", []):
        fail(f"{path}: all Linux capabilities must be dropped")
    if pod_spec.get("automountServiceAccountToken") is not False:
        fail(f"{path}: Kubernetes API token automount must be disabled")
    if any(key not in container for key in ("startupProbe", "readinessProbe", "livenessProbe")):
        fail(f"{path}: all three health probes are required")
    if not container.get("resources", {}).get("requests") or not container.get("resources", {}).get("limits"):
        fail(f"{path}: resource requests and limits are required")
    if "envFrom" in container:
        fail(f"{path}: envFrom is forbidden; reference Secret keys individually")
    expected_replicas = 1 if path.stem == "dev" else 2
    if deployment["spec"].get("replicas") != expected_replicas:
        fail(f"{path}: expected {expected_replicas} replicas")
    if not pod_spec.get("topologySpreadConstraints"):
        fail(f"{path}: topology spread is required")
    if pod_spec.get("nodeSelector") != {"workload": "litellm"}:
        fail(f"{path}: Pod must select the user node pool (workload=litellm)")

    services = [item for item in documents if item.get("kind") == "Service"]
    if not services or any(item.get("spec", {}).get("type", "ClusterIP") != "ClusterIP" for item in services):
        fail(f"{path}: only ClusterIP Services are allowed in Stage 3")
    forbidden = {"Ingress", "Secret"}
    present = forbidden.intersection(item.get("kind") for item in documents)
    if present:
        fail(f"{path}: forbidden rendered objects: {sorted(present)}")
    budgets = [item for item in documents if item.get("kind") == "PodDisruptionBudget"]
    if len(budgets) != 1 or budgets[0].get("spec", {}).get("minAvailable") != 1:
        fail(f"{path}: one PDB with minAvailable=1 is required")

    config_maps = [item for item in documents if item.get("kind") == "ConfigMap"]
    config_text = "\n".join(str(item.get("data", {})) for item in config_maps)
    if "store_prompts_in_spend_logs: false" not in config_text:
        fail(f"{path}: Prompt/Response body logging must remain disabled")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: validate_manifests.py <rendered.yaml> [...]")
    for manifest in map(Path, sys.argv[1:]):
        validate(manifest)
        print(f"validated: {manifest}")
