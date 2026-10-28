import { AzureDelivery, settings } from './azure.js';
import { Queue, signal, spoolFailureCode } from './queue.js';
import { createProxy } from './proxy.js';
import { randomUUID } from 'node:crypto';
import type { Index } from './record.js';
import { WorkloadIdentityCredential } from '@azure/identity';

const delivery = new AzureDelivery(settings(), new WorkloadIdentityCredential());
const queue = new Queue(process.env.L3_SPOOL ?? '/spool', delivery);
let spoolReady = false;
let spoolErrorCode = '';
try { await queue.init(); spoolReady = true; }
catch (error) { spoolErrorCode = spoolFailureCode(error); signal('spool-init-failed', { errorCode: spoolErrorCode }); }
const server = createProxy(new URL(process.env.L3_UPSTREAM ?? 'http://litellm.litellm.svc.cluster.local:4000'),
  record => { if (spoolReady) queue.enqueue(record); else queue.abandon(record, 'capture-without-spool'); },
  () => ({ spoolReady, spoolErrorCode, ...queue.counters }));
server.listen(8080, '0.0.0.0');
setInterval(() => { if (spoolReady) void queue.tick(); }, 5000);
let previousDrops = 0, previousFailures = 0;
setInterval(() => {
  const health: Index = { id: randomUUID(), observedAt: new Date().toISOString(), transport: 'http', path: '/l3/health',
    keyHash: '', model: '', previousResponseHash: '', responseHash: '', callId: '', status: 200,
    outcome: 'unverified', gap: '', capturedBytes: 0, observedBytes: 0, contentSha256: '', blob: '',
    risk: queue.counters.dropped > previousDrops || queue.counters.failures > previousFailures || !spoolReady
      ? 'audit-delivery-degraded' : queue.counters.diskBytes >= 0.8 * 1024 ** 3 ? 'audit-backlog-high' : '' };
  previousDrops = queue.counters.dropped; previousFailures = queue.counters.failures;
  void delivery.index(health).catch(() => signal('heartbeat-unavailable'));
}, 60000);
process.on('SIGTERM', () => {
  server.close();
  void queue.flush().then(() => queue.tick()).catch(() => signal('shutdown-flush-failed'));
  setTimeout(() => process.exit(0), 25000).unref();
});
