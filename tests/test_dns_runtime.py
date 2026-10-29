import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from scripts.dns_runtime import DnsClient, change_dns, dns_settings
from tests.test_customer_migration import customer_config


class DnsTests(unittest.TestCase):
    def test_wrong_tenant_is_rejected_before_dns_reads_or_writes(self):
        azure = Mock()
        azure.run.return_value = {"tenantId": "other", "id": "other"}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "scope mismatch"):
            DnsClient(customer_config(), Path(directory), azure)
        azure.scoped.assert_not_called()

    def test_record_delete_accepts_successful_empty_azure_response(self):
        config = customer_config()
        config["dns"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "ttl": 300}
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        with tempfile.TemporaryDirectory() as directory:
            client = DnsClient(config, Path(directory), azure)
            client.write("admin", None, {"etag": "known"})
        azure.scoped_allow_empty.assert_called_once()
        self.assertIn("If-Match=known", azure.scoped_allow_empty.call_args.args[0])
    def test_checkpoint_precedes_write_and_rollback_restores_exact_record(self):
        config = customer_config()
        config["dns"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "ttl": 300}
        original = {plane: {"etag": "before-" + plane, "properties": {"TTL": 600, "CNAMERecord": {"cname": f"legacy-{plane}.synthetic.invalid"}, "metadata": {"owner": "customer"}}} for plane in ("api", "admin")}
        class Client:
            records = copy.deepcopy(original)
            saved = None
            def read(self): return copy.deepcopy(self.records)
            def checkpoint(self): return copy.deepcopy(self.saved)
            def save(self, value): self.saved = copy.deepcopy(value)
            def write(self, plane, desired, previous):
                assert self.saved and self.records[plane] == previous
                self.records[plane] = {"etag": "next-" + plane, "properties": copy.deepcopy(desired)} if desired else None
        client = Client()
        edge = {"apiHost": "llm-api." + config["baseDomain"], "adminHost": "llm-admin." + config["baseDomain"], "apiTrafficEnabled": True, "adminTrafficEnabled": True, "endpointHost": "api.azurefd.net", "adminEndpointHost": "admin.azurefd.net", "adminAccessMode": "SourceIpAllowlistAndNativeLogin"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            plan = change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge=edge)
            self.assertIsNone(client.saved)
            change_dns(config, "dns-publish", "execute", "a" * 40, path, plan["planSha256"], client=client, edge=edge)
            rollback = change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-rollback", "execute", "a" * 40, path, rollback["planSha256"], client=client, edge=edge)
            self.assertEqual({plane: client.records[plane]["properties"] for plane in client.records}, {plane: original[plane]["properties"] for plane in original})
            self.assertEqual(client.saved["phase"], "rolled-back")
            retry = change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-rollback", "execute", "a" * 40, path, retry["planSha256"], client=client, edge=edge)
            # A clean rollback can be published again with a fresh checkpoint; the rolled-back one is replaced.
            again = change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-publish", "execute", "a" * 40, path, again["planSha256"], client=client, edge=edge)
            self.assertEqual(client.saved["phase"], "published")
            self.assertEqual(client.saved["before"], {plane: original[plane]["properties"] for plane in original})
            self.assertEqual(client.records["api"]["properties"]["CNAMERecord"]["cname"], "api.azurefd.net")
            rollback = change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-rollback", "execute", "a" * 40, path, rollback["planSha256"], client=client, edge=edge)
            # After a rollback, DNS edited by someone else is never republished over.
            client.records["admin"]["properties"]["TTL"] = 99
            with self.assertRaisesRegex(ValueError, "modified externally"):
                change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge=edge)
            with self.assertRaisesRegex(ValueError, "modified externally"):
                change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            duplicated = {**edge, "adminEndpointHost": edge["endpointHost"]}
            with self.assertRaisesRegex(ValueError, "distinct"):
                change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge=duplicated)

    def test_dns_scope_targets_only_split_domains_in_approved_subscription(self):
        config = customer_config()
        config["dns"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "ttl": 300}
        self.assertEqual(dns_settings(config)["records"], {"api": "llm-api", "admin": "llm-admin"})
        config["dns"]["zoneResourceId"] += ".untrusted.invalid"
        with self.assertRaises(ValueError): dns_settings(config)