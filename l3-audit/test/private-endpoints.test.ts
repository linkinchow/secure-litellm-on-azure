import { test } from 'node:test';
import assert from 'node:assert/strict';
import { discoverPrivateIps, type AzureRead } from '../src/private-endpoints.js';
import { render, type DeploymentSettings } from '../src/render.js';

const subscription = '11111111-1111-4111-8111-111111111111';
const prefix = `/subscriptions/${subscription}/resourceGroups/customer-rg/providers/`;
const config = { azure: { subscriptionId: subscription }, target: { resourceGroup: 'customer-rg' },
  parameters: { platform: { stage4Network: { virtualNetworkName: 'customer-vnet', privateEndpointSubnetName: 'snet-pe' } } } };
const peId = (kind: string): string => `${prefix}Microsoft.Network/privateEndpoints/l3-customeraudit-${kind}-pe`;
const nicId = (kind: string): string => `${prefix}Microsoft.Network/networkInterfaces/${kind}-nic`;
const outputs = { deploymentSettings: { value: { storageUrl: 'https://customeraudit.blob.core.windows.net/' } },
  privateEndpoints: { value: [peId('blob'), peId('monitor')] } };
function responses(): Map<string, unknown> {
  const result = new Map<string, unknown>();
  for (const kind of ['blob', 'monitor']) {
    result.set(peId(kind), { id: peId(kind), provisioningState: 'Succeeded',
      subnet: `${prefix}Microsoft.Network/virtualNetworks/customer-vnet/subnets/snet-pe`,
      nics: [nicId(kind)], connections: [{ status: 'Approved', groups: [kind === 'blob' ? 'blob' : 'azuremonitor'],
        target: `${prefix}${kind === 'blob' ? 'Microsoft.Storage/storageAccounts/customeraudit'
          : 'Microsoft.Insights/privateLinkScopes/customer-scope'}` }] });
    result.set(nicId(kind), { id: nicId(kind), ips: kind === 'blob' ? ['10.60.8.4'] : ['10.60.8.5', '10.60.8.6', '10.60.8.5'] });
  }
  return result;
}
function reader(result: Map<string, unknown>, calls: string[][]): AzureRead {
  return async args => {
    calls.push(args);
    assert.ok(args.includes('show'));
    assert.ok(!args.some(arg => ['create', 'update', 'delete', 'list-keys'].includes(arg)));
    assert.equal(args[args.indexOf('--subscription') + 1], subscription);
    const id = args[args.indexOf('--ids') + 1]!;
    assert.ok(result.has(id), `Unexpected resource ${id}`);
    return result.get(id);
  };
}
test('discovers all actual NIC IPs from both output PE IDs without modifying Azure', async () => {
  const calls: string[][] = [];
  assert.deepEqual(await discoverPrivateIps(config, outputs, reader(responses(), calls)),
    ['10.60.8.4', '10.60.8.5', '10.60.8.6']);
  assert.equal(calls.length, 4);
});
test('missing deployment outputs and wrong environment block discovery before any Azure call', async () => {
  const invalid = [
    {},
    { ...outputs, privateEndpoints: { value: [peId('blob')] } },
    { ...outputs, privateEndpoints: { value: [peId('blob'), peId('blob')] } },
    { ...outputs, privateEndpoints: { value: [peId('blob'), peId('monitor').replace('customer-rg', 'other-rg')] } },
  ];
  for (const output of invalid) {
    const calls: string[][] = [];
    await assert.rejects(discoverPrivateIps(config, output, reader(responses(), calls)));
    assert.equal(calls.length, 0);
  }
});
test('unapproved, wrong-subnet, wrong-target, foreign NIC and public IP results are rejected', async () => {
  const changes = [
    { provisioningState: 'Updating' },
    { subnet: 'other-subnet' },
    { connections: [{ status: 'Pending', groups: ['blob'], target: `${prefix}Microsoft.Storage/storageAccounts/customeraudit` }] },
    { connections: [{ status: 'Approved', groups: ['blob'], target: `${prefix}Microsoft.Storage/storageAccounts/other` }] },
    { nics: [nicId('blob').replace('customer-rg', 'other-rg')] },
  ];
  for (const change of changes) {
    const result = responses();
    result.set(peId('blob'), { ...result.get(peId('blob')) as object, ...change });
    await assert.rejects(discoverPrivateIps(config, outputs, reader(result, [])));
  }
  for (const ips of [[], ['8.8.8.8'], ['REPLACE_BLOB_PE_IP'], ['10.60.8.4', '20.1.2.3']]) {
    const result = responses(); result.set(nicId('monitor'), { id: nicId('monitor'), ips });
    await assert.rejects(discoverPrivateIps(config, outputs, reader(result, [])));
  }
});
test('rendering identifies invalid field without printing its value', () => {
  const settings: DeploymentSettings = { image: 'customer.azurecr.io/l3@sha256:' + 'a'.repeat(64),
    clientId: subscription, storageUrl: 'https://customeraudit.blob.core.windows.net/',
    dce: 'https://customer.westus3-1.ingest.monitor.azure.com', dcr: 'dcr-' + 'a'.repeat(32),
    privateEndpointIps: ['10.60.8.4', '10.60.8.5'] };
  for (const key of ['image', 'clientId', 'storageUrl', 'dce', 'dcr'] as const) {
    assert.throws(() => render({ ...settings, [key]: 'private-invalid-value' }), error =>
      error instanceof Error && error.message.includes(key) && !error.message.includes('private-invalid-value'));
  }
  for (const ips of [[], ['REPLACE_BLOB_PE_IP']]) {
    assert.throws(() => render({ ...settings, privateEndpointIps: ips }), /privateEndpointIps/);
  }
});
