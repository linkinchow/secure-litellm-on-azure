"""Add exact public Admin source IPs to a live native dev/test canary."""

import argparse
import copy
import ipaddress
import json
from pathlib import Path
import subprocess
import sys

from local_execution.release_report import generate_report, install_report, report_path
from local_execution.runner import (
    authenticate_azure, execute_infrastructure_plan, load_config, operation_root, reviewed_revision,
)
from scripts.customer_migration import ROOT, MigrationError, admin_source_cidrs, private_write, require, stage_fingerprint
from scripts.edge_binding import bind_edge
from scripts.migration_deploy import AzureCommands, deploy_component
from scripts.workflow_diagnostics import diagnostic_exit


def progress(message):
    print(f"Admin allowlist: {message}", file=sys.stderr, flush=True)


def desired_configuration(config, additions):
    desired = copy.deepcopy(config)
    before = config["parameters"]["edge"]["adminAllowedCidrs"]
    values = list(before)
    for value in additions:
        network = ipaddress.ip_network(value, strict=True)
        require(network.prefixlen == network.max_prefixlen,
                "Only exact public source IPs are accepted: IPv4 /32 or IPv6 /128")
        canonical = str(network)
        require(canonical not in values, "CIDR is already configured; do not repeat an update")
        values.append(canonical)
    desired["parameters"]["edge"]["adminAllowedCidrs"] = admin_source_cidrs(values)
    return desired


def refresh_binding(config, revision, directory, azure):
    directory.mkdir(mode=0o700)
    progress("Checking existing ingress bindings; no route or Pod template changes are allowed.")
    preview = bind_edge(config, "plan", revision, directory, "", azure=azure)
    document = json.loads((directory / "runtime-review.json").read_text())
    require(document.get("bindingMode") == "native-private-ingress", "Native binding required")
    for plane in ("api", "admin"):
        state = document["planes"][plane]
        require(state["currentRouteConfigSha256"] == state["desiredRouteConfigSha256"]
                and state["currentPodTemplateSha256"] == state["desiredPodTemplateSha256"],
                "Allowlist receipt refresh must not change live ingress bindings")
    progress("Verifying API/Admin ingress rollouts and saving the binding receipt; each rollout may wait up to 15 minutes.")
    return bind_edge(config, "execute", revision, directory, preview["planSha256"], azure=azure)


def update(config_path, additions, ticket, approvers, operation, approved):
    require(not config_path.is_symlink(), "Customer configuration cannot be a symlink")
    config, settings = load_config(config_path)
    from scripts.app_gateway import require_front_door_edge
    require_front_door_edge(config, "The Admin allowlist update tool")
    require(config_path.resolve().is_relative_to(ROOT.resolve()),
            "Customer configuration must be inside the reviewed checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(config_path.resolve())],
        cwd=ROOT, check=False,
    )
    require(ignored.returncode == 0, "Customer configuration must be Git-ignored")
    target = report_path(settings)
    revision = reviewed_revision()
    desired = desired_configuration(config, additions)
    directory = operation_root(ROOT / "temp/local-stage09", "stage9-admin-allowlist")
    progress(f"Validating Azure identity, live baseline and What-if. Output: {directory.relative_to(ROOT)}")
    authenticate_azure(config, settings, "deploy", directory)
    azure = AzureCommands(config, directory)
    report = generate_report(config, settings, revision, ticket, approvers, azure)
    report["adminAllowedCidrs"] = desired["parameters"]["edge"]["adminAllowedCidrs"]
    report["configSha256"] = stage_fingerprint(desired, 9)
    plan_directory = directory / "plan"
    plan = deploy_component(desired, 9, "edge", revision, "plan", [], plan_directory,
                            azure=azure, release=report, allowlist_baseline=config)
    document = json.loads(config_path.read_text())
    document["parameters"]["edge"]["adminAllowedCidrs"] = report["adminAllowedCidrs"]
    private_write(directory / "desired-customer.json", json.dumps(document, indent=2) + "\n")
    private_write(directory / "desired-release.json", json.dumps(report, indent=2) + "\n")
    summary = {"planSha256": plan["planSha256"], "outputDirectory": str(directory.relative_to(ROOT)),
               "adminAllowedCidrs": report["adminAllowedCidrs"], "deploymentPerformed": False,
               "bindingRefreshed": False, "status": "planned"}
    print(json.dumps(summary), flush=True)
    if operation == "plan":
        return summary
    require(plan["planSha256"] == approved, "Allowlist plan changed or was not approved; replan")
    require(reviewed_revision() == revision and load_config(config_path) == (config, settings),
            "Code or customer configuration changed during planning; replan")
    private_write(directory / "previous-customer.json", config_path.read_text())
    summary["status"] = "executing"
    private_write(directory / "allowlist-summary.json", json.dumps(summary, indent=2) + "\n")
    try:
        progress("Approved plan verified. Deploying Admin WAF; waiting for Azure. No terminal confirmation is required.")
        execute_infrastructure_plan(desired, 9, "edge", plan, plan_directory, directory / "execute")
        summary["deploymentPerformed"] = True
        private_write(directory / "allowlist-summary.json", json.dumps(summary, indent=2) + "\n")
        progress("Admin WAF deployment succeeded. Saving customer configuration and release report.")
        install_report(document, config_path, directory / "execute", replace=True)
        install_report(report, target, directory, replace=True)
        progress("Configuration and report saved. Refreshing the binding receipt; temporary kubeconfig overwrite is automatic.")
        refresh_binding(desired, revision, directory / "binding", azure)
        summary["bindingRefreshed"] = True
        summary["status"] = "completed"
    except Exception:
        summary["status"] = "failed"
        raise
    finally:
        private_write(directory / "allowlist-summary.json", json.dumps(summary, indent=2) + "\n")
    progress("Completed. Allow time for WAF edge propagation before public access verification.")
    print(json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--add-cidr", action="append", required=True)
    parser.add_argument("--change-ticket", required=True)
    parser.add_argument("--approved-by", action="append", required=True)
    parser.add_argument("--operation", choices=("plan", "execute"), required=True)
    parser.add_argument("--approved-plan-sha256", default="")
    args = parser.parse_args()
    update(args.config, args.add_cidr, args.change_ticket, args.approved_by,
           args.operation, args.approved_plan_sha256)


def cli():
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-admin-allowlist")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-admin-allowlist",
                                        "Admin allowlist update failed; inspect its output directory before retrying")) from None


if __name__ == "__main__":
    cli()
