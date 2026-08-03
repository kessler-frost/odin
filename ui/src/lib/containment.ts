// Spatial containment — the owner-mandated rule (NORTHSTAR ledger 2026-07-23):
// "a node drawn inside a VPC/Subnet box BELONGS to it; geometry compiles to
// infrastructure."
//
// ONE rule for everything: the inner node's FULL rect inside the outer node's
// rect, compared with `<=`. A box hanging off an edge — by one pixel or by
// half — is OUTSIDE. That covers leaf-in-container, subnet-in-vpc and
// ecs-in-ec2 alike; where two containers both qualify, the SMALLEST wins, so
// nested boxes resolve to the deepest one.
//
// (The original task-v1 brief gave leaves a looser CENTER-point rule. It was
// replaced by the owner's decision on 2026-07-28 — the long comment above the
// leaf loop below has the reasoning, and this header described the discarded
// rule for a while after the code stopped implementing it.)
//
// Node positions are absolute canvas coordinates — the canvas never uses
// ReactFlow parent-child nesting (containment is spatial + z-index).

import type { Node } from '@xyflow/react';

export type Containment = {
  vpc?: string;
  subnet?: string;
  /**
   * The EC2 instance this workload was drawn INSIDE, if any.
   *
   * Owner's intelligence-layer ask: "when I expand the ec2 box and put an ecs
   * box inside it, that means I want ecs on ec2". In odin that is not a label
   * change -- an EC2 node IS a real Lima VM (`odin-ec2-<env>-<id>`) and
   * `LimaRuntime` can run containers inside a NAMED VM, so an ECS task drawn
   * inside an instance can genuinely run in that instance rather than on the
   * shared host.
   *
   * Note what this is NOT: a Fargate/EC2 launch-type switch. odin already emits
   * `launch_type = "EC2"` unconditionally and has no Fargate substrate at all
   * (`iac/hcl.py`, the "least-fiction" note), so flipping a label would claim
   * a distinction odin cannot back. PLACEMENT is the part that is real.
   */
  host?: string;
};

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

  // Workload-in-instance: an ecs node drawn fully inside an ec2 node. Same
  // strict full-rect rule as everything else here, so "partially inside" is
  // outside and the user can see which they have.
  const instances = nodes.filter((n) => n.type === 'ec2');
  const hostOf: Record<string, Node | undefined> = {};
  for (const n of nodes) {
    if (n.type !== 'ecs') continue;
    hostOf[n.id] = smallest(instances.filter((i) => containsRect(rectOf(i), rectOf(n))));
  }

  // Leaf nodes (sg + any future leaf): FULL-RECT containment, same as
  // subnet-in-vpc above (owner decision, 2026-07-28).
  //
  // This used to be CENTRE-point: a node counted as inside a VPC once its
  // middle crossed the boundary, so a box half-hanging out of a container was
  // silently claimed by it. Containment is not decoration here -- it is where
  // an SG's `vpc_id` comes from, a required and immutable field on a real
  // `aws_security_group` -- so "roughly inside" meant infrastructure decided by
  // a few pixels of overlap, with no way for the user to tell which side of the
  // line they were on.
  //
  // A partially-overlapping box is now OUTSIDE. That is the answer that can be
  // made unambiguous: fully-inside is a property the user can see and aim for,
  // whereas "more than half" is not. Being wrong in the direction of "not
  // contained" is also the recoverable one -- odin reports an SG with no VPC
  // rather than quietly attaching it to the wrong one.
  for (const n of nodes) {
    if (n.type === 'vpc' || n.type === 'subnet') continue;
    const r = rectOf(n);
    const subnet = smallest(subnets.filter((s) => containsRect(rectOf(s), r)));
    const direct = smallest(vpcs.filter((v) => containsRect(rectOf(v), r)));
    const vpc = (subnet ? vpcOfSubnet[subnet.id] : undefined) ?? direct;
    const host = hostOf[n.id];
    result[n.id] = {
      ...(vpc ? { vpc: labelOf(vpc) } : {}),
      ...(subnet ? { subnet: labelOf(subnet) } : {}),
      ...(host ? { host: labelOf(host) } : {}),
    };
  }
  return result;
}

// Does `pos` fall inside a VPC or Subnet's rect? Used to keep a direct
// sidebar-drop INTO a container from being shoved back out by deCollide's
// proximity nudge — the container's geometry is the same source of truth
// computeContainment stamps from.
export function isInsideContainer(pos: { x: number; y: number }, nodes: Node[]): boolean {
  return nodes.some((n) => (n.type === 'vpc' || n.type === 'subnet') && containsPoint(rectOf(n), pos.x, pos.y));
}

// Stamp computeContainment's verdicts onto node data. Returns the SAME array
// when nothing changed, so callers can setNodes unconditionally without
// dirtying history/undo with no-op entries.
export function withContainment(nodes: Node[]): Node[] {
  const stamps = computeContainment(nodes);
  let changed = false;
  const out = nodes.map((n) => {
    const { vpc, subnet, host } = stamps[n.id] ?? {};
    const data = n.data as Record<string, unknown>;
    if (
      (data.vpc ?? undefined) === vpc
      && (data.subnet ?? undefined) === subnet
      && (data.host ?? undefined) === host
    ) return n;
    changed = true;
    // ONLY the containment-derived keys are touched. The owner's invariant for
    // this whole layer: "things like name and stuff remains as is" -- a gesture
    // may set what containment genuinely determines and must never rewrite
    // something a person typed. `rest` carries every other field through
    // untouched, and the three keys are dropped-then-re-added so moving a node
    // OUT clears them rather than leaving a stale claim behind.
    const { vpc: _vpc, subnet: _subnet, host: _host, ...rest } = data;
    return {
      ...n,
      data: {
        ...rest,
        ...(vpc ? { vpc } : {}),
        ...(subnet ? { subnet } : {}),
        ...(host ? { host } : {}),
      },
    };
  });
  return changed ? out : nodes;
}
