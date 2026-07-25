// Run with `bun test` (bun's built-in runner — no other UI test runner is
// configured in this repo). Pure geometry, no DOM.
import { describe, expect, test } from 'bun:test';

import { placeUnpositioned, type Placeable } from './placement';

// The footprint placement assumes for a node whose style has no numbers yet.
const LEAF = { w: 220, h: 120 };

function node(style?: Record<string, unknown>, position?: { x: number; y: number }): Placeable {
  return { ...(position ? { position } : {}), ...(style ? { style } : {}) };
}

function overlapping(nodes: { position: { x: number; y: number }; style?: Record<string, unknown> | null }[]) {
  const rect = (n: (typeof nodes)[number]) => ({
    x: n.position.x,
    y: n.position.y,
    w: typeof n.style?.width === 'number' ? (n.style.width as number) : LEAF.w,
    h: typeof n.style?.height === 'number' ? (n.style.height as number) : LEAF.h,
  });
  const pairs: string[] = [];
  nodes.forEach((a, i) => nodes.slice(i + 1).forEach((b, j) => {
    const [ra, rb] = [rect(a), rect(b)];
    if (ra.x < rb.x + rb.w && rb.x < ra.x + ra.w && ra.y < rb.y + rb.h && rb.y < ra.y + ra.h) {
      pairs.push(`${i}/${i + 1 + j}`);
    }
  }));
  return pairs;
}

describe('placeUnpositioned', () => {
  test('a fully positioned canvas is left exactly as authored', () => {
    const nodes = [node(undefined, { x: 0, y: 0 }), node(undefined, { x: 1000, y: 640 })];
    const result = placeUnpositioned(nodes);
    expect(result.placed).toBe(0);
    expect(result.nodes.map((n) => n.position)).toEqual([{ x: 0, y: 0 }, { x: 1000, y: 640 }]);
  });

  test('with nothing to avoid, it is the CLI\'s grid: five per row, reading order', () => {
    const result = placeUnpositioned(Array.from({ length: 6 }, () => node()));
    expect(result.placed).toBe(6);
    expect(result.nodes.map((n) => n.position)).toEqual([
      { x: 80, y: 80 }, { x: 340, y: 80 }, { x: 600, y: 80 }, { x: 860, y: 80 }, { x: 1120, y: 80 },
      { x: 80, y: 280 },
    ]);
  });

  // Field test 4: an SQS queue authored at the first grid slot, a DynamoDB
  // table and an S3 bucket authored with no position at all. v0.7.3 dropped
  // the table on 80,80 — directly on top of the queue, hiding it.
  test('an auto-placed node never lands on an explicitly positioned one', () => {
    const result = placeUnpositioned([
      node({ width: 200, height: 62 }, { x: 80, y: 80 }),
      node(),
      node(),
    ]);
    expect(result.placed).toBe(2);
    expect(result.nodes[1].position).not.toEqual({ x: 80, y: 80 });
    expect(overlapping(result.nodes)).toEqual([]);
  });

  test('a canvas whose whole first row is occupied pushes placement down a row', () => {
    const row = [0, 1, 2, 3, 4].map((i) => node({ width: 200, height: 62 }, { x: 80 + i * 260, y: 80 }));
    const result = placeUnpositioned([...row, node()]);
    expect(result.nodes[5].position).toEqual({ x: 80, y: 280 });
    expect(overlapping(result.nodes)).toEqual([]);
  });

  // Containment is spatial (containment.ts): a node dropped inside a VPC box
  // BELONGS to it. Placement must never hand a node containment it was never
  // given, so a container's rect is occupied space like any other.
  test('placement stays out of a VPC box rather than stealing containment', () => {
    const vpc = node({ width: 560, height: 380 }, { x: 0, y: 0 });
    const result = placeUnpositioned([vpc, node()]);
    const { x, y } = result.nodes[1].position;
    expect(x >= 560 || y >= 380).toBe(true);
    expect(overlapping(result.nodes)).toEqual([]);
  });

  test('every placed position sits on odin\'s 20px grid', () => {
    const result = placeUnpositioned([
      node({ width: 200, height: 62 }, { x: 80, y: 80 }),
      ...Array.from({ length: 7 }, () => node()),
    ]);
    for (const { position } of result.nodes.slice(1)) {
      expect(position.x % 20).toBe(0);
      expect(position.y % 20).toBe(0);
    }
  });
});
