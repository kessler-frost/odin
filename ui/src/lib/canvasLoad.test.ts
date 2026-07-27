import { describe, expect, it } from 'bun:test';
import { readCanvas } from './canvasLoad';

const ok = (body: unknown): typeof fetch =>
  (async () => new Response(JSON.stringify(body), { status: 200 })) as unknown as typeof fetch;

describe('readCanvas: a failed read is not an empty canvas', () => {
  // Each of these used to return `{nodes: [], edges: []}` to the caller, which
  // then saved that emptiness over the user's real canvas.
  it('is null when the fetch rejects (the Tailscale hiccup that lost a canvas)', async () => {
    const rejects = (async () => { throw new TypeError('Failed to fetch'); }) as unknown as typeof fetch;
    expect(await readCanvas(rejects, '/canvas')).toBeNull();
  });

  it('is null on a non-2xx status, without parsing the body', async () => {
    const five00 = (async () => new Response('upstream is down', { status: 500 })) as unknown as typeof fetch;
    expect(await readCanvas(five00, '/canvas')).toBeNull();
  });

  it('is null when the body is not JSON (a proxy error page, say)', async () => {
    const html = (async () => new Response('<html>502</html>', { status: 200 })) as unknown as typeof fetch;
    expect(await readCanvas(html, '/canvas')).toBeNull();
  });

  it('is null for JSON that is not a canvas object — an array reads as "no nodes"', async () => {
    expect(await readCanvas(ok([]), '/canvas')).toBeNull();
    expect(await readCanvas(ok('nope'), '/canvas')).toBeNull();
    expect(await readCanvas(ok(null), '/canvas')).toBeNull();
  });

  it('is null for an error body that happens to be an object', async () => {
    // The destructive one. `{"detail": ...}` is FastAPI's error shape; it
    // passes a bare typeof-object check with `nodes` undefined, and
    // `POST /apply-full` with that body returns HTTP 200 "applied" and
    // commits a revision with ZERO nodes -- which reconciles the environment
    // down to nothing. Measured against a live server.
    expect(await readCanvas(ok({ detail: 'Internal Server Error' }), '/canvas')).toBeNull();
    expect(await readCanvas(ok({ error: 'nope' }), '/canvas')).toBeNull();
    expect(await readCanvas(ok({ nodes: 'not-an-array' }), '/canvas')).toBeNull();
  });
});

describe('readCanvas: a successful read is returned intact', () => {
  it('returns the canvas', async () => {
    const canvas = { nodes: [{ id: 's3-1' }], edges: [] };
    expect(await readCanvas(ok(canvas), '/canvas')).toEqual(canvas);
  });

  it('a GENUINELY empty canvas is a success, not a failure', async () => {
    // The whole point of the distinction: this one is safe to save over.
    expect(await readCanvas(ok({ nodes: [], edges: [] }), '/canvas')).toEqual({ nodes: [], edges: [] });
  });

  it('a fresh odin that has never written the file still loads', async () => {
    // `api/canvas.py` answers `_EMPTY` in that case, so `nodes` is present.
    expect(await readCanvas(ok({ nodes: [], edges: [] }), '/canvas')).toEqual({ nodes: [], edges: [] });
  });
});
