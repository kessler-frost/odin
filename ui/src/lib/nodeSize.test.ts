// Run with `bun test` (bun's built-in runner — no other UI test runner is
// configured in this repo). Pure size bookkeeping, no DOM.
//
// These pin the ROOT CAUSE of the overflow bug, not its symptom. The symptom —
// a lambda rendering `lambda_function.lambda_handler` below its own border —
// is a rendered HEIGHT, and no test here can prove it: neither jsdom nor
// happy-dom runs a layout engine, so `clientHeight` is 0 under both. That half
// was verified by measuring the real browser (40 + 20*rows, on grid, zero
// overflow). What IS testable is the rule that caused it: which heights get
// persisted and which get reapplied.
import { describe, expect, test } from 'bun:test';

import { isContainerKind, sizeForSave, sizeOnLoad } from './nodeSize';

// Mirrors Canvas's `defaultStyleForType`: containers declare a height, leaves
// declare only a width.
const DEFAULTS = {
  vpc: { width: 560, height: 380 },
  subnet: { width: 520, height: 280 },
  lambda: { width: 220 },
  s3: { width: 200 },
};

describe('container vs leaf', () => {
  test('a declared height is what makes a kind a container', () => {
    expect(isContainerKind(DEFAULTS, 'vpc')).toBe(true);
    expect(isContainerKind(DEFAULTS, 'subnet')).toBe(true);
    expect(isContainerKind(DEFAULTS, 'lambda')).toBe(false);
    expect(isContainerKind(DEFAULTS, 's3')).toBe(false);
  });

  test('an unknown kind is treated as a leaf, not a container', () => {
    expect(isContainerKind(DEFAULTS, 'kinesis')).toBe(false);
  });
});

describe('sizeOnLoad', () => {
  // The regression itself: 62px is what ReactFlow measured for a lambda before
  // its handler line existed, and older builds wrote it to disk. Reapplying it
  // is what clipped the handler.
  test('a stored height is KEPT, for a leaf as much as a container', () => {
    // This used to assert the opposite, and the opposite outlived its reason.
    // Heights were dropped on load because an older build baked
    // `measured.height` into the saved canvas and froze every box. `sizeForSave`
    // stopped writing measurements, which fixed it at the source -- after which
    // dropping heights on load only discarded DELIBERATE resizes. Positions and
    // widths always persisted; a height is the same fact.
    expect(sizeOnLoad(DEFAULTS, 'lambda', { width: 220, height: 62 })).toEqual({
      width: 220,
      height: 62,
    });
  });

  test('a leaf keeps its stored width', () => {
    expect(sizeOnLoad(DEFAULTS, 's3', { width: 300 }).width).toBe(300);
  });

  test('a container keeps its stored height — it is a region the user sized', () => {
    expect(sizeOnLoad(DEFAULTS, 'vpc', { width: 800, height: 640 })).toEqual({
      width: 800,
      height: 640,
    });
  });
});

describe('sizeForSave', () => {
  test('a measurement is never persisted', () => {
    const measuredOnly = { type: 'lambda', measured: { width: 220, height: 80 } };
    expect(sizeForSave(DEFAULTS, measuredOnly).height).toBeUndefined();
  });

  test('a height the user chose with the resizer IS persisted', () => {
    expect(sizeForSave(DEFAULTS, { type: 'lambda', height: 160 }).height).toBe(160);
  });

  test("a container falls back to its declared height, so it round-trips", () => {
    expect(sizeForSave(DEFAULTS, { type: 'vpc' }).height).toBe(380);
  });

  test('width still falls back to the declared default', () => {
    expect(sizeForSave(DEFAULTS, { type: 's3' }).width).toBe(200);
  });
});

describe('round trip', () => {
  // The invariant that keeps boxes content-sized: save then load must not
  // reintroduce a height for a leaf nobody resized, however many times the
  // canvas is saved and reloaded.
  test('a never-resized leaf never acquires a height from being MEASURED', () => {
    // The frozen-boxes guarantee, now enforced entirely on the save side: no
    // matter how many times a node is measured and re-saved, a height nobody
    // chose is never written. `measured: { height: 80 }` is deliberately fed in
    // on every pass -- that is exactly the value the old bug persisted.
    let size = sizeOnLoad(DEFAULTS, 'lambda', { width: 220 });
    for (let i = 0; i < 3; i++) {
      const saved = sizeForSave(DEFAULTS, { type: 'lambda', style: size, measured: { height: 80 } });
      expect(saved.height).toBeUndefined();
      size = sizeOnLoad(DEFAULTS, 'lambda', saved);
      expect(size.height).toBeUndefined();
    }
  });

  test('a resized container survives the same round trip', () => {
    let size = sizeOnLoad(DEFAULTS, 'vpc', { width: 800, height: 640 });
    for (let i = 0; i < 3; i++) {
      const saved = sizeForSave(DEFAULTS, { type: 'vpc', style: size });
      size = sizeOnLoad(DEFAULTS, 'vpc', saved);
    }
    expect(size).toEqual({ width: 800, height: 640 });
  });
});

describe('an EC2 box the user expanded keeps its height', () => {
  // The owner's gesture only works if the expansion SURVIVES: "when I expand the
  // ec2 box and put an ecs box inside it". Before this, a leaf's stored height
  // was dropped on load and re-derived from content, so an instance expanded to
  // hold a workload snapped back on reload and the workload fell outside it --
  // silently un-placing it.
  const defaults = { vpc: { width: 400, height: 300 }, ec2: { width: 200 }, s3: { width: 200 } };

  test('a height the user chose is kept', () => {
    expect(sizeOnLoad(defaults, 'ec2', { width: 480, height: 200 })).toEqual({ width: 480, height: 200 });
  });

  test('an un-expanded instance stays adaptive', () => {
    // No default height, so nothing is invented: the node is as tall as its
    // content until someone drags it bigger.
    expect(sizeOnLoad(defaults, 'ec2', { width: 200 }).height).toBeUndefined();
    expect(sizeOnLoad(defaults, 'ec2', undefined).height).toBeUndefined();
  });

  test('every other kind keeps a chosen height too', () => {
    // Generalised after the owner asked why this was ec2-only: nothing about an
    // S3 or Lambda box makes a deliberate resize less real.
    expect(sizeOnLoad(defaults, 's3', { width: 200, height: 140 }).height).toBe(140);
  });

  test('a real container is unaffected', () => {
    expect(sizeOnLoad(defaults, 'vpc', { width: 900, height: 500 })).toEqual({ width: 900, height: 500 });
  });

  test('an absent height stays absent — nothing is invented', () => {
    expect(sizeOnLoad(defaults, 'ec2', { width: 200 }).height).toBeUndefined();
    expect(sizeOnLoad(defaults, 'ec2', undefined).height).toBeUndefined();
  });
});
