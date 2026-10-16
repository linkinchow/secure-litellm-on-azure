import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile } from 'node:fs/promises';
import { isIP } from 'node:net';
import { object } from './record.js';

export type AzureRead = (arguments_: string[]) => Promise<unknown>;
const execute = promisify(execFile);
const azureRead: AzureRead = async arguments_ => {
  let stdout: string;
  try {
    ({ stdout } = await execute('az', [...arguments_, '--output', 'json', '--only-show-errors'],
      { timeout: 120000, maxBuffer: 1024 * 1024 }));
  } catch {
    throw new Error('Azure metadata read failed; check login, resource scope and Reader permissions');
  }
  try { return JSON.parse(stdout); }
  catch { throw new Error('Azure metadata response is not valid JSON'); }
};
function items(value: unknown, name: string): unknown[] {
  if (!Array.isArray(value) || !value.length) throw new Error(`Missing ${name} metadata`);
  return value;
}
export function privateIPv4(value: unknown): value is string {
  return typeof value === 'string' && isIP(value) === 4
    && /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(value);
}
export async function discoverPrivateIps(config: unknown, outputs: unknown, read: AzureRead = azureRead): Promise<string[]> {
  const cfg = object(config), scope = object(cfg.azure);
  const subscription = scope.subscriptionId, group = object(cfg.target).resourceGroup;
  if (typeof subscription !== 'string' || !/^[a-f0-9-]{36}$/i.test(subscription)
    || typeof group !== 'string' || !/^[a-zA-Z0-9_.()-]{1,90}$/.test(group)) {
    throw new Error('Invalid customer subscription or target resource group');
  }
  const output = object(outputs), settings = object(object(output.deploymentSettings).value);
  const url = settings.storageUrl;
  if (typeof url !== 'string' || !/^https:\/\/[a-z0-9]{3,24}\.blob\.core\.windows\.net\/?$/.test(url)) {
    throw new Error('Missing or invalid deploymentSettings.value.storageUrl in outputs.json');
  }
  const storage = new URL(url).hostname.split('.')[0]!;
  const prefix = `/subscriptions/${subscription}/resourceGroups/${group}/providers/`;
  const network = object(object(object(cfg.parameters).platform).stage4Network);
  if (typeof network.virtualNetworkName !== 'string' || typeof network.privateEndpointSubnetName !== 'string') {
    throw new Error('Customer CFG requires the target VNet and private endpoint subnet');
  }
  const subnet = `${prefix}Microsoft.Network/virtualNetworks/${network.virtualNetworkName}/subnets/${network.privateEndpointSubnetName}`;
  const ids = items(object(output.privateEndpoints).value, 'privateEndpoints.value');
  const expectedIds = new Set(['blob', 'monitor'].map(kind =>
    `${prefix}Microsoft.Network/privateEndpoints/l3-${storage}-${kind}-pe`.toLowerCase()));
  if (ids.length !== 2 || ids.some(id => typeof id !== 'string' || !expectedIds.delete(id.toLowerCase()))
    || expectedIds.size) throw new Error('Deployment outputs must identify the two expected L3 private endpoints in the CFG target RG');
  const result = new Set<string>();
  for (const value of ids) {
    const id = String(value);
    const pe = object(await read(['network', 'private-endpoint', 'show', '--subscription', subscription,
      '--ids', id, '--query',
      '{id:id,provisioningState:provisioningState,subnet:subnet.id,connections:privateLinkServiceConnections[].{target:privateLinkServiceId,groups:groupIds,status:privateLinkServiceConnectionState.status},nics:networkInterfaces[].id}']));
    if (typeof pe.id !== 'string' || pe.id.toLowerCase() !== id.toLowerCase()
      || pe.provisioningState !== 'Succeeded' || typeof pe.subnet !== 'string' || pe.subnet.toLowerCase() !== subnet.toLowerCase()) {
      throw new Error('L3 private endpoint is not Succeeded in the expected subnet');
    }
    const blob = id.toLowerCase().endsWith('-blob-pe');
    const connections = items(pe.connections, 'private endpoint connections');
    if (connections.length !== 1) throw new Error('Unexpected L3 private endpoint connection count');
    const connection = object(connections[0]);
    const target = blob ? `${prefix}Microsoft.Storage/storageAccounts/${storage}`
      : `${prefix}Microsoft.Insights/privateLinkScopes/`;
    if (connection.status !== 'Approved' || !Array.isArray(connection.groups)
      || connection.groups.length !== 1 || connection.groups[0] !== (blob ? 'blob' : 'azuremonitor')
      || typeof connection.target !== 'string'
      || (blob ? connection.target.toLowerCase() !== target.toLowerCase()
        : !connection.target.toLowerCase().startsWith(target.toLowerCase()))) {
      throw new Error('L3 private endpoint target or approval does not match the deployment');
    }
    for (const nicValue of items(pe.nics, 'private endpoint NICs')) {
      if (typeof nicValue !== 'string'
        || !nicValue.toLowerCase().startsWith(`${prefix}Microsoft.Network/networkInterfaces/`.toLowerCase())) {
        throw new Error('Private endpoint NIC is outside the expected target RG');
      }
      const nic = object(await read(['network', 'nic', 'show', '--subscription', subscription, '--ids', nicValue,
        '--query', '{id:id,ips:ipConfigurations[].privateIPAddress}']));
      if (typeof nic.id !== 'string' || nic.id.toLowerCase() !== nicValue.toLowerCase()) {
        throw new Error('Azure returned a different NIC');
      }
      for (const ip of items(nic.ips, 'NIC IP configurations')) {
        if (!privateIPv4(ip)) throw new Error('NIC contains a missing or non-private IPv4 address');
        result.add(ip);
      }
    }
  }
  return [...result].sort();
}
if (process.argv[1]?.endsWith('/private-endpoints.js')) {
  const [configPath, outputsPath] = process.argv.slice(2);
  try {
    if (!configPath || !outputsPath) throw new Error('Use private-endpoints.js CFG OUTPUTS_JSON');
    const config: unknown = JSON.parse(await readFile(configPath, 'utf8'));
    const outputs: unknown = JSON.parse(await readFile(outputsPath, 'utf8'));
    console.log(JSON.stringify(await discoverPrivateIps(config, outputs)));
  } catch (error) {
    console.error(error instanceof SyntaxError ? 'Invalid JSON input; values withheld'
      : error instanceof Error ? error.message : 'L3 private endpoint discovery failed');
    process.exitCode = 1;
  }
}
