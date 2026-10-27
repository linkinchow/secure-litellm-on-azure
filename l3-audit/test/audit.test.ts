import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readdir, rm, writeFile, mkdir, chmod, symlink, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { Capture, CONTENT_LIMIT, MEMORY_LIMIT, parse, type RecordEnvelope } from '../src/record.js';
import { Queue, spoolFailureCode, type Delivery } from '../src/queue.js';
import { FrameTap, Session } from '../src/websocket.js';
import { createProxy } from '../src/proxy.js';
import { render } from '../src/render.js';
import type { Duplex } from 'node:stream';
import { pgConfiguration, patchFor, snapshot } from '../src/activation.js';
import { parse as yamlParse } from 'yaml';

function frame(value: unknown, masked = false): Buffer {
  const body = Buffer.from(typeof value === 'string' ? value : JSON.stringify(value));
  assert.ok(body.length < 65536);
  const header = Buffer.alloc(body.length < 126 ? 2 : 4);
  header[0] = 129; header[1] = (masked ? 128 : 0) | (body.length < 126 ? body.length : 126);
  if (body.length >= 126) header.writeUInt16BE(body.length, 2);
  const mask = Buffer.from([1, 2, 3, 4]);
  return masked ? Buffer.concat([header, mask, body.map((byte, i) => byte ^ mask[i % 4]!)]) : Buffer.concat([header, body]);
}
function sample(): RecordEnvelope {
  const records: RecordEnvelope[] = [];
  const capture = new Capture('http', '/v1/responses', 'Bearer private-vkey', { bytes: 0 }, record => records.push(record));
  capture.add('request', Buffer.from('{"model":"coding","input":"synthetic-private-text"}'));
  capture.readRequest(); capture.add('response', Buffer.from('{"status":"completed"}')); capture.finish('completed');
  return records[0]!;
}
test('raw content only in Blob envelope; LAW index excludes key, headers and content', () => {
  const record = sample();
  assert.equal(record.index.model, 'coding');
  assert.equal(record.index.gap, '');
  assert.equal(record.index.outcome, 'completed');
  assert.ok(!JSON.stringify(record.index).includes('synthetic-private-text'));
  assert.ok(!JSON.stringify(record).includes('private-vkey'));
  assert.match(record.index.keyHash, /^[a-f0-9]{64}$/);
  assert.equal(Buffer.from(record.parts[0]!.data, 'base64').toString(), '{"model":"coding","input":"synthetic-private-text"}');
});
test('large body metadata does not materialize arbitrary code/tool JSON trees', () => {
  const padding = 'x'.repeat(100000);
  const value = JSON.stringify({ type: 'response.completed', response: {
    output: [{ type: 'message', id: 'not-the-response', content: padding }],
    id: 'actual-response', status: 'completed' }, model: 'coding' });
  const metadata = parse(value);
  assert.equal(metadata.model, 'coding');
  assert.equal(metadata.type, 'response.completed');
  assert.deepEqual(metadata.response, { id: 'actual-response', status: 'completed' });
  assert.ok(!JSON.stringify(metadata).includes(padding));
  assert.equal(parse(JSON.stringify({ input: padding, previous_response_id: 'resp-one', model: 'coding' })).previous_response_id, 'resp-one');
});
test('content/memory limits explicitly mark gaps; embedding vectors excluded', () => {
  const records: RecordEnvelope[] = [], budget = { bytes: 0 };
  const capture = new Capture('http', '/v1/responses', undefined, budget, item => records.push(item));
  capture.add('request', Buffer.alloc(CONTENT_LIMIT)); capture.add('response', Buffer.alloc(1)); capture.finish('completed');
  assert.equal(records[0]!.index.gap, 'content-limit'); assert.equal(budget.bytes, 0);
  const memory = new Capture('http', '/v1/responses', undefined, { bytes: MEMORY_LIMIT }, item => records.push(item));
  memory.add('request', Buffer.from('x')); memory.finish('completed');
  assert.equal(records[1]!.index.gap, 'memory-budget');
  const embedding = new Capture('http', '/v1/embeddings', undefined, budget, item => records.push(item));
  embedding.add('request', Buffer.from('input')); embedding.add('response', Buffer.from('vector')); embedding.finish('completed');
  assert.equal(records[2]!.parts.length, 1);
});
test('persistent queue survives restart; content precedes index; retries do not re-upload after checkpoint', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-test-'));
  let content = 0, index = 0, fail = true;
  const sink: Delivery = {
    async content() { content++; },
    async index() { index++; if (fail) throw new Error('synthetic service failure'); },
  };
  try {
    const first = new Queue(directory, sink); await first.init(); first.enqueue(sample()); await first.flush();
    assert.equal((await readdir(directory)).length, 1);
    await first.tick(); assert.equal(content, 1); assert.equal(index, 1);
    const restarted = new Queue(directory, sink); await restarted.init();
    fail = false; await restarted.tick();
    assert.equal(content, 1); assert.equal(index, 2); assert.equal((await readdir(directory)).length, 0);
  } finally { await rm(directory, { recursive: true }); }
});
test('Azure Disk lost+found directory remains untouched and does not prevent queue init or recovery', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-azure-disk-'));
  const recovery = join(directory, 'lost+found');
  const delivered: string[] = [];
  const sink: Delivery = { async content() {}, async index(value) { delivered.push(value.id); } };
  try {
    await mkdir(recovery, { mode: 0o700 });
    await writeFile(join(recovery, 'filesystem-recovery-metadata'), 'do-not-read-or-remove');
    await chmod(recovery, 0);
    const queue = new Queue(directory, sink); await queue.init();
    assert.equal(queue.counters.diskBytes, 0);
    const item = sample(); queue.enqueue(item); await queue.flush();
    const restarted = new Queue(directory, sink); await restarted.init(); await restarted.tick();
    assert.deepEqual(delivered, [item.index.id]);
    assert.deepEqual(await readdir(directory), ['lost+found']);
    assert.equal((await stat(recovery)).mode & 0o777, 0);
  } finally { await chmod(recovery, 0o700); await rm(directory, { recursive: true }); }
});
test('only real lost+found directory is ignored; unknown entries, links and queue-named directories fail explicitly', async () => {
  for (const kind of ['unknown-directory', 'queue-directory', 'lost-found-file', 'lost-found-symlink']) {
    const directory = await mkdtemp(join(tmpdir(), 'l3-unexpected-'));
    try {
      if (kind === 'unknown-directory') await mkdir(join(directory, 'unexpected'));
      if (kind === 'queue-directory') await mkdir(join(directory, 'record.json'));
      if (kind === 'lost-found-file') await writeFile(join(directory, 'lost+found'), 'unexpected-file');
      if (kind === 'lost-found-symlink') await symlink('/tmp', join(directory, 'lost+found'));
      const queue = new Queue(directory, { async content() {}, async index() {} });
      await assert.rejects(queue.init(), error => spoolFailureCode(error) === 'unexpected-spool-entry');
    } finally { await rm(directory, { recursive: true }); }
  }
  assert.equal(spoolFailureCode(Object.assign(new Error('private-path'), { code: 'EACCES' })), 'EACCES');
  assert.equal(spoolFailureCode(new Error('private-path private-body')), 'spool-initialization-error');
});
test('disk budget rejects audit only; expired records produce gap index and are removed', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-expiry-'));
  const gaps: string[] = [];
  const sink: Delivery = { async content() { throw new Error('must not upload expired'); },
    async index(value) { gaps.push(value.gap); } };
  try {
    const limited = new Queue(directory, sink, 1); await limited.init(); limited.enqueue(sample()); await limited.flush();
    assert.equal(limited.counters.dropped, 1);
    const queue = new Queue(directory, sink, 1e6, 1); await queue.init(); queue.enqueue(sample()); await queue.flush();
    await queue.tick(Date.now() + 1000);
    assert.deepEqual(gaps, ['delivery-expired']);
    assert.equal((await readdir(directory)).length, 0);
  } finally { await rm(directory, { recursive: true }); }
});
test('corrupt spool record is quarantined rather than blocking all following records', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-corrupt-'));
  let delivered = 0;
  const sink: Delivery = { async content() {}, async index() { delivered++; } };
  try {
    await writeFile(join(directory, '00000000-0000-4000-8000-000000000000.json'), '{invalid');
    const queue = new Queue(directory, sink); await queue.init(); queue.enqueue(sample()); await queue.flush();
    await queue.tick();
    assert.equal(delivered, 1); assert.equal(queue.counters.dropped, 1);
    assert.equal((await readdir(directory)).length, 1);
    assert.ok((await readdir(directory))[0]!.endsWith('.corrupt'));
  } finally { await rm(directory, { recursive: true }); }
});
test('passive frame parsing handles masking and arbitrarily split TCP chunks', () => {
  const messages: string[] = [], gaps: string[] = [], budget = { bytes: 0 };
  const tap = new FrameTap(data => messages.push(data.toString()), gap => gaps.push(gap), budget);
  const bytes = frame({ type: 'response.create', input: 'hello' }, true);
  for (const byte of bytes) tap.feed(Buffer.from([byte]));
  assert.equal(messages.length, 1); assert.equal(JSON.parse(messages[0]!).input, 'hello');
  assert.deepEqual(gaps, []); assert.equal(budget.bytes, 0);
});
test('WSS tool roundtrip records each turn and continuation link; disconnect is not completion', () => {
  const records: RecordEnvelope[] = [], gaps: string[] = [];
  const session = new Session('/v1/responses', 'Bearer secret', { bytes: 0 }, r => records.push(r), g => gaps.push(g));
  session.client.feed(frame({ type: 'response.create', model: 'coding', input: 'call tool' }, true));
  session.server.feed(frame({ type: 'response.created', response: { id: 'resp-one' } }));
  session.server.feed(frame({ type: 'response.completed', response: { id: 'resp-one', status: 'completed',
    output: [{ type: 'function_call', name: 'ping', call_id: 'call-one' }] } }));
  session.client.feed(frame({ type: 'response.create', model: 'coding', previous_response_id: 'resp-one',
    input: [{ type: 'function_call_output', call_id: 'call-one', output: 'OK' }] }, true));
  session.server.feed(frame({ type: 'response.created', response: { id: 'resp-two' } }));
  session.server.feed(frame({ type: 'response.completed', response: { id: 'resp-two', status: 'completed' } }));
  assert.equal(records.length, 2); assert.equal(records[0]!.index.responseHash, records[1]!.index.previousResponseHash);
  assert.equal(records[0]!.index.outcome, 'completed'); assert.equal(records[1]!.index.outcome, 'completed');
  assert.ok(Buffer.from(records[1]!.parts[0]!.data, 'base64').toString().includes('function_call_output'));
  session.client.feed(frame({ type: 'response.create', model: 'coding', input: 'unfinished' }, true)); session.close();
  assert.equal(records[2]!.index.outcome, 'disconnected'); assert.deepEqual(gaps, []);
});
async function listen(server: http.Server): Promise<URL> {
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  return new URL(`http://127.0.0.1:${address.port}`);
}
test('real HTTP/SSE keeps bytes and headers; upstream 400 and audit sink failure do not break inference', async () => {
  const upstream = http.createServer((request, response) => {
    request.resume();
    request.on('end', () => {
      if (request.url === '/v1/embeddings') { response.writeHead(200).end('{"data":[{"embedding":[1,2,3]}]}'); return; }
      if (request.url === '/v1/chat/completions') {
        response.writeHead(200, { 'content-type': 'text/event-stream' });
        response.write('data: {"choices":[]}\n\n'); response.end('data: [DONE]\n\n'); return;
      }
      response.writeHead(400, { 'content-type': 'application/json' }).end('{"error":"synthetic"}');
    });
  });
  const upstreamUrl = await listen(upstream), records: RecordEnvelope[] = [];
  const proxy = createProxy(upstreamUrl, r => records.push(r), () => ({})), url = await listen(proxy);
  try {
    const response = await fetch(new URL('/v1/responses', url), { method: 'POST',
      headers: { authorization: 'Bearer local-test-secret' }, body: '{"model":"coding","input":"test"}' });
    assert.equal(response.status, 400); assert.equal(await response.text(), '{"error":"synthetic"}');
    const sse = await fetch(new URL('/v1/chat/completions', url), { method: 'POST', body: '{"model":"coding"}' });
    assert.equal(await sse.text(), 'data: {"choices":[]}\n\ndata: [DONE]\n\n');
    const embeddings = await fetch(new URL('/v1/embeddings', url), { method: 'POST', body: '{"model":"embedding","input":"test"}' });
    assert.equal(await embeddings.text(), '{"data":[{"embedding":[1,2,3]}]}');
    assert.equal(records.length, 3);
    assert.equal(records[0]!.index.outcome, 'failed');
    assert.equal(records[1]!.index.outcome, 'completed');
    assert.ok(records[2]!.parts.every(part => part.direction === 'request'));
  } finally { proxy.closeAllConnections(); upstream.closeAllConnections(); await Promise.all([new Promise<void>(r => proxy.close(() => r())), new Promise<void>(r => upstream.close(() => r()))]); }
});
test('real WebSocket upgrade and frames traverse the transparent proxy unchanged', async () => {
  const upstream = http.createServer();
  const sockets = new Set<Duplex>();
  upstream.on('upgrade', (request, socket, head) => {
    sockets.add(socket); socket.on('close', () => sockets.delete(socket));
    assert.equal(request.headers['sec-websocket-extensions'], undefined);
    const accept = createHash('sha1').update(String(request.headers['sec-websocket-key']) + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const tap = new FrameTap(() => {
      socket.write(frame({ type: 'response.created', response: { id: 'synthetic' } }));
      socket.write(frame({ type: 'response.completed', response: { id: 'synthetic', status: 'completed', output: [] } }));
    }, () => {});
    socket.on('data', data => tap.feed(data)); if (head.length) tap.feed(head);
    socket.on('error', () => {}); socket.on('end', () => socket.end());
  });
  const records: RecordEnvelope[] = [], upstreamUrl = await listen(upstream);
  const proxy = createProxy(upstreamUrl, r => records.push(r), () => ({})), url = await listen(proxy);
  const client = new WebSocket(`ws://${url.host}/v1/responses`);
  try {
    await once(client, 'open');
    const received = new Promise<string>(resolve => client.addEventListener('message', event => {
      const value = String(event.data); if (value.includes('response.completed')) resolve(value);
    }));
    client.send('{"type":"response.create","model":"coding","input":"hello"}');
    assert.ok((await received).includes('response.completed'));
    assert.equal(records.length, 1); assert.equal(records[0]!.index.outcome, 'completed');
  } finally {
    for (const socket of sockets) socket.destroy();
    client.close();
    await new Promise<void>(resolve => setTimeout(resolve, 50));
    proxy.closeAllConnections(); upstream.closeAllConnections(); proxy.close(); upstream.close();
  }
});
test('real HTTP inference remains successful when persistent audit queue is full', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-fail-open-'));
  const queue = new Queue(directory, { async content() { throw new Error('must not deliver'); }, async index() {} }, 1);
  await queue.init();
  const upstream = http.createServer((request, response) => { request.resume(); request.on('end', () => response.end('OK')); });
  const upstreamUrl = await listen(upstream);
  const proxy = createProxy(upstreamUrl, r => queue.enqueue(r), () => queue.counters), url = await listen(proxy);
  try {
    const response = await fetch(new URL('/v1/responses', url), { method: 'POST', body: '{"model":"coding"}' });
    assert.equal(response.status, 200); assert.equal(await response.text(), 'OK');
    assert.equal(queue.counters.dropped, 1); assert.equal((await readdir(directory)).length, 0);
  } finally {
    proxy.closeAllConnections(); upstream.closeAllConnections();
    await Promise.all([new Promise<void>(r => proxy.close(() => r())), new Promise<void>(r => upstream.close(() => r()))]);
    await rm(directory, { recursive: true });
  }
});
test('manifest uses two replicas, per-replica PVC, immutable image and only private API source', () => {
  const documents = render({ image: 'example.azurecr.io/l3@sha256:' + 'a'.repeat(64),
    clientId: '11111111-1111-4111-8111-111111111111', storageUrl: 'https://example.blob.core.windows.net',
    dce: 'https://example.eastus-1.ingest.monitor.azure.com', dcr: 'dcr-' + 'a'.repeat(32),
    privateEndpointIps: ['10.0.1.4', '10.0.1.5'] });
  const text = JSON.stringify(documents);
  assert.ok(text.includes('"kind":"StatefulSet"')); assert.ok(text.includes('"replicas":2'));
  assert.ok(text.includes('volumeClaimTemplates')); assert.ok(!text.includes('NodePort'));
  assert.ok(!text.includes('LoadBalancer')); assert.ok(!text.includes('api_key'));
});
test('zone-redundant manifest spreads replicas across zones on ZRS spool disks', () => {
  const settings = { image: 'example.azurecr.io/l3@sha256:' + 'a'.repeat(64),
    clientId: '11111111-1111-4111-8111-111111111111', storageUrl: 'https://example.blob.core.windows.net',
    dce: 'https://example.eastus-1.ingest.monitor.azure.com', dcr: 'dcr-' + 'a'.repeat(32),
    privateEndpointIps: ['10.0.1.4', '10.0.1.5'] };
  type StatefulSet = { spec: {
    template: { spec: { topologySpreadConstraints: { topologyKey: string; whenUnsatisfiable: string }[] } };
    volumeClaimTemplates: { spec: { storageClassName: string } }[] } };
  const workload = (documents: object[]) =>
    documents.find(item => (item as { kind: string }).kind === 'StatefulSet') as StatefulSet;
  for (const regional of [render(settings), render({ ...settings, zoneRedundant: false })]) {
    assert.ok(!JSON.stringify(regional).includes('StorageClass'));
    assert.deepEqual(workload(regional).spec.template.spec.topologySpreadConstraints.map(item => item.topologyKey), ['kubernetes.io/hostname']);
    assert.equal(workload(regional).spec.volumeClaimTemplates[0]?.spec.storageClassName, 'managed-csi');
  }
  const zonal = render({ ...settings, zoneRedundant: true });
  const spoolClass = zonal[0] as { kind: string; metadata: { name: string; namespace?: string }; parameters: { skuName: string }; volumeBindingMode: string };
  assert.equal(spoolClass.kind, 'StorageClass');
  assert.equal(spoolClass.metadata.namespace, undefined);
  assert.equal(spoolClass.parameters.skuName, 'StandardSSD_ZRS');
  assert.equal(spoolClass.volumeBindingMode, 'WaitForFirstConsumer');
  assert.equal(workload(zonal).spec.volumeClaimTemplates[0]?.spec.storageClassName, spoolClass.metadata.name);
  assert.deepEqual(workload(zonal).spec.template.spec.topologySpreadConstraints.map(item => [item.topologyKey, item.whenUnsatisfiable]),
    [['kubernetes.io/hostname', 'ScheduleAnyway'], ['topology.kubernetes.io/zone', 'ScheduleAnyway']]);
  assert.throws(() => render({ ...settings, zoneRedundant: 'true' as unknown as boolean }), /zoneRedundant/);
});
test('PG activation changes only content flag and uses template/UID compare-and-swap', () => {
  const original = 'model_list: []\ngeneral_settings:\n  disable_spend_logs: false\n  store_prompts_in_spend_logs: true\n  maximum_spend_logs_retention_period: 30d\n';
  const enabled = yamlParse(pgConfiguration(original, true));
  assert.equal(enabled.general_settings.store_prompts_in_spend_logs, false);
  assert.equal(enabled.general_settings.disable_spend_logs, false);
  assert.equal(enabled.general_settings.maximum_spend_logs_retention_period, '30d');
  assert.equal(yamlParse(pgConfiguration(pgConfiguration(original, true), false)).general_settings.store_prompts_in_spend_logs, true);
  const current = snapshot({ metadata: { name: 'litellm', namespace: 'litellm', uid: 'uid' },
    spec: { template: { metadata: { annotations: { preserved: 'true' } },
      spec: { volumes: [{ name: 'config', configMap: { name: 'litellm-config-old' } }] } } } },
  { metadata: { name: 'litellm-config-old' }, data: { 'config.yaml': original } });
  const patch = patchFor(current, 'new');
  assert.deepEqual(patch.map(value => Object.keys(value)), [
    ['op', 'path', 'value'], ['op', 'path', 'value'], ['op', 'path', 'value']]);
  assert.equal(JSON.stringify(patch).includes('/metadata/uid'), true);
  assert.equal(JSON.stringify(patch).includes('/spec/template/spec/volumes/0/configMap/name'), true);
  assert.throws(() => pgConfiguration('general_settings: {}', true));
});
