import { mkdir, readdir, readFile, rename, stat, unlink, open } from 'node:fs/promises';
import { join } from 'node:path';
import { DISK_LIMIT, RETRY_MS, type Index, type RecordEnvelope } from './record.js';

export interface Delivery { content(record: RecordEnvelope): Promise<void>; index(index: Index): Promise<void> }
export interface Counters { queued: number; delivered: number; dropped: number; failures: number; diskBytes: number }
export function signal(code: string): void {
  console.error(JSON.stringify({ component: 'optional-l3', code, time: new Date().toISOString() }));
}
interface Pending { created: number; record: RecordEnvelope; contentDone: boolean }
function code(error: unknown): string { return String((error as NodeJS.ErrnoException | null)?.code ?? ''); }
export class Queue {
  readonly counters: Counters = { queued: 0, delivered: 0, dropped: 0, failures: 0, diskBytes: 0 };
  private serial: Promise<void> = Promise.resolve();
  private pendingBytes = 0;
  private pendingCount = 0;
  private running = false;
  constructor(readonly directory: string, private readonly sink: Delivery,
    private readonly limit = DISK_LIMIT, private readonly ttl = RETRY_MS) {}
  async init(): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    for (const name of await readdir(this.directory)) {
      if (name.endsWith('.tmp')) {
        await unlink(join(this.directory, name)); this.counters.dropped++; signal('crash-incomplete-enqueue'); continue;
      }
      if (name.endsWith('.corrupt')) {
        this.counters.diskBytes += (await stat(join(this.directory, name))).size;
        this.counters.dropped++; signal('quarantined-spool-record'); continue;
      }
      if (!name.endsWith('.json')) throw new Error('Unexpected spool file');
      this.counters.diskBytes += (await stat(join(this.directory, name))).size;
      this.pendingCount++;
    }
    if (this.counters.diskBytes > this.limit) signal('spool-over-limit');
  }
  enqueue(record: RecordEnvelope): void {
    const text = JSON.stringify({ created: Date.now(), record, contentDone: false } satisfies Pending);
    const size = Buffer.byteLength(text);
    if (size + this.pendingBytes + this.counters.diskBytes > this.limit || this.pendingBytes + size > 64 * 1024 * 1024
      || this.pendingCount >= 4096) {
      this.counters.dropped++; signal('enqueue-budget-exhausted'); return;
    }
    this.pendingBytes += size;
    this.pendingCount++;
    this.serial = this.serial.then(async () => {
      try {
        await this.atomic(record.index.id + '.json', text);
        this.counters.diskBytes += size; this.counters.queued++;
      } catch {
        this.counters.failures++; signal('spool-write-failed');
        try {
          this.counters.diskBytes += (await stat(join(this.directory, record.index.id + '.json'))).size;
          this.counters.queued++; signal('spool-ack-uncertain');
        } catch (error) {
          if (code(error) !== 'ENOENT') signal('spool-status-unavailable');
          this.pendingCount--; this.counters.dropped++;
        }
      }
      finally { this.pendingBytes -= size; }
    });
  }
  private async atomic(name: string, text: string): Promise<void> {
    const temporary = join(this.directory, name + '.tmp');
    try {
      const handle = await open(temporary, 'w', 0o600);
      try { await handle.writeFile(text); await handle.sync(); } finally { await handle.close(); }
      await rename(temporary, join(this.directory, name));
      const directory = await open(this.directory, 'r');
      try { await directory.sync(); } finally { await directory.close(); }
    } finally {
      try { await unlink(temporary); }
      catch (error) { if (code(error) !== 'ENOENT') signal('spool-temp-cleanup-failed'); }
    }
  }
  async flush(): Promise<void> { await this.serial; }
  async tick(now = Date.now()): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      await this.flush();
      for (const name of (await readdir(this.directory)).filter(name => name.endsWith('.json')).sort()) {
        const path = join(this.directory, name);
        try {
          const size = (await stat(path)).size;
          if (size > this.limit) throw new Error('Spool record exceeds budget');
          let item: Pending;
          try {
            item = JSON.parse(await readFile(path, 'utf8'));
            if (item.record?.schemaVersion !== 1 || item.record.index.id + '.json' !== name
              || !Number.isFinite(item.created) || !Array.isArray(item.record.parts)
              || typeof item.contentDone !== 'boolean') throw new Error('Invalid spool record');
          } catch (error) {
            if (code(error)) throw error;
            await rename(path, path + '.corrupt'); this.pendingCount--;
            this.counters.dropped++; this.counters.failures++; signal('spool-record-quarantined'); continue;
          }
          if (now - item.created >= this.ttl) {
            this.counters.dropped++; signal('delivery-expired');
            // A small gap index is attempted before abandoning the expired record.
            try { await this.sink.index({ ...item.record.index, gap: 'delivery-expired', risk: 'delivery-expired' }); }
            catch { signal('expired-gap-index-unavailable'); }
          } else {
            if (!item.contentDone) {
              await this.sink.content(item.record);
              item.contentDone = true;
              item.record.parts = [];
              const replacement = JSON.stringify(item);
              await this.atomic(name, replacement);
              this.counters.diskBytes += Buffer.byteLength(replacement) - size;
            }
            await this.sink.index(item.record.index);
            this.counters.delivered++;
          }
          const actualSize = (await stat(path)).size;
          await unlink(path); this.counters.diskBytes -= actualSize;
          this.pendingCount--;
        } catch (error) {
          this.counters.failures++; signal(code(error) === 'ENOENT' ? 'spool-race' : 'delivery-retry');
          break;
        }
      }
    } catch { this.counters.failures++; signal('spool-read-failed'); }
    finally { this.running = false; }
  }
}
