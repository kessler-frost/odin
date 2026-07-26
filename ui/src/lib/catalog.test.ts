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

import { builtins } from '../components/Sidebar';
import { CATALOG, catalogIamActions } from './catalog';

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
// renders: `Sidebar.tsx` builds `[...builtins, ...catalogItems]`, and those seven
// bespoke builtins (VPC, Subnet, SG, EC2, Lambda, S3, DynamoDB) never pass
// through catalog.ts. A uniqueness rule that skips a quarter of the tiles is a
// guard that only half fires, so it is held over the union here.
describe('the whole rendered sidebar', () => {
  const dupes = (values: string[]) =>
    [...new Set(values.filter((v, i) => values.indexOf(v) !== i))].sort();

  test('no builtin and catalog tile share a label', () => {
    expect(dupes([...builtins, ...CATALOG].map((t) => t.label))).toEqual([]);
  });

  test('no catalog abbr shadows a builtin abbr in Canvas.tsx nodeTypeMap', async () => {
    // The sharper half, and the reason this test reads Canvas.tsx instead of
    // trusting Sidebar.tsx: `nodeTypeMap` spreads `...catalogNodeTypeMap` AFTER
    // its seven literal builtin keys, so a catalog entry declaring abbr 'S3' or
    // 'EC2' would silently OVERRIDE the builtin -- dragging the bespoke tile
    // would create the catalog node type instead, with nothing on screen to say
    // so. Nothing else in the codebase prevents that collision.
    const source = await Bun.file(
      new URL('../components/Canvas.tsx', import.meta.url).pathname,
    ).text();
    const block = source.split('const nodeTypeMap: Record<string, string> = {')[1];
    expect(block, 'nodeTypeMap was renamed or moved in Canvas.tsx').toBeDefined();
    const literalAbbrs = [...block.split('}')[0].matchAll(/^\s*(\w+):\s*'[\w-]+',$/gm)]
      .map((m) => m[1]);
    // Guard the guard: a parse that matched nothing would pass vacuously.
    expect(literalAbbrs.length).toBe(7);
    // And the two hardcoded copies of the builtin set must agree, or this test
    // would be policing a list the sidebar no longer renders.
    expect(literalAbbrs.sort()).toEqual(builtins.map((b) => b.abbr).sort());
    expect(CATALOG.map((s) => s.abbr).filter((a) => literalAbbrs.includes(a))).toEqual([]);
  });
});
