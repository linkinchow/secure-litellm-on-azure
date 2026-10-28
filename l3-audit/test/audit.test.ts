import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readdir, rm, writeFile, mkdir, chmod, symlink, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { Capture, CONTENT_LIMIT, MEMORY_LIMIT, parse, type Index, type RecordEnvelope } from '../src/record.js';
import { Queue, spoolFailureCode, type Delivery } from '../src/queue.js';
import { FrameTap, Session } from '../src/websocket.js';
import { createProxy } from '../src/proxy.js';
import { render } from '../src/render.js';
import type { Duplex } from 'node:stream';
import { activationFor, expectedSpec, extractOwned, pgConfiguration, snapshot } from '../src/activation.js';
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
test('disk budget rejects audit content only; dropped and expired records produce gap index and are removed', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-expiry-'));
  const indexes: Index[] = [];
  const sink: Delivery = { async content() { throw new Error('must not upload expired'); },
    async index(value) { indexes.push(value); } };
  try {
    const limited = new Queue(directory, sink, 1); await limited.init();
    const dropped = sample(); limited.enqueue(dropped); await limited.flush();
    assert.equal(limited.counters.dropped, 1); assert.equal((await readdir(directory)).length, 0);
    assert.equal(indexes.length, 1);
    assert.deepEqual(indexes[0], { ...dropped.index, gap: 'enqueue-budget-exhausted', risk: 'enqueue-budget-exhausted',
      contentSha256: '', blob: '' });
    assert.ok(!JSON.stringify(indexes[0]).includes('synthetic-private-text'));
    const queue = new Queue(directory, sink, 1e6, 1); await queue.init(); queue.enqueue(sample()); await queue.flush();
    await queue.tick(Date.now() + 1000);
    assert.deepEqual(indexes.map(value => value.gap), ['enqueue-budget-exhausted', 'delivery-expired']);
    assert.equal((await readdir(directory)).length, 0);
  } finally { await rm(directory, { recursive: true }); }
});
test('gap index delivery for dropped records is bounded and failures are counted', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'l3-gap-bound-'));
  let calls = 0;
  const sink: Delivery = { async content() {}, index() { calls++; return new Promise<void>(() => {}); } };
  try {
    const queue = new Queue(directory, sink, 1); await queue.init();
    for (let i = 0; i < 300; i++) queue.enqueue(sample());
    assert.equal(calls, 256); assert.equal(queue.counters.dropped, 300); assert.equal(queue.counters.failures, 44);
    const failing = new Queue(directory, { async content() {}, async index() { throw new Error('synthetic outage'); } });
    failing.abandon(sample(), 'capture-without-spool'); await failing.flush();
    assert.equal(failing.counters.dropped, 1); assert.equal(failing.counters.failures, 1);
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
  session.client.feed(frame({ type: 'response.create', model: 'not-allowed', input: 'rejected' }, true));
  session.server.feed(frame({ type: 'error', error: { code: 'model_not_found' } }));
  assert.equal(records[2]!.index.outcome, 'response-error'); assert.equal(records[2]!.index.risk, '');
  session.client.feed(frame({ type: 'response.create', model: 'coding', input: 'unfinished' }, true)); session.close();
  assert.equal(records[3]!.index.outcome, 'disconnected'); assert.equal(records[3]!.index.risk, 'disconnected');
  assert.deepEqual(gaps, []);
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
    assert.equal(records[0]!.index.outcome, 'http-error'); assert.equal(records[0]!.index.status, 400);
    assert.equal(records[0]!.index.risk, ''); assert.equal(records[0]!.index.gap, '');
    assert.equal(records[1]!.index.outcome, 'completed'); assert.equal(records[1]!.index.risk, '');
    assert.ok(records[2]!.parts.every(part => part.direction === 'request'));
  } finally { proxy.closeAllConnections(); upstream.closeAllConnections(); await Promise.all([new Promise<void>(r => proxy.close(() => r())), new Promise<void>(r => upstream.close(() => r()))]); }
});
test('upstream failure events are business outcomes; only an unreachable upstream is audit risk', async () => {
  const upstream = http.createServer((request, response) => {
    request.resume();
    request.on('end', () => {
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.end('data: {"type":"response.failed","response":{"id":"resp-failed","status":"failed"}}\n\n');
    });
  });
  const upstreamUrl = await listen(upstream), records: RecordEnvelope[] = [];
  const proxy = createProxy(upstreamUrl, r => records.push(r), () => ({})), url = await listen(proxy);
  const closed = http.createServer(), closedUrl = await listen(closed);
  await new Promise<void>(r => closed.close(() => r()));
  const unreachable = createProxy(closedUrl, r => records.push(r), () => ({})), unreachableUrl = await listen(unreachable);
  try {
    await (await fetch(new URL('/v1/responses', url), { method: 'POST', body: '{"model":"coding"}' })).text();
    const failed = await fetch(new URL('/v1/responses', unreachableUrl), { method: 'POST', body: '{"model":"coding"}' });
    assert.equal(failed.status, 502); await failed.text();
    assert.deepEqual(records.map(r => [r.index.outcome, r.index.risk]), [['response-error', ''], ['failed', 'failed']]);
  } finally {
    for (const server of [proxy, upstream, unreachable]) server.closeAllConnections();
    await Promise.all([proxy, upstream, unreachable].map(server => new Promise<void>(r => server.close(() => r()))));
  }
});
test('HTTP records link to LiteLLM Spend Logs by response id hash and call id', async () => {
  const padding = 'x'.repeat(100000);
  const upstream = http.createServer((request, response) => {
    request.resume();
    request.on('end', () => {
      const headers = { 'x-litellm-call-id': 'call-' + request.url!.length };
      if (request.url === '/v1/chat/completions') {
        response.writeHead(200, { ...headers, 'content-type': 'text/event-stream' });
        response.write('data: {"id":"chatcmpl-stream","choices":[]}\n\n'); response.end('data: [DONE]\n\n'); return;
      }
      if (request.url === '/v1/responses') {
        response.writeHead(200, { ...headers, 'content-type': 'text/event-stream' });
        response.write('data: {"type":"response.created","response":{"id":"resp-stream"}}\n\n');
        response.end('data: {"type":"response.completed","response":{"id":"resp-stream","status":"completed"}}\n\n'); return;
      }
      if (request.url === '/chat/completions') {
        response.writeHead(200, { ...headers, 'content-type': 'application/json' });
        response.end(JSON.stringify({ choices: [{ message: { content: padding } }], id: 'chatcmpl-large' })); return;
      }
      response.writeHead(400, { ...headers, 'content-type': 'application/json' }).end('{"error":"synthetic"}');
    });
  });
  const upstreamUrl = await listen(upstream), records: RecordEnvelope[] = [];
  const proxy = createProxy(upstreamUrl, r => records.push(r), () => ({})), url = await listen(proxy);
  const hash = (value: string) => createHash('sha256').update(value).digest('hex');
  try {
    for (const path of ['/v1/chat/completions', '/v1/responses', '/chat/completions', '/responses']) {
      const response = await fetch(new URL(path, url), { method: 'POST', body: '{"model":"coding"}' });
      await response.text();
    }
    assert.deepEqual(records.map(r => [r.index.path, r.index.responseHash, r.index.callId]), [
      ['/v1/chat/completions', hash('chatcmpl-stream'), 'call-20'],
      ['/v1/responses', hash('resp-stream'), 'call-13'],
      ['/chat/completions', hash('chatcmpl-large'), 'call-17'],
      ['/responses', '', 'call-10'],
    ]);
    assert.ok(!JSON.stringify(records.map(r => r.index)).includes('resp-stream'));
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
  const indexes: Index[] = [];
  const queue = new Queue(directory, { async content() { throw new Error('must not deliver'); },
    async index(value) { indexes.push(value); } }, 1);
  await queue.init();
  const upstream = http.createServer((request, response) => { request.resume(); request.on('end', () => response.end('OK')); });
  const upstreamUrl = await listen(upstream);
  const proxy = createProxy(upstreamUrl, r => queue.enqueue(r), () => queue.counters), url = await listen(proxy);
  try {
    const response = await fetch(new URL('/v1/responses', url), { method: 'POST', body: '{"model":"coding"}' });
    assert.equal(response.status, 200); assert.equal(await response.text(), 'OK');
    assert.equal(queue.counters.dropped, 1); assert.equal((await readdir(directory)).length, 0);
    await queue.flush();
    assert.deepEqual(indexes.map(value => [value.status, value.outcome, value.gap, value.blob]),
      [[200, 'completed', 'enqueue-budget-exhausted', '']]);
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
test('PG activation changes only content flag and re-applies only the platform-owned volume reference', () => {
  const original = 'model_list: []\ngeneral_settings:\n  disable_spend_logs: false\n  store_prompts_in_spend_logs: true\n  maximum_spend_logs_retention_period: 30d\n';
  const enabled = yamlParse(pgConfiguration(original, true));
  assert.equal(enabled.general_settings.store_prompts_in_spend_logs, false);
  assert.equal(enabled.general_settings.disable_spend_logs, false);
  assert.equal(enabled.general_settings.maximum_spend_logs_retention_period, '30d');
  assert.equal(yamlParse(pgConfiguration(pgConfiguration(original, true), false)).general_settings.store_prompts_in_spend_logs, true);
  const platform = { manager: 'llmgw-migration', operation: 'Apply', apiVersion: 'apps/v1', fieldsType: 'FieldsV1', fieldsV1: {
    'f:metadata': { 'f:labels': { '.': {}, 'f:app': {} } },
    'f:spec': { 'f:replicas': {}, 'f:template': { 'f:metadata': { 'f:annotations': { 'f:preserved': {} } }, 'f:spec': {
      'f:containers': { 'k:{"name":"litellm"}': { '.': {}, 'f:name': {}, 'f:image': {}, 'f:args': {},
        'f:env': { 'k:{"name":"A"}': { '.': {}, 'f:name': {}, 'f:value': {} } },
        'f:ports': { 'k:{"containerPort":4000,"protocol":"TCP"}': { '.': {}, 'f:containerPort': {}, 'f:protocol': {} } } } },
      'f:volumes': { 'k:{"name":"config"}': { '.': {}, 'f:name': {}, 'f:configMap': { 'f:name': {} } },
        'k:{"name":"tmp"}': { '.': {}, 'f:name': {}, 'f:emptyDir': {} } } } } } } };
  const deployment = (managedFields: unknown[]) => ({ metadata: { name: 'litellm', namespace: 'litellm', uid: 'uid',
    resourceVersion: '7', labels: { app: 'litellm', other: 'kept' }, managedFields },
    spec: { replicas: 2, revisionHistoryLimit: 10, template: { metadata: { annotations: { preserved: 'true', restartedAt: 'x' } },
      spec: { containers: [{ name: 'litellm', image: 'img', args: ['--port', '4000'], terminationMessagePath: '/dev/termination-log',
        env: [{ name: 'A', value: '1' }, { name: 'B', value: 'other-manager' }], ports: [{ containerPort: 4000, protocol: 'TCP' }] }],
      volumes: [{ name: 'tmp', emptyDir: {} }, { name: 'config', configMap: { name: 'litellm-config-old', defaultMode: 420 } }] } } } });
  const config = { metadata: { name: 'litellm-config-old' }, data: { 'config.yaml': original } };
  const current = snapshot(deployment([platform]), config);
  assert.equal(current.volumeIndex, 1);
  const activation = activationFor(current, 'litellm-config-l3-new');
  assert.equal(activation.forceConflicts, false);
  // Only what llmgw-migration applied is re-applied, so Stage 6/8 keep owning (and can later change) every field.
  assert.deepEqual(activation.document, { apiVersion: 'apps/v1', kind: 'Deployment',
    metadata: { name: 'litellm', namespace: 'litellm', uid: 'uid', labels: { app: 'litellm' } },
    spec: { replicas: 2, template: { metadata: { annotations: { preserved: 'true' } }, spec: {
      containers: [{ name: 'litellm', image: 'img', args: ['--port', '4000'], env: [{ name: 'A', value: '1' }],
        ports: [{ containerPort: 4000, protocol: 'TCP' }] }],
      volumes: [{ name: 'tmp', emptyDir: {} }, { name: 'config', configMap: { name: 'litellm-config-l3-new' } }] } } } });
  const expected = expectedSpec(current, 'litellm-config-l3-new') as { template: { spec: { volumes: unknown[] } } };
  assert.deepEqual(expected.template.spec.volumes[1], { name: 'config', configMap: { name: 'litellm-config-l3-new', defaultMode: 420 } });
  assert.deepEqual(extractOwned([{ name: 'a' }, 'x'], { 'i:1': {}, 'v:"x"': {} }), ['x']);
  const legacy = { manager: 'kubectl-patch', operation: 'Update', apiVersion: 'apps/v1', fieldsType: 'FieldsV1', fieldsV1: {
    'f:spec': { 'f:template': { 'f:spec': { 'f:volumes': { 'k:{"name":"config"}': { 'f:configMap': { 'f:name': {} } } } } } } } };
  assert.equal(activationFor(snapshot(deployment([platform, legacy]), config), 'new').forceConflicts, true);
  for (const [managers, message] of [
    [[], /--show-managed-fields/],
    [[legacy], /llmgw-migration server-side apply owner/],
    [[platform, { ...legacy, manager: 'someone-else' }], /Another field manager/],
    [[{ ...platform, fieldsV1: { 'f:metadata': { 'f:labels': { 'f:app': {} } } } }], /not owned by llmgw-migration/],
  ] as const) {
    assert.throws(() => activationFor(snapshot(deployment([...managers]), config), 'new'), message);
  }
  assert.throws(() => pgConfiguration('general_settings: {}', true));
});
