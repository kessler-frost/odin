/**
 * The things a drawn edge is allowed to MEAN, and the one thing it is allowed to
 * mean nothing by. Four when this file was written; `connection` was the fifth
 * (v0.8.15) and lives in `connection-edge.test.ts`, along with the multi-select
 * picker its arrival made real.
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
  dnsTargetTypes,
  encryptionTargetTypes,
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

describe('the encryption edge (kms -> the sidecars odin holds keys for)', () => {
  it('kms <-> secret and kms <-> ssm are ENCRYPTION, not the catch-all', () => {
    // It authors the target's own key field (`_merge_encryption_edges` ->
    // `hcl.py::_secret`/`_ssm` -> a real `kms_key_id`/`key_id`), so unlike
    // `target`/`subscription` this one is not presentational.
    expect(detectEdgeTypes('kms', 'secret')).toEqual(['encryption']);
    expect(detectEdgeTypes('secret', 'kms')).toEqual(['encryption']);
    expect(detectEdgeTypes('kms', 'ssm')).toEqual(['encryption']);
    expect(edgeTypes.encryption.label).toBe('Encrypted With');
  });

  it('renders as itself rather than a grey fallback', () => {
    expect(edgeStyle('encryption').stroke).toBe(edgeTypes.encryption.color);
    expect(edgeStyle('encryption').stroke).not.toBe(edgeTypes[UNMODELLED].color);
  });

  it('carries no permissions — being sealed under a key is not a grant', () => {
    expect(edgeDataForConnection('kms', 'secret').permissions).toEqual([]);
  });

  it('is limited to the kinds odin really holds the plaintext of', () => {
    // NOT a deferral, unlike `roleHolderTypes`' ec2/ecs: an s3 object lives in
    // RustFS, an rds volume in a Postgres container, a dynamodb item in
    // dynalite, and odin holds no key for any of them. A teal line there would
    // claim an encryption that does not happen.
    expect([...encryptionTargetTypes].sort()).toEqual(['secret', 'ssm']);
    for (const kind of ['s3', 'rds', 'dynamodb', 'ec2']) {
      expect(detectEdgeTypes('kms', kind)).not.toContain('encryption');
    }
  });

  it('a workload -> kms edge is an IAM grant, and a separate question', () => {
    // The two meanings never collide: `kms -> secret` is encryption, `lambda ->
    // kms` is permission to call Encrypt/Decrypt. Neither pair carries both, so
    // `edge-ambiguity.test.ts` stays green.
    expect(detectEdgeTypes('lambda', 'kms')).toEqual(['iam']);
    expect(edgeDataForConnection('lambda', 'kms').permissions)
      .toEqual(defaultPermissions.kms);
  });

  it('offers only kms actions the gateway has a handler for', () => {
    // The ecr lesson: classifiable is not answerable. Every action here is in
    // `kmsctl.py`'s dispatch table; the alias/grant verbs it answers
    // `InvalidAction` 400 for are deliberately absent.
    expect(iamActionsForTarget.kms).toEqual([
      'kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey', 'kms:DescribeKey', 'kms:*',
    ]);
    for (const action of defaultPermissions.kms) {
      expect(iamActionsForTarget.kms).toContain(action);
    }
  });
});

describe('the dns edge (route53 -> the one kind a hosts entry can name)', () => {
  it('route53 <-> ec2 is a DNS RECORD, not the catch-all', () => {
    // It emits a real `aws_route53_record`, and the substrate is real name
    // resolution: an `--add-host` on every container in the env and an
    // `/etc/hosts` line on every Lima VM in it.
    expect(detectEdgeTypes('route53', 'ec2')).toEqual(['dns']);
    expect(detectEdgeTypes('ec2', 'route53')).toEqual(['dns']);
    expect(edgeTypes.dns.label).toBe('DNS Record');
  });

  it('renders as itself rather than a grey fallback', () => {
    expect(edgeStyle('dns').stroke).toBe(edgeTypes.dns.color);
    expect(edgeStyle('dns').stroke).not.toBe(edgeTypes[UNMODELLED].color);
  });

  it('carries no permissions — resolving a name is not a grant', () => {
    // route53 declares no `iamActions` on purpose: a workload does not resolve a
    // name by making a signed AWS call, it reads a hosts file, which consults
    // nobody. Giving it actions would also make this pair AMBIGUOUS -- `pairKey`
    // sorts, so the `computeTypes x iamTargets` loop would land `iam` on the very
    // key `dns` uses, and `edge-ambiguity.test.ts` would fail naming it.
    expect(edgeDataForConnection('route53', 'ec2').permissions).toEqual([]);
    expect(iamActionsForTarget.route53).toBeUndefined();
    expect(iamTargetTypes.has('route53')).toBe(false);
  });

  it('is limited to the one kind that publishes an address a hosts entry can hold', () => {
    // NOT a deferral. A hosts entry is `<ip> <name>` -- no port, no scheme --
    // and measured against the real projectors in `reconcile/tf_status.py` only
    // ec2 publishes that shape (`PRIVATE_IP`). An alb publishes
    // `http://127.0.0.1:<dynamic port>` and rds a `host:port`, so a name pointing
    // at either would resolve and then fail to connect. `hcl.py` declines those
    // by name; the canvas must not offer them in the first place.
    expect([...dnsTargetTypes]).toEqual(['ec2']);
    for (const kind of ['alb', 'rds', 'ecs', 'lambda', 's3']) {
      expect(detectEdgeTypes('route53', kind)).not.toContain('dns');
    }
  });

  it('is PRESENTATIONAL — the record pass may not gate on the name', () => {
    // Sharper here than for `target`/`subscription`/`volume`: `route53` has been
    // a DRAWABLE catalog tile since long before it had a builder, so canvases
    // whose route53 edge is typed `network` already exist. A pass requiring
    // `kind === 'dns'` would emit no record for a single one of them.
    // `agent/hcl.py`'s record pass keys on the two NODE kinds instead.
    expect(edgeTypes.dns).toBeDefined();
    expect(edgeTypes.network).toBeDefined();
  });
});

describe('the renamed catch-all', () => {
  it('an unmodelled pair says so instead of claiming to be a network', () => {
    // s3 is NOT an encryption target -- nothing odin runs encrypts a RustFS
    // object -- so this pair stayed unmodelled when kms became real.
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

  // TWO guards over the same paragraph, from two worktrees that fixed the same
  // duplicated bullet independently (this branch, and `develop`'s 38d1a99).
  // Both are kept because they fail on DIFFERENT halves of the defect: the
  // exactly-once check catches a merge that leaves two copies, and the
  // every-type check catches a single copy whose enumeration is stale. Neither
  // subsumes the other, and the bug that prompted them was both at once.
  it('states it EXACTLY ONCE, so a merge cannot leave two contradictory counts', () => {
    // MEASURED FAILURE, v0.8.18: the kms/ebs three-way merge kept both sides of
    // this bullet, so docs/limits.md carried `47 of the 378`/`331` AND
    // `42 of the 378`/`336` four lines apart. The assertions above PASSED --
    // `toContain` is satisfied by the true copy and never sees the false one.
    // That is the same defect as the IAM record guard replaced this release: a
    // substring check answers "does this appear" when the question is "is this
    // what the file says".
    const claims = LIMITS.match(/A drawn edge carries a modelled TYPE for only \d+ of the \d+ kind pairs/g) ?? [];
    expect(claims.length).toBe(1);
    const majority = LIMITS.match(/The honest majority answer is `unmodelled` — \d+ of the \d+ unordered/g) ?? [];
    expect(majority.length).toBe(1);
  });

  it('states the right per-type count for EVERY type it enumerates', () => {
    // It used to check three of the nine, and the six unchecked ones were where
    // the paragraph actually rotted: `volume` and `encryption` landed in one
    // release from two worktrees, each edit rewrote the enumeration, and the
    // result was a DUPLICATED bullet claiming both 47 and 42 typed pairs.
    // Pinning all of them is what makes a half-merged enumeration fail instead
    // of merely looking odd.
    const { byType } = counts();
    expect(LIMITS).toContain(`\`iam\`\n  (${byType.iam} pairs, a real policy)`);
    expect(LIMITS).toContain(`\`connection\` **and** \`iam\` together (${byType.connection})`);
    expect(LIMITS).toContain(`\`sg\` (${byType.sg}, security-group membership)`);
    expect(LIMITS).toContain(`\`target\` (${byType.target} —`);
    expect(LIMITS).toContain(`\`role\` (${byType.role} —`);
    expect(LIMITS).toContain(`\`subscription\` (${byType.subscription} —`);
    expect(LIMITS).toContain(`\`volume\` (${byType.volume} —`);
    expect(LIMITS).toContain(`\`encryption\` (${byType.encryption} —`);
    expect(LIMITS).toContain(`\`dns\` (${byType.dns} —`);
  });

  it('enumerates every type the registry can actually answer', () => {
    // The half `toContain` cannot see: a NEW edge type would add a line to the
    // prose that nothing above asks for, so the counts would all still match
    // while the paragraph quietly under-reported what a canvas can mean. Every
    // type that is the primary answer for at least one pair must be named.
    const { byType } = counts();
    const unnamed = Object.keys(byType)
      .filter((type) => type !== UNMODELLED)
      .filter((type) => !LIMITS.includes(`\`${type}\``));
    expect(unnamed).toEqual([]);
  });
});
