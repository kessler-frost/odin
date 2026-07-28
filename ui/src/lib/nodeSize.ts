import type { CSSProperties } from 'react';

// Which stored dimensions a node gets back, and which it is allowed to store.
//
// The rule, and the bug it exists to prevent: leaf nodes size themselves to
// their CONTENT. Canvas used to persist `measured.height` -- whatever ReactFlow
// happened to measure on first render -- and reapply it as a hard CSS height,
// which froze every box at its first-render size. A lambda measured at 60px
// before its `handler` line existed then rendered
// `lambda_function.lambda_handler` BELOW its own border, and an S3 node with no
// ARN kept a dead 20px strip under the separator forever.
//
// A CONTAINER (a kind with a declared height: vpc, subnet) is different -- it
// is a region the user sizes to hold other nodes, so its height is real and
// round-trips. The declared-height check IS the discriminator; there is no
// second list to keep in sync.

export type NodeSize = { width?: number; height?: number };

type SizedNode = {
  type?: string;
  width?: number | null;
  height?: number | null;
  style?: CSSProperties;
  measured?: { width?: number | null; height?: number | null };
};

export const isContainerKind = (
  defaults: Record<string, CSSProperties>,
  type: string,
): boolean => 'height' in (defaults[type] ?? {});



// Loading: EVERY node keeps a stored size, whatever its kind.
//
// A stored height is always a deliberate choice, because `sizeForSave` below
// only ever writes one the RESIZER set or a kind's declared default -- never
// `measured.height`, which is what once froze every box at its first-render
// size. Dropping heights on load was the belt-and-braces half of that fix, and
// it outlived its cause: it meant resizing an S3 or Lambda box silently snapped
// back on the next load, and (until this was noticed) that an EC2 box expanded
// to hold a workload lost the expansion, taking the workload's placement with
// it.
//
// Positions and widths already persisted for everything; heights are simply
// the third dimension of the same fact, and there is no kind for which "the
// user dragged this bigger" should be discarded.
//
// The one caveat, stated rather than guarded: a canvas written by a build from
// BEFORE the save-side fix may contain a height baked from a measurement. Such
// a node now renders at that height instead of re-deriving it. That is visible
// and one drag away from fixed, which is a better trade than silently
// discarding every deliberate resize to protect against an old file.
export const sizeOnLoad = (
  _defaults: Record<string, CSSProperties>,
  _type: string,
  size: NodeSize | undefined,
): NodeSize => ({ ...size });

// Saving: store only a height the user actually CHOSE (the resizer sets
// `node.height`) or a container's declared default. Deliberately never
// `measured.height` -- persisting a measurement is what froze the boxes.
export const sizeForSave = (
  defaults: Record<string, CSSProperties>,
  node: SizedNode,
): NodeSize => {
  const fallback = defaults[node.type ?? ''] ?? {};
  return {
    width: node.width ?? (node.style?.width as number) ?? (fallback.width as number),
    height: node.height ?? (node.style?.height as number) ?? (fallback.height as number),
  };
};
