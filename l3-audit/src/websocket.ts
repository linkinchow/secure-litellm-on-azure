import { Capture, CONTENT_LIMIT, MEMORY_LIMIT, object, parse, sha, type Budget, type Emit } from './record.js';

// A passive parser: bytes are forwarded unchanged; parsing never gates delivery.
export class FrameTap {
  private header = Buffer.alloc(0);
  private remaining = 0;
  private offset = 0;
  private mask = Buffer.alloc(0);
  private opcode = 0;
  private final = true;
  private fragments: Buffer[] = [];
  private size = 0;
  private discarding = false;
  private stopped = false;
  constructor(private readonly message: (data: Buffer) => void, private readonly gap: (code: string) => void,
    private readonly budget: Budget = { bytes: 0 }) {}
  feed(chunk: Buffer): void {
    if (this.stopped) return;
    try {
      while (chunk.length) {
        if (!this.remaining) {
          this.header = Buffer.concat([this.header, chunk.subarray(0, 1)]);
          chunk = chunk.subarray(1);
          if (this.header.length < 2) continue;
          const first = this.header[0]!, second = this.header[1]!;
          const short = second & 127, extra = short === 126 ? 2 : short === 127 ? 8 : 0;
          const length = 2 + extra + (second & 128 ? 4 : 0);
          if (this.header.length < length) continue;
          if (first & 112) { this.stop('compressed-or-reserved-frame'); return; }
          const bytes = short === 126 ? this.header.readUInt16BE(2)
            : short === 127 ? this.header.readBigUInt64BE(2) : BigInt(short);
          if (BigInt(bytes) > BigInt(Number.MAX_SAFE_INTEGER)) { this.stop('frame-length'); return; }
          this.remaining = Number(bytes); this.offset = 0;
          this.mask = second & 128 ? this.header.subarray(length - 4) : Buffer.alloc(0);
          this.opcode = first & 15; this.final = Boolean(first & 128);
          this.header = Buffer.alloc(0);
          if (this.opcode === 1 || this.opcode === 2) {
            if (this.size) { this.stop('fragment-sequence'); return; }
            this.discarding = this.opcode === 2;
            if (this.discarding) this.gap('binary-frame');
          }
          if (![0, 1, 2, 8, 9, 10].includes(this.opcode)) { this.stop('frame-opcode'); return; }
          if (this.opcode < 8 && this.size + this.remaining > CONTENT_LIMIT) {
            this.discarding = true; this.release(); this.gap('frame-content-limit');
          }
          if (!this.remaining) this.endFrame();
          continue;
        }
        const count = Math.min(this.remaining, chunk.length);
        if (this.opcode < 8 && !this.discarding
          && (this.budget.bytes + count > MEMORY_LIMIT || this.fragments.length >= 4096)) {
          this.discarding = true; this.release(); this.gap('frame-memory-budget');
        }
        if (this.opcode < 8 && !this.discarding) {
          const data = Buffer.from(chunk.subarray(0, count));
          if (this.mask.length) for (let i = 0; i < count; i++) data[i] = data[i]! ^ this.mask[(this.offset + i) % 4]!;
          this.fragments.push(data); this.size += count; this.budget.bytes += count;
        }
        chunk = chunk.subarray(count); this.offset += count; this.remaining -= count;
        if (!this.remaining) this.endFrame();
      }
    } catch { this.stop('frame-parser-error'); }
  }
  private endFrame(): void {
    if (this.opcode < 8 && this.final) {
      const data = this.discarding ? undefined : Buffer.concat(this.fragments);
      this.release(); this.discarding = false;
      if (data) this.message(data);
    }
  }
  private stop(code: string): void {
    this.stopped = true; this.release(); this.header = Buffer.alloc(0); this.gap(code);
  }
  private release(): void { this.budget.bytes -= this.size; this.size = 0; this.fragments = []; }
  close(): void { if (this.remaining || this.header.length || this.size) this.gap('partial-frame'); this.release(); }
}
export class Session {
  readonly client: FrameTap;
  readonly server: FrameTap;
  private pending: Capture[] = [];
  private active = new Map<string, Capture>();
  private all = new Set<Capture>();
  constructor(path: string, authorization: string | undefined, budget: Budget, emit: Emit,
    private readonly reportGap: (reason: string) => void) {
    this.client = new FrameTap(data => {
      const event = parse(data);
      if (event.type !== 'response.create') {
        const unknown = new Capture('wss', path, authorization, budget, emit);
        unknown.index.status = 101; unknown.add('request', data);
        unknown.gap('unsupported-client-event'); unknown.finish('unverified');
        this.mark('unsupported-client-event'); return;
      }
      if (this.all.size >= 32) { this.mark('session-turn-limit'); return; }
      const capture = new Capture('wss', path, authorization, budget, emit);
      capture.index.status = 101; capture.add('request', data); capture.request(data);
      this.pending.push(capture); this.all.add(capture);
    }, code => this.mark(code), budget);
    this.server = new FrameTap(data => {
      const event = parse(data), response = object(event.response);
      let id = typeof response.id === 'string' ? response.id : event.response_id;
      if (typeof id !== 'string' || id.length > 2048) id = '';
      let capture = typeof id === 'string' ? this.active.get(id) : undefined;
      if (!capture && (event.type === 'response.created' || event.type === 'error')) {
        if (this.pending.length > 1) this.mark('ambiguous-concurrent-turns');
        capture = this.pending.shift();
        if (capture && typeof id === 'string' && id) {
          this.active.set(id, capture); capture.index.responseHash = sha(id);
        }
      }
      if (!capture && this.all.size === 1) capture = this.all.values().next().value;
      if (!capture) { this.mark('unassociated-server-event'); return; }
      capture.add('response', data);
      if (['response.completed', 'response.failed', 'response.incomplete', 'error'].includes(String(event.type))) {
        const outcome = event.type === 'response.completed' && response.status === 'completed' ? 'completed'
          : event.type === 'response.incomplete' ? 'incomplete' : 'failed';
        capture.finish(outcome); this.all.delete(capture);
        this.pending = this.pending.filter(item => item !== capture);
        for (const [key, item] of this.active) if (item === capture) this.active.delete(key);
      }
    }, code => this.mark(code), budget);
  }
  private mark(code: string): void { this.reportGap(code); for (const capture of this.all) capture.gap(code); }
  close(): void {
    this.client.close(); this.server.close();
    for (const capture of this.all) capture.finish('disconnected');
    this.all.clear(); this.pending = []; this.active.clear();
  }
}
