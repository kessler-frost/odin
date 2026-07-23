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
  test('leaf center inside a subnet inside a vpc stamps both levels', () => {
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
