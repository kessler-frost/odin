// Run with `bun test` (bun's built-in runner — no other UI test runner is
// configured in this repo).
//
// The sidebar's honesty contract. A tile whose `type` is not in
// spec/translate.py's `_KIND` can never become a Stack resource -- Apply reports
// it under `skipped_node_types` and touches nothing -- so the tile has to SAY so,
// and no two tiles may claim the same name. Both properties were broken at once
// before this file existed: `iamrole` (unmodelled) and `iam_role` (real) both
// rendered "IAM Role" in the same Security group, so dragging the wrong one meant
// a silent no-op Apply with nothing on the tile to tell them apart.
//
// `_KIND` is PARSED OUT OF translate.py rather than restated here on purpose. A
// hardcoded copy of the modelled-kinds list is a second source of truth that
// drifts the moment someone adds a kind, and would then assert against a signal
// the backend never sent -- the exact failure mode CLAUDE.md's honesty rule 1
// warns about. Read the real thing and the test breaks when reality moves.
import { describe, expect, test } from 'bun:test';

import { BUILTINS, CATALOG, PALETTE, catalogByType, catalogFields, catalogIamActions, isPlaceholder } from './catalog';

const TRANSLATE = new URL('../../../src/odin/spec/translate.py', import.meta.url).pathname;
const MARKER = '(placeholder)';

async function modelledKinds(): Promise<Set<string>> {
  const source = await Bun.file(TRANSLATE).text();
  const [, after] = source.split('_KIND = {');
  expect(after, `no "_KIND = {" in ${TRANSLATE} — the map was renamed or moved`).toBeDefined();
  const block = after.split('}')[0];
  const kinds = [...block.matchAll(/^\s*"([\w-]+)":\s*"([\w-]+)",$/gm)].map((m) => m[1]);
  // Guard the guard: if the parse silently matched nothing, every assertion
  // below would "pass" by calling the whole catalog modelled/unmodelled.
  expect(kinds.length).toBeGreaterThan(10);
  expect(kinds).toContain('iam_role');
  return new Set(kinds);
}

describe('catalog ↔ translate.py', () => {
  test('a tile is marked (placeholder) if and only if Apply skips its type', async () => {
    const kinds = await modelledKinds();
    const state = (s: (typeof CATALOG)[number]) => ({
      type: s.type,
      marked: s.sublabel.includes(MARKER),
      modelled: kinds.has(s.type),
    });
    // Two directions, both spelled out so a failure names which way it broke:
    // an unmarked placeholder is the original trap; a marked real service is a
    // stale marker nobody removed when the kind landed (honesty rule 3).
    expect(CATALOG.map(state).filter((s) => !s.modelled && !s.marked)).toEqual([]);
    expect(CATALOG.map(state).filter((s) => s.modelled && s.marked)).toEqual([]);
  });

  test('no placeholder advertises IAM actions it can never enforce', async () => {
    const kinds = await modelledKinds();
    // A permission ticked on an edge to a node Apply never creates cannot be
    // enforced or even reached; the gateway classifies no action for these
    // namespaces at all. `kinesis` used to be here.
    expect(Object.keys(catalogIamActions).filter((type) => !kinds.has(type))).toEqual([]);
  });

  test('no two tiles share a label, and none share an abbr', () => {
    // The abbr is the drag key (`catalogNodeTypeMap`, abbr -> type): a collision
    // there does not just confuse a reader, it makes one tile drop the other's
    // node type on drop. The label is what the user actually reads.
    const dupes = (values: string[]) =>
      [...new Set(values.filter((v, i) => values.indexOf(v) !== i))].sort();
    expect(dupes(CATALOG.map((s) => s.label))).toEqual([]);
    expect(dupes(CATALOG.map((s) => s.abbr))).toEqual([]);
    expect(dupes(CATALOG.map((s) => s.type))).toEqual([]);
  });

  test('every tile width stays on the 20px grid', () => {
    expect(CATALOG.filter((s) => s.width % 20 !== 0).map((s) => s.type)).toEqual([]);
  });
});

// The tests above cover CATALOG, which is only 20 of the 27 tiles the sidebar
// renders: `Sidebar.tsx` builds `[...BUILTINS, ...catalogItems]`. A rule that
// skips a quarter of the tiles is a guard that only half fires, so the invariants
// are held over the union here -- which is the whole reason BUILTINS carries a
// `type` and lives in catalog.ts instead of being a shape private to the sidebar.
describe('the whole rendered sidebar', () => {
  const TILES = [...BUILTINS, ...CATALOG];
  const dupes = (values: string[]) =>
    [...new Set(values.filter((v, i) => values.indexOf(v) !== i))].sort();

  test('neither half of the sidebar is missing from the union', () => {
    // Guard the guard: were BUILTINS ever emptied, every assertion below would
    // pass by checking the catalog alone -- the gap this block exists to close.
    // Named for what it asserts, not for the count it does not: a test whose
    // name claims more than its body is the same overselling this suite exists
    // to prevent, one level up.
    expect(BUILTINS.length).toBe(7);
    expect(TILES.length).toBeGreaterThan(20);
  });

  test('a tile is marked (placeholder) if and only if Apply skips its type', async () => {
    const kinds = await modelledKinds();
    const state = (t: { type: string; sublabel: string }) => ({
      type: t.type,
      marked: t.sublabel.includes(MARKER),
      modelled: kinds.has(t.type),
    });
    expect(TILES.map(state).filter((t) => !t.modelled && !t.marked)).toEqual([]);
    expect(TILES.map(state).filter((t) => t.modelled && t.marked)).toEqual([]);
  });

  test('no two tiles share a label, an abbr, or a type', () => {
    expect(dupes(TILES.map((t) => t.label))).toEqual([]);
    expect(dupes(TILES.map((t) => t.type))).toEqual([]);
    // The abbr is the sharpest of the three. `Canvas.tsx`'s `nodeTypeMap` spreads
    // `...catalogNodeTypeMap` AFTER the BUILTINS entries, so a catalog tile
    // declaring abbr 'S3' or 'EC2' would silently OVERRIDE the bespoke one --
    // dragging the bespoke tile would create the catalog node type instead, with
    // nothing on screen to say so.
    expect(dupes(TILES.map((t) => t.abbr))).toEqual([]);
  });

  test('nodeTypeMap keeps deriving its bespoke keys from BUILTINS', async () => {
    // The collision test above is only meaningful while Canvas.tsx has ONE source
    // for these keys. It used to hold a second hardcoded copy; if someone
    // reintroduces literal abbr keys there, this suite would be policing a list
    // the canvas no longer reads.
    const source = await Bun.file(
      new URL('../components/Canvas.tsx', import.meta.url).pathname,
    ).text();
    const block = source.split('const nodeTypeMap: Record<string, string> = {')[1];
    expect(block, 'nodeTypeMap was renamed or moved in Canvas.tsx').toBeDefined();
    const body = block.split('};')[0];
    expect(body).toContain('BUILTINS.map');
    expect([...body.matchAll(/^\s*(\w+):\s*'[\w-]+',$/gm)].map((m) => m[1])).toEqual([]);
  });
});

describe('the palette hides placeholder kinds', () => {
  test('offers no tile that Apply would silently skip', () => {
    const offered = PALETTE.filter((s) => isPlaceholder(s.sublabel));
    expect(offered).toEqual([]);
  });

  test('still keeps every kind in CATALOG, so a saved canvas renders', () => {
    // Palette-only hiding. A canvas authored earlier (or by `odin canvas set`)
    // may contain a placeholder node; it must not become an unknown type.
    const hidden = CATALOG.filter((s) => isPlaceholder(s.sublabel));
    expect(hidden.length).toBeGreaterThan(0);
    for (const s of hidden) expect(catalogByType[s.type]).toBeDefined();
  });

  test('a kind stops being hidden the moment its marker comes off', () => {
    // The marker is the single source of truth -- no second list to update.
    expect(isPlaceholder('Block storage (placeholder)')).toBe(true);
    expect(isPlaceholder('Block storage')).toBe(false);
  });
});

describe('every kind a canvas can name has somewhere to render', () => {
  // The gap this closes: ReactFlow falls back to its `default` node for a type
  // it has no component for, and that default is a blank white rectangle. A
  // canvas saying `"type": "role"` (the kind is `iam_role`) drew an unlabelled
  // white box while regenerating the README hero -- indistinguishable from odin
  // mis-rendering a resource it DOES know, and I took it for one.
  //
  // `Canvas.tsx` now registers `default: UnknownNode`, which names the kind. A
  // component test would need DOM infrastructure this repo does not have, so
  // what is pinned here is the INVARIANT that makes the fallback necessary and
  // sufficient: every real kind has its own entry, so anything reaching the
  // fallback is genuinely unrecognised rather than merely unregistered.
  test('every catalog + builtin type is a known kind', () => {
    const known = new Set([...CATALOG.map((s) => s.type), ...BUILTINS.map((b) => b.type)]);
    expect(known.size).toBe(CATALOG.length + BUILTINS.length);
    for (const type of known) expect(type).toMatch(/^[a-z0-9_]+$/);
  });

  test('the near-miss that caused this is still a near-miss', () => {
    // `role` vs `iam_role` — if a future rename made `role` real, the hero
    // canvas that exposed the bug would stop exercising it and this test says so.
    const known = new Set([...CATALOG.map((s) => s.type), ...BUILTINS.map((b) => b.type)]);
    expect(known.has('iam_role')).toBe(true);
    expect(known.has('role')).toBe(false);
  });
});


// A containment stamp is authored by DRAGGING, and odin acts on it: an ecs
// node's `host` becomes a real `placement_constraints { memberOf }` and decides
// which Lima VM the task runs in. The gesture is only honest if the answer is
// legible -- the containment rule is strict full-rect, so "I dragged it in" and
// "it went in" are genuinely different states a user must be able to tell apart.
describe('containment stamps are shown, and never editable', () => {
  test('the ECS tile surfaces the instance it was drawn into', () => {
    const host = catalogFields['ecs'].find((f) => f.key === 'host');
    expect(host).toBeDefined();
    expect(host!.label).toContain('placement');
  });

  test('no containment stamp is editable anywhere in the catalog', () => {
    // Typing one would be a second source of truth that the next drag silently
    // overwrites -- `withContainment` re-derives these keys from geometry on
    // every change and would discard whatever was typed, with no warning.
    const stamps = new Set(['vpc', 'subnet', 'host']);
    const editable = Object.entries(catalogFields).flatMap(([type, fields]) =>
      fields.filter((f) => stamps.has(f.key) && f.editable).map((f) => `${type}.${f.key}`),
    );
    expect(editable).toEqual([]);
  });
});
