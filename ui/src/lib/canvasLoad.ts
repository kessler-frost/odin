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
 * An array body (`[]`) or a string would both survive `r.json()` and then
 * read as a canvas with no nodes, which is the same silent-emptiness bug
 * wearing a different hat. `nodes`/`edges` may be absent — a brand-new odin
 * has never written the file, and `GET /canvas` answers `{}`-shaped default.
 */
function isCanvasPayload(value: unknown): value is CanvasPayload {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
