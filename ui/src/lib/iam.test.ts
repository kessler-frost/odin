/**
 * What a drawn edge MEANS -- the half of drag-to-connect that is testable.
 *
 * The gesture cannot be automated here: odin's connection handles are 6px and
 * `pointerdown` arrives with a non-handle target even when the pointer is at the
 * handle's measured centre (recorded in .claude/CLAUDE.md as open). So this file
 * covers what the drag PRODUCES, which is pure logic and was previously untested
 * in its entirety -- `lib/iam.ts` had no test file at all.
 */
import { describe, expect, it } from 'bun:test';
import { BUILTINS, CATALOG } from './catalog';
import { computeTypes, defaultPermissions, detectEdgeTypes, edgeDataForConnection, edgeTypes } from './iam';

describe('edgeDataForConnection', () => {
  it('a compute -> data-resource edge is an IAM edge with that resource\'s permissions', () => {
    const { edgeType, permissions } = edgeDataForConnection('ec2', 's3');
    expect(edgeType).toBe('iam');
    expect(permissions).toEqual(defaultPermissions.s3);
    expect(permissions.length).toBeGreaterThan(0);
  });

  it('permissions come from the NON-COMPUTE end, whichever way it was drawn', () => {
    // The same intent either way round: dragging ec2->s3 or s3->ec2 both mean
    // "this instance may use this bucket", so both must grant S3 permissions.
    expect(edgeDataForConnection('ec2', 's3')).toEqual(edgeDataForConnection('s3', 'ec2'));
  });

  it('holds for every compute kind, not just ec2', () => {
    for (const compute of computeTypes) {
      const { edgeType, permissions } = edgeDataForConnection(compute, 's3');
      expect(edgeType).toBe('iam');
      expect(permissions).toEqual(defaultPermissions.s3);
    }
  });

  it('a non-IAM pair carries no permissions at all', () => {
    // Permissions on a network edge would be a claim odin does not enforce.
    const { edgeType, permissions } = edgeDataForConnection('vpc', 'subnet');
    expect(edgeType).not.toBe('iam');
    expect(permissions).toEqual([]);
  });

  it('returns a fresh array, never the shared default', () => {
    // The config panel edits these in place; handing out the module-level array
    // would rewrite the defaults for every future edge.
    const first = edgeDataForConnection('ec2', 's3').permissions;
    first.push('s3:DeleteObject');
    expect(edgeDataForConnection('ec2', 's3').permissions).not.toContain('s3:DeleteObject');
  });

  it('is total — an unknown kind still yields a usable edge', () => {
    const { edgeType, permissions } = edgeDataForConnection('nonsense', 'alsononsense');
    expect(typeof edgeType).toBe('string');
    expect(edgeType.length).toBeGreaterThan(0);
    expect(Array.isArray(permissions)).toBe(true);
  });
});

describe('edge-type ambiguity: the selector this repo does NOT have yet', () => {
  // The owner's design call (2026-07-28): what an edge MEANS depends on the
  // components it connects, and where a pair could legitimately mean more than
  // one thing, odin should ASK rather than pick.
  //
  // `detectEdgeTypes` already returns an ARRAY, so the model anticipates that.
  // What it does today is take `[0]` and say nothing. Measured across the whole
  // catalog: 729 ordered pairs, ZERO ambiguous, because only two edge types
  // exist (`iam`, `network`) and no pair maps to both. So the selector would
  // never open, and building it now would be UI with nothing to select.
  //
  // This test is the trigger instead. The moment a pair becomes genuinely
  // ambiguous -- a third edge type, or a pair that legitimately means either --
  // it FAILS, and it fails pointing at the work that has become necessary
  // rather than letting `[0]` quietly decide on the user's behalf.
  it('no pair is ambiguous yet — when one is, build the selector', () => {
    const types = [...new Set([...CATALOG.map(s => s.type), ...BUILTINS.map(b => b.type)])];
    const ambiguous: string[] = [];
    for (const a of types) {
      for (const b of types) {
        const candidates = detectEdgeTypes(a, b);
        if (candidates.length > 1) ambiguous.push(`${a} -> ${b}: ${candidates.join(' | ')}`);
      }
    }
    expect(ambiguous).toEqual([]);
  });

  it('every pair still resolves to a type that actually exists', () => {
    // A pair mapping to a type with no definition would render with fallback
    // styling and no label, which reads as "odin drew a plain line" rather than
    // as the misconfiguration it is.
    const types = [...new Set([...CATALOG.map(s => s.type), ...BUILTINS.map(b => b.type)])];
    for (const a of types) {
      for (const b of types) {
        for (const candidate of detectEdgeTypes(a, b)) {
          expect(Object.keys(edgeTypes)).toContain(candidate);
        }
      }
    }
  });
});
