"""Generate a live-verified native test/dev canary report without releasing traffic."""

import argparse
import json
import os
from pathlib import Path
import tempfile

from local_execution.runner import authenticate_azure, load_config, operation_root, reviewed_revision
from scripts.customer_migration import ROOT, MigrationError, approval_policy, private_write, require, stage_fingerprint
from scripts.edge_binding import deployed_edge
from scripts.migration_deploy import AzureCommands, deployment_name, resolve_origin
from scripts.stage9_release import validate_release
from scripts.workflow_diagnostics import diagnostic_exit


def generate_report(config, settings, revision, change_ticket, approvers, azure):
    require(config["environment"] in {"dev", "test"}, "Report generation is limited to dev/test canary")
    require(config.get("application", {}).get("authentication", {}).get("mode") == "native"
            and "contentAudit" in config, "Report generation requires native authentication and native audit")
    require(settings["features"]["allowTrafficRelease"] is True,
            "Review and apply approved-release configuration before generating the report")
    resolved = resolve_origin(config, "edge", azure)
    live = deployed_edge(resolved, azure)
    parameters = resolved["parameters"]["edge"]
    deployment = azure.scoped([
        "deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"],
        "--name", deployment_name(config, 9, "edge"),
        "--query", "{state:properties.provisioningState,parameters:properties.parameters}",
    ])
    require(deployment.get("state") == "Succeeded", "Edge deployment must be Succeeded")
    actual = deployment.get("parameters", {})
    for field, default in (("logAnalyticsWorkspaceName", None), ("rateLimitPerMinute", 600),
                           ("adminRateLimitPerMinute", 120)):
        require(actual.get(field, {}).get("value") == parameters.get(field, default),
                f"Deployed {field} differs from the current configuration")
    report = {
        "phase": "canary",
        "authenticationMode": "native",
        "auditMode": "native",
        "telemetryEnabled": "observability" in config,
        "environmentName": config["environment"],
        "baseDomain": config["baseDomain"],
        "privateOrigin": live["privateOrigin"],
        "adminPrivateOrigin": live["adminPrivateOrigin"],
        "frontDoorId": live["frontDoorId"],
        "adminAllowedCidrs": parameters["adminAllowedCidrs"],
        "logAnalyticsWorkspaceName": parameters["logAnalyticsWorkspaceName"],
        "rateLimitPerMinute": parameters.get("rateLimitPerMinute", 600),
        "adminRateLimitPerMinute": parameters.get("adminRateLimitPerMinute", 120),
        "wafMode": "Detection",
        "changeTicket": change_ticket,
        "approvedBy": approvers,
        "revision": revision,
        "configSha256": stage_fingerprint(config, 9),
    }
    if "l3Audit" in config:
        report["optionalL3Enabled"] = True
    mode, eligible = approval_policy(config)
    count = 1 if mode == "single-operator" else 2
    require(len(approvers) == count, "Approval count differs from the configured policy")
    validate_release(report, required_approvers=count, eligible_approvers=eligible)
    return report


def report_path(settings):
    value = settings.get("releaseReportPath")
    require(isinstance(value, str) and value, "Configure localExecution.releaseReportPath")
    supplied = Path(value).expanduser()
    supplied = supplied if supplied.is_absolute() else ROOT / supplied
    require(not supplied.is_symlink(), "Release report cannot be a symlink")
    target = supplied.resolve()
    require(target.is_relative_to((ROOT / "temp").resolve())
            and target != (ROOT / "temp").resolve(), "Release report must stay under ignored temp/")
    require(not target.exists() or target.is_file(), "Release report must be a regular file")
    return target


def install_report(report, target, directory, replace=False):
    require(not target.is_symlink(), "Release report cannot be a symlink")
    if target.exists():
        require(replace, "Release report exists; review it and use --replace to back it up and replace it")
        private_write(directory / "previous-release.json", target.read_text())
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".release-", suffix=".json", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--change-ticket", required=True, help="Actual approved change reference")
    parser.add_argument("--approved-by", action="append", required=True,
                        help="Actual approver object ID; repeat for each required approver")
    parser.add_argument("--replace", action="store_true", help="Back up and replace an existing report")
    args = parser.parse_args()
    config, settings = load_config(args.config)
    target = report_path(settings)
    require(args.replace or not target.exists(), "Report exists; review it before using --replace")
    revision = reviewed_revision()
    directory = operation_root(ROOT / "temp/local-stage09", "stage9-release-report")
    authenticate_azure(config, settings, "deploy", directory)
    report = generate_report(config, settings, revision, args.change_ticket, args.approved_by,
                             AzureCommands(config, directory))
    require(reviewed_revision() == revision and load_config(args.config) == (config, settings),
            "Code or configuration changed during report generation; retry")
    install_report(report, target, directory, args.replace)
    print(json.dumps({"releaseReportPath": str(target.relative_to(ROOT)),
                      "outputDirectory": str(directory.relative_to(ROOT)),
                      "revision": revision, "stageConfigSha256": report["configSha256"],
                      "deploymentPerformed": False, "trafficReleasePerformed": False}))


def cli():
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-stage09-release-report")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-stage09-release-report",
                                        "Release report generation failed")) from None


if __name__ == "__main__":
    cli()
