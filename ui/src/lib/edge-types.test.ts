/**
 * The four things a drawn edge is now allowed to MEAN, and the one thing it is
 * allowed to mean nothing by.
 *
 * Each block below pins a measured defect rather than a design. Before this
 * file: `iam_role -> lambda` was inert, `ecs -> lambda` granted nothing while
 * emitting an empty role, and 341 of 378 unordered pairs answered "Network" --
 * a positive claim about layer 3 that odin never checks.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'bun:test';

import { catalogTypes } from './catalog';
import {
  UNMODELLED,
  computeTypes,
  defaultPermissions,
  detectEdgeTypes,
  edgeDataForConnection,
  edgeStyle,
  edgeTypes,
  iamActionsForTarget,
  iamTargetTypes,
  roleHolderTypes,
  subscriptionTargetTypes,
} from './iam';

describe('role edges (iam_role -> workload)', () => {
  it('an iam_role drawn against a lambda is a ROLE edge, not the catch-all', () => {
    // The whole defect in one assertion: this used to be ['network'], which no
    // pass in odin reads, so the line was stored for ever and consumed by
    // nothing while the lambda's `role` field decided the real answer.
    expect(detectEdgeTypes('iam_role', 'lambda')).toEqual(['role']);
  });

  it('reads the same either way round', () => {
    expect(detectEdgeTypes('lambda', 'iam_role')).toEqual(['role']);
    expect(edgeDataForConnection('iam_role', 'lambda'))
      .toEqual(edgeDataForConnection('lambda', 'iam_role'));
  });

  it('carries no permissions — assuming a role is not a grant', () => {
    expect(edgeDataForConnection('iam_role', 'lambda').permissions).toEqual([]);
  });

  it('has a definition, so it renders as itself rather than a grey fallback', () => {
    expect(edgeTypes.role).toBeDefined();
    expect(edgeTypes.role.label).toBe('IAM Role');
    expect(edgeStyle('role').stroke).toBe(edgeTypes.role.color);
    expect(edgeStyle('role').stroke).not.toBe(edgeTypes[UNMODELLED].color);
  });

  it('is limited to the kinds whose HCL actually reads a `role` field', () => {
    // ec2 and ecs reach a role through an auto-role plus an instance profile /
    // task_role_arn and read no `role` field at all, so offering `role` there
    // would author a field nothing consumes -- the exact bug this type fixes.
    // They stay on the catch-all, whose LABEL is the report: odin does nothing
    // with the line. Recorded in docs/limits.md.
    expect([...roleHolderTypes]).toEqual(['lambda']);
    expect(detectEdgeTypes('iam_role', 'ec2')).toEqual([UNMODELLED]);
    expect(detectEdgeTypes('iam_role', 'ecs')).toEqual([UNMODELLED]);
  });
});

describe('compute -> compute IAM edges', () => {
  // `edgeDataForConnection` used to pick "whichever end is not compute" as the
  // resource. When BOTH ends are compute that has no answer, so it returned ''
  // and no permissions -- a cyan IAM edge with nothing ticked, and downstream an
  // auto-role reserved and an `aws_iam_role` emitted with no policy attached.
  // Both pairs below are explicitly supported by the catalog.
  it('ecs -> lambda grants the lambda invoke permission', () => {
    expect(edgeDataForConnection('ecs', 'lambda').permissions).toEqual(defaultPermissions.lambda);
    expect(edgeDataForConnection('ecs', 'lambda').permissions.length).toBeGreaterThan(0);
  });

  it('lambda -> ecs grants the ECS permissions — the ARROW decides, both ways', () => {
    // Both ends are IAM targets here, so direction is the only thing that says
    // who calls whom, and the two orderings must NOT agree.
    expect(edgeDataForConnection('lambda', 'ecs').permissions).toEqual(defaultPermissions.ecs);
    expect(edgeDataForConnection('ecs', 'lambda').permissions)
      .not.toEqual(edgeDataForConnection('lambda', 'ecs').permissions);
  });

  it('ec2 <-> ecs grants ECS permissions in BOTH orderings', () => {
    // Only one end (ecs) is something you can be granted actions on, so a plain
    // "the target end wins" tie-break would have left `ecs -> ec2` empty. The
    // rule is "which end is an IAM target", which answers this correctly.
    expect(edgeDataForConnection('ec2', 'ecs').permissions).toEqual(defaultPermissions.ecs);
    expect(edgeDataForConnection('ecs', 'ec2').permissions).toEqual(defaultPermissions.ecs);
  });

  it('never yields an IAM edge with an empty grant, for ANY compute pair', () => {
    // The generalisation of the bug: an `iam` edge with no permissions reserves
    // a role and emits a policy-less `aws_iam_role`, so it must be unreachable.
    const empty: string[] = [];
    for (const a of computeTypes) {
      for (const b of computeTypes) {
        const { edgeType, permissions } = edgeDataForConnection(a, b);
        if (edgeType === 'iam' && permissions.length === 0) empty.push(`${a} -> ${b}`);
      }
    }
    expect(empty).toEqual([]);
  });

  it('mixed pairs are unchanged — the resource end still wins either way round', () => {
    expect(edgeDataForConnection('ec2', 's3')).toEqual(edgeDataForConnection('s3', 'ec2'));
    expect(edgeDataForConnection('ec2', 's3').permissions).toEqual(defaultPermissions.s3);
  });

  it('every IAM target has default permissions to hand out', () => {
    // Guards the guard: the tie-break picks a resource end out of
    // `iamTargetTypes` and then reads `defaultPermissions`. A target missing
    // from the second map would silently produce the empty grant again.
    const undefaulted = [...iamTargetTypes].filter((t) => !defaultPermissions[t]?.length);
    expect(undefaulted).toEqual([]);
    expect(iamTargetTypes.size).toBe(Object.keys(iamActionsForTarget).length);
  });
});

describe('the two meanings that were hiding inside `network`', () => {
  it('alb <-> ecs is a load-balancer TARGET', () => {
    // It compiles to a real `load_balancer` block on the ECS service
    // (`agent/hcl.py` pass 1.5) -- which is why that pass has to explain in
    // prose that a "NETWORK edge" means a target. The registry says it now.
    expect(detectEdgeTypes('alb', 'ecs')).toEqual(['target']);
    expect(detectEdgeTypes('ecs', 'alb')).toEqual(['target']);
    expect(edgeTypes.target.label).toBe('LB Target');
  });

  it('sns <-> sqs is a SUBSCRIPTION', () => {
    // It emits a real `aws_sns_topic_subscription`.
    expect(detectEdgeTypes('sns', 'sqs')).toEqual(['subscription']);
    expect(detectEdgeTypes('sqs', 'sns')).toEqual(['subscription']);
    expect(edgeTypes.subscription.label).toBe('Subscription');
    expect([...subscriptionTargetTypes]).toEqual(['sqs']);
  });

  it('both are PRESENTATIONAL — no consumer may gate on the name', () => {
    // This is a safety property, not a style note. Every canvas saved before
    // these types existed stores `network` on those edges and works anyway,
    // because both `agent/hcl.py` passes key on the two NODE kinds. A builder
    // that started requiring the new name without a migration in the same
    // commit would drop the subscription from the generated HCL for all of
    // them and `tofu` would DESTROY the live subscription on the next apply --
    // and the reconciler would stay quiet, because `_desired_subs` only ever
    // ADDS a missing subscription and never unsubscribes.
    //
    // The Python side of this claim is pinned by
    // `tests/spec/test_edge_types.py::test_a_legacy_network_typed_subscription_still_builds`,
    // which runs a `network`-typed canvas through the real generator.
    expect(edgeTypes.subscription).toBeDefined();
    expect(edgeTypes.target).toBeDefined();
  });
});

describe('the renamed catch-all', () => {
  it('an unmodelled pair says so instead of claiming to be a network', () => {
    expect(detectEdgeTypes('s3', 'kms')).toEqual([UNMODELLED]);
    expect(detectEdgeTypes('vpc', 'subnet')).toEqual([UNMODELLED]);
    expect(edgeTypes[UNMODELLED].label).toBe('Not modelled');
  });

  it('never answers `network` any more', () => {
    // The word is retired as an ANSWER while staying valid as stored data.
    expect(detectEdgeTypes('vpc', 'subnet')).not.toContain('network');
    expect(detectEdgeTypes('route53', 'efs')).not.toContain('network');
  });

  it('keeps `network` DEFINED, because every saved canvas stores it', () => {
    // `Canvas.tsx` styles a loaded edge by its STORED kind
    // (`edgeTypes[eType] ?? edgeTypes.network`, four call sites). Deleting the
    // entry would drop every pre-rename edge onto a fallback.
    expect(edgeTypes.network).toBeDefined();
    expect(edgeStyle('network').stroke).toBe(edgeTypes[UNMODELLED].color);
    expect(edgeTypes.network.label).toBe(edgeTypes[UNMODELLED].label);
  });

  it('styles an unknown kind as unmodelled rather than crashing', () => {
    expect(edgeStyle('a-kind-nobody-registered').stroke).toBe(edgeTypes[UNMODELLED].color);
  });
});

describe('the counts in docs/limits.md are measured, not written', () => {
  // They went stale within ONE DAY of being written: `alb <-> ec2` became a
  // target, which moved a pair out of `unmodelled`, and the paragraph went on
  // saying 40/338. Nobody was careless -- the number simply lives in a file that
  // no build reads, which is the definition of prose that cannot fail. So the
  // paragraph is recomputed here from the real registry instead.
  //
  // Same reasoning `.claude/CLAUDE.md` gives for pinning the thread inventory in
  // `tests/test_thread_inventory.py`: "prose about inventories has gone stale
  // here twice and prose cannot fail a build".
  const BESPOKE_KINDS = ['vpc', 'subnet', 'sg', 'ec2', 'lambda', 's3', 'dynamodb'];
  const ALL_KINDS = [...new Set([...BESPOKE_KINDS, ...catalogTypes])].sort();

  const counts = () => {
    const unordered = new Set<string>();
    for (const a of ALL_KINDS) for (const b of ALL_KINDS) unordered.add([a, b].sort().join('~'));
    const byType: Record<string, number> = {};
    for (const key of unordered) {
      const [a, b] = key.split('~');
      const type = detectEdgeTypes(a, b)[0];
      byType[type] = (byType[type] ?? 0) + 1;
    }
    return { total: unordered.size, byType };
  };

  const LIMITS = readFileSync(new URL('../../../docs/limits.md', import.meta.url), 'utf8');

  it('reads the real limits.md', () => {
    // Guards the guard: a bad path would make every assertion below vacuous.
    expect(LIMITS).toContain('A drawn edge carries a modelled TYPE');
  });

  it('states the right number of typed and unmodelled pairs', () => {
    const { total, byType } = counts();
    const unmodelled = byType[UNMODELLED] ?? 0;
    expect(LIMITS).toContain(`modelled TYPE for only ${total - unmodelled} of the ${total} kind pairs`);
    expect(LIMITS).toContain(`\`unmodelled\` — ${unmodelled} of the ${total} unordered`);
  });

  it('states the right per-type counts for the two it enumerates', () => {
    const { byType } = counts();
    expect(LIMITS).toContain(`\`iam\`\n  (${byType.iam} pairs, a real policy)`);
    expect(LIMITS).toContain(`\`sg\` (${byType.sg}, security-group membership)`);
    expect(LIMITS).toContain(`\`target\` (${byType.target} —`);
  });
});
