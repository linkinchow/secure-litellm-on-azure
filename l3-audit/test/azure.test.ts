import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { BlobClient, BlockBlobClient, ContainerClient } from '@azure/storage-blob';
import { AzureDelivery, type Settings } from '../src/azure.js';
import { Capture, type RecordEnvelope, sha } from '../src/record.js';
import { runAdmin } from '../src/admin.js';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const config: Settings = {
  storageUrl: 'https://syntheticaudit.blob.core.windows.net',
  dce: 'https://synthetic.eastus-1.ingest.monitor.azure.com', dcr: 'dcr-' + 'a'.repeat(32),
  subscription: '11111111-1111-4111-8111-111111111111', resourceGroup: 'synthetic',
  storageAccount: 'syntheticaudit',
};
function record(): RecordEnvelope {
  const items: RecordEnvelope[] = [];
  const capture = new Capture('http', '/v1/responses', 'Bearer sensitive', { bytes: 0 }, r => items.push(r));
  capture.add('request', Buffer.from('synthetic-private-body')); capture.finish('completed');
  return items[0]!;
}
test('Azure delivery uploads full envelope and LAW receives only index; idempotent collision verifies hash', async () => {
  const sink = new AzureDelivery(config), item = record();
  let uploaded = '', sent = '';
  mock.method(sink.credential, 'getToken', async () => ({ token: 'synthetic-token', expiresOnTimestamp: Date.now() + 10000 }));
  mock.method(BlockBlobClient.prototype, 'upload', async (text: string) => { uploaded = text; return {}; });
  mock.method(globalThis, 'fetch', async (_input: unknown, init?: RequestInit) => {
    sent = String(init?.body); return new Response(null, { status: 204 });
  });
  try {
    await sink.content(item); await sink.index(item.index);
    assert.ok(uploaded.includes('c3ludGhldGljLXByaXZhdGUtYm9keQ=='));
    assert.ok(!sent.includes('c3ludGhldGlj')); assert.ok(!sent.includes('sensitive'));
    mock.method(BlockBlobClient.prototype, 'upload', async () => { throw { statusCode: 412 }; });
    mock.method(BlobClient.prototype, 'getProperties', async () => ({ metadata: { envelopehash: sha(uploaded) } }));
    await sink.content(item);
    mock.method(BlobClient.prototype, 'getProperties', async () => ({ metadata: { envelopehash: 'wrong' } }));
    await assert.rejects(sink.content(item), /conflict/);
  } finally { mock.restoreAll(); }
});
test('administrator hold uses an independent case container and legal hold; held get works after normal expiry', async () => {
  const sink = new AzureDelivery(config), item = record(), bytes = Buffer.from(JSON.stringify(item));
  const directory = await mkdtemp(join(tmpdir(), 'l3-admin-'));
  const calls: string[] = [], uploaded: string[] = [], deleted: string[] = [];
  mock.method(sink.credential, 'getToken', async () => ({ token: 'synthetic-token', expiresOnTimestamp: Date.now() + 10000 }));
  mock.method(BlobClient.prototype, 'getProperties', async () => ({
    contentLength: bytes.length, etag: 'synthetic-etag', metadata: { envelopehash: sha(bytes) },
  }));
  mock.method(BlobClient.prototype, 'downloadToBuffer', async () => bytes);
  mock.method(BlockBlobClient.prototype, 'uploadData', async function(this: BlockBlobClient) {
    uploaded.push(this.containerName); return {};
  });
  mock.method(ContainerClient.prototype, 'delete', async function(this: ContainerClient) {
    deleted.push(this.containerName); return {};
  });
  mock.method(globalThis, 'fetch', async (input: unknown, init?: RequestInit) => {
    calls.push(String(init?.method) + ' ' + String(input));
    return Response.json({ properties: { legalHold: { hasLegalHold: true } } });
  });
  mock.method(console, 'log', () => {});
  try {
    await runAdmin(['hold', '--case', 'caseone', '--blob', item.index.blob], sink);
    assert.deepEqual(uploaded, ['l3-hold-caseone']);
    assert.ok(calls.some(call => call.includes('/l3-hold-caseone/setLegalHold')));
    const output = join(directory, 'raw.json');
    await runAdmin(['get', '--case', 'caseone', '--id', item.index.id, '--output', output], sink);
    assert.equal(await readFile(output, 'utf8'), bytes.toString());
    await assert.rejects(runAdmin(['get', '--case', 'caseone', '--id', item.index.id, '--output', output], sink));
    await assert.rejects(runAdmin(['release', '--case', 'caseone', '--confirm', 'casetwo'], sink));
    await runAdmin(['release', '--case', 'caseone', '--confirm', 'caseone'], sink);
    assert.deepEqual(deleted, ['l3-hold-caseone']);
    mock.method(globalThis, 'fetch', async () => Response.json({ properties: { legalHold: { hasLegalHold: false } } }));
    await assert.rejects(runAdmin(['hold', '--case', 'casetwo', '--blob', item.index.blob], sink), /not active/);
  } finally { mock.restoreAll(); await rm(directory, { recursive: true }); }
});
