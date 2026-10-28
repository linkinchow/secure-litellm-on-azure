import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import urlsplit

from scripts import model_account_network as network
from scripts.customer_migration import ROOT, MigrationError


SUB = "7a1b2c3d-0000-4000-8000-000000000001"
EGRESS = "20.62.10.5"


def account(number, **properties):
    identity = f"/subscriptions/{SUB}/resourceGroups/rg-aoai-{number}/providers/Microsoft.CognitiveServices/accounts/api{number}-resource"
    base = {"provisioningState": "Succeeded", "customSubDomainName": f"api{number}-resource",
            "publicNetworkAccess": "Enabled", "networkAcls": {"defaultAction": "Allow", "ipRules": [], "virtualNetworkRules": []},
            "privateEndpointConnections": []}
    if "disableLocalAuth" not in properties:
        base["disableLocalAuth"] = False
    base.update(properties)
    return {"id": identity, "name": f"api{number}-resource", "kind": "OpenAI", "location": "eastus", "properties": base}


class FakeAzure:
    def __init__(self, *resources):
        self.resources = {item["id"].lower(): copy.deepcopy(item) for item in resources}
        self.calls = []
        self.lag = self.pending = 0

    def ids(self):
        return [item["id"] for item in self.resources.values()]

    def run(self, arguments):
        self.calls.append(arguments)
        if arguments[:2] == ["resource", "show"]:
            resource = self.resources[arguments[arguments.index("--ids") + 1].lower()]
            if self.pending:
                self.pending -= 1
                return {**copy.deepcopy(resource), "properties": {**resource["properties"], "provisioningState": "Accepted"}}
            return copy.deepcopy(resource)
        if arguments[:3] == ["rest", "--method", "patch"]:
            url = urlsplit(arguments[arguments.index("--url") + 1])
            assert url.query == "api-version=" + network.API_VERSION and url.netloc == "management.azure.com"
            resource = self.resources[url.path.lower()]
            resource["properties"].update(json.loads(arguments[arguments.index("--body") + 1])["properties"])
            self.pending = self.lag
            return copy.deepcopy(resource)
        raise AssertionError("unexpected az call " + " ".join(arguments))

    def writes(self):
        return [call for call in self.calls if call[0] != "resource"]


class NetworkCase(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "temp"))

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def plan(self, azure, directory="plan", **options):
        return network.run(azure, self.root / directory, identities=options.pop("identities", azure.ids()),
                           egress=options.pop("egress", [EGRESS]), sleep=lambda _: None, **options)

    def execute(self, azure, approved, directory="plan", **options):
        return self.plan(azure, directory, execute_plan=True, approved=approved, **options)


class ApplyTests(NetworkCase):
    def test_plan_reads_only_records_snapshot_and_execute_applies_approved_plan(self):
        azure = FakeAzure(account(1), account(2, disableLocalAuth=True, networkAcls={"defaultAction": "Deny", "ipRules": [{"value": EGRESS + "/32"}], "virtualNetworkRules": []}))
        summary = self.plan(azure)
        self.assertEqual(summary["refusals"], [])
        self.assertEqual(list(summary["operations"].values()), ["update", "keep"])
        self.assertEqual(azure.writes(), [])
        snapshot = json.loads((self.root / "plan" / network.SNAPSHOT).read_text())
        self.assertEqual(snapshot["planSha256"], summary["planSha256"])
        self.assertEqual(snapshot["accounts"], [{"accountResourceId": azure.ids()[0], "previous": {
            "publicNetworkAccess": "Enabled", "disableLocalAuth": False,
            "networkAcls": {"defaultAction": "Allow", "ipRules": [], "virtualNetworkRules": []}}}])
        if os.name == "posix":
            self.assertEqual((self.root / "plan" / network.SNAPSHOT).stat().st_mode & 0o777, 0o600)

        with self.assertRaisesRegex(MigrationError, "not approved"):
            self.execute(azure, "0" * 64)
        self.assertEqual(azure.writes(), [])
        azure.lag = 1
        result = self.execute(azure, summary["planSha256"])
        self.assertTrue(result["applied"])
        self.assertEqual(len(azure.writes()), 1)
        properties = azure.resources[azure.ids()[0].lower()]["properties"]
        self.assertEqual((properties["publicNetworkAccess"], properties["disableLocalAuth"]), ("Enabled", True))
        self.assertEqual(properties["networkAcls"], {"defaultAction": "Deny", "ipRules": [{"value": EGRESS}], "virtualNetworkRules": []})
        receipt = json.loads((self.root / "plan" / "execute-receipt.json").read_text())
        self.assertTrue(receipt["complete"])
        # Replanning is idempotent and never overwrites the original snapshot.
        again = self.plan(azure)
        self.assertEqual(set(again["operations"].values()), {"keep"})
        self.assertEqual(json.loads((self.root / "plan" / network.SNAPSHOT).read_text()), snapshot)

    def test_drift_after_review_blocks_execute(self):
        azure = FakeAzure(account(1))
        summary = self.plan(azure)
        azure.resources[azure.ids()[0].lower()]["properties"]["disableLocalAuth"] = True
        with self.assertRaisesRegex(MigrationError, "changed since review"):
            self.execute(azure, summary["planSha256"])
        self.assertEqual(azure.writes(), [])

    def test_execute_requires_the_plan_snapshot_in_the_same_directory(self):
        azure = FakeAzure(account(1))
        summary = self.plan(azure)
        with self.assertRaisesRegex(MigrationError, "rollback snapshot matches"):
            self.execute(azure, summary["planSha256"], directory="elsewhere")
        self.assertEqual(azure.writes(), [])

    def test_refusals_and_explicit_overrides(self):
        vnet = {"defaultAction": "Deny", "ipRules": [], "virtualNetworkRules": [{"id": "/subscriptions/x/resourceGroups/r/providers/Microsoft.Network/virtualNetworks/v/subnets/s"}]}
        cases = [(account(1, privateEndpointConnections=[{"id": "pe-1"}]), "private endpoint", {"allow_private_endpoints": True}),
                 (account(2, networkAcls=vnet), "virtualNetworkRules", {"clear_virtual_network_rules": True}),
                 (account(3, networkAcls={"defaultAction": "Deny", "ipRules": [{"value": "52.1.2.3"}]}), "allows other IPs", {"remove_ip_rules": True}),
                 (account(4, publicNetworkAccess="Disabled"), "publicNetworkAccess Disabled", {"enable_public_access": True})]
        for resource, pattern, override in cases:
            with self.subTest(pattern=pattern):
                azure = FakeAzure(resource)
                summary = self.plan(azure, directory=pattern.replace(" ", "-"))
                self.assertTrue(any(pattern in item for item in summary["refusals"]), summary["refusals"])
                self.assertFalse((self.root / pattern.replace(" ", "-") / network.SNAPSHOT).exists())
                with self.assertRaisesRegex(MigrationError, "refusals"):
                    self.execute(azure, summary["planSha256"], directory=pattern.replace(" ", "-"))
                approved = self.plan(azure, directory=pattern.replace(" ", "-") + "-ok", **override)
                self.assertEqual(approved["refusals"], [])
                self.execute(azure, approved["planSha256"], directory=pattern.replace(" ", "-") + "-ok", **override)
                acls = azure.resources[resource["id"].lower()]["properties"]["networkAcls"]
                self.assertEqual((acls["defaultAction"], acls["ipRules"]), ("Deny", [{"value": EGRESS}]))
                if "privateEndpointConnections" in resource["properties"] and resource["properties"]["privateEndpointConnections"]:
                    self.assertEqual(azure.resources[resource["id"].lower()]["properties"]["privateEndpointConnections"], [{"id": "pe-1"}])

    def test_keep_local_auth_and_bypass_are_preserved(self):
        azure = FakeAzure(account(1, networkAcls={"defaultAction": "Allow", "bypass": "AzureServices"}))
        summary = self.plan(azure, keep_local_auth=True)
        self.execute(azure, summary["planSha256"], keep_local_auth=True)
        properties = azure.resources[azure.ids()[0].lower()]["properties"]
        self.assertFalse(properties["disableLocalAuth"])
        self.assertEqual(properties["networkAcls"]["bypass"], "AzureServices")

    def test_input_validation(self):
        azure = FakeAzure(account(1))
        for egress in (["10.0.0.4"], ["2001:db8::1"], ["20.0.0.0/16"], [], [EGRESS, EGRESS + "/32"]):
            with self.subTest(egress=egress), self.assertRaises(MigrationError):
                self.plan(azure, egress=egress)
        for identities in (["/subscriptions/x/resourceGroups/r/providers/Microsoft.Storage/storageAccounts/a"], azure.ids() * 2, []):
            with self.subTest(identities=identities), self.assertRaises(MigrationError):
                self.plan(azure, identities=identities)
        azure.resources[azure.ids()[0].lower()]["kind"] = "TextAnalytics"
        with self.assertRaisesRegex(MigrationError, "Succeeded Azure OpenAI"):
            self.plan(azure)


class RollbackTests(NetworkCase):
    def test_rollback_restores_snapshot_with_plan_and_execute(self):
        original = [account(1), account(2, networkAcls={"defaultAction": "Deny", "ipRules": [{"value": "52.1.2.3"}], "virtualNetworkRules": []}),
                    account(3, disableLocalAuth=None, networkAcls=None)]
        azure = FakeAzure(*original)
        summary = self.plan(azure, remove_ip_rules=True)
        self.execute(azure, summary["planSha256"], remove_ip_rules=True)
        self.assertTrue(all(item["properties"]["networkAcls"]["defaultAction"] == "Deny" for item in azure.resources.values()))
        snapshot = self.root / "plan" / network.SNAPSHOT
        writes = len(azure.writes())
        rollback = network.run(azure, self.root / "rollback", snapshot_path=snapshot, sleep=lambda _: None)
        self.assertEqual(len(azure.writes()), writes)
        self.assertEqual(set(rollback["operations"].values()), {"update"})
        self.assertTrue((self.root / "rollback" / "rollback-plan.json").exists())
        self.assertFalse((self.root / "rollback" / network.SNAPSHOT).exists())
        with self.assertRaisesRegex(MigrationError, "not approved"):
            network.run(azure, self.root / "rollback", snapshot_path=snapshot, execute_plan=True, approved="", sleep=lambda _: None)
        network.run(azure, self.root / "rollback", snapshot_path=snapshot, execute_plan=True,
                    approved=rollback["planSha256"], sleep=lambda _: None)
        for resource in original:
            restored = azure.resources[resource["id"].lower()]
            self.assertEqual(network.effective(network.network_state(restored)), network.effective(network.network_state(resource)))
        self.assertEqual(azure.resources[original[1]["id"].lower()]["properties"]["networkAcls"]["ipRules"], [{"value": "52.1.2.3"}])
        again = network.run(azure, self.root / "rollback-2", snapshot_path=snapshot, sleep=lambda _: None)
        self.assertEqual(set(again["operations"].values()), {"keep"})


class CliTests(NetworkCase):
    def test_cli_reads_fragment_connections_and_guards_execute(self):
        azure = FakeAzure(account(1), account(2))
        fragment = {"parameters": {"platform": {"azureOpenAIConnections": [
            {"accountResourceId": azure.ids()[0], "privateEndpoint": False},
            {"accountResourceId": azure.ids()[1], "privateEndpoint": True}]}}}
        path = self.root / "fragment.json"
        path.write_text(json.dumps(fragment))
        arguments = ["--accounts-file", str(path), "--egress-ip", EGRESS, "--output-dir", str(self.root / "cli")]
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(network.main(arguments, azure=azure), 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(list(summary["operations"]), [azure.ids()[0]])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(network.main([*arguments, "--execute"], azure=azure), 2)
            self.assertEqual(network.main(["--rollback-snapshot", str(path), "--egress-ip", EGRESS, "--output-dir", str(self.root / "x")], azure=azure), 2)
        self.assertEqual(azure.writes(), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(network.main([*arguments, "--execute", "--approved-plan-sha256", summary["planSha256"]], azure=azure), 0)
        self.assertEqual(len(azure.writes()), 1)


if __name__ == "__main__":
    unittest.main()
