"""Build or import, scan, sign, and verify local migration images."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone

from scripts.customer_migration import ROOT, MigrationError, private_write, require
from scripts.collector_image import COLLECTOR_DIGEST, COLLECTOR_SOURCE
from scripts.runtime_revision import ANNOTATION, build_runtime_revision
from scripts.workflow_diagnostics import command_failure_summary


TARGET_TAG = re.compile(r"[a-z0-9][a-z0-9/._-]*:[A-Za-z0-9][A-Za-z0-9._-]*")
IMAGE_KINDS = {
    "backend": {"runtime": "azure", "tag": "backendTargetTag"},
    "proxy": {"runtime": "auth-proxy", "tag": "proxyTargetTag"},
    "collector": {"runtime": "collector", "tag": "collectorTargetTag"},
}


def regular_file(value, field, *, private=False):
    require(isinstance(value, str) and value, f"Configure localExecution.imageSigning.{field}")
    supplied = Path(value).expanduser()
    supplied = supplied if supplied.is_absolute() else ROOT / supplied
    require(not supplied.is_symlink(), f"localExecution.imageSigning.{field} cannot be a symlink")
    path = supplied.resolve()
    require(path.is_file(), f"localExecution.imageSigning.{field} must reference a regular file")
    if private:
        require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "Cosign private key must not be group or world accessible")
    return path


def signing_settings(settings, kind):
    require(kind in IMAGE_KINDS, "Unknown local image kind")
    value = settings.get("imageSigning")
    allowed = {"privateKeyPath", "publicKeyPath", "backendTargetTag", "proxyTargetTag", "collectorTargetTag"}
    require(isinstance(value, dict) and not set(value) - allowed, "localExecution.imageSigning is missing or contains unknown fields")
    private_key = regular_file(value.get("privateKeyPath"), "privateKeyPath", private=True)
    public_key = regular_file(value.get("publicKeyPath"), "publicKeyPath")
    require(private_key != public_key, "Cosign private and public key paths must differ")
    tag_field = IMAGE_KINDS[kind]["tag"]
    target_tag = value.get(tag_field)
    require(isinstance(target_tag, str) and TARGET_TAG.fullmatch(target_tag), f"Invalid localExecution.imageSigning.{tag_field}")
    password = os.environ.get("COSIGN_PASSWORD")
    require(bool(password), "Set a nonempty COSIGN_PASSWORD interactively; replace keys generated with an empty passphrase before image promotion")
    return {"privateKey": private_key, "publicKey": public_key, "targetTag": target_tag, "password": password, **IMAGE_KINDS[kind]}


def public_key_path(settings):
    value = settings.get("imageSigning")
    require(isinstance(value, dict), "Configure localExecution.imageSigning before publishing managed applications")
    return regular_file(value.get("publicKeyPath"), "publicKeyPath")


def promote_image(config, settings, revision, kind, directory, run=subprocess.run):
    selected = signing_settings(settings, kind)
    # Sign the digest of the reviewed runtime inputs, not the commit: unrelated commits keep the image valid.
    runtime_revision = build_runtime_revision(selected["runtime"], revision)
    registry = config["parameters"]["platform"]["containerRegistryName"]
    require(re.fullmatch(r"[a-zA-Z0-9]{5,50}", registry) is not None, "Invalid target ACR name")
    target_tag = selected["targetTag"]
    repository, tag = target_tag.rsplit(":", 1)
    tagged_image = f"{registry}.azurecr.io/{target_tag}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)

    def execute(arguments, label, *, environment=None, timeout=1200, record_stdout=True):
        try:
            result = run(arguments, capture_output=True, text=True, check=False, timeout=timeout, env=environment)
        except OSError:
            raise MigrationError(f"Local image command is unavailable: {label}") from None
        if record_stdout:
            private_write(directory / f"{label}.stdout.txt", result.stdout)
        private_write(directory / f"{label}.stderr.txt", result.stderr)
        safe_stdout = result.stdout if record_stdout else ""
        require(result.returncode == 0, f"Local image operation failed ({label}): {command_failure_summary(safe_stdout, result.stderr, result.returncode)}")
        return result.stdout

    login = execute([
        "az", "acr", "login", "--name", registry, "--expose-token",
        "--subscription", config["azure"]["subscriptionId"], "--output", "json", "--only-show-errors",
    ], "acr-login", record_stdout=False, timeout=120)
    try:
        credentials = json.loads(login)
    except ValueError:
        raise MigrationError("ACR login returned invalid JSON") from None
    require(credentials.get("loginServer", "").lower() == f"{registry}.azurecr.io".lower() and credentials.get("accessToken"), "ACR login returned unexpected credentials")
    runtime_parent = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
    credential_directory = tempfile.TemporaryDirectory(prefix="llmgw-acr-", dir=runtime_parent)
    docker_directory = Path(credential_directory.name)
    docker_directory.chmod(0o700)
    encoded = base64.b64encode(("00000000-0000-0000-0000-000000000000:" + credentials["accessToken"]).encode()).decode()
    docker_config = docker_directory / "config.json"
    private_write(docker_config, json.dumps({"auths": {credentials["loginServer"]: {"auth": encoded}}}))
    environment = {key: value for key, value in os.environ.items() if key != "COSIGN_PASSWORD"}
    environment["DOCKER_CONFIG"] = str(docker_directory)
    signing_environment = {**environment, "COSIGN_PASSWORD": selected["password"]}
    try:
        if kind == "backend":
            execute(["docker", "build", "--network", "none", "-f", str(ROOT / "LiteLLM/runtime/Dockerfile"), "-t", tagged_image, str(ROOT)], "image-build", environment=environment)
            execute(["docker", "push", tagged_image], "image-push", environment=environment)
        elif kind == "proxy":
            execute(["docker", "build", "-f", str(ROOT / "auth-proxy/Dockerfile"), "-t", tagged_image, str(ROOT / "auth-proxy")], "image-build", environment=environment)
            execute(["docker", "push", tagged_image], "image-push", environment=environment)
        else:
            execute([
                "az", "acr", "import", "--name", registry, "--source", COLLECTOR_SOURCE,
                "--image", target_tag, "--force", "--subscription", config["azure"]["subscriptionId"],
                "--output", "none", "--only-show-errors",
            ], "image-import", environment=environment)
        digest = execute([
            "az", "acr", "manifest", "list-metadata", "--registry", registry, "--name", repository,
            "--query", f"[?tags[?@=='{tag}']].digest | [0]", "--output", "tsv",
            "--subscription", config["azure"]["subscriptionId"], "--only-show-errors",
        ], "image-digest", environment=environment, timeout=120).strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None, "Target ACR did not return an immutable image digest")
        if kind == "collector":
            require(digest == COLLECTOR_DIGEST, "Imported collector digest differs from the reviewed source")
        require(build_runtime_revision(selected["runtime"], revision) == runtime_revision, "Runtime image inputs changed during promotion")
        image = f"{registry}.azurecr.io/{repository}@{digest}"
        execute(["docker", "pull", image], "image-pull", environment=environment)
        sbom = directory / "image-sbom.spdx.json"
        scan = directory / "image-scan.json"
        execute(["syft", image, "--output", f"spdx-json={sbom}"], "image-sbom", environment=environment)
        execute(["trivy", "image", "--exit-code", "1", "--severity", "CRITICAL", "--ignore-unfixed", "--format", "json", "--output", str(scan), image], "image-scan", environment=environment)
        annotations = [
            "-a", "llmgw.runtime=" + selected["runtime"],
            "-a", "llmgw.revision=" + revision,
            "-a", ANNOTATION + "=" + runtime_revision,
            "-a", "llmgw.environment=" + config["environment"],
        ]
        execute(["cosign", "sign", "--yes", "--key", str(selected["privateKey"]), *annotations, image], "image-sign", environment=signing_environment)
        verification_text = execute(["cosign", "verify", "--key", str(selected["publicKey"]), *annotations, image], "image-signature", environment=signing_environment)
        verification = json.loads(verification_text)
        require(isinstance(verification, list) and verification, "Cosign verification returned no signatures")
        for evidence in (sbom, scan):
            require(evidence.is_file() and evidence.stat().st_size > 0, "Image evidence file is missing")
        hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (sbom, scan)}
        private_write(directory / "image-signature.json", json.dumps(verification, indent=2) + "\n")
        hashes["image-signature.json"] = hashlib.sha256((directory / "image-signature.json").read_bytes()).hexdigest()
        summary = {
            "schemaVersion": 1,
            "check": "target_image_signature_sbom",
            "revision": revision,
            "runtimeRevision": runtime_revision,
            "environment": config["environment"],
            "image": image,
            "runtime": selected["runtime"],
            "signatureVerified": True,
            "publicKeySha256": hashlib.sha256(selected["publicKey"].read_bytes()).hexdigest(),
            "vulnerabilityPolicy": {"severity": ["CRITICAL"], "ignoreUnfixed": True},
            "evidenceSha256": hashes,
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "stageAccepted": False,
        }
        private_write(directory / "target-image-summary.json", json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        docker_config.unlink(missing_ok=True)
        credential_directory.cleanup()