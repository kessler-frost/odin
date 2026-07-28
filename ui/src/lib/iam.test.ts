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
import {
  computeTypes,
  defaultPermissions,
  detectEdgeTypes,
  edgeDataForConnection,
  edgeTypes,
  sgMemberTypes,
} from './iam';

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

describe('security-group membership edges', () => {
  // Which instances a group gates is a RELATIONSHIP; an SG's own `vpc_id` is
  // ownership and comes from containment. Before this, membership could only be
  // typed into an ec2/rds `securityGroups` field, so the canvas could not show it.
  it('an sg drawn against ec2 or rds means membership, not network', () => {
    for (const member of sgMemberTypes) {
      expect(detectEdgeTypes('sg', member)).toEqual(['sg']);
      expect(edgeDataForConnection('sg', member).edgeType).toBe('sg');
    }
  });

  it('reads the same either way round', () => {
    expect(edgeDataForConnection('sg', 'ec2')).toEqual(edgeDataForConnection('ec2', 'sg'));
  });

  it('carries no permissions — membership is not a grant', () => {
    // Permissions on a membership edge would imply odin enforces something it
    // does not; the SG's own rules are what gate traffic.
    expect(edgeDataForConnection('sg', 'ec2').permissions).toEqual([]);
  });

  it('is limited to the kinds whose HCL actually reads securityGroups', () => {
    // s3 has no such field, so an sg edge to it must not claim to configure one.
    expect(detectEdgeTypes('sg', 's3')).not.toEqual(['sg']);
  });

  it('has a definition, so it renders as itself rather than a fallback line', () => {
    expect(edgeTypes.sg).toBeDefined();
    expect(edgeTypes.sg.label).toBe('Security Group');
  });
});
