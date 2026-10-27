import http, { type IncomingHttpHeaders } from 'node:http';
import { Transform } from 'node:stream';
import { createParser } from 'eventsource-parser';
import { Capture, label, object, paths, wsPaths, type Budget, type Emit, type Outcome } from './record.js';
import { Session } from './websocket.js';
import { signal } from './queue.js';

export function createProxy(upstream: URL, emit: Emit, counters: () => unknown): http.Server {
  if (upstream.protocol !== 'http:' || upstream.username || upstream.password || upstream.pathname !== '/') {
    throw new Error('Upstream must be a fixed internal HTTP origin');
  }
  const budget: Budget = { bytes: 0 };
  let sessions = 0;
  const headers = (input: IncomingHttpHeaders): IncomingHttpHeaders => {
    const result = { ...input };
    // Passive frame parsing requires no compression; don't negotiate it upstream.
    delete result['sec-websocket-extensions'];
    return result;
  };
  const server = http.createServer((request, response) => {
    const path = new URL(request.url ?? '/', 'http://local').pathname;
    if (path === '/l3/health') {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify(counters())); return;
    }
    const capture = request.method === 'POST' && paths.has(path)
      ? new Capture('http', path, request.headers.authorization, budget, emit) : undefined;
    capture?.setEncoding('request', request.headers['content-encoding']);
    const uri = new URL(request.url ?? '/', upstream);
    if (uri.origin !== upstream.origin) { response.writeHead(400).end(); return; }
    const outgoing = http.request(uri, { method: request.method, headers: request.headers });
    let requestComplete = false;
    const requestTap = new Transform({
      transform(chunk: Buffer, _encoding, callback) {
        capture?.add('request', chunk);
        callback(null, chunk);
      },
      flush(callback) {
        requestComplete = true; capture?.readRequest(); callback();
      },
    });
    outgoing.on('response', incoming => {
      if (capture) {
        capture.index.status = incoming.statusCode ?? 0;
        capture.index.callId = label(incoming.headers['x-litellm-call-id']);
      }
      capture?.setEncoding('response', incoming.headers['content-encoding']);
      response.writeHead(incoming.statusCode ?? 502, incoming.headers);
      const sse = incoming.headers['content-type']?.includes('text/event-stream');
      let terminal: Outcome | undefined;
      let metadataHealthy = true;
      const decoder = new TextDecoder('utf-8', { fatal: true });
      const parser = sse && capture?.bodyEncoding.response === 'identity' ? createParser({
        maxBufferSize: 65536,
        onEvent(event) {
          if (event.data === '[DONE]') {
            if (!terminal) terminal = 'completed';
            return;
          }
          try {
            const value = object(JSON.parse(event.data));
            capture?.responseId(typeof value.type === 'string' ? object(value.response).id : value.id);
            if (value.type === 'response.completed' && object(value.response).status === 'completed') terminal = 'completed';
            if (value.type === 'error' || value.type === 'response.failed') terminal = 'failed';
            if (value.type === 'response.incomplete') terminal = 'incomplete';
          } catch { metadataHealthy = false; capture?.gap('invalid-sse-metadata'); }
        },
        onError() { metadataHealthy = false; capture?.gap('invalid-sse-metadata'); },
      }) : undefined;
      const responseTap = new Transform({
        transform(chunk: Buffer, _encoding, callback) {
          if (!path.endsWith('/embeddings')) capture?.add('response', chunk);
          if (parser && metadataHealthy) {
            try { parser.feed(decoder.decode(chunk, { stream: true })); }
            catch { metadataHealthy = false; capture?.gap('sse-metadata-budget'); }
          }
          callback(null, chunk);
        },
      });
      incoming.on('end', () => {
        if (parser && metadataHealthy) {
          try { parser.feed(decoder.decode()); }
          catch { capture?.gap('invalid-sse-metadata'); }
        }
        if (!requestComplete) capture?.gap('request-not-complete');
        if (!sse) capture?.readResponse();
        capture?.finish((incoming.statusCode ?? 500) >= 400 ? 'failed'
          : sse && capture.bodyEncoding.response !== 'identity' ? 'unverified'
          : sse ? terminal ?? 'incomplete' : 'completed');
      });
      incoming.on('error', () => { capture?.finish('incomplete'); response.destroy(); });
      response.on('close', () => {
        if (!response.writableFinished) { capture?.finish('disconnected'); incoming.destroy(); }
      });
      incoming.pipe(responseTap).pipe(response);
    });
    outgoing.on('error', () => {
      capture?.finish('failed');
      if (!response.headersSent) response.writeHead(502, { 'content-type': 'application/json' }).end('{"error":"Upstream unavailable"}');
      else response.destroy();
    });
    request.on('aborted', () => { capture?.finish('disconnected'); outgoing.destroy(); });
    request.on('error', () => { capture?.finish('disconnected'); outgoing.destroy(); });
    request.pipe(requestTap).pipe(outgoing);
  });
  server.on('upgrade', (request, socket, head) => {
    const path = new URL(request.url ?? '/', 'http://local').pathname;
    if (!wsPaths.has(path)) { socket.end('HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n'); return; }
    const uri = new URL(request.url ?? '/', upstream);
    if (uri.origin !== upstream.origin) { socket.destroy(); return; }
    const outgoing = http.request(uri, { headers: headers(request.headers) });
    outgoing.on('response', incoming => {
      const responseHeaders = incoming.rawHeaders.reduce<string[]>((rows, value, index, values) => {
        if (index % 2 === 0) rows.push(`${value}: ${values[index + 1]}\r\n`);
        return rows;
      }, []).join('');
      socket.write(`HTTP/1.1 ${incoming.statusCode ?? 502} ${incoming.statusMessage ?? 'Rejected'}\r\n${responseHeaders}\r\n`);
      incoming.on('error', () => socket.destroy());
      socket.on('close', () => incoming.destroy());
      incoming.pipe(socket);
    });
    outgoing.on('upgrade', (incoming, backend, backendHead) => {
      socket.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(incoming.headers)
        .flatMap(([key, value]) => value === undefined ? [] : `${key}: ${Array.isArray(value) ? value.join(', ') : value}\r\n`).join('')}\r\n`);
      const gaps = new Set<string>();
      const reportGap = (reason: string): void => {
        if (gaps.has(reason)) return;
        gaps.add(reason);
        signal(reason);
        const gap = new Capture('wss', path, request.headers.authorization, budget, emit);
        gap.index.status = 101; gap.gap(reason); gap.finish('unverified');
      };
      const capture = sessions < 32 ? new Session(path, request.headers.authorization, budget, emit, reportGap) : undefined;
      if (!capture) reportGap('session-capture-budget');
      sessions++;
      let closed = false;
      const close = (): void => {
        if (closed) return; closed = true; sessions--; capture?.close();
        socket.destroy(); backend.destroy();
      };
      socket.on('error', close); backend.on('error', close);
      socket.on('close', close); backend.on('close', close);
      socket.on('data', data => capture?.client.feed(data));
      backend.on('data', data => capture?.server.feed(data));
      if (head.length) { capture?.client.feed(head); backend.write(head); }
      if (backendHead.length) { capture?.server.feed(backendHead); socket.write(backendHead); }
      socket.pipe(backend).pipe(socket);
    });
    outgoing.on('error', () => { socket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n'); });
    socket.on('error', () => outgoing.destroy());
    outgoing.end();
  });
  server.requestTimeout = 0;
  return server;
}
