"""Fresh native ingress receipt revalidation after a completed model-only sync."""

import argparse
import copy
import json
from pathlib import Path

from local_execution.model_sync import approvals, progress, protected_source, sha, source_fingerprints
from local_execution.runner import authenticate_azure, load_config, operation_root, reviewed_revision
from scripts import model_sync_runtime as runtime
from scripts.audit_runtime import AuditCluster
from scripts.customer_migration import ROOT, MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.edge_binding import deployed_edge, native_ingress_document_state
from scripts.migration_deploy import AzureCommands, deployment_name
from scripts.model_sync_infra import endpoint_state, network_context, role_state
from scripts.private_ingress_runtime import frontend_for, verify_native_ingress_routes
from scripts.workflow_diagnostics import diagnostic_exit


def sync_proof(config_path, sync_output):
    require(not sync_output.is_symlink() and sync_output.resolve().is_relative_to((ROOT / "temp").resolve()),
            "Completed sync output must be a protected directory under temp/")
    for name in ("model-sync-state.json", "model-sync-review.json", "previous-customer.json", "desired-customer.json"):
        protected_source(sync_output / name, "Model-sync proof")
    state = json.loads((sync_output / "model-sync-state.json").read_text())
    review = json.loads((sync_output / "model-sync-review.json").read_text())
    digest = review.pop("planSha256")
    require(fingerprint(review) == digest == state.get("planSha256")
            and state.get("status") == "completed" and state.get("rolloutVerified") is True,
            "Receipt refresh requires genuine completed and rollout-verified model-sync proof")
    previous = json.loads((sync_output / "previous-customer.json").read_text())
    desired = json.loads((sync_output / "desired-customer.json").read_text())
    current = json.loads(config_path.read_text())
    expected = copy.deepcopy(previous)
    expected["application"]["models"] = current["application"]["models"]
    expected["parameters"]["platform"]["azureOpenAIConnections"] = current["parameters"]["platform"]["azureOpenAIConnections"]
    require(expected == current == desired and fingerprint(current) == review["desiredConfigSha256"],
            "Only the completed sync's exact models/connections delta can be revalidated")
    previous_config, _ = load_config(sync_output / "previous-customer.json")
    return previous_config, review, digest


def unchanged_workloads(config, observed, kube, directory):
    require(runtime.baseline(config, observed["identity"]["identity"], kube) == observed["backend"],
            "Backend changed during revalidation; no receipt writes performed")
    kube_base = kube[:-2] if kube[-2:] == ["--namespace", "litellm"] else kube
    for plane in ("api", "admin"):
        client = AuditCluster([*kube_base, "--namespace", "llm-" + plane + "-ingress"], directory)
        name = "llm-" + plane + "-ingress"
        objects = [client.get(kind, name) for kind in ("configmap", "deployment", "service")]
        require(native_ingress_document_state(config, *objects, observed["edge"]["frontDoorId"], plane) == observed["planes"][plane],
                "Ingress changed during revalidation; no receipt writes performed")


def receipt(config, stage, component, output, azure):
    response = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"],
                             "--name", deployment_name(config, stage, component),
                             "--query", "{state:properties.provisioningState,value:properties.outputs." + output + ".value}"])
    require(response.get("state") == "Succeeded" and isinstance(response.get("value"), dict),
            "Existing successful ingress receipts are required; initial setup is not an evidence refresh")
    return response["value"]


def observe(config, previous, proof, revision, authorization, kube, directory, azure):
    from scripts.app_gateway import require_front_door_edge
    require_front_door_edge(config, "Model sync edge evidence")
    require(config.get("application", {}).get("authentication", {}).get("mode") == "native",
            "Focused evidence refresh currently supports native private ingress only; Entra needs separate approved revalidation")
    identity = runtime.identity_context(config, azure)
    current = runtime.baseline(config, identity["identity"], kube)
    require(fingerprint(current["runtime"]) == proof["application"]["runtimeSha256"],
            "Live backend config differs from completed sync; evidence refresh blocked")
    payload = runtime.render(config, current)
    require(not payload["modelChanged"], "Evidence refresh cannot change any model configuration")
    runtime.apply(kube, payload, current, directory, {})
    context = network_context(config, azure)
    require(context["links"], "Private DNS link is missing")
    for item in proof["infrastructure"]["accounts"]:
        endpoint = endpoint_state(item["account"], item["parameters"]["accountAlias"], context, azure)
        role = role_state(item["account"], identity["identity"], item["parameters"]["principalSourceResourceId"], azure)
        require(not endpoint["createEndpoint"] and not endpoint["createDnsBinding"] and not role["createRole"],
                "Current private prerequisite is missing; refresh never deploys infrastructure")
        runtime.network_probe(kube, item["account"], endpoint["ips"])
    ingress = receipt(config, 4, "private-ingress", "privateIngress", azure)
    backend = receipt(config, 6, "private-ingress-backend", "privateIngressBackend", azure)
    require(ingress.get("configSha256") in {stage_fingerprint(previous, 4), stage_fingerprint(config, 4)}
            and backend.get("configSha256") in {stage_fingerprint(previous, 6), stage_fingerprint(config, 6)}
            and backend.get("backendRoutesVerified") is True and backend.get("authenticationMode") == "native",
            "Receipts do not bind the before/after sync configuration; unrelated stale evidence cannot be refreshed")
    edge = deployed_edge(config, azure)
    binding = receipt(config, 9, "edge-bind", "edgeBinding", azure)
    require(binding.get("bindingMode") == "native-private-ingress" and binding.get("rolloutVerified") is True
            and binding.get("configSha256") in {stage_fingerprint(previous, 9), stage_fingerprint(config, 9)}
            and binding.get("frontDoorId") == edge["frontDoorId"],
            "An existing verified native edge binding for this before/after configuration is required")
    planes = {}
    kube_base = kube[:-2] if kube[-2:] == ["--namespace", "litellm"] else kube
    cluster = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"],
                            "--name", config["parameters"]["platform"]["stage4Aks"]["name"], "--query", "{nodeResourceGroup:nodeResourceGroup}"])
    for plane in ("api", "admin"):
        client = AuditCluster([*kube_base, "--namespace", "llm-" + plane + "-ingress"], directory)
        name = "llm-" + plane + "-ingress"
        config_map, deployment, service = (client.get(kind, name) for kind in ("configmap", "deployment", "service"))
        state = native_ingress_document_state(config, config_map, deployment, service, edge["frontDoorId"], plane)
        require(state == binding.get("planes", {}).get(plane),
                "Ingress differs from the previously verified binding; model-only receipt refresh cannot approve ingress changes")
        require(deployment["spec"]["template"]["spec"]["containers"][0]["image"] == ingress.get("image"),
                "Live ingress image differs from original verified receipt")
        addresses = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        require(len(addresses) == 1 and addresses[0].get("ip") == ingress[plane]["privateIpAddress"]
                and addresses[0].get("ipMode", "VIP") == "VIP" and not addresses[0].get("hostname"),
                "Live ingress frontend differs from receipt")
        require(frontend_for(config, azure, cluster["nodeResourceGroup"], ingress[plane]["privateIpAddress"]) == ingress[plane],
                "Live Standard LB/subnet frontend differs from receipt")
        planes[plane] = state
    verify_native_ingress_routes(config, ingress, front_door_id=edge["frontDoorId"])
    revalidation = {"operation": "model-sync-evidence", "revision": revision, "approval": authorization,
                    "completedSyncPlanSha256": proof["completedSyncPlanSha256"],
                    "backendSpecSha256": current["deploymentSpecSha256"], "backendConfigSha256": current["configMapSha256"],
                    "planes": planes, "inferenceVerified": False, "stageAccepted": False}
    # These are new verification records, not rewritten claims of historical approval.
    desired_ingress = copy.deepcopy(ingress)
    desired_ingress.pop("verifiedAt", None)
    desired_ingress.update(revision=revision, configSha256=stage_fingerprint(config, 4), revalidation=revalidation)
    desired_backend = {"revision": revision, "configSha256": stage_fingerprint(config, 6),
                       "privateIngressSha256": fingerprint(desired_ingress), "authenticationMode": "native",
                       "backendRoutesVerified": True, "revalidation": revalidation}
    return {"identity": identity, "backend": current, "edge": edge, "binding": binding, "planes": planes,
            "before": {"privateIngress": ingress, "privateIngressBackend": backend},
            "after": {"privateIngress": desired_ingress, "privateIngressBackend": desired_backend}}


def refresh(config_path, sync_output, operation, approved, ticket, approvers):
    require(operation in {"plan", "execute"}, "Operation must be plan or execute")
    protected_source(config_path, "Customer config")
    config, settings = load_config(config_path)
    from scripts.app_gateway import require_front_door_edge
    require_front_door_edge(config, "Model sync edge evidence")
    previous, proof, sync_digest = sync_proof(config_path, sync_output)
    proof["completedSyncPlanSha256"] = sync_digest
    revision, sources = reviewed_revision(), source_fingerprints()
    authorization = approvals(config, ticket, approvers)
    config_hash = sha(config_path)
    proof_hash = fingerprint({name: sha(sync_output / name) for name in
                              ("model-sync-state.json", "model-sync-review.json", "previous-customer.json", "desired-customer.json")})
    directory = operation_root(ROOT / "temp/model-sync-evidence", "revalidate-" + operation)
    state = {"status": "planning", "receiptsAttempted": [], "receiptsWritten": [], "inferenceVerified": False,
             "stageAccepted": False, "outputDirectory": str(directory.relative_to(ROOT))}
    try:
        progress("Revalidating completed sync, mounted config and existing native ingress; no resource writes.")
        authenticate_azure(config, settings, "runtime", directory)
        kube = runtime.connect_cluster(config, directory, False)
        azure = AzureCommands(config, directory)
        observed = observe(config, previous, proof, revision, authorization, kube, directory, azure)
        templates = {}
        for output, value in observed["after"].items():
            templates[output] = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
                                 "contentVersion": "1.0.0.0", "resources": [],
                                 "outputs": {output: {"type": "object", "value": value}}}
            private_write(directory / (output + ".json"), json.dumps(templates[output], indent=2))
        review = {"revision": revision, "sources": sources, "configInputSha256": config_hash,
                  "syncProofSha256": proof_hash, "approval": authorization, "observed": observed, "templates": templates}
        digest = fingerprint(review)
        private_write(directory / "evidence-review.json", json.dumps({**review, "planSha256": digest}, indent=2))
        state.update(status="planned", planSha256=digest)
        state["noOp"] = observed["before"] == observed["after"]
        if operation == "plan":
            return state
        require(approved == digest, "Fresh evidence revalidation differs from approval; no receipt writes performed")
        require(sha(config_path) == config_hash and sources == source_fingerprints() and revision == reviewed_revision(),
                "Inputs changed during evidence revalidation; no receipt writes performed")
        require(proof_hash == fingerprint({name: sha(sync_output / name) for name in
                                          ("model-sync-state.json", "model-sync-review.json", "previous-customer.json", "desired-customer.json")}),
                "Sync proof changed during revalidation; no receipt writes performed")
        unchanged_workloads(config, observed, kube, directory)
        authenticate_azure(config, settings, "deploy", directory)
        progress("Fresh revalidation approval verified. Saving only empty-resources ingress verification receipts.")
        state["status"] = "executing"
        for stage, component, output in ((4, "private-ingress", "privateIngress"), (6, "private-ingress-backend", "privateIngressBackend")):
            require(receipt(config, stage, component, output, azure) == observed["before"][output],
                    "Ingress receipt drifted after approval; replan")
            if observed["before"][output] == observed["after"][output]:
                continue
            state["receiptsAttempted"].append(output)
            private_write(directory / "evidence-state.json", json.dumps(state, indent=2))
            result = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"],
                                   "--name", deployment_name(config, stage, component), "--mode", "Incremental",
                                   "--template-file", str(directory / (output + ".json"))])
            require(result.get("properties", {}).get("provisioningState") == "Succeeded",
                    "Saving freshly verified evidence failed; inspect receiptsWritten before replanning")
            require(receipt(config, stage, component, output, azure) == observed["after"][output],
                    "Saved evidence does not match exact approved fresh verification")
            state["receiptsWritten"].append(output)
        state["status"] = "completed"
        return state
    except Exception:
        state["status"] = "failed"
        raise
    finally:
        private_write(directory / "evidence-state.json", json.dumps(state, indent=2))


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sync-output", type=Path, required=True)
    parser.add_argument("--operation", choices=("plan", "execute"), required=True)
    parser.add_argument("--approved-plan-sha256", default="")
    parser.add_argument("--change-ticket", required=True)
    parser.add_argument("--approved-by", action="append", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args.config, args.sync_output, args.operation, args.approved_plan_sha256,
                                 args.change_ticket, args.approved_by)), flush=True)
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-model-sync-evidence")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-model-sync-evidence",
                                        "Receipt revalidation failed; inspect protected evidence-state.json")) from None


if __name__ == "__main__":
    cli()
