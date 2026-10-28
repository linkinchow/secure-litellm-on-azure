import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, copyFile, chmod, readFile, writeFile, rm, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { sha } from '../src/record.js';
import { parse as yamlParse } from 'yaml';
import { privateApiOrigin } from '../src/activation.js';

const execute = promisify(execFile);
test('AKS API normalization equates default HTTPS port and root only, not different endpoints', () => {
  const origin = 'https://aks-test-synthetic.id.privatelink.westus3.azmk8s.io';
  for (const value of [origin, origin + ':443', origin + '/', origin + ':443/',
    'https://' + new URL(origin).hostname.toUpperCase() + ':443/']) {
    assert.equal(privateApiOrigin(value, 'aks-test'), origin);
  }
  for (const value of [origin.replace('https:', 'http:'), origin + ':8443', origin + '/api',
    origin + '/a/..', origin + '?x=1', origin + '#x', origin.replace('https://', 'https://user@'),
    origin.replace('https://', 'https://user:password@'), ' ' + origin, origin + '\n',
    origin + '\\', origin + '.', origin.replace('aks-test-', 'aks-other-'), null]) {
    assert.throws(() => privateApiOrigin(value, 'aks-test'));
  }
  const other = origin.replace('synthetic.id', 'different.id');
  assert.notEqual(privateApiOrigin(other, 'aks-test'), privateApiOrigin(origin, 'aks-test'));
});
test('real activation CLI validates approval, blocks stale state and re-applies only the runtime config volume as the platform', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-activation-'));
  const fake = join(directory, 'kubectl'), statePath = join(directory, 'state.json');
  const cfg = join(directory, 'customer.json'), plan = join(directory, 'plan.json');
  const expected = 'https://aks-test-synthetic.id.privatelink.westus3.azmk8s.io';
  const image = 'synthetic.azurecr.io/backend@sha256:' + 'a'.repeat(64);
  const state = {
    server: expected + ':443', applies: 0, deploymentApplies: 0, dryRunDrift: false,
    deployment: { metadata: { name: 'litellm', namespace: 'litellm', uid: 'original-uid', resourceVersion: '41',
      managedFields: [
        { manager: 'llmgw-migration', operation: 'Apply', apiVersion: 'apps/v1', fieldsType: 'FieldsV1', fieldsV1: {
          'f:spec': { 'f:template': { 'f:spec': {
            'f:containers': { 'k:{"name":"litellm"}': { '.': {}, 'f:name': {}, 'f:image': {} } },
            'f:volumes': { 'k:{"name":"config"}': { '.': {}, 'f:name': {}, 'f:configMap': { 'f:name': {} } } } } } } } },
        // An earlier activation release patched the volume reference with kubectl's default patch manager.
        { manager: 'kubectl-patch', operation: 'Update', apiVersion: 'apps/v1', fieldsType: 'FieldsV1', fieldsV1: {
          'f:spec': { 'f:template': { 'f:spec': { 'f:volumes': { 'k:{"name":"config"}': {
            'f:configMap': { 'f:name': {} } } } } } } } }] },
      spec: { template: { metadata: { annotations: { preserved: 'yes' } },
        spec: { containers: [{ name: 'litellm', image }],
          volumes: [{ name: 'config', configMap: { name: 'litellm-config-old' } }] } } } },
    configMap: { metadata: { name: 'litellm-config-old' }, data: {
      'config.yaml': 'model_list: []\ngeneral_settings:\n  disable_spend_logs: false\n  store_prompts_in_spend_logs: true\n' } },
  };
  try {
    await copyFile(new URL('../../test/fixtures/kubectl.mjs', import.meta.url), fake);
    await chmod(fake, 0o700);
    await writeFile(statePath, JSON.stringify(state));
    await writeFile(cfg, JSON.stringify({ l3Audit: { enabled: true },
      application: { backendImage: image, authentication: { mode: 'native' } },
      parameters: { platform: { stage4Aks: { name: 'aks-test' } } } }));
    const env = { ...process.env, PATH: `${directory}:${process.env.PATH}`, L3_FIXTURE_STATE: statePath };
    const base = [new URL('../src/activation.js', import.meta.url).pathname];
    const rest = [cfg, join(directory, 'synthetic-kubeconfig'), expected, plan, 'enable'];
    const planned = await execute(process.execPath, [...base, 'plan', ...rest], { env });
    const approved = JSON.parse(planned.stdout).planSha256 as string;
    assert.equal(approved, sha(await readFile(plan)));
    assert.equal(JSON.parse(await readFile(plan, 'utf8')).expectedServer, expected);
    state.server = expected.replace('synthetic.id', 'other.id') + ':443';
    await writeFile(statePath, JSON.stringify(state));
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, approved], { env }), error =>
      error instanceof Error && error.message.includes('Kubeconfig does not target the approved AKS API'));
    assert.equal(JSON.parse(await readFile(statePath, 'utf8')).applies, 0);
    state.server = expected + ':443/';
    await writeFile(statePath, JSON.stringify(state));
    const originalConfig = await readFile(cfg, 'utf8');
    const missingOptIn = JSON.parse(originalConfig);
    delete missingOptIn.l3Audit;
    await writeFile(cfg, JSON.stringify(missingOptIn));
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, approved], { env }), error =>
      error instanceof Error && error.message.includes('Enable requires top-level l3Audit.enabled=true'));
    assert.equal(JSON.parse(await readFile(statePath, 'utf8')).applies, 0);
    missingOptIn.l3Audit = { enabled: 'true' };
    await writeFile(cfg, JSON.stringify(missingOptIn));
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, approved], { env }), error =>
      error instanceof Error && error.message.includes('Enable requires top-level l3Audit.enabled=true'));
    await writeFile(cfg, originalConfig);
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, '0'.repeat(64)], { env }));
    assert.equal(JSON.parse(await readFile(statePath, 'utf8')).applies, 0);
    state.deployment.metadata.uid = 'changed-uid';
    await writeFile(statePath, JSON.stringify(state));
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, approved], { env }));
    assert.equal(JSON.parse(await readFile(statePath, 'utf8')).applies, 0);
    state.deployment.metadata.uid = 'original-uid';
    state.dryRunDrift = true;
    await writeFile(statePath, JSON.stringify(state));
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, approved], { env }), error =>
      error instanceof Error && error.message.includes('Server dry-run changes more than the runtime configuration reference'));
    const drifted = JSON.parse(await readFile(statePath, 'utf8'));
    assert.equal(drifted.applies, 0); assert.equal(drifted.deploymentApplies, 0);
    await unlink(plan + '.deployment.json');
    state.dryRunDrift = false;
    await writeFile(statePath, JSON.stringify(state));
    const executed = await execute(process.execPath, [...base, 'execute', ...rest, approved], { env });
    assert.equal(JSON.parse(executed.stdout).rolloutVerified, true);
    const after = JSON.parse(await readFile(statePath, 'utf8'));
    assert.equal(after.applies, 1); assert.equal(after.deploymentApplies, 1);
    assert.equal(after.deployment.spec.template.spec.volumes[0].configMap.name, after.configMap.metadata.name);
    assert.deepEqual(after.deployment.metadata.managedFields.map((entry: { manager: string }) => entry.manager), ['llmgw-migration']);
    const applied = JSON.parse(await readFile(plan + '.deployment.json', 'utf8'));
    assert.deepEqual(applied.metadata, { name: 'litellm', namespace: 'litellm', uid: 'original-uid', resourceVersion: '41' });
    assert.deepEqual(applied.spec, { template: { spec: { containers: [{ name: 'litellm', image }],
      volumes: [{ name: 'config', configMap: { name: after.configMap.metadata.name } }] } } });
    assert.equal(after.deployment.spec.template.metadata.annotations.preserved, 'yes');
    assert.equal(after.deployment.spec.template.spec.containers[0].image, image);
    assert.equal(yamlParse(after.configMap.data['config.yaml']).general_settings.store_prompts_in_spend_logs, false);
    await assert.rejects(execute(process.execPath, [...base, 'execute', ...rest, approved], { env }));
  } finally { await rm(directory, { recursive: true }); }
});
