import base64
import copy
import json
import hashlib
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from scripts.customer_migration import ROOT, fingerprint
from scripts.runner_arm import RunnerArm
from scripts.runner_job import cleanup_runner, runner_intent, start_runner, wait_runner_online
from tests.test_customer_migration import customer_config
from scripts.runner_lifecycle import GitHubRunnerRegistration, enroll_runner, retire_runner, runner_snapshot


class RunnerLifecycleTests(unittest.TestCase):
    def test_standard_runner_network_modes_preserve_existing_subnet(self):
        entry = (ROOT / "infra/private-runner/deploy.bicep").read_text()
        self.assertIn("@allowed(['Standard_D2s_v5', 'Standard_D4s_v3', 'Standard_D4s_v5', 'Standard_D8s_v5'])", entry)
        self.assertIn("param virtualMachineSize string = 'Standard_D4s_v3'", entry)
        self.assertIn("param subnetResourceId string = ''", entry)
        self.assertIn("param createBastion bool = empty(subnetResourceId)", entry)
        self.assertIn("module network 'management-network.bicep' = if (empty(subnetResourceId))", entry)
        self.assertIn("subnetResourceId: empty(subnetResourceId) ? network!.outputs.runnerSubnetId : subnetResourceId", entry)
        self.assertIn("sshSourceCidr: empty(subnetResourceId) && createBastion ? network!.outputs.bastionSubnetPrefix : sshSourceCidr", entry)
        self.assertIn("uniqueString(subscription().subscriptionId, subnetResourceId, environmentName)", entry)
        self.assertIn("output targetPrivateConnectivityVerified bool = false", entry)

    def test_standard_runner_network_has_egress_and_optional_native_bastion(self):
        network = (ROOT / "infra/private-runner/management-network.bicep").read_text()
        machine = (ROOT / "infra/private-runner/standard-vm.bicep").read_text()
        self.assertIn("cidrSubnet(addressPrefix, 26, 0)", network)
        self.assertIn("cidrSubnet(addressPrefix, 26, 1)", network)
        self.assertIn("defaultOutboundAccess: false", network)
        self.assertIn("natGateway: { id: nat.id }", network)
        self.assertIn("resource bastion 'Microsoft.Network/bastionHosts@2024-07-01' = if (createBastion)", network)
        self.assertIn("enableTunneling: true", network)
        self.assertIn("securityRules: concat(empty(sshSourceCidr) ? [] : [", machine)
        self.assertIn("deny-all-other-inbound", machine)
        self.assertNotIn("publicIPAddress", machine)
        self.assertNotIn("identity:", machine)
        self.assertNotIn("virtualNetworkPeerings", network)
        self.assertNotIn("roleAssignments", network)

    def test_standard_runner_bootstrap_and_registration_shell_syntax(self):
        script = ROOT / "infra/private-runner/bootstrap.sh"
        subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True)
        source = script.read_text()
        registration = source.split("<<'REGISTER'\n", 1)[1].split("\nREGISTER\n", 1)[0]
        subprocess.run(["bash", "-n"], input=registration, text=True, check=True, capture_output=True)
        self.assertLess(len(source.encode()), 65535)
        self.assertNotIn("--token", registration)
        self.assertIn("runuser --user actions-runner", registration)

    def test_standard_runner_downloads_pin_six_https_assets(self):
        source = (ROOT / "infra/private-runner/bootstrap.sh").read_text()
        downloads = re.findall(r'verified_download (https://github\.com/\S+)\s+\\\n\s+"[^"\n]+" (\S+)', source)
        self.assertEqual(len(downloads), 6)
        for url, checksum in downloads:
            with self.subTest(url=url):
                self.assertNotIn("/latest/", url)
                self.assertRegex(checksum, r"^[0-9a-f]{64}$")

    def test_standard_runner_rejects_tampered_download(self):
        source = ROOT / "infra/private-runner/bootstrap.sh"
        probe = 'source "$1"\ndownload() { printf %s synthetic-package > "$2"; }\nverified_download https://example.invalid/package "$2" "$3"\n'
        expected = hashlib.sha256(b"synthetic-package").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            for checksum, accepted in ((expected, True), ("0" * 64, False)):
                result = subprocess.run(["bash", "-s", "--", str(source), directory + "/package", checksum], input=probe, text=True, capture_output=True)
                with self.subTest(checksum=checksum):
                    self.assertEqual(result.returncode == 0, accepted)

    def test_standard_runner_bootstrap_does_not_run_when_sourced(self):
        source = ROOT / "infra/private-runner/bootstrap.sh"
        probe = 'source "$1"\ndeclare -F prepare_work_disk verified_download main\n'
        result = subprocess.run(["bash", "-s", "--", str(source)], input=probe, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ["prepare_work_disk", "verified_download", "main"])

    def test_job_orchestration_records_creation_enrollment_online_and_cleanup(self):
        arm, _, _ = self.arm_fixture()
        original = arm.snapshot()
        contract = {"lease": {"repository": "synthetic/gateway", "group": arm.machine_id.split("/providers/")[0]}, "runnerGroupId": 7, "parameters": {"runnerName": arm.machine_id.rsplit("/", 1)[1]}, "templateSha256": hashlib.sha256((ROOT / "infra/private-runner/main.bicep").read_bytes()).hexdigest()}
        intent = {**contract, "planSha256": fingerprint(contract), "machineId": arm.machine_id}
        managed = Mock()
        managed.machine_id, managed.repository = arm.machine_id, "synthetic/gateway"
        managed.snapshot.side_effect = [None, original, original, original]
        registration = Mock()
        registration.find.return_value = []
        registration.generate.side_effect = lambda repo, body: {"runner": {"id": 17, "name": body["name"], "ephemeral": True}, "encoded_jit_config": base64.b64encode(b"synthetic-private-jit").decode()}
        saved = []
        ready = lambda registry, record: [record["name"]]
        state = start_runner(intent, intent["planSha256"], Mock(), managed, registration, lambda value: saved.append(copy.deepcopy(value)), ready=ready)
        self.assertEqual([value["phase"] for value in saved], ["creating", "created", "registering", "registered", "started", "online"])
        self.assertEqual(registration.generate.call_args.args[1]["runner_group_id"], 7)
        self.assertTrue(state["ready"])
        self.assertNotIn("encoded_jit_config", json.dumps(saved))
        changed = copy.deepcopy(intent)
        changed["runnerGroupId"] = 99
        provision = Mock()
        with self.assertRaisesRegex(ValueError, "content changed"):
            start_runner(changed, intent["planSha256"], provision, managed, registration, Mock())
        provision.assert_not_called()
        with self.assertRaisesRegex(ValueError, "replay"):
            start_runner(intent, intent["planSha256"], Mock(), managed, registration, Mock(), previous=state)
        managed.snapshot.side_effect = None
        managed.snapshot.return_value = original
        result = cleanup_runner(state, managed, registration, lambda value: saved.append(copy.deepcopy(value)))
        self.assertEqual(result["phase"], "retired")
        self.assertFalse(result["ready"])
        self.assertFalse(result["stageAccepted"])

    def test_online_handoff_refuses_busy_replaced_or_non_ephemeral_runner(self):
        record = {"snapshot": {"repository": "synthetic/gateway"}, "name": "synthetic", "runnerId": 17}
        registration = Mock()
        good = {"id": 17, "ephemeral": True, "busy": False, "status": "online"}
        registration.find.return_value = [good]
        self.assertIn("synthetic", wait_runner_online(registration, record))
        for updates in ({"busy": True}, {"id": 99}, {"ephemeral": False}):
            registration.find.return_value = [{**good, **updates}]
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                wait_runner_online(registration, record)

    def test_runner_intent_requires_immutable_image_and_unique_workflow_attempt(self):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        config = customer_config()
        arm, _, _ = self.arm_fixture()
        settings = {"imageVersionResourceId": arm.image_id, "subnetResourceId": arm.subnet_id.replace("11111111-1111-4111-8111-111111111111", config["azure"]["subscriptionId"]), "virtualMachineSize": "Standard_D4s_v5", "runnerGroupId": 3, "sshPublicKey": ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()}
        first = runner_intent(config, settings, "synthetic/gateway", "123", "1")
        self.assertNotEqual(first["machineId"], runner_intent(config, settings, "synthetic/gateway", "123", "2")["machineId"])
        self.assertEqual(first["runnerGroupId"], 3)
        with self.assertRaisesRegex(ValueError, "gallery"):
            runner_intent(config, {**settings, "imageVersionResourceId": arm.image_id.rsplit("/", 1)[0] + "/latest"}, "synthetic/gateway", "123", "1")

    def arm_fixture(self):
        group = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/synthetic"
        machine_id = group + "/providers/Microsoft.Compute/virtualMachines/llmgw-runner-" + "a" * 20
        subnet_id = group + "/providers/Microsoft.Network/virtualNetworks/private/subnets/runner"
        image_id = group + "/providers/Microsoft.Compute/galleries/tools/images/runner/versions/1.0.0"
        session = Mock()
        arm = RunnerArm(machine_id, subnet_id, image_id, "synthetic/gateway", "test", Mock(), session)
        arm.credential.get_token.return_value.token = "synthetic-azure-token"
        tags = {"managedBy": "llmgw-runner", "repository": "synthetic/gateway", "environment": "test"}
        resources = {
            machine_id: {"id": machine_id, "location": "westus", "tags": tags, "properties": {"provisioningState": "Succeeded", "vmId": "original-vm-id", "securityProfile": {"securityType": "TrustedLaunch", "encryptionAtHost": True, "uefiSettings": {"secureBootEnabled": True, "vTpmEnabled": True}}, "storageProfile": {"imageReference": {"id": image_id}, "osDisk": {"deleteOption": "Delete", "managedDisk": {"id": arm.disk_id}}}, "networkProfile": {"networkInterfaces": [{"id": arm.nic_id, "properties": {"deleteOption": "Delete"}}]}}},
            arm.nic_id: {"id": arm.nic_id, "tags": tags, "properties": {"networkSecurityGroup": {"id": arm.nsg_id}, "ipConfigurations": [{"properties": {"subnet": {"id": subnet_id}}}]}},
            subnet_id: {"id": subnet_id, "properties": {"routeTable": {"id": group + "/providers/Microsoft.Network/routeTables/firewall"}}},
            arm.nsg_id: {"id": arm.nsg_id, "tags": tags, "properties": {"securityRules": [{"properties": {"access": "Deny", "direction": "Inbound", "priority": 100, "protocol": "*", "sourceAddressPrefix": "*", "sourcePortRange": "*", "destinationAddressPrefix": "*", "destinationPortRange": "*"}}]}},
            arm.disk_id: {"id": arm.disk_id},
        }
        def request(method, url, **kwargs):
            resource = url.removeprefix("https://management.azure.com")
            response = Mock()
            response.status_code = 200 if resource in resources else 404
            response.json.return_value = copy.deepcopy(resources.get(resource))
            if method == "PUT":
                resources[resource] = {"id": resource, **kwargs["json"]}
                response.status_code = 201
            if method == "DELETE":
                resources.pop(resource, None)
                if resource == machine_id:
                    resources.pop(arm.nic_id, None)
                    resources.pop(arm.disk_id, None)
                response.status_code = 202
            return response
        session.request.side_effect = request
        return arm, resources, session

    def test_arm_adapter_checks_security_and_confirms_all_owned_resources_deleted(self):
        arm, resources, session = self.arm_fixture()
        snapshot = arm.snapshot()
        self.assertEqual(snapshot["diskId"], arm.disk_id)
        changed = resources[arm.machine_id]["properties"]["securityProfile"]["uefiSettings"]
        changed["vTpmEnabled"] = False
        with self.assertRaisesRegex(ValueError, "vTPM"):
            arm.delete_machine(arm.machine_id, snapshot["vmId"])
        self.assertFalse(any(call.args[0] == "DELETE" for call in session.request.call_args_list))
        changed["vTpmEnabled"] = True
        arm.delete_machine(arm.machine_id, snapshot["vmId"])
        for resource in (arm.machine_id, arm.nic_id, arm.disk_id, arm.nsg_id):
            self.assertNotIn(resource, resources)
        self.assertIn(arm.subnet_id, resources)
        with self.assertRaisesRegex(ValueError, "allowlist"):
            arm.request("DELETE", "/subscriptions/unrelated")

    def test_arm_start_uses_protected_material_once_and_fixed_script(self):
        arm, resources, session = self.arm_fixture()
        snapshot = arm.snapshot()
        registration = Mock()
        registration.find.return_value = []
        private = base64.b64encode(b"synthetic-jit-config").decode()
        registration.generate.side_effect = lambda repo, body: {"runner": {"id": 123, "name": body["name"], "ephemeral": True}, "encoded_jit_config": private}
        saved = []
        result = enroll_runner(snapshot, fingerprint(snapshot), arm.snapshot, registration, arm.managed_command, lambda record: saved.append(copy.deepcopy(record)))
        self.assertEqual(result["phase"], "started")
        self.assertNotIn(private, json.dumps(saved))
        body = resources[arm.command_id]
        self.assertEqual(body["location"], "westus")
        self.assertEqual(body["properties"]["protectedParameters"][0]["value"], private)
        with self.assertRaisesRegex(ValueError, "already exists"):
            arm.managed_command(arm.machine_id, {"properties": body["properties"]})
        self.assertEqual(len([call for call in session.request.call_args_list if call.args[0] == "PUT"]), 1)

    def test_partial_cleanup_resumes_only_from_retiring_checkpoint(self):
        arm, resources, session = self.arm_fixture()
        snapshot = arm.snapshot()
        record = {"snapshot": snapshot, "name": "llmgw-" + "b" * 24, "runnerId": 123, "phase": "started"}
        registration = Mock()
        registration.find.return_value = []
        saved = []
        original_request = session.request.side_effect
        def fail_security_delete(method, url, **kwargs):
            if method == "DELETE" and url.endswith(arm.nsg_id):
                return Mock(status_code=503)
            return original_request(method, url, **kwargs)
        session.request.side_effect = fail_security_delete
        current = lambda: arm.snapshot(optional=True)
        save = lambda value: saved.append(copy.deepcopy(value))
        with self.assertRaisesRegex(ValueError, "suppressed"):
            retire_runner(record, current, registration, arm.delete_machine, save)
        self.assertNotIn(arm.machine_id, resources)
        self.assertIn(arm.nsg_id, resources)
        self.assertEqual(saved[-1]["phase"], "retiring")
        with self.assertRaisesRegex(ValueError, "replaced"):
            retire_runner(record, current, registration, arm.delete_machine, save)
        session.request.side_effect = original_request
        result = retire_runner(saved[-1], current, registration, arm.delete_machine, save)
        self.assertEqual(result["phase"], "retired")
        self.assertNotIn(arm.nsg_id, resources)

    def test_registration_transport_is_scoped_bounded_and_does_not_retry_writes(self):
        session = Mock()
        repository = "synthetic/gateway"
        name = "llmgw-" + "a" * 24
        registration = GitHubRunnerRegistration(repository, "synthetic-installation-token", session)
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"full_name": repository, "private": True, "permissions": {"admin": True}}
        self.assertTrue(registration.is_private_admin(repository))
        self.assertFalse(session.trust_env)
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.request.call_args.args[1], "https://api.github.com/repos/" + repository)
        with self.assertRaisesRegex(ValueError, "scope"):
            registration.find("another/gateway", name)
        session.request.reset_mock()
        session.request.return_value.status_code = 502
        body = {"name": name, "runner_group_id": 1, "work_folder": "_work", "labels": ["self-hosted", "linux", "x64", name]}
        with self.assertRaisesRegex(ValueError, "suppressed"):
            registration.generate(repository, body)
        self.assertEqual(session.request.call_count, 1)
        registration.close()
        self.assertEqual(registration.headers, {})

    def test_runner_enumeration_cannot_overlook_later_pages_or_duplicate_ids(self):
        session = Mock()
        name = "llmgw-" + "b" * 24
        registration = GitHubRunnerRegistration("synthetic/gateway", "synthetic-token", session)
        first = {"total_count": 101, "runners": [{"id": number, "name": "other-" + str(number)} for number in range(1, 101)]}
        last = {"id": 101, "name": name, "busy": False, "status": "offline"}
        session.request.return_value.status_code = 200
        session.request.return_value.json.side_effect = [first, {"total_count": 101, "runners": [last]}]
        self.assertEqual(registration.find("synthetic/gateway", name), [last])
        self.assertIn("page=2", session.request.call_args.args[1])
        session.request.return_value.json.side_effect = [first, {"total_count": 101, "runners": [{**last, "id": 1}]}]
        with self.assertRaisesRegex(ValueError, "repeated"):
            registration.find("synthetic/gateway", name)

    def snapshot(self):
        tags = {"managedBy": "llmgw-runner", "repository": "synthetic/gateway", "environment": "test"}
        image = "/subscriptions/synthetic/resourceGroups/image/providers/Microsoft.Compute/galleries/tools/images/runner/versions/1.0.0"
        machine = {"id": "/synthetic/vm", "tags": tags, "properties": {"vmId": "synthetic-uid", "securityProfile": {"securityType": "TrustedLaunch", "encryptionAtHost": True, "uefiSettings": {"secureBootEnabled": True}}, "storageProfile": {"imageReference": {"id": image}}, "networkProfile": {"networkInterfaces": [{"id": "/synthetic/nic"}]}}}
        network = {"id": "/synthetic/nic", "tags": tags, "properties": {"ipConfigurations": [{"properties": {"subnet": {"id": "/synthetic/subnet"}}}]}}
        subnet = {"id": "/synthetic/subnet", "properties": {"routeTable": {"id": "/synthetic/firewall-route"}}}
        return runner_snapshot(machine, network, subnet, "synthetic/gateway", "test", image)

    def test_jit_only_uses_protected_parameter_and_never_enters_checkpoint(self):
        snapshot = self.snapshot()
        registration = Mock()
        registration.is_private_admin.return_value = True
        registration.find.return_value = []
        private = base64.b64encode(b"synthetic-registration-secret").decode()
        registration.generate.side_effect = lambda repo, body: {"runner": {"id": 123, "name": body["name"], "ephemeral": True}, "encoded_jit_config": private}
        command = Mock()
        saved = []
        result = enroll_runner(snapshot, fingerprint(snapshot), lambda: snapshot, registration, command, lambda record: saved.append(copy.deepcopy(record)))
        self.assertEqual(result["phase"], "started")
        self.assertNotIn(private, json.dumps(saved))
        body = command.call_args.args[1]["properties"]
        self.assertEqual(body["protectedParameters"], [{"name": "LLMGW_JIT_CONFIG", "value": private}])
        self.assertNotIn(private, body["source"]["script"])
        self.assertEqual(body["runAsUser"], "runner")

    def test_busy_or_replaced_runner_is_not_deleted(self):
        snapshot = self.snapshot()
        record = {"snapshot": snapshot, "name": "synthetic", "runnerId": 123}
        registration, delete, save = Mock(), Mock(), Mock()
        registration.find.return_value = [{"id": 123, "busy": True}]
        with self.assertRaisesRegex(ValueError, "busy"):
            retire_runner(record, lambda: snapshot, registration, delete, save)
        delete.assert_not_called()
        registration.find.return_value = []
        changed = {**snapshot, "vmId": "replacement"}
        with self.assertRaisesRegex(ValueError, "replaced"):
            retire_runner(record, lambda: changed, registration, delete, save)
        delete.assert_not_called()

    def test_unknown_jit_result_leaves_recoverable_checkpoint_not_a_second_runner(self):
        snapshot = self.snapshot()
        registration = Mock()
        registration.find.return_value = []
        registration.generate.side_effect = ValueError("synthetic acknowledgement loss")
        saved = []
        with self.assertRaises(ValueError):
            enroll_runner(snapshot, fingerprint(snapshot), lambda: snapshot, registration, Mock(), lambda record: saved.append(copy.deepcopy(record)))
        self.assertEqual(saved[0]["phase"], "registering")