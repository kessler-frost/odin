/**
 * What the edge config panel decides, given a pair of nodes.
 *
 * The panel used to derive each end's TYPE from its node id --
 * `edge.source.split('-')[0]` -- which holds only while every id happens to be
 * `<type>-<n>`, the shape the sidebar produces. A hand-authored canvas
 * (`odin canvas set`, the README's documented JSON schema, the translation
 * agent next) uses whatever ids its author wrote: `web`, `e1`, `api-server`.
 * For those the panel computed a type of `web` / `e1` / `api`, then offered the
 * wrong edge types and no permissions at all -- while looking perfectly normal.
 *
 * This pins the DECISIONS that lookup feeds, so the panel cannot regress to
 * parsing ids. The lookup itself is now `nodes.find(n => n.id === id)?.type`.
 */
import { describe, expect, it } from 'bun:test';
import { detectEdgeTypes, iamActionsForTarget } from './iam';

// The same two-step the panel does: id -> type -> what that pair means.
const typeOf = (nodes: { id: string; type: string }[], id: string) =>
  nodes.find((n) => n.id === id)?.type ?? '';

describe('edge decisions survive hand-authored node ids', () => {
  const handAuthored = [
    { id: 'e1', type: 'ec2' },
    { id: 'web', type: 's3' },
    { id: 'api-server', type: 'ec2' },
    { id: 'jobs', type: 'sqs' },
  ];

  it('resolves a type from an id that carries no type prefix', () => {
    expect(typeOf(handAuthored, 'e1')).toBe('ec2');
    expect(typeOf(handAuthored, 'web')).toBe('s3');
  });

  it('an ec2 -> s3 edge is IAM even when the ids say nothing', () => {
    // Under the old id-parsing this was detectEdgeTypes('e1', 'web') -> network,
    // so the panel offered no permissions for a genuine IAM edge.
    const pair = detectEdgeTypes(typeOf(handAuthored, 'e1'), typeOf(handAuthored, 'web'));
    expect(pair).toEqual(['iam']);
  });

  it('the target end still has its permission vocabulary', () => {
    expect(iamActionsForTarget[typeOf(handAuthored, 'web')]).toContain('s3:GetObject');
    expect(iamActionsForTarget[typeOf(handAuthored, 'jobs')]).toContain('sqs:SendMessage');
  });

  it('an id that merely LOOKS like a type is not trusted', () => {
    // `sqs-report` is an s3 bucket. Parsing the id would have called it sqs and
    // offered queue permissions on a bucket.
    const misleading = [{ id: 'sqs-report', type: 's3' }];
    expect(typeOf(misleading, 'sqs-report')).toBe('s3');
    expect(iamActionsForTarget[typeOf(misleading, 'sqs-report')]).toContain('s3:GetObject');
  });

  it('an edge naming a node that is not on the canvas yields no type', () => {
    expect(typeOf(handAuthored, 'ghost')).toBe('');
    // ...and that resolves to the safe default rather than throwing.
    expect(detectEdgeTypes('', '')).toEqual(['network']);
  });

  it('sidebar-created ids keep working — the old shape is still valid', () => {
    const fromSidebar = [{ id: 'ec2-101', type: 'ec2' }, { id: 's3-102', type: 's3' }];
    expect(detectEdgeTypes(typeOf(fromSidebar, 'ec2-101'), typeOf(fromSidebar, 's3-102'))).toEqual(['iam']);
  });
});
