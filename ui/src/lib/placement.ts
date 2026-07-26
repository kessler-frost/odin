// Laying out canvas nodes that arrived WITHOUT a `position`.
//
// A canvas authored outside the UI (`odin canvas set`, `odin import-tf`, an
// agent, a jq one-liner, the README's own example) may carry no `position`:
// translate/apply never need one, only the browser does. v0.7.3 stopped that
// from blanking the canvas by dropping such nodes on a grid — but the grid was
// blind, so an auto-placed node could land exactly on top of a node the author
// HAD positioned (field test 4: a DynamoDB table drawn over an SQS queue,
// hiding it completely).
//
// The grid itself is not a second layout algorithm: it is the one the CLI's
// `odin canvas set` already prints ("placed on the grid", cli/canvas.py) and
// the same reading order `import-tf` lays imported nodes out in — five per row,
// left to right, top to bottom, every step a multiple of odin's 20px grid.
// The only thing added here is that an occupied slot is skipped rather than
// stacked on, so placement never hides a node and never drops a node inside a
// VPC/Subnet box (which would silently stamp containment on it — see
// containment.ts, where geometry IS the infrastructure).

export type XY = { x: number; y: number };
// `style` is deliberately narrower than React.CSSProperties (an interface, so
// not assignable to an index signature): all placement reads is the footprint.
export type Placeable = { position?: XY; style?: { width?: unknown; height?: unknown } };

// cli/canvas.py's `_COLUMNS` / `_ORIGIN` / `_COL_STEP` / `_ROW_STEP`, verbatim.
const COLUMNS = 5;
const ORIGIN = 80;
const COL_STEP = 260;
const ROW_STEP = 200;

// A leaf node's footprint when its style carries no numbers yet (nothing has
// been measured at load time): import-tf's `_LEAF_SIZE`, deliberately larger
// than the ~200x62 a leaf actually renders at, so a skipped slot stays clear.
const LEAF_W = 220;
const LEAF_H = 120;

type Rect = XY & { w: number; h: number };

const num = (v: unknown, fallback: number): number => (typeof v === 'number' ? v : fallback);

const isPlaced = (n: Placeable): boolean =>
  typeof n.position?.x === 'number' && typeof n.position?.y === 'number';

function sizeOf(n: Placeable): { w: number; h: number } {
  const style = n.style ?? {};
  return { w: num(style.width, LEAF_W), h: num(style.height, LEAF_H) };
}

const rectAt = (pos: XY, n: Placeable): Rect => ({ ...pos, ...sizeOf(n) });

const overlaps = (a: Rect, b: Rect): boolean =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

// Slot n of the grid, in reading order. Unbounded downwards: slots always run
// out of occupied space, so the search below terminates.
const slotAt = (index: number): XY => ({
  x: ORIGIN + (index % COLUMNS) * COL_STEP,
  y: ORIGIN + Math.floor(index / COLUMNS) * ROW_STEP,
});

/**
 * Give every node lacking a usable `position` one, in canvas order, on the
 * first grid slot that is clear of every node already on the canvas (including
 * the ones placed here). Returns the same nodes with positions filled in, and
 * how many were placed — the count the UI says out loud.
 */
export function placeUnpositioned<T extends Placeable>(
  nodes: T[],
): { nodes: (T & { position: XY })[]; placed: number } {
  const occupied: Rect[] = nodes.filter(isPlaced).map((n) => rectAt(n.position as XY, n));
  let slot = 0;
  let placed = 0;
  const out = nodes.map((n) => {
    if (isPlaced(n)) return n as T & { position: XY };
    placed += 1;
    while (occupied.some((r) => overlaps(r, rectAt(slotAt(slot), n)))) slot += 1;
    const position = slotAt(slot);
    occupied.push(rectAt(position, n));
    slot += 1;
    return { ...n, position };
  });
  return { nodes: out, placed };
}
