import { writeFile } from 'node:fs/promises';
import { AzureDelivery, settings } from './azure.js';
import { object, sha, type RecordEnvelope } from './record.js';

function blobName(value: string): string {
  if (!/^content\/\d{4}\/\d{2}\/\d{2}\/[a-f0-9-]{36}\.json$/.test(value)) throw new Error('Use blob reference from LAW');
  return value;
}
export async function runAdmin(input: string[], sink = new AzureDelivery(settings())): Promise<void> {
  const args = [...input], command = args.shift();
  function option(name: string, required = true): string {
    const position = args.indexOf('--' + name);
    const value = position >= 0 ? args[position + 1] : undefined;
    if (required && !value) throw new Error(`Missing --${name}`);
    return value ?? '';
  }
  const config = sink.config;
  async function arm(path: string, method: string, body?: unknown): Promise<Record<string, unknown>> {
    if (!/^[a-f0-9-]{36}$/i.test(config.subscription) || !/^[\w().-]+$/.test(config.resourceGroup)
      || !/^[a-z0-9]{3,24}$/.test(config.storageAccount)
      || new URL(config.storageUrl).hostname !== `${config.storageAccount}.blob.core.windows.net`) {
      throw new Error('Invalid or mismatched ARM audit scope');
    }
    const token = await sink.credential.getToken('https://management.azure.com/.default');
    const response = await fetch(`https://management.azure.com/subscriptions/${config.subscription}/resourceGroups/${config.resourceGroup}/providers/Microsoft.Storage/storageAccounts/${config.storageAccount}/blobServices/default/containers/${path}?api-version=2025-06-01`, {
      method, redirect: 'error', signal: AbortSignal.timeout(30000),
      headers: { authorization: `Bearer ${token.token}`, 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) throw new Error(`Hold management failed: ${response.status}`);
    const text = await response.text();
    return text ? object(JSON.parse(text)) : {};
  }
  if (command === 'query') {
    const workspace = option('workspace');
    if (!/^[a-f0-9-]{36}$/i.test(workspace)) throw new Error('Invalid LAW workspace ID');
    const hours = Number(option('hours', false) || 24);
    if (!Number.isInteger(hours) || hours < 1 || hours > 720) throw new Error('Hours must be 1..720');
    const id = option('id', false), model = option('model', false), key = option('key-hash', false);
    for (const value of [id, model, key]) if (value && !/^[a-zA-Z0-9_.:/-]{1,128}$/.test(value)) throw new Error('Invalid query filter');
    const filters = [[id, 'id'], [model, 'model'], [key, 'keyHash']]
      .filter(([value]) => value).map(([value, field]) => `| where tostring(Record.${field}) == '${value}'`).join('\n');
    const query = `L3Audit_CL | where TimeGenerated > ago(${hours}h)
| where tostring(Record.path) != '/l3/health'
${filters}
| extend id=tostring(Record.id)
| summarize arg_max(TimeGenerated, *) by id
| top 100 by TimeGenerated desc
| project TimeGenerated, Record`;
    const token = await sink.credential.getToken('https://api.loganalytics.io/.default');
    const response = await fetch(`https://api.loganalytics.azure.com/v1/workspaces/${workspace}/query`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(60000),
      headers: { authorization: `Bearer ${token.token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ query, timespan: `PT${hours}H` }),
    });
    if (!response.ok) throw new Error(`LAW query failed: ${response.status}`);
    const result = object(await response.json());
    if (result.error || !Array.isArray(result.tables)) throw new Error('LAW query returned an invalid or partial response');
    console.log(JSON.stringify(result)); return;
  }
  const caseId = command === 'hold' || command === 'release' ? option('case') : '';
  if (caseId && !/^[a-z][a-z0-9]{2,30}$/.test(caseId)) throw new Error('Case must be lowercase alphanumeric, length 3..31');
  const container = `l3-hold-${caseId}`;
  if (command === 'release') {
    if (option('confirm') !== caseId) throw new Error('Release requires matching --confirm case');
    await arm(`${container}/clearLegalHold`, 'POST', { tags: [caseId] });
    // Keep lifecycle independent: remove a case only after its hold is explicitly cleared.
    await sink.blobs.getContainerClient(container).delete();
    console.log(JSON.stringify({ releasedAndDeleted: caseId })); return;
  }
  if (command !== 'get' && command !== 'hold') throw new Error('Use query, get, hold or release');
  const heldCase = command === 'get' ? option('case', false) : '';
  if (heldCase && !/^[a-z][a-z0-9]{2,30}$/.test(heldCase)) throw new Error('Invalid held case');
  const heldId = heldCase ? option('id') : '';
  if (heldId && !/^[a-f0-9-]{36}$/.test(heldId)) throw new Error('Invalid held call ID');
  const name = heldCase ? `${heldId}.json` : blobName(option('blob'));
  const source = sink.blobs.getContainerClient(heldCase ? `l3-hold-${heldCase}` : 'l3-content').getBlobClient(name);
  const properties = await source.getProperties();
  if (!properties.contentLength || properties.contentLength > 24 * 1024 * 1024 || !properties.etag) throw new Error('Unexpected audit object size');
  const bytes = await source.downloadToBuffer(0, undefined, { conditions: { ifMatch: properties.etag } });
  const record: RecordEnvelope = JSON.parse(bytes.toString('utf8'));
  if (record.schemaVersion !== 1 || (heldCase ? record.index.id !== heldId : record.index.blob !== name)
    || sha(JSON.stringify(record.parts)) !== record.index.contentSha256
    || sha(bytes) !== properties.metadata?.envelopehash) throw new Error('Audit object integrity check failed');
  if (command === 'get') {
    await writeFile(option('output'), bytes, { mode: 0o600, flag: 'wx' });
    console.log(JSON.stringify({ id: record.index.id, saved: true, gap: record.index.gap })); return;
  }
  await arm(container, 'PUT', { properties: { publicAccess: 'None' } });
  await arm(`${container}/setLegalHold`, 'POST', { tags: [caseId] });
  const target = sink.blobs.getContainerClient(container).getBlockBlobClient(record.index.id + '.json');
  try {
    await target.uploadData(bytes, { conditions: { ifNoneMatch: '*' },
      metadata: { envelopehash: sha(bytes) }, blobHTTPHeaders: { blobContentType: 'application/json' } });
  } catch (error) {
    if (![409, 412].includes((error as { statusCode?: number }).statusCode ?? 0)) throw error;
    const current = await target.getProperties();
    if (current.metadata?.envelopehash !== sha(bytes)) throw new Error('Hold object conflict');
  }
  const state = await arm(container, 'GET');
  if (object(object(state.properties).legalHold).hasLegalHold !== true) throw new Error('Hold is not active after copy');
  console.log(JSON.stringify({ case: caseId, id: record.index.id, held: true, gap: record.index.gap }));
}
if (process.argv[1]?.endsWith('/admin.js')) runAdmin(process.argv.slice(2)).catch(error => {
  console.error(JSON.stringify({ error: 'l3-administrator-operation-failed',
    errorType: error instanceof Error ? error.constructor.name : 'unknown',
    guidance: 'Check arguments, private connectivity, Azure permissions and metadata; service error bodies withheld.' }));
  process.exitCode = 1;
});
