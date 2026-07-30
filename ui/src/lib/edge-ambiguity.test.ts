/**
 * The ambiguity ratchet, over every ordered pair of node kinds on the canvas.
 *
 * ## Why this file exists
 *
 * ROADMAP's edge-type-selector entry says: "`iam.test.ts` carries the trigger
 * instead: the moment a pair becomes genuinely ambiguous the test FAILS, naming
 * the pair, which is when the selector becomes real work rather than
 * speculation. Mutation-tested by making one pair ambiguous."
 *
 * That test did not exist. `iam.test.ts` is 96 lines, iterates no pairs, and
 * mentions the catalog nowhere; `git log -S"ambiguity ratchet" -- ui/src`
 * returns nothing. The only trace of it was a COMMENT in `iam.ts` claiming it
 * stays green. So the measurement the whole design rests on -- every pair means
 * exactly one thing, therefore a picker would never open -- had nothing holding
 * it, and would have gone stale the first time anyone added an edge type.
 *
 * That is this repo's own most-repeated bug, in its documentation: a guard that
 * was described, reviewed, believed, and never fired. It is the reason honesty
 * rule 1 exists.
 *
 * ## What it asserts
 *
 * `detectEdgeTypes` returns an ARRAY because a pair may legitimately mean more
 * than one thing. It FIRED, as designed, on 2026-07-28 -- the connection edge
 * gave `rds` and `elasticache` a second honest reading against `ecs` and
 * `lambda`, and this file printed all eight ordered pairs by name:
 *
 *     ecs -> elasticache: connection | iam    elasticache -> ecs: connection | iam
 *     ecs -> rds: connection | iam            elasticache -> lambda: connection | iam
 *     lambda -> elasticache: connection | iam rds -> ecs: connection | iam
 *     lambda -> rds: connection | iam         rds -> lambda: connection | iam
 *
 * So the assertion is no longer "nothing is ambiguous". It is "these eight are
 * ambiguous ON PURPOSE, and nothing else is" -- which keeps every property the
 * original had. A NEW pair acquiring a second meaning still fails by name, an
 * intended pair that silently LOSES its second meaning fails too (that would
 * mean the picker stopped opening for it), and a pair whose meanings quietly
 * change fails on the set rather than the count.
 *
 * A failure naming an unlisted pair is not a bug. It is the signal that a user
 * drawing that pair now has a genuine choice, and odin should ask rather than
 * silently pick `[0]`. Add it here once the picker really does ask.
 */
import { describe, expect, it } from 'bun:test';

import { catalogTypes } from './catalog';
import { connectionConsumerTypes, connectionRefs, detectEdgeTypes, edgeTypes } from './iam';

// Every kind the canvas can render, from the two places kinds really come from:
// the seven bespoke components registered in `Canvas.tsx`'s `nodeTypes`, and
// every catalog service (which renders with the generic `ServiceNode`). Derived
// rather than hand-listed, so a kind added to the catalog is covered here the
// day it appears.
const BESPOKE_KINDS = ['vpc', 'subnet', 'sg', 'ec2', 'lambda', 's3', 'dynamodb'];
const ALL_KINDS = [...new Set([...BESPOKE_KINDS, ...catalogTypes])].sort();

const orderedPairs = (): Array<[string, string]> =>
  ALL_KINDS.flatMap((a) => ALL_KINDS.map((b): [string, string] => [a, b]));

describe('edge-type ambiguity', () => {
  it('covers every kind the canvas can render', () => {
    // Guards the guard: if `catalogTypes` were empty or the import silently
    // resolved to nothing, every assertion below would pass over an empty set
    // and this file would prove nothing at all.
    expect(ALL_KINDS.length).toBeGreaterThanOrEqual(27);
    expect(ALL_KINDS).toContain('s3');
    expect(ALL_KINDS).toContain('sqs');
    expect(ALL_KINDS).toContain('alb');
    expect(orderedPairs().length).toBe(ALL_KINDS.length * ALL_KINDS.length);
  });

  // The pairs odin means to be ambiguous, and what each one means. DERIVED from
  // the registry's own two sets rather than typed out, so a producer or consumer
  // added to `iam.ts` is expected here the day it appears -- but the MEANINGS
  // are spelled out literally, so a pair quietly acquiring a third one still
  // fails. Every entry is a pair where AWS itself says both readings at once:
  // the workload is wired to the endpoint AND may call the service's API.
  const intended = (): string[] =>
    [...Object.keys(connectionRefs)].flatMap((producer) =>
      [...connectionConsumerTypes].flatMap((consumer) => [
        `${producer} -> ${consumer}: connection | iam`,
        `${consumer} -> ${producer}: connection | iam`,
      ]),
    ).sort();

  it('is ambiguous for exactly the pairs that mean two things on purpose', () => {
    const ambiguous = orderedPairs()
      .filter(([a, b]) => detectEdgeTypes(a, b).length > 1)
      .map(([a, b]) => `${a} -> ${b}: ${detectEdgeTypes(a, b).join(' | ')}`)
      .sort();

    // Both directions of the comparison matter. An UNEXPECTED pair here means
    // `edgeDataForConnection` is picking `[0]` without telling the user, and the
    // picker must learn to ask about it. A MISSING one means a pair that used to
    // offer a choice silently stopped -- `ConfigPanel.tsx` renders the picker on
    // `choices.length > 1`, so it would simply vanish.
    expect(ambiguous).toEqual(intended());
  });

  it('the intended set is not empty, and is the size it should be', () => {
    // Guards the guard, the same way the coverage test above does: were
    // `connectionRefs` or `connectionConsumerTypes` to resolve to nothing, the
    // assertion above would compare [] against [] and prove nothing at all.
    // 2 producers x 2 consumers x 2 directions.
    expect(intended().length).toBe(8);
    expect(intended()).toContain('rds -> ecs: connection | iam');
  });

  it('never returns an edge type that has no definition', () => {
    // A pair mapped to a type with no entry in `edgeTypes` would draw with the
    // `network` fallback style in `edgeStyle` -- rendering as a plain grey line
    // while meaning something else entirely, which is the silent-wrong-answer
    // shape rather than a visible break.
    const undefinedTypes = orderedPairs()
      .flatMap(([a, b]) => detectEdgeTypes(a, b).map((type) => ({ a, b, type })))
      .filter(({ type }) => edgeTypes[type] === undefined)
      .map(({ a, b, type }) => `${a} -> ${b}: ${type}`);

    expect(undefinedTypes).toEqual([]);
  });

  it('is symmetric: the same two kinds mean the same thing either way round', () => {
    // `pairKey` sorts, so this holds by construction today. It is asserted
    // anyway because the property is load-bearing for users rather than
    // incidental: `edgeDataForConnection`'s own docstring pins that `ec2 -> s3`
    // and `s3 -> ec2` produce the same permissions, "because the user drew the
    // same intent either way". If direction ever starts to matter, it must be a
    // decision someone made, not a refactor nobody noticed.
    const asymmetric = orderedPairs()
      .filter(([a, b]) => detectEdgeTypes(a, b).join(',') !== detectEdgeTypes(b, a).join(','))
      .map(([a, b]) => `${a}/${b}: ${detectEdgeTypes(a, b)} vs ${detectEdgeTypes(b, a)}`);

    expect(asymmetric).toEqual([]);
  });
});
