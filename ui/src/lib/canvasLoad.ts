/**
 * Reading the saved canvas — where "could not read" must never look like
 * "there is nothing here".
 *
 * This exists because the collapse of those two cases DESTROYED user data.
 * `Canvas.tsx` used to load with
 *
 *     fetch(`${API}/canvas`).then(r => r.json()).catch(() => ({ nodes: [], edges: [] }))
 *
 * so any failed read rendered an empty canvas, flipped `loaded` true, and let
 * the debounced save write that emptiness over `.odin/canvas.json`. Measured
 * by rejecting a single GET: 2 nodes on disk before, 0 after, unrecoverable.
 * It was reported from a Tailscale-served odin ("refresh nukes the canvas")
 * and is invisible on loopback, where the fetch does not fail.
 *
 * So the contract here is deliberately narrow: `null` means COULD NOT READ,
 * and the caller must not render, must not save, and must say so. An object
 * means the read succeeded — possibly describing a genuinely empty canvas,
 * which is a completely different thing and safe to save over.
 */

export type CanvasPayload = { nodes?: unknown[]; edges?: unknown[] };

/**
 * `null` on any failure: a rejected fetch, a non-2xx status, a body that
 * isn't JSON, or JSON that isn't a canvas object. Each of those used to
 * arrive at the caller as an empty canvas.
 *
 * `fetchImpl` is injected so this is testable without a browser or a server —
 * the failure modes that matter here are precisely the ones a live server is
 * least likely to produce on demand.
 */
export async function readCanvas(
  fetchImpl: typeof fetch,
  url: string,
): Promise<CanvasPayload | null> {
  const payload = await fetchImpl(url)
    .then(r => (r.ok ? r.json() : null))
    .catch(() => null);
  return isCanvasPayload(payload) ? payload : null;
}

/**
 * `nodes` MUST be an array. That is not pedantry — it is what stops an error
 * body from being applied as a canvas.
 *
 * `api/canvas.py` always answers with a `nodes` list (`_EMPTY = {"nodes": [],
 * "edges": []}` when the file has never been written), so this rejects
 * nothing the real server sends. What it does reject is FastAPI's own error
 * shape, `{"detail": "Internal Server Error"}` — an object, so a laxer check
 * passes it, with `nodes` simply undefined and read downstream as "no nodes".
 *
 * Measured against the live server, that is destructive rather than merely
 * wrong: `POST /apply-full` with `{"detail": "Internal Server Error"}` returns
 * `HTTP 200 {"status": "applied"}` and commits a real revision with ZERO
 * nodes, because `CanvasGraph.nodes` defaults to `[]`. Reconciling an env to
 * an empty desired state tears down every resource in it. So a 500 arriving
 * where a canvas was expected could destroy an environment.
 */
function isCanvasPayload(value: unknown): value is CanvasPayload {
  return typeof value === 'object' && value !== null && Array.isArray((value as CanvasPayload).nodes);
}
