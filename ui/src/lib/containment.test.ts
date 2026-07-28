// Run with `bun test` (bun's built-in runner — no other UI test runner is
// configured in this repo). Pure geometry, no DOM.
import { describe, expect, test } from 'bun:test';
import type { Node } from '@xyflow/react';

import { computeContainment, withContainment } from './containment';

function node(
  id: string,
  type: string,
  x: number,
  y: number,
  width: number,
  height: number,
  data: Record<string, unknown> = {},
): Node {
  return { id, type, position: { x, y }, data: { label: id, ...data }, style: { width, height } };
}

describe('computeContainment', () => {
  test('a leaf fully inside a subnet inside a vpc stamps both levels', () => {
    const nodes = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('web', 'subnet', 20, 60, 520, 280),
      node('web-sg', 'sg', 40, 100, 200, 80),
    ];
    const c = computeContainment(nodes);
    expect(c['web']).toEqual({ vpc: 'net' });
    expect(c['web-sg']).toEqual({ vpc: 'net', subnet: 'web' });
    expect(c['net']).toEqual({});
  });

  test('leaf outside everything gets no stamps', () => {
    const nodes = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('web-sg', 'sg', 1000, 1000, 200, 80),
    ];
    expect(computeContainment(nodes)['web-sg']).toEqual({});
  });

  test('subnet hanging off the vpc edge is NOT in it (full-rect rule, not center)', () => {
    // Subnet center (500, 190) is well inside the vpc, but its right edge
    // (x 240..760) overhangs the vpc's right edge (560) — center-point logic
    // would stamp it; the full-rect-inside-rect rule must not.
    const nodes = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('overhang', 'subnet', 240, 100, 520, 180),
    ];
    expect(computeContainment(nodes)['overhang']).toEqual({});
  });

  test('leaf directly in a vpc with no subnet takes the direct vpc hit', () => {
    const nodes = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('lone-sg', 'sg', 40, 100, 200, 80),
    ];
    expect(computeContainment(nodes)['lone-sg']).toEqual({ vpc: 'net' });
  });

  test('leaf in a subnet inherits the subnet vpc, not an unrelated overlapping vpc', () => {
    // The subnet is fully inside vpc `net`; a second vpc also covers the
    // leaf's center. Deepest-subnet membership wins: vpc comes via the subnet.
    const nodes = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('big', 'vpc', -100, -100, 2000, 2000),
      node('web', 'subnet', 20, 60, 520, 280),
      node('web-sg', 'sg', 40, 100, 200, 80),
    ];
    const c = computeContainment(nodes);
    expect(c['web']).toEqual({ vpc: 'net' });
    expect(c['web-sg']).toEqual({ vpc: 'net', subnet: 'web' });
  });

  test('an ec2 node is a leaf like sg -- same full-rect rule (V3c)', () => {
    const nodes = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('web', 'subnet', 20, 60, 520, 280),
      node('server', 'ec2', 40, 100, 200, 60),
    ];
    const c = computeContainment(nodes);
    expect(c['server']).toEqual({ vpc: 'net', subnet: 'web' });
  });

  test('measured dimensions win over stale style dimensions', () => {
    const stale = node('web', 'subnet', 20, 60, 9999, 9999);
    stale.measured = { width: 520, height: 280 };
    const c = computeContainment([node('net', 'vpc', 0, 0, 560, 380), stale]);
    expect(c['web']).toEqual({ vpc: 'net' });
  });
});

describe('withContainment', () => {
  test('moving a node out clears its stamps', () => {
    const inside = [
      node('net', 'vpc', 0, 0, 560, 380),
      node('web', 'subnet', 20, 60, 520, 280),
    ];
    const stamped = withContainment(inside);
    expect(stamped[1].data.vpc).toBe('net');

    const movedOut = stamped.map((n) =>
      n.id === 'web' ? { ...n, position: { x: 2000, y: 2000 } } : n,
    );
    const restamped = withContainment(movedOut);
    expect(restamped[1].data.vpc).toBeUndefined();
  });

  test('returns the same array reference when nothing changed (no history churn)', () => {
    const nodes = withContainment([
      node('net', 'vpc', 0, 0, 560, 380),
      node('web', 'subnet', 20, 60, 520, 280),
    ]);
    expect(withContainment(nodes)).toBe(nodes);
  });
});

describe('a leaf must be COMPLETELY inside to count as contained', () => {
  // Owner decision, 2026-07-28. This was centre-point containment: a box whose
  // middle had crossed the boundary was claimed by the container even while it
  // visibly hung out of it. Containment supplies an SG's `vpc_id` -- required
  // and immutable on a real `aws_security_group` -- so that meant a few pixels
  // of overlap silently decided infrastructure.
  const vpc = () => node('v1', 'vpc', 0, 0, 400, 400, { label: 'main-vpc' });

  test('fully inside is contained', () => {
    const result = computeContainment([vpc(), node('sg1', 'sg', 100, 100, 100, 100, { label: 'web-sg' })]);
    expect(result.sg1).toEqual({ vpc: 'main-vpc' });
  });

  test('an exact fit counts — the rule is <=, not <', () => {
    const result = computeContainment([vpc(), node('sg1', 'sg', 0, 0, 400, 400, { label: 'web-sg' })]);
    expect(result.sg1).toEqual({ vpc: 'main-vpc' });
  });

  test('hanging out by ONE pixel is outside, on every side', () => {
    // The case that used to be "inside": centre still within, edge not.
    for (const [x, y, w, h] of [
      [-1, 100, 100, 100],   // past the left
      [100, -1, 100, 100],   // past the top
      [301, 100, 100, 100],  // past the right
      [100, 301, 100, 100],  // past the bottom
    ]) {
      const result = computeContainment([vpc(), node('sg1', 'sg', x, y, w, h, { label: 'web-sg' })]);
      expect(result.sg1).toEqual({});
    }
  });

  test('a box larger than its would-be container is never contained', () => {
    const result = computeContainment([vpc(), node('sg1', 'sg', -50, -50, 500, 500, { label: 'web-sg' })]);
    expect(result.sg1).toEqual({});
  });

  test('half-overlapping reports NO vpc rather than guessing one', () => {
    // Being wrong toward "not contained" is the recoverable direction: odin
    // reports an SG with no VPC instead of quietly attaching it to the wrong one.
    const result = computeContainment([
      vpc(),
      node('v2', 'vpc', 400, 0, 400, 400, { label: 'other-vpc' }),
      node('sg1', 'sg', 350, 100, 100, 100, { label: 'straddler' }),
    ]);
    expect(result.sg1).toEqual({});
  });
});

describe('an ecs node drawn inside an ec2 box means ECS-on-that-instance', () => {
  // Owner's ask: "when I expand the ec2 box and put an ecs box inside it, that
  // means I want ecs on ec2 ... and the configuration and stuff updates
  // accordingly if needed but things like name and stuff remains as is."
  //
  // In odin this is PLACEMENT, not a launch-type label: an EC2 node is a real
  // Lima VM and `LimaRuntime` can run containers inside a named one, so the
  // task can genuinely run in that instance. odin already emits
  // `launch_type = "EC2"` unconditionally and has no Fargate substrate, so
  // flipping a label would claim a distinction it cannot back.
  const instance = () => node('ec2-1', 'ec2', 0, 0, 600, 400, { label: 'api-server' });

  test('fully inside stamps the host instance', () => {
    const result = computeContainment([instance(), node('ecs-1', 'ecs', 100, 100, 200, 80, { label: 'web' })]);
    expect(result['ecs-1']).toEqual({ host: 'api-server' });
  });

  test('partially inside is NOT placed — same strict rule as everything else', () => {
    // Chosen so the two candidate rules DISAGREE: the box spans 500..700 across
    // an instance ending at 600, so its centre (600) is still inside while the
    // box plainly is not. A first version used x=550, whose centre (650) is
    // outside under either rule -- it passed against centre-point containment
    // too, and so proved nothing.
    const result = computeContainment([instance(), node('ecs-1', 'ecs', 500, 100, 200, 80, { label: 'web' })]);
    expect(result['ecs-1']).toEqual({});
  });

  test('a workload outside every instance keeps no host', () => {
    const result = computeContainment([instance(), node('ecs-1', 'ecs', 900, 100, 200, 80, { label: 'web' })]);
    expect(result['ecs-1']).toEqual({});
  });

  test('the deepest instance wins when boxes nest', () => {
    const outer = node('ec2-1', 'ec2', 0, 0, 800, 600, { label: 'big' });
    const inner = node('ec2-2', 'ec2', 100, 100, 400, 300, { label: 'small' });
    const result = computeContainment([outer, inner, node('ecs-1', 'ecs', 150, 150, 200, 80, { label: 'web' })]);
    expect(result['ecs-1']).toEqual({ host: 'small' });
  });

  test('only ecs is placed this way — an s3 bucket inside an instance is not', () => {
    const result = computeContainment([instance(), node('s3-1', 's3', 100, 100, 200, 80, { label: 'uploads' })]);
    expect(result['s3-1'].host).toBeUndefined();
  });
});

describe('withContainment never rewrites what the user authored', () => {
  // The owner's invariant for the whole intelligence layer, and the line
  // between a language and a trap.
  const nodes = () => [
    node('ec2-1', 'ec2', 0, 0, 600, 400, { label: 'api-server' }),
    node('ecs-1', 'ecs', 100, 100, 200, 80, {
      label: 'web', image: 'myapp:v2', count: '3', port: '8080',
    }),
  ];

  test('placement is stamped and every authored field survives', () => {
    const [, ecs] = withContainment(nodes());
    expect(ecs.data).toMatchObject({
      label: 'web', image: 'myapp:v2', count: '3', port: '8080', host: 'api-server',
    });
  });

  test('dragging back OUT clears the placement rather than leaving a stale claim', () => {
    const placed = withContainment(nodes());
    const moved = placed.map((n) => (n.id === 'ecs-1' ? { ...n, position: { x: 900, y: 900 } } : n));
    const [, ecs] = withContainment(moved);
    expect(ecs.data.host).toBeUndefined();
    // ...and the authored values are still untouched by the round trip.
    expect(ecs.data).toMatchObject({ label: 'web', image: 'myapp:v2', count: '3', port: '8080' });
  });

  test('a node whose containment did not change is returned identically', () => {
    const once = withContainment(nodes());
    expect(withContainment(once)).toBe(once);
  });
});
