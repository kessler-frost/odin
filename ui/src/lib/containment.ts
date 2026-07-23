// Spatial containment — the owner-mandated rule (NORTHSTAR ledger 2026-07-23):
// "a node drawn inside a VPC/Subnet box BELONGS to it; geometry compiles to
// infrastructure." Two deliberately different rules (task-v1 brief):
//   - leaf-in-container: the leaf's CENTER point inside the container's rect;
//   - subnet-in-vpc: the subnet's FULL rect inside the vpc's rect (a subnet
//     hanging off the edge of a VPC is not in it).
// Node positions are absolute canvas coordinates — the canvas never uses
// ReactFlow parent-child nesting (containment is spatial + z-index).

import type { Node } from '@xyflow/react';

export type Containment = { vpc?: string; subnet?: string };

type Rect = { x: number; y: number; w: number; h: number };

const num = (v: unknown): number => (typeof v === 'number' ? v : 0);

// measured dims exist post-render; style dims cover a node that hasn't
// rendered yet (fresh drop, programmatic resize).
function rectOf(n: Node): Rect {
  const style = (n.style ?? {}) as { width?: number | string; height?: number | string };
  return {
    x: n.position.x,
    y: n.position.y,
    w: n.measured?.width ?? num(style.width),
    h: n.measured?.height ?? num(style.height),
  };
}

const containsPoint = (r: Rect, px: number, py: number): boolean =>
  px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h;

const containsRect = (outer: Rect, inner: Rect): boolean =>
  inner.x >= outer.x && inner.y >= outer.y &&
  inner.x + inner.w <= outer.x + outer.w && inner.y + inner.h <= outer.y + outer.h;

const labelOf = (n: Node): string => ((n.data as { label?: string })?.label ?? n.id);

const area = (n: Node): number => rectOf(n).w * rectOf(n).h;

// Deepest container = the smallest enclosing rect (nested boxes contain each other).
const smallest = (candidates: Node[]): Node | undefined =>
  [...candidates].sort((a, b) => area(a) - area(b))[0];

export function computeContainment(nodes: Node[]): Record<string, Containment> {
  const vpcs = nodes.filter((n) => n.type === 'vpc');
  const subnets = nodes.filter((n) => n.type === 'subnet');
  const result: Record<string, Containment> = {};

  // Subnet-in-vpc: FULL-rect containment. VPCs themselves are never contained (V1).
  const vpcOfSubnet: Record<string, Node | undefined> = {};
  for (const v of vpcs) result[v.id] = {};
  for (const s of subnets) {
    const vpc = smallest(vpcs.filter((v) => containsRect(rectOf(v), rectOf(s))));
    vpcOfSubnet[s.id] = vpc;
    result[s.id] = { ...(vpc ? { vpc: labelOf(vpc) } : {}) };
  }

  // Leaf nodes (sg + any future leaf): CENTER-point containment. A leaf in a
  // subnet inherits the subnet's vpc; a leaf in a vpc but no subnet takes the
  // direct vpc hit.
  for (const n of nodes) {
    if (n.type === 'vpc' || n.type === 'subnet') continue;
    const r = rectOf(n);
    const cx = r.x + r.w / 2;
    const cy = r.y + r.h / 2;
    const subnet = smallest(subnets.filter((s) => containsPoint(rectOf(s), cx, cy)));
    const direct = smallest(vpcs.filter((v) => containsPoint(rectOf(v), cx, cy)));
    const vpc = (subnet ? vpcOfSubnet[subnet.id] : undefined) ?? direct;
    result[n.id] = {
      ...(vpc ? { vpc: labelOf(vpc) } : {}),
      ...(subnet ? { subnet: labelOf(subnet) } : {}),
    };
  }
  return result;
}

// Stamp computeContainment's verdicts onto node data. Returns the SAME array
// when nothing changed, so callers can setNodes unconditionally without
// dirtying history/undo with no-op entries.
export function withContainment(nodes: Node[]): Node[] {
  const stamps = computeContainment(nodes);
  let changed = false;
  const out = nodes.map((n) => {
    const { vpc, subnet } = stamps[n.id] ?? {};
    const data = n.data as Record<string, unknown>;
    if ((data.vpc ?? undefined) === vpc && (data.subnet ?? undefined) === subnet) return n;
    changed = true;
    const { vpc: _vpc, subnet: _subnet, ...rest } = data;
    return { ...n, data: { ...rest, ...(vpc ? { vpc } : {}), ...(subnet ? { subnet } : {}) } };
  });
  return changed ? out : nodes;
}
