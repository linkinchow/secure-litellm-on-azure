#!/usr/bin/env node
import { readFile, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const filename = process.env.L3_FIXTURE_STATE;
assert.ok(filename);
const state = JSON.parse(await readFile(filename, 'utf8'));
const args = process.argv.slice(2);
assert.equal(args[0], '--kubeconfig');
assert.equal(args[2], '--namespace');
assert.equal(args[3], 'litellm');
const command = args.slice(4);
if (command[0] === 'config') {
  console.log(JSON.stringify({ clusters: [{ cluster: { server: state.server } }] }));
} else if (command[0] === 'get') {
  if (command[1] === 'deployment') assert.ok(command.includes('--show-managed-fields'));
  console.log(JSON.stringify(command[1] === 'deployment' ? state.deployment : state.configMap));
} else if (command[0] === 'apply') {
  assert.ok(command.includes('--server-side'));
  const document = JSON.parse(await readFile(command[command.indexOf('-f') + 1], 'utf8'));
  if (document.kind === 'Deployment') {
    // A minimal server-side apply model: preconditions, ownership conflicts and the volume reference.
    assert.ok(command.includes('--field-manager=llmgw-migration'));
    assert.equal(document.metadata.uid, state.deployment.metadata.uid);
    assert.equal(document.metadata.resourceVersion, state.deployment.metadata.resourceVersion);
    const legacy = state.deployment.metadata.managedFields.some(entry => entry.manager === 'kubectl-patch');
    if (legacy && !command.includes('--force-conflicts')) {
      console.error('Apply failed with 1 conflict: conflict with "kubectl-patch"'); process.exit(1);
    }
    const result = structuredClone(state.deployment);
    for (const volume of document.spec.template.spec.volumes) {
      result.spec.template.spec.volumes.find(item => item.name === volume.name).configMap.name = volume.configMap.name;
    }
    if (state.dryRunDrift) result.spec.template.spec.containers[0].image = 'drifted';
    if (command.includes('--dry-run=server')) {
      console.log(JSON.stringify(result));
    } else {
      result.metadata.managedFields = result.metadata.managedFields.filter(entry => entry.manager !== 'kubectl-patch');
      result.metadata.resourceVersion = String(Number(result.metadata.resourceVersion) + 1);
      state.deployment = result;
      state.deploymentApplies++;
      await writeFile(filename, JSON.stringify(state));
    }
  } else {
    assert.ok(command.includes('--field-manager=llmgw-l3'));
    state.configMap = document;
    state.applies++;
    await writeFile(filename, JSON.stringify(state));
  }
} else {
  assert.deepEqual(command.slice(0, 3), ['rollout', 'status', 'deployment/litellm']);
}
