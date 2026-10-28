"""Run implemented local contracts; never certify customer migration or call cloud APIs."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone

from scripts.customer_migration import ROOT, fingerprint, private_write, require
from scripts.source_supply_chain import source_image

DELIVERY_BLOCKERS = (
    "Private runner adapters are locally tested; VM/toolchain provisioning, workflow scheduling, orphan cleanup and effective permissions remain incomplete or unaccepted",
    "Customer client token refresh and all required protocols remain unaccepted",
    "Native UI core log reading is locally tested; full management writes, auxiliary UI compatibility, mobile access, credential migration and retention/recovery remain incomplete or unaccepted",
    "Actual GitHub approval binding and automatic technical stage acceptance are incomplete",
    "Legacy exposure and credential lifecycle operations remain incomplete",
    "Locked 1.95.0 to 1.104.0 synthetic restore/upgrade is tested (1.104.8 ships the same 189 migrations; rerun pending); final write freeze/synchronization, HA/PITR, customer-sized downtime and data-aware rollback remain incomplete or unaccepted",
)


def source_state():
    def git(*arguments):
        return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, check=True).stdout
    files = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    digests = {}
    for name in sorted(set(files) - {""}):
        path = ROOT / name
        require(not path.is_symlink(), "Local rehearsal refuses source symlinks")
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "deleted"
    return {"revision": git("rev-parse", "HEAD").decode().strip(), "dirty": bool(git("status", "--porcelain")), "sourceTreeSha256": fingerprint(digests)}


def python_worker(kind):
    if kind == "unit":
        modules = ["tests." + path.stem for path in sorted((ROOT / "tests").glob("test_*.py"))]
    else:
        os.environ.update(RUN_PRIVATE_INGRESS_CONTAINER_TESTS="1", RUN_DATABASE_ROLES_CONTAINER_TESTS="1", RUN_AZURE_SCHEMA_CONTAINER_TESTS="1", RUN_COLLECTOR_CONTAINER_TESTS="1", RUN_NATIVE_UI_BROWSER_TESTS="1")
        modules = ["tests.test_private_ingress_container", "tests.test_database_roles_container", "tests.test_azure_schema_container", "tests.test_observability", "tests.test_certificate_runtime"]
    output = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(stream=output, buffer=True).run(suite)
    report = {"testsRun": result.testsRun, "failedTests": [case.id() for case, _ in result.failures + result.errors], "skippedTests": [{"test": case.id(), "reason": reason} for case, reason in result.skipped]}
    print(json.dumps(report))
    return 0 if result.wasSuccessful() else 1


def checks(profile, revision):
    result = [
        {"id": "python-unit", "command": [sys.executable, "-m", "scripts.local_rehearsal", "--worker", "unit"], "tools": [], "structured": True},
        {"id": "node-proxy", "command": ["npm", "test", "--prefix", "auth-proxy"], "tools": ["npm", "node"]},
        {"id": "workflow-lint", "command": ["go", "run", "github.com/rhysd/actionlint/cmd/actionlint@v1.7.7", *[str(path.relative_to(ROOT)) for path in sorted((ROOT / ".github/workflows").glob("*.yml"))]], "tools": ["go"]},
        {"id": "public-config", "command": [sys.executable, "-m", "scripts.validate_public_config"], "tools": []},
    ]
    if profile == "full":
        image = source_image()
        result.extend([
            {"id": "stage3-to-stage9", "command": ["bash", "scripts/validate-stage9.sh"], "tools": ["az", "kubectl", "node", "npm"]},
            {"id": "isolated-runtime", "command": [sys.executable, "-m", "scripts.local_rehearsal", "--worker", "containers"], "tools": ["docker", "openssl", "node", os.environ.get("BROWSER_EXECUTABLE_PATH", "/usr/bin/google-chrome")], "structured": True, "noSkips": True},
            {"id": "locked-prisma", "command": ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=128m", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "LITELLM_LOCAL_MODEL_COST_MAP=True", "--env", "RUN_AZURE_PRISMA_CONTRACT_TESTS=1", "--mount", f"type=bind,src={ROOT},dst=/workspace,readonly", "--workdir", "/workspace", "--entrypoint", "/app/.venv/bin/python", image, "-m", "unittest", "-b", "tests.test_azure_postgresql", "tests.test_azure_postgresql_prisma"], "tools": ["docker"]},
            {"id": "oss-callbacks", "command": ["bash", "scripts/validate-oss-callbacks.sh"], "tools": ["docker"]},
            {"id": "azure-runtime-build", "command": ["docker", "build", "--network", "none", "-f", "LiteLLM/runtime/Dockerfile", "-t", "litellm-azure-runtime:local-rehearsal", "."], "tools": ["docker"]},
            {"id": "auth-proxy-build", "command": ["docker", "build", "-f", "auth-proxy/Dockerfile", "-t", "llmgw-auth-proxy:local-rehearsal", "auth-proxy"], "tools": ["docker"]},
            {"id": "source-supply-chain", "command": [sys.executable, "-m", "scripts.source_supply_chain", "--revision", revision], "tools": ["docker", "syft", "trivy"]},
        ])
    return result


def run_check(spec, environment, run=subprocess.run):
    missing = [name for name in spec["tools"] if shutil.which(name, path=environment.get("PATH")) is None]
    result = {"id": spec["id"], "status": "blocked", "command": spec["command"]}
    if missing:
        return {**result, "reason": "Missing required tools", "missingTools": missing}
    started = time.monotonic()
    try:
        completed = run(spec["command"], cwd=ROOT, env=environment, capture_output=True, text=True, check=False, timeout=2400)
        result.update(status="passed" if completed.returncode == 0 else "failed", exitCode=completed.returncode,
                      outputSha256=hashlib.sha256((completed.stdout + completed.stderr).encode()).hexdigest())
        if spec.get("structured"):
            summary = json.loads(completed.stdout)
            require(isinstance(summary.get("testsRun"), int) and summary["testsRun"] > 0, "Empty test suite")
            require(isinstance(summary.get("failedTests"), list) and isinstance(summary.get("skippedTests"), list), "Invalid test result")
            result["tests"] = summary
            if summary["failedTests"]:
                result["status"] = "failed"
            elif spec.get("noSkips") and summary["skippedTests"]:
                result.update(status="blocked", reason="Required isolated tests were skipped")
    except subprocess.TimeoutExpired:
        result.update(status="failed", reason="Local check timed out; inspect isolated runtime cleanup")
    except (OSError, ValueError):
        result.update(status="failed", reason="Check could not execute or returned invalid results")
    result["durationSeconds"] = round(time.monotonic() - started, 2)
    return result


def summary_report(profile, state, results, after):
    stable = state["sourceTreeSha256"] == after["sourceTreeSha256"] and state["revision"] == after["revision"]
    expected = [item["id"] for item in checks(profile, state["revision"])]
    complete = [item["id"] for item in results] == expected
    status = "failed" if not stable or any(item["status"] == "failed" for item in results) else "blocked" if any(item["status"] == "blocked" for item in results) else "passed"
    if not complete and status == "passed":
        status = "incomplete"
    return {"schemaVersion": 1, "scope": "implemented-local-contracts-only", "profile": profile, "status": status,
            "observedAt": datetime.now(timezone.utc).isoformat(), **state, "sourceUnchangedDuringRun": stable,
            "runComplete": complete, "expectedChecks": expected,
            "stageAccepted": False, "customerEnvironmentVerified": False, "readyForCustomerMigration": False,
            "checks": results, "deliveryBlockers": list(DELIVERY_BLOCKERS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    parser.add_argument("--worker", choices=("unit", "containers"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/local-rehearsal")
    args = parser.parse_args()
    if args.worker:
        return python_worker(args.worker)
    directory = args.output_dir.resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Report must stay under ignored temp/")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    state = source_state()
    environment = {name: value for name, value in os.environ.items() if not name.startswith(("AZURE_", "CUSTOMER_", "MIGRATION_", "RUN_")) and name not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    environment["PYTHON_BIN"] = sys.executable
    results = []
    for spec in checks(args.profile, state["revision"]):
        print("Running " + spec["id"], flush=True)
        results.append(run_check(spec, environment))
        private_write(directory / "report.json", json.dumps(summary_report(args.profile, state, results, source_state()), indent=2))
        print(spec["id"] + ": " + results[-1]["status"], flush=True)
    report = summary_report(args.profile, state, results, source_state())
    private_write(directory / "report.json", json.dumps(report, indent=2))
    text = "# Local Rehearsal\n\nScope: implemented local contracts only. Customer migration is NOT accepted.\n\n"
    text += f"Revision: {state['revision']}\n\nSource tree: {state['sourceTreeSha256']}\n\nResult: {report['status']}\n\n"
    text += "| Check | Result |\n| --- | --- |\n" + "".join(f"| {item['id']} | {item['status']} |\n" for item in results)
    text += "\n## Delivery Blockers\n\n" + "".join("- " + item + "\n" for item in DELIVERY_BLOCKERS)
    private_write(directory / "report.md", text)
    print(json.dumps({"status": report["status"], "report": str(directory / "report.json"), "readyForCustomerMigration": False}))
    return {"passed": 0, "failed": 1, "blocked": 2, "incomplete": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())