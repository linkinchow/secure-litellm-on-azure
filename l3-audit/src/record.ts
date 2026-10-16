import { createHash, randomUUID } from 'node:crypto';

export const CONTENT_LIMIT = 16 * 1024 * 1024;
export const MEMORY_LIMIT = 64 * 1024 * 1024;
export const DISK_LIMIT = 1024 * 1024 * 1024;
export const RETRY_MS = 24 * 60 * 60 * 1000;
export const paths = new Set(['/chat/completions', '/v1/chat/completions', '/responses',
  '/v1/responses', '/embeddings', '/v1/embeddings']);
export const wsPaths = new Set(['/responses', '/v1/responses']);
export const sha = (value: string | Buffer): string => createHash('sha256').update(value).digest('hex');
export type Outcome = 'completed' | 'failed' | 'incomplete' | 'disconnected' | 'unverified';
export interface Part { direction: 'request' | 'response'; encoding: 'base64'; data: string }
export interface Index {
  id: string; observedAt: string; transport: 'http' | 'wss'; path: string;
  keyHash: string; model: string; previousResponseHash: string; responseHash: string;
  status: number; outcome: Outcome; gap: string; capturedBytes: number; observedBytes: number;
  contentSha256: string; blob: string; risk: string;
}
export interface RecordEnvelope {
  schemaVersion: 1; index: Index; parts: Part[];
  bodyEncoding?: { request: string; response: string };
}
export function label(value: unknown): string {
  return typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$/.test(value) ? value : '';
}
export function object(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function parse(value: Buffer | string): Record<string, unknown> {
  const text = value.toString();
  try { return Buffer.byteLength(text) > 65536 ? largeMetadata(text) : object(JSON.parse(text)); }
  catch (error) { if (error instanceof SyntaxError) return {}; throw error; }
}
function largeMetadata(text: string): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const keys: string[] = [], objects: boolean[] = [];
  let responseDepth = -1;
  const fields = new Set(['type', 'model', 'previous_response_id', 'response_id']);
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '{' || char === '[') {
      if (objects.length === 1 && keys[0] === 'response' && char === '{') {
        responseDepth = 2; result.response = {};
      }
      objects.push(char === '{'); keys.push('');
      if (objects.length > 128) return {};
    } else if (char === '}' || char === ']') {
      objects.pop(); keys.pop();
      if (objects.length < responseDepth) responseDepth = -1;
    } else if (char === ',') {
      if (keys.length) keys[keys.length - 1] = '';
    } else if (char === '"') {
      let end = i + 1;
      while (end < text.length && text[end] !== '"') end += text[end] === '\\' ? 2 : 1;
      if (end >= text.length) return {};
      let next = end + 1;
      while (next < text.length && /\s/.test(text[next]!)) next++;
      const depth = objects.length;
      const relevant = depth === 1 || depth === responseDepth;
      if (relevant && objects[depth - 1] && text[next] === ':') {
        keys[depth - 1] = end - i <= 64 ? JSON.parse(text.slice(i, end + 1)) as string : '';
      } else if (relevant && end - i <= 2048) {
        const key = keys[depth - 1] ?? '';
        if (depth === 1 && fields.has(key)) result[key] = JSON.parse(text.slice(i, end + 1));
        else if (depth === responseDepth && (key === 'id' || key === 'status')) {
          object(result.response)[key] = JSON.parse(text.slice(i, end + 1));
        }
      }
      i = end;
    }
  }
  return result;
}
export interface Budget { bytes: number }
export type Emit = (record: RecordEnvelope) => void;
export class Capture {
  readonly index: Index;
  readonly bodyEncoding = { request: 'identity', response: 'identity' };
  private parts: Array<{ direction: Part['direction']; data: Buffer }> = [];
  private finished = false;
  constructor(transport: Index['transport'], path: string, authorization: string | undefined,
    private readonly budget: Budget, private readonly emit: Emit) {
    const id = randomUUID(), observedAt = new Date().toISOString();
    this.index = { id, observedAt, transport, path, keyHash: authorization ? sha(authorization.replace(/^Bearer\s+/i, '')) : '',
      model: '', previousResponseHash: '', responseHash: '', status: 0, outcome: 'unverified', gap: '',
      capturedBytes: 0, observedBytes: 0, contentSha256: '', blob: '', risk: '' };
  }
  gap(reason: string): void { if (!this.index.gap) this.index.gap = reason; }
  setEncoding(direction: Part['direction'], value: unknown): void {
    if (value === undefined) return;
    this.bodyEncoding[direction] = typeof value === 'string' && ['identity', 'gzip', 'deflate', 'br'].includes(value)
      ? value : 'unsupported';
    if (this.bodyEncoding[direction] === 'unsupported') this.gap('unsupported-body-encoding');
  }
  add(direction: Part['direction'], data: Buffer): void {
    if (this.finished) return;
    this.index.observedBytes += data.length;
    if (this.index.gap) return;
    if (this.parts.length >= 4096) { this.gap('capture-part-limit'); return; }
    if (this.index.capturedBytes + data.length > CONTENT_LIMIT) { this.gap('content-limit'); return; }
    if (this.budget.bytes + data.length > MEMORY_LIMIT) { this.gap('memory-budget'); return; }
    this.parts.push({ direction, data: Buffer.from(data) });
    this.index.capturedBytes += data.length;
    this.budget.bytes += data.length;
  }
  request(data: Buffer): void {
    const body = parse(data);
    this.index.model = label(body.model);
    if (typeof body.previous_response_id === 'string') this.index.previousResponseHash = sha(body.previous_response_id);
  }
  readRequest(): void {
    if (!this.index.gap && this.bodyEncoding.request === 'identity') {
      this.request(Buffer.concat(this.parts.filter(part => part.direction === 'request').map(part => part.data)));
    }
  }
  finish(outcome: Outcome): void {
    if (this.finished) return;
    this.finished = true;
    this.index.outcome = outcome;
    // Embedding vectors aren't part of the approved capture scope.
    const embedding = this.index.path.endsWith('/embeddings');
    const selected = this.parts.filter(part => !embedding || part.direction === 'request');
    const parts = selected.map(part => ({ direction: part.direction, encoding: 'base64' as const,
      data: part.data.toString('base64') }));
    this.index.risk = this.index.gap || (outcome === 'completed' ? '' : outcome);
    this.index.contentSha256 = sha(JSON.stringify(parts));
    this.index.blob = `content/${this.index.observedAt.slice(0, 10).replaceAll('-', '/')}/${this.index.id}.json`;
    this.budget.bytes -= this.index.capturedBytes;
    this.parts = [];
    this.emit({ schemaVersion: 1, index: this.index, parts, bodyEncoding: this.bodyEncoding });
  }
}
