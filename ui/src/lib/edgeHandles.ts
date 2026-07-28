/**
 * Which side of each node an edge should leave from and arrive at.
 *
 * A canvas authored by hand, by `odin canvas set`, or by the translation agent
 * is a FIRST-CLASS input -- the README documents the JSON shape, and NORTHSTAR
 * has an agent writing it. But such a canvas routinely omits
 * `sourceHandle`/`targetHandle`, because they are an artefact of having drawn
 * the edge with a mouse.
 *
 * ReactFlow then routes from an arbitrary default handle, and the result is not
 * subtly off, it is wrong: measured on an EC2 -> S3 IAM edge whose nodes sat at
 * x 500..700 and x 1000..1200, the path took a bezier control point to the LEFT
 * of the source and curved backwards, dragging its
 * "GetObject, PutObject, ListBucket" label to x=601 -- dead centre of the source
 * node, 250px from the midpoint it belongs at. Nothing warned; the edge simply
 * drew badly, which is the "renders wrong in silence" shape odin's honesty rules
 * exist to prevent.
 *
 * So the handles are INFERRED from geometry when absent. Explicit handles always
 * win -- an edge the user drew says which sides they meant, and this must never
 * second-guess that.
 *
 * Inference stays in the UI rather than being written into the canvas on save:
 * it is deterministic from positions, so persisting it would add a field the
 * author did not write and would go stale the moment a node moved.
 */

export type HandlePair = { sourceHandle: string; targetHandle: string };

export type Positioned = { x: number; y: number };

/**
 * The facing sides for an edge between two nodes, from their CENTRES.
 *
 * Whichever axis separates them more decides: mostly-horizontal neighbours join
 * right->left (or left->right), mostly-vertical ones bottom->top (or
 * top->bottom). Centres rather than origins so a wide node next to a narrow one
 * still reads correctly.
 *
 * Ties go to the horizontal pair, which is the common canvas layout (odin's own
 * drop placement lays resources out in a row) and keeps the choice total: there
 * is no input for which this returns nothing.
 */
export function facingHandles(source: Positioned, target: Positioned): HandlePair {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0
      ? { sourceHandle: 'right', targetHandle: 'left' }
      : { sourceHandle: 'left', targetHandle: 'right' };
  }
  return dy >= 0
    ? { sourceHandle: 'bottom', targetHandle: 'top' }
    : { sourceHandle: 'top', targetHandle: 'bottom' };
}

/**
 * `centre` for a node, given its position (top-left, as ReactFlow stores it)
 * and whatever size is known. Width/height are optional because a canvas that
 * omits handles usually omits `size` too; falling back to the position alone is
 * still far better than an arbitrary handle.
 */
export function centreOf(node: { position?: Positioned; size?: { width?: number; height?: number } }): Positioned {
  const { x = 0, y = 0 } = node.position ?? {};
  return { x: x + (node.size?.width ?? 0) / 2, y: y + (node.size?.height ?? 0) / 2 };
}

/**
 * Fill in an edge's handles if it has none. Returns the pair to use.
 *
 * `null` for either endpoint (an edge naming a node that is not on the canvas)
 * leaves the handles exactly as they arrived: inventing a side for an edge that
 * cannot be drawn anyway would be guessing about nothing.
 */
export function resolveHandles(
  edge: { sourceHandle?: string | null; targetHandle?: string | null },
  source: Positioned | null,
  target: Positioned | null,
): { sourceHandle: string | null; targetHandle: string | null } {
  if (edge.sourceHandle && edge.targetHandle) {
    return { sourceHandle: edge.sourceHandle, targetHandle: edge.targetHandle };
  }
  if (!source || !target) {
    return { sourceHandle: edge.sourceHandle ?? null, targetHandle: edge.targetHandle ?? null };
  }
  const inferred = facingHandles(source, target);
  // A partially-specified edge keeps the half it named.
  return {
    sourceHandle: edge.sourceHandle ?? inferred.sourceHandle,
    targetHandle: edge.targetHandle ?? inferred.targetHandle,
  };
}
