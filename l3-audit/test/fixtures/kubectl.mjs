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
  console.log(JSON.stringify(command[1] === 'deployment' ? state.deployment : state.configMap));
} else if (command[0] === 'apply') {
  state.configMap = JSON.parse(await readFile(command[command.indexOf('-f') + 1], 'utf8'));
  state.applies++;
  await writeFile(filename, JSON.stringify(state));
} else if (command[0] === 'patch') {
  const patch = JSON.parse(await readFile(command[command.indexOf('--patch-file') + 1], 'utf8'));
  assert.equal(patch[0].value, state.deployment.metadata.uid);
  assert.deepEqual(patch[1].value, state.deployment.spec.template);
  state.deployment.spec.template.spec.volumes[0].configMap.name = patch[2].value;
  state.patches++;
  await writeFile(filename, JSON.stringify(state));
} else {
  assert.deepEqual(command.slice(0, 3), ['rollout', 'status', 'deployment/litellm']);
}
