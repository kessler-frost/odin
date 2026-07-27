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

// Loading: a container keeps its stored size; a leaf drops any stored height,
// including one an older build baked in, and re-derives it from its content.
export const sizeOnLoad = (
  defaults: Record<string, CSSProperties>,
  type: string,
  size: NodeSize | undefined,
): NodeSize => (isContainerKind(defaults, type) ? { ...size } : { ...size, height: undefined });

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
