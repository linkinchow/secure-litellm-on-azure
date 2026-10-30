import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_execution.release_report import generate_report, install_report, main, report_path
from scripts.customer_migration import MigrationError, stage_fingerprint
from tests.test_customer_migration import customer_config


class ReleaseReportTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.config["application"] = {"authentication": {"mode": "native"}}
        self.config["contentAudit"] = {}
        self.owner = "11111111-1111-4111-8111-111111111111"
        self.config["governance"] = {
            "approvalMode": "single-operator", "singleOperatorRiskAccepted": True,
            "approverObjectIds": [self.owner],
        }
        self.settings = {"features": {"allowTrafficRelease": True}}
        parameters = self.config["parameters"]["edge"]
        self.live = {
            "privateOrigin": parameters["privateOrigin"],
            "adminPrivateOrigin": parameters["adminPrivateOrigin"],
            "frontDoorId": "22222222-2222-4222-8222-222222222222",
        }
        self.azure = Mock()
        self.azure.scoped.return_value = {
            "state": "Succeeded",
            "parameters": {
                "logAnalyticsWorkspaceName": {"value": "target-logs"},
                "rateLimitPerMinute": {"value": 600},
                "adminRateLimitPerMinute": {"value": 120},
                "modelListRateLimitPerMinute": {"value": 120},
            },
        }

    def generate(self, **kwargs):
        with patch("local_execution.release_report.resolve_origin", return_value=copy.deepcopy(self.config)), \
                patch("local_execution.release_report.deployed_edge", return_value=self.live) as live:
            result = generate_report(self.config, self.settings, "a" * 40,
                                     kwargs.get("ticket", "approved-test-change"),
                                     kwargs.get("owners", [self.owner]), self.azure)
            live.assert_called_once()
            return result

    def test_generates_current_report_without_mutating_configuration(self):
        previous = copy.deepcopy(self.config)
        report = self.generate()
        self.assertEqual(self.config, previous)
        self.assertEqual(report["configSha256"], stage_fingerprint(self.config, 9))
        self.assertEqual(report["revision"], "a" * 40)
        self.assertEqual(report["frontDoorId"], self.live["frontDoorId"])
        self.assertEqual(report["privateOrigin"], self.live["privateOrigin"])
        self.assertEqual(report["logAnalyticsWorkspaceName"], "target-logs")
        self.assertEqual(report["modelListRateLimitPerMinute"], 120)
        self.assertFalse(report["telemetryEnabled"])
        self.assertNotIn("checks", report)
        self.assertEqual(self.azure.scoped.call_args.args[0][:3], ["deployment", "group", "show"])

    def test_reads_telemetry_decision(self):
        self.config["observability"] = {}
        self.assertTrue(self.generate()["telemetryEnabled"])

    def test_rejects_wrong_scope_auth_and_disabled_release(self):
        for field, value in (("environment", "prod"), ("contentAudit", None), ("application", {})):
            with self.subTest(field=field):
                previous = self.config
                self.config = copy.deepcopy(previous)
                if value is None:
                    self.config.pop(field)
                else:
                    self.config[field] = value
                with self.assertRaises(MigrationError):
                    self.generate()
                self.config = previous
        self.settings["features"]["allowTrafficRelease"] = False
        with self.assertRaises(MigrationError):
            self.generate()

    def test_rejects_deployment_failure_and_parameter_drift(self):
        self.azure.scoped.return_value["state"] = "Failed"
        with self.assertRaisesRegex(MigrationError, "Succeeded"):
            self.generate()
        self.azure.scoped.return_value["state"] = "Succeeded"
        for field in ("logAnalyticsWorkspaceName", "rateLimitPerMinute", "adminRateLimitPerMinute", "modelListRateLimitPerMinute"):
            with self.subTest(field=field):
                actual = self.azure.scoped.return_value["parameters"][field]["value"]
                self.azure.scoped.return_value["parameters"][field]["value"] = "old-value"
                with self.assertRaisesRegex(MigrationError, field):
                    self.generate()
                self.azure.scoped.return_value["parameters"][field]["value"] = actual

    def test_optional_l3_alert_state_comes_from_the_l3_deployment(self):
        self.config["l3Audit"] = {"enabled": True}
        edge = copy.deepcopy(self.azure.scoped.return_value)
        for alerts, expected in (("", False), ("/subscriptions/x/resourceGroups/rg/providers/microsoft.insights/actionGroups/l3", True)):
            with self.subTest(alerts=alerts):
                self.azure.scoped.side_effect = [edge, {"state": "Succeeded", "alerts": alerts}]
                report = self.generate()
                self.assertTrue(report["optionalL3Enabled"])
                self.assertIs(report["optionalL3AlertsEnabled"], expected)
                self.assertEqual(self.azure.scoped.call_args.args[0][6], "optional-l3")
        self.azure.scoped.side_effect = [edge, {"state": "Failed", "alerts": ""}]
        with self.assertRaisesRegex(MigrationError, "Optional L3 deployment"):
            self.generate()
        self.azure.scoped.side_effect = None

    def test_prod_report_requires_passing_evidence_for_every_check(self):
        self.config["environment"] = "prod"
        with self.assertRaisesRegex(MigrationError, "evidence-dir"):
            self.generate()
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            def run():
                with patch("local_execution.release_report.resolve_origin", return_value=copy.deepcopy(self.config)),                         patch("local_execution.release_report.deployed_edge", return_value=self.live):
                    return generate_report(self.config, self.settings, "a" * 40, "approved-test-change", [self.owner], self.azure, evidence)
            with self.assertRaisesRegex(MigrationError, "Missing evidence file"):
                run()
            from scripts.stage9_release import release_checks
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            names = release_checks({"phase": "canary", "authenticationMode": "native", "auditMode": "native", "telemetryEnabled": False, "environmentName": "prod"})
            self.assertIn("database_restore", names)
            for name in names:
                (evidence / f"{name}.json").write_text(json.dumps({"check": name, "passed": True, "observedAt": now}))
            report = run()
            self.assertEqual(set(report["checks"]), names)
            self.assertTrue(all(item["passed"] and item["observedAt"] == now for item in report["checks"].values()))
            (evidence / "database_restore.json").write_text(json.dumps({"check": "database_restore", "passed": False, "observedAt": now}))
            with self.assertRaisesRegex(MigrationError, "did not pass: database_restore"):
                run()
            (evidence / "database_restore.json").write_text(json.dumps({"check": "database_restore", "passed": True, "observedAt": "2020-01-01T00:00:00+00:00"}))
            with self.assertRaisesRegex(ValueError, "Expired"):
                run()

    def test_requires_explicit_valid_approval(self):
        with self.assertRaises(ValueError):
            self.generate(ticket="REPLACE_TICKET")
        with self.assertRaisesRegex(MigrationError, "count"):
            self.generate(owners=[])
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.generate(owners=["33333333-3333-4333-8333-333333333333"])

    def test_install_is_private_and_requires_explicit_replacement(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "report.json"
            report = self.generate()
            install_report(report, target, root)
            self.assertEqual(json.loads(target.read_text()), report)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaisesRegex(MigrationError, "exists"):
                install_report({"new": True}, target, root)
            install_report({"new": True}, target, root, replace=True)
            self.assertEqual(json.loads((root / "previous-release.json").read_text()), report)
            self.assertEqual(stat.S_IMODE((root / "previous-release.json").stat().st_mode), 0o600)
            self.assertEqual(json.loads(target.read_text()), {"new": True})
            self.assertEqual(list(root.glob(".release-*")), [])

    def test_paths_reject_public_locations_and_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch("local_execution.release_report.ROOT", root):
                self.assertEqual(report_path({"releaseReportPath": "temp/private/report.json"}),
                                 root / "temp/private/report.json")
                with self.assertRaises(MigrationError):
                    report_path({"releaseReportPath": "report.json"})
                (root / "temp").mkdir()
                (root / "temp/link.json").symlink_to(root / "outside.json")
                with self.assertRaisesRegex(MigrationError, "symlink"):
                    report_path({"releaseReportPath": "temp/link.json"})

    def test_cli_authenticates_reads_and_installs_without_deploying(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "temp").mkdir()
            directory = root / "temp/run"
            directory.mkdir()
            target = root / "temp/report.json"
            settings = {**self.settings, "releaseReportPath": str(target)}
            with patch("sys.argv", ["release_report", "--config", "customer.json",
                                    "--change-ticket", "approved-change", "--approved-by", self.owner]), \
                    patch("local_execution.release_report.ROOT", root), \
                    patch("local_execution.release_report.load_config", return_value=(self.config, settings)), \
                    patch("local_execution.release_report.reviewed_revision", return_value="a" * 40), \
                    patch("local_execution.release_report.operation_root", return_value=directory), \
                    patch("local_execution.release_report.authenticate_azure") as authenticate, \
                    patch("local_execution.release_report.AzureCommands", return_value=self.azure), \
                    patch("local_execution.release_report.generate_report", return_value=self.generate()) as generate, \
                    patch("builtins.print") as output:
                main()
            authenticate.assert_called_once_with(self.config, settings, "deploy", directory)
            generate.assert_called_once_with(self.config, settings, "a" * 40,
                                              "approved-change", [self.owner], self.azure, None)
            summary = json.loads(output.call_args.args[0])
            self.assertFalse(summary["deploymentPerformed"])
            self.assertFalse(summary["trafficReleasePerformed"])
            self.assertTrue(target.is_file())

    def test_live_validation_failure_is_not_swallowed(self):
        with patch("local_execution.release_report.resolve_origin", return_value=self.config), \
                patch("local_execution.release_report.deployed_edge",
                      side_effect=MigrationError("PLS connection not approved")):
            with self.assertRaisesRegex(MigrationError, "not approved"):
                generate_report(self.config, self.settings, "a" * 40,
                                "approved-change", [self.owner], self.azure)


if __name__ == "__main__":
    unittest.main()
