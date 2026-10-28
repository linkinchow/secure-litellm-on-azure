"""Build and check the hardened runtime without Azure or target registry access."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

from scripts.customer_migration import ROOT, MigrationError, private_write, require
from scripts.workflow_diagnostics import command_failure_summary, exception_diagnostic, format_diagnostic


RUNTIME_DOCKERFILE = ROOT / "LiteLLM/runtime/Dockerfile"
RUNTIME_DOCKERIGNORE = ROOT / "LiteLLM/runtime/Dockerfile.dockerignore"
HARDENING_REQUIREMENTS = ROOT / "LiteLLM/runtime/security-requirements.txt"
RUNTIME_CONTRACT = {
    "uid": 10001,
    "versions": {"litellm": "1.104.8", "anyio": "4.14.2", "PyJWT": "2.15.0"},
    "gpt6": {"gpt-6.1": True, "azure/gpt-6.1": True},
    "catalog": {"azure/gpt-6-astra": True, "azure/gpt-6-sol": True, "azure/gpt-6-luna": True},
    "customDeploymentConfig": "AzureOpenAIGPT5Config",
    "callbacks": {"LiteLLM.runtime.callbacks.codex_passthrough": True},
}
RUNTIME_CONTRACT_SCRIPT = """import asyncio
import importlib.metadata as metadata
import json
import os
import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.openai.chat.gpt_5_transformation import OpenAIGPT5Config
from litellm.proxy.types_utils.utils import get_instance_fn
from litellm.types.utils import CallTypes, LlmProviders
from litellm.utils import ProviderConfigManager
models = ("gpt-6.1", "azure/gpt-6.1")
catalog = ("azure/gpt-6-astra", "azure/gpt-6-sol", "azure/gpt-6-luna")
custom = ProviderConfigManager.get_provider_chat_config(model="azure/custom-deployment", provider=LlmProviders.AZURE, base_model="azure/gpt-6.1")
def callback(name):
    hook = get_instance_fn(value=name, config_file_path="/app/config/config.yaml")
    item = {"type": "message", "internal_chat_message_metadata_passthrough": {"content_item_metadata": []}}
    stripped = asyncio.run(hook.async_pre_call_deployment_hook({"input": [item]}, CallTypes.aresponses))
    other = asyncio.run(hook.async_pre_call_deployment_hook({"input": [item]}, CallTypes.acompletion))
    return isinstance(hook, CustomLogger) and stripped == {"input": [{"type": "message"}]} and other is None and len(item) == 2
print(json.dumps({
    "uid": os.getuid(),
    "versions": {name: metadata.version(name) for name in ("litellm", "anyio", "PyJWT")},
    "gpt6": {name: OpenAIGPT5Config.is_model_gpt_6_plus_model(name) for name in models},
    "catalog": {name: name in litellm.model_cost for name in catalog},
    "customDeploymentConfig": type(custom).__name__,
    "callbacks": {name: callback(name) for name in ("LiteLLM.runtime.callbacks.codex_passthrough",)},
}))
"""


def source_image():
    dockerfile = RUNTIME_DOCKERFILE.read_text()
    matches = re.findall(r"^FROM (docker\.litellm\.ai/berriai/litellm@sha256:[0-9a-f]{64})$", dockerfile, re.M)
    require(len(matches) == 1, "Expected one digest-locked public LiteLLM source")
    return matches[0]


def hardened_runtime_image(revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision or ""), "A full source revision is required")
    return f"litellm-azure-source-check:{revision[:12]}"


def runtime_build_inputs_sha256():
    return hashlib.sha256(RUNTIME_DOCKERFILE.read_bytes() + RUNTIME_DOCKERIGNORE.read_bytes() + HARDENING_REQUIREMENTS.read_bytes()).hexdigest()


def runtime_build_command(revision):
    return ["docker", "build", "--network", "none", "-f", str(RUNTIME_DOCKERFILE), "-t", hardened_runtime_image(revision), str(ROOT)]


def image_content_sha256(document):
    require(isinstance(document, dict) and isinstance(document.get("RootFS", {}).get("Layers"), list) and document["RootFS"]["Layers"], "Hardened runtime image filesystem is unavailable")
    contract = {
        "architecture": document.get("Architecture"),
        "os": document.get("Os"),
        "rootfs": document["RootFS"],
        "config": document.get("Config"),
    }
    require(all(contract[key] for key in ("architecture", "os", "config")), "Hardened runtime image configuration is unavailable")
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_hardened_runtime(revision, run=subprocess.run):
    image = hardened_runtime_image(revision)
    try:
        result = run(runtime_build_command(revision), capture_output=True, text=True, check=False, timeout=1200)
    except OSError:
        raise MigrationError("Docker is unavailable for the hardened runtime build") from None
    except subprocess.SubprocessError:
        raise MigrationError("Hardened runtime build timed out") from None
    require(result.returncode == 0, "Hardened runtime build failed: " + command_failure_summary(result.stdout, result.stderr, result.returncode))
    try:
        inspected = run(["docker", "image", "inspect", "--format", "{{json .}}", image], capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise MigrationError("Unable to inspect the hardened runtime image") from None
    try:
        document = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        document = {}
    image_id = document.get("Id", "")
    require(inspected.returncode == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None, "Hardened runtime image ID is unavailable")
    return {"reference": image, "id": image_id, "contentSha256": image_content_sha256(document), "buildInputsSha256": runtime_build_inputs_sha256(), "source": source_image(), "stderr": result.stderr}


def check_source(directory, revision, run=subprocess.run):
    directory = Path(directory).resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Source evidence must stay under ignored temp/")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or ""), "A full source revision is required")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    image = source_image()
    evaluated_image = hardened_runtime_image(revision)
    build_inputs_sha256 = runtime_build_inputs_sha256()
    report = {"schemaVersion": 1, "check": "source_image_sbom_scan", "revision": revision, "sourceImage": image,
              "evaluatedImage": evaluated_image, "buildInputsSha256": build_inputs_sha256,
              "observedAt": datetime.now(timezone.utc).isoformat(), "status": "failed", "stageAccepted": False,
              "policy": {"severity": ["CRITICAL"], "ignoreUnfixed": True}, "results": [],
              "notCovered": ["upstream publisher identity", "target ACR signature and private pull", "customer stage approval"]}
    build_entry = {"name": "build", "status": "failed", "sha256": build_inputs_sha256}
    try:
        built = build_hardened_runtime(revision, run)
        private_write(directory / "source-build.stderr.txt", built["stderr"])
        build_entry.update(status="passed", exitCode=0, imageId=built["id"])
        report["evaluatedImageId"] = built["id"]
        report["evaluatedImageContentSha256"] = built["contentSha256"]
    except MigrationError as error:
        build_entry["reason"] = str(error)
        build_entry["diagnostic"] = exception_diagnostic(error, "source-supply-chain")
    report["results"].append(build_entry)
    if build_entry["status"] != "passed":
        private_write(directory / "source-summary.json", json.dumps(report, indent=2))
        return report
    evaluated_reference = report["evaluatedImageId"]
    operations = (
        ("runtime", ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,size=64m", "--entrypoint", "/app/.venv/bin/python", evaluated_reference, "-c", RUNTIME_CONTRACT_SCRIPT], "source-runtime.json"),
        ("sbom", ["syft", evaluated_reference, "--output", "spdx-json"], "source-sbom.spdx.json"),
        ("scan", ["trivy", "image", "--exit-code", "1", "--severity", "CRITICAL", "--ignore-unfixed", "--format", "json", evaluated_reference], "source-scan.json"),
    )
    for name, command, filename in operations:
        path = directory / filename
        path.unlink(missing_ok=True)
        entry = {"name": name, "status": "failed"}
        try:
            result = run(command, capture_output=True, text=True, check=False, timeout=1200)
            document = json.loads(result.stdout)
            require(isinstance(document, dict), "Tool output must be a JSON object")
            if name == "runtime":
                require(document == RUNTIME_CONTRACT, "Runtime version, security dependency, GPT-6 model or shipped callback contract differs from the reviewed release")
            elif name == "sbom":
                require(document.get("spdxVersion") and isinstance(document.get("packages"), list) and document["packages"], "Incomplete SPDX inventory")
            else:
                require(document.get("ArtifactName") == evaluated_reference and isinstance(document.get("Results"), list) and document["Results"], "Scan output is not bound to the hardened runtime")
            content = json.dumps(document, sort_keys=True)
            private_write(path, content)
            entry.update(status="passed" if result.returncode == 0 else "failed", exitCode=result.returncode,
                         artifact=filename, sha256=hashlib.sha256(content.encode()).hexdigest())
            if result.returncode:
                entry["reason"] = {"runtime": "Runtime contract command returned a nonzero exit code", "scan": "Fixable CRITICAL vulnerabilities matched policy"}.get(name, "SBOM command returned a nonzero exit code")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            entry["reason"] = "Tool unavailable, timed out, or returned invalid evidence"
            entry["diagnostic"] = exception_diagnostic(error, "source-supply-chain")
        report["results"].append(entry)
    if all(item["status"] == "passed" for item in report["results"]):
        report["status"] = "passed"
    private_write(directory / "source-summary.json", json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/source-supply-chain")
    args = parser.parse_args()
    report = check_source(args.output_dir, args.revision)
    print(json.dumps({"check": report["check"], "status": report["status"], "results": {item["name"]: item["status"] for item in report["results"]}, "stageAccepted": False}))
    if report["status"] == "failed":
        failures = []
        for result in report["results"]:
            if result["status"] != "failed":
                continue
            diagnostic = result.get("diagnostic", {})
            detail = diagnostic.get("message") if isinstance(diagnostic, dict) and isinstance(diagnostic.get("message"), str) else result.get("reason", "failed")
            failures.append(f"{result['name']}: {detail}")
        raise MigrationError("Source supply-chain checks failed: " + "; ".join(failures))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        diagnostic = exception_diagnostic(error, "source-supply-chain", "Source supply-chain checks failed")
        raise SystemExit(format_diagnostic(diagnostic)) from None