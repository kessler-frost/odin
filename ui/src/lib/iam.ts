import { catalogIamActions } from './catalog';

export const iamActionsForTarget: Record<string, string[]> = {
  s3: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket', 's3:GetBucketLocation', 's3:*'],
  dynamodb: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:DeleteItem', 'dynamodb:*'],
  // lambda is a BUILTIN, so its actions live here rather than in the catalog.
  // NOTE the op name: odin's gateway classifies an invoke as `lambda:Invoke`
  // (`classify.py::_LAMBDA_ROUTES`), NOT AWS's `lambda:InvokeFunction`. Granting
  // the AWS spelling is DECORATIVE -- measured against the real evaluator:
  //   evaluate([lambda:InvokeFunction], action=lambda:Invoke) -> False
  //   evaluate([lambda:Invoke],         action=lambda:Invoke) -> True
  // `tests/gateway/test_iam_vocabulary_is_enforceable.py` pins every entry in
  // this file against what the classifier can actually emit, so a permission
  // that cannot bite can no longer be offered in the UI.
  lambda: ['lambda:Invoke', 'lambda:GetFunction', 'lambda:GetFunctionConfiguration', 'lambda:*'],
  ...catalogIamActions,
};

export const defaultPermissions: Record<string, string[]> = {
  s3: ['s3:GetObject', 's3:PutObject'],
  lambda: ['lambda:Invoke'],
  // `ecr:BatchGetImage` was ticked here by default and gates NOTHING locally.
  // Two independent reasons, both measured: `gateway/models/ecr.py::_HANDLERS`
  // has no `BatchGetImage` entry (the gateway answers `InvalidAction` 400), and
  // the image bytes never reach the gateway in the first place -- ecr.py's own
  // docstring: "The gateway does NOT proxy the registry's v2 HTTP protocol in
  // this slice", a real `docker pull` dials the `registry:2` container's port
  // directly. `GetAuthorizationToken` (the docker-login step) is the one ECR
  // action odin can actually answer and therefore actually gate, so it is the
  // only one odin ticks FOR you. The rest stay TICKABLE in the catalog because
  // the generated Terraform is meant to be portable -- see the note there.
  ecr: ['ecr:GetAuthorizationToken'],
  ecs: ['ecs:RunTask', 'ecs:DescribeTasks'],
  dynamodb: ['dynamodb:GetItem', 'dynamodb:PutItem'],
  sqs: ['sqs:SendMessage', 'sqs:ReceiveMessage'],
  sns: ['sns:Publish'],
  // `rds-db:connect` was ticked here by default and is the SAME defect as ecr's,
  // one step worse: `classify.py` builds every rds action as `rds:<Action>` out
  // of the query protocol's `Action` param, so `rds-db:` is a prefix the
  // classifier cannot even EMIT, let alone answer. odin implements no IAM
  // database authentication -- the Postgres container takes its password out of
  // `DATABASE_URL` and consults nobody.
  //
  // Same split as ecr's, for the same reason: it stays TICKABLE in the catalog
  // because the generated Terraform is meant to be portable, and stops being
  // pre-ticked because a default is what odin ticks FOR you. What is left is the
  // one rds action that really is classified and really is enforced -- and what
  // a user drawing rds -> workload usually wants is the `connection` edge below,
  // which authors that `DATABASE_URL`.
  rds: ['rds:DescribeDBInstances'],
  // W2.1: what a workload actually needs to WRITE to a log group it's edged
  // to. The read verbs (GetLogEvents/FilterLogEvents/DescribeLogStreams) are
  // in the catalog's `iamActions` list to tick, but aren't defaults — a
  // workload writing its own logs is the common case, reading them back isn't.
  logs: ['logs:CreateLogStream', 'logs:PutLogEvents'],
  // W2.4: reading the one secret / parameter it's edged to is the whole point
  // of a workload having the edge. The write and describe verbs
  // (PutSecretValue/DescribeSecret, PutParameter, GetParametersByPath) are in
  // the catalog's `iamActions` list to tick, but a workload that rewrites its
  // own credentials is the rare case, not the default.
  secret: ['secretsmanager:GetSecretValue'],
  ssm: ['ssm:GetParameter'],
  // CONTROL plane only. ElastiCache's data plane is the raw Redis protocol —
  // not AWS-signed, never routed through odin's gateway — so an edge here
  // grants Describe/Modify, and nothing odin does at the IAM layer can gate a
  // GET/SET (that's a security-group question). See ROADMAP's limits.
  elasticache: ['elasticache:DescribeCacheClusters'],
  // W2.9: the two symmetric data-plane ops, and both really bite --
  // `kmsctl.py` answers Encrypt and Decrypt for real against the env's key
  // material. `GenerateDataKey` and `DescribeKey` are tickable in the catalog
  // and not pre-ticked: envelope encryption is a deliberate choice a user
  // makes, and Describe is metadata a workload rarely needs. NOT offered at
  // all, in either place: the alias and grant verbs, which `kmsctl` answers
  // `InvalidAction` 400 for -- the ecr lesson, that classifiable is not the
  // same as answerable.
  kms: ['kms:Encrypt', 'kms:Decrypt'],
  // W2.5 note: `alb` is deliberately ABSENT here and from `iamActionsForTarget`.
  // A load balancer is not an IAM data-plane target -- you don't "call" an ALB
  // with signed AWS requests, you send it plain HTTP, and no IAM policy gates
  // that. The elasticloadbalancing:* namespace is a CONTROL plane only tofu (the
  // operator principal) ever touches, so listing it here would offer users
  // permissions that change nothing. What an alb<->compute edge DOES mean is
  // below: it's a network/target edge.
};

// W2.5: an alb <-> compute edge is a TARGET edge -- "this load balancer fronts
// that service". `agent/hcl.py`'s pass 1.5 stamps a `load_balancer` block onto
// the aws_ecs_service, which is how real ECS attaches to a target group.
//
// It was carried as the `network` edge type until v0.8.14 and now has its own
// (`target`, registered below) -- the meaning was always this, the name just
// described the wire instead of the relationship. Keeping alb out of
// `iamActionsForTarget` above is what stops the IAM loop from claiming the pair
// first. Pass 1.5 reads the two NODE kinds and not `edge.kind`, so the type is
// presentational and naming it changed nothing about what gets built.
// ec2 joined in v0.8.15, the same change that taught `hcl.py` to emit an
// `aws_lb_target_group_attachment` for an instance. The two lists are pinned
// against each other by tests/spec/test_edge_registry_matches_builders.py --
// which is what caught this line being one merge behind, naming the side.
export const albTargetTypes = new Set(['ecs', 'ec2']);

// Compute kinds act as IAM principals; permission edges run compute → resource.
// The app-workload kinds (service/dep/batch/llm) are parked (see NORTHSTAR.md,
// git tag app-layer-parked) — ec2/lambda/ecs are the AWS compute placeholders
// that return per northstar directive 5, and all three are real, wired-up
// nodes on the canvas today, so edges from any of them to an IAM target do
// have a principal to draw from.
export const computeTypes = new Set(['ec2', 'lambda', 'ecs']);

// Every kind you can be granted actions ON. A Set rather than a truthiness test
// against `iamActionsForTarget`, because an object lookup answers `true` for
// inherited keys -- a node kind called `constructor` or `toString` would be
// treated as an IAM target and then blow up on the permission spread.
export const iamTargetTypes = new Set(Object.keys(iamActionsForTarget));

// --- Edge type registry ---

export type EdgeTypeDef = {
  id: string;
  label: string;
  color: string;
  dashed: boolean;
};

export const edgeTypes: Record<string, EdgeTypeDef> = {
  iam: { id: 'iam', label: 'IAM Policy', color: '#00e5ff', dashed: true },
  // THE FALLBACK, renamed from `network`. Measured before the rename: `network`
  // was the answer for 341 of the 378 unordered kind pairs, and for 340 of them
  // it meant "odin has no model for this pair" -- it enforced nothing, emitted
  // nothing and was read by nothing. "Network" is a positive claim about layer 3
  // that odin never checks: drawing s3 <-> kms produced a line labelled Network
  // that is not a network anything. The two pairs that DID mean something are
  // lifted out below (`target`, `subscription`), so what is left is exactly the
  // absence of a model, and the label now says that instead of guessing.
  // Spelling matches `spec/translate.py::MODELLED_NODE_TYPES`.
  unmodelled: { id: 'unmodelled', label: 'Not modelled', color: '#4a4a60', dashed: false },
  // LEGACY, and it must stay defined: every canvas saved before the rename
  // stores `edgeType: "network"`, and `Canvas.tsx` styles a loaded edge by its
  // STORED kind (`edgeTypes[eType] ?? edgeTypes.network`, four call sites).
  // Removing this entry would make every old edge fall through to a fallback.
  // Same grey, and the same words as `unmodelled`, because that is what those
  // stored edges always were. `detectEdgeTypes` never returns it any more --
  // see `ConfigPanel.tsx` for how a stored `network` is displayed truthfully on
  // a pair that odin DOES model.
  network: { id: 'network', label: 'Not modelled', color: '#4a4a60', dashed: false },
  // Membership: "this security group gates this resource". A relationship
  // between peers, unlike an SG's own `vpc_id`, which containment supplies
  // because a group belongs to exactly one VPC. Solid and red, matching the SG
  // node's own accent, so a glance separates "what may reach what" (IAM, cyan
  // dashed) from "what this is behind" (membership).
  sg: { id: 'sg', label: 'Security Group', color: '#ff3355', dashed: false },
  // "This workload assumes this role". Dashed and amber: dashed groups it with
  // IAM as an identity fact rather than a wire, amber matches the IAM Role
  // node's own accent (`catalog.ts`'s `amber` bundle), the same rule the sg
  // edge follows.
  role: { id: 'role', label: 'IAM Role', color: '#fbbf24', dashed: true },
  // "This load balancer fronts that service" -- previously carried as `network`
  // and registered as such, which is why `agent/hcl.py`'s pass 1.5 comment has
  // to spell out that a NETWORK edge between an alb and a compute node means a
  // target. It compiles to a real `load_balancer` block on the ECS service.
  target: { id: 'target', label: 'LB Target', color: '#38bdf8', dashed: false },
  // "This topic fans out to that queue" -- it emits a real
  // `aws_sns_topic_subscription`. Rose, matching the SNS node's accent.
  subscription: { id: 'subscription', label: 'Subscription', color: '#fb7185', dashed: false },
  // "This workload's environment is wired to that endpoint": the edge AUTHORS
  // the `${{producer.ATTR}}` ref a user would otherwise have to type by hand
  // (`spec/translate.py::_merge_connection_edges` -> `ResourceDesired.refs` ->
  // `gateway/wiring.py::node_env`, which injects it into the real container at
  // launch). Emerald and solid: solid because it is a wire that carries a value,
  // and a colour of its own because it is neither a grant nor a firewall.
  connection: { id: 'connection', label: 'Connection', color: '#34d399', dashed: false },
  // "This key encrypts that sidecar at rest" -- it AUTHORS the target's own
  // key-naming field (`spec/translate.py::_merge_encryption_edges` ->
  // `agent/hcl.py::_secret`/`_ssm` -> a real `kms_key_id`/`key_id` argument), so
  // it is the same author-a-field-a-builder-already-reads shape as `role` and
  // `sg`, not a presentational label.
  //
  // Teal and SOLID, by the two rules the edges above already follow. Teal
  // matches the KMS tile's own accent (`catalog.ts`'s `color: 'teal'`), the
  // rule `sg` states for red and `role` for amber. Solid rather than dashed
  // because dashed is reserved here for an IDENTITY fact -- who may act as whom
  // (`iam`, `role`) -- and this is not one: a sealed value is a property of the
  // data at rest, true whether or not anybody is calling anything, which puts it
  // with `sg`/`subscription`/`connection` on the solid side.
  encryption: { id: 'encryption', label: 'Encrypted With', color: '#2dd4bf', dashed: false },
};

// Given a pair of node types (unordered), return which edge types are valid
// First entry is the auto-detected default
const pairKey = (a: string, b: string) => [a, b].sort().join(':');

const edgeTypesForPair: Record<string, string[]> = {};

// APPENDS rather than assigns, and that is the point. An `=` here would let a
// later registration silently overwrite an earlier one -- a pair that genuinely
// acquired two meanings would keep looking like it had one, and the ambiguity
// ratchet (`edge-ambiguity.test.ts`), which counts how many meanings a pair
// has, could never see it. Appending makes such a pair fail that test by name,
// which is exactly the trigger the ROADMAP's edge-type selector waits on.
// Deduplicated because the IAM loop below legitimately reaches the same pair
// twice (`ecs` and `lambda` are each both a workload and an IAM target).
const register = (a: string, b: string, edgeTypeId: string) => {
  const key = pairKey(a, b);
  const already = edgeTypesForPair[key] ?? [];
  edgeTypesForPair[key] = already.includes(edgeTypeId) ? already : [...already, edgeTypeId];
};

// --- the connection edge ------------------------------------------------------
//
// The most-drawn line in any architecture diagram -- `rds -> ecs`, `cache ->
// lambda` -- and until v0.8.15 the only one odin did nothing with. It produced a
// cyan IAM edge whose default grant was `rds-db:connect`, an action the gateway
// can never emit (see `defaultPermissions.rds`), or for elasticache a
// Describe-only control-plane grant that cannot gate a Redis GET/SET at all.
//
// "Connection" turned out to be THREE mechanisms, and only one of them was
// missing. Reachability is the `sg` edge and is real. Permission is the `iam`
// edge and is real where the data plane is AWS-signed. The ADDRESS -- typing
// `${{db.DATABASE_URL}}` into a consumer's env field by hand -- was real too,
// and no gesture authored it. So this edge's one job is authoring that ref from
// the drag: no new substrate, no new gateway model, no new Terraform resource
// type. `spec/translate.py::_merge_connection_edges` folds it into `refs`, the
// same way `_merge_sg_edges` and `_merge_role_edges` fold theirs into a field a
// builder already reads, which is why neither needed a builder change.
//
// PRODUCERS are the kinds that publish a wiring fact worth naming
// (`spec/models.py::REFERENCEABLE_KINDS`), paired with the var a user would have
// typed. alb/ec2/ecr are referenceable too and deliberately absent: an
// ALB_ENDPOINT or a REPOSITORY_URI has no single obvious variable name, and
// guessing one is how you author a field the app does not read.
export const connectionRefs: Record<string, { var: string; attr: string }> = {
  rds: { var: 'DATABASE_URL', attr: 'DATABASE_URL' },
  elasticache: { var: 'REDIS_URL', attr: 'REDIS_URL' },
};

// CONSUMERS are exactly the kinds whose real container is launched with the
// node's `env` map, and that is a MEASURED list, not the obvious one:
// `gateway/wiring.py::node_env` has two callers, `ecsctl.py` and `lambdactl.py`.
// `ec2compute.py` imports only `workload_env` (the issued gateway credentials)
// and never `node_env`, so a ref authored onto an ec2 node reaches nothing at
// all -- the drawn-line-that-does-nothing bug this edge exists to fix, which is
// why `rds -> ec2` stays IAM-only and says so in docs/limits.md. Same rule
// `roleHolderTypes` and `sgMemberTypes` already hold.
export const connectionConsumerTypes = new Set(['ecs', 'lambda']);

// Registered BEFORE the IAM loop, so `connection` is `detectEdgeTypes`'s first
// entry and therefore what a fresh drag defaults to. These eight ordered pairs
// are odin's first genuinely ambiguous ones: in AWS both readings are
// simultaneously true, and `edge-ambiguity.test.ts` names them.
for (const producer of Object.keys(connectionRefs)) {
  for (const consumer of connectionConsumerTypes) register(producer, consumer, 'connection');
}

// Workload → any IAM target (s3/dynamodb + every catalog entry with
// iamActions) is an IAM permission edge; everything else falls through to
// `unmodelled`.
for (const target of Object.keys(iamActionsForTarget)) {
  for (const workload of computeTypes) register(workload, target, 'iam');
}
// W2.5: alb <-> compute is a target edge (see `albTargetTypes` above). It was
// registered as `network` until this change, which is why `agent/hcl.py`'s pass
// 1.5 has to explain in prose that a "NETWORK edge between an `alb` node and a
// compute node" means a load-balancer target. Naming the type says it in the
// registry instead. The type is PRESENTATIONAL: hcl.py's pass keys on the two
// NODE kinds and never reads `edge.kind`, and it must stay that way -- see the
// `subscription` note below for what gating it would destroy.
for (const target of albTargetTypes) register('alb', target, 'target');
// An sg drawn against a kind whose HCL reads `securityGroups` means MEMBERSHIP,
// and only that -- a plain "network" line between a group and an instance would
// describe nothing. Kept deliberately to the kinds `agent/hcl.py` actually
// consumes it for (`_ec2`, `_rds`), so the edge cannot author a field nothing
// reads. Unambiguous, so the ambiguity ratchet stays green: there is one honest
// meaning here, and odin should not ask about it.
export const sgMemberTypes = new Set(['ec2', 'rds']);
for (const member of sgMemberTypes) register('sg', member, 'sg');

// An sns -> sqs edge is a SUBSCRIPTION: it emits a real
// `aws_sns_topic_subscription` and the reconciler fans the topic out to the
// queue. It rendered as a grey "Network" line, which describes neither.
//
// PRESENTATIONAL ONLY, and this is a safety property rather than a style
// choice. Both consumers -- `agent/hcl.py`'s subscription pass and
// `reconcile/reconciler.py::_desired_subs` -- key on the two NODE kinds and
// never read `edge.kind`, and every canvas saved before this change types the
// edge `network`. If a builder ever started REQUIRING `kind === 'subscription'`
// without a migration landing in the same commit, every one of those canvases
// would silently drop its subscription from the generated HCL and `tofu` would
// DESTROY the live subscription on the next apply -- and the reconciler would
// not notice, because `_desired_subs` only ever ADDS a missing subscription and
// never unsubscribes. So: name the edge, do not gate on the name.
export const subscriptionTargetTypes = new Set(['sqs']);
for (const queue of subscriptionTargetTypes) register('sns', queue, 'subscription');

// "This workload assumes this role." Folded into the `role` FIELD the builder
// already reads (`spec/translate.py::_merge_role_edges` ->
// `agent/hcl.py::_lambda`), so the edge is another way to author a fact odin
// already consumes rather than a second source of truth beside it.
//
// Before this, `iam_role` declared no `iamActions` (correctly -- a role is not
// an IAM data-plane target), so it was never registered at all and every
// iam_role -> workload edge fell through to the catch-all. It was stored in the
// Stack, survived every revision, and was read by NOTHING: draw
// `admin-role -> my-lambda` while the lambda's `role` field says `other-role`
// and you got a dead edge, `other-role` in the generated file, and
// `other-role`'s statements enforced by the gateway.
//
// Limited to the kinds whose HCL actually reads a `role` field, the same rule
// `sgMemberTypes` holds. ec2 and ecs reach a role through an auto-generated
// role plus an instance profile / `task_role_arn` and read no `role` field at
// all, so registering them here would author a field nothing consumes -- the
// very bug this type exists to fix. Those two pairs stay `unmodelled`, whose
// label says so on the canvas at draw time; see docs/limits.md.
export const roleHolderTypes = new Set(['lambda']);
for (const holder of roleHolderTypes) register('iam_role', holder, 'role');

// W2.9: "this key encrypts that sidecar at rest." Folded into the field the
// builder already reads -- a secret's `kmsKeyId` and a parameter's `keyId`
// (`spec/translate.py::_merge_encryption_edges`) -- so `agent/hcl.py` gained a
// `kms_key_id`/`key_id` argument and no knowledge of edges at all.
//
// Limited to the kinds whose HCL actually reads such a field, the same rule
// `roleHolderTypes` and `sgMemberTypes` hold. Here that limit is not a
// deferral, it is the truth about the substrate: `gateway/kms.py` seals exactly
// the two sidecars odin holds the plaintext of. An s3 object lives in RustFS,
// an rds volume in a Postgres container, a dynamodb item in dynalite -- odin
// holds no key for any of them, so `kms -> s3` would be a line that encrypts
// nothing. Those pairs stay `unmodelled`, whose label says on the canvas that
// odin does nothing with the line, rather than a teal one implying it does.
//
// Pinned against the Python half by
// `tests/spec/test_edge_registry_matches_builders.py`, like albTargetTypes /
// sgMemberTypes / roleHolderTypes before it.
export const encryptionTargetTypes = new Set(['secret', 'ssm']);
for (const t of encryptionTargetTypes) register('kms', t, 'encryption');

// The catch-all every unregistered pair falls to. Deliberately a NAMED type
// with a definition, not a bare string, so `edgeStyle` and the ambiguity
// ratchet's "never returns an edge type that has no definition" both resolve it.
export const UNMODELLED = 'unmodelled';

export function detectEdgeTypes(nodeTypeA: string, nodeTypeB: string): string[] {
  return edgeTypesForPair[pairKey(nodeTypeA, nodeTypeB)] ?? [UNMODELLED];
}

export function detectDefaultEdgeType(nodeTypeA: string, nodeTypeB: string): string {
  const types = detectEdgeTypes(nodeTypeA, nodeTypeB);
  return types[0] ?? UNMODELLED;
}

// --- more than one meaning on one line ----------------------------------------
//
// A pair CAN mean two things at once, and in AWS the two usually arrive
// together: an event source mapping does not work unless the role also holds
// `sqs:ReceiveMessage`, and a workload wired to a database may also call its
// control plane. So the picker is multi-select, and `data.edgeType` -- which
// ROADMAP fixes as the store, so a choice survives a node being moved or
// retyped -- has to hold a SET.
//
// It stays a single string, `+`-joined in registry order. Three reasons that
// beat adding a second field:
//   * ONE meaning serialises to exactly the bytes it does today (`"iam"`), so
//     every canvas ever saved, `spec/translate.py::_EDGE_DATA_SHAPE`'s
//     `edgeType: str`, and `agent/chat.py`'s `EDGE_KINDS` check are all
//     untouched. There is no migration, because there is nothing to migrate.
//   * A second field (`edgeTypes: string[]`) beside `edgeType` would be two
//     sources of truth for one fact, which is the bug class this whole edge
//     registry exists to remove.
//   * `spec/translate.py::_edge` splits it into ONE `Edge` per meaning, so every
//     Python consumer keeps matching a single kind (`compile_policies` and
//     `hcl.py::_granted_ids` both gate on `kind == "iam"`) and none of them
//     needed to change. Collapsing two meanings into one string there would
//     have silently dropped the grant.
export const EDGE_TYPE_SEPARATOR = '+';

export function parseEdgeTypes(stored: string | undefined | null): string[] {
  return [...new Set((stored ?? '').split(EDGE_TYPE_SEPARATOR).map(s => s.trim()).filter(Boolean))];
}

/** Canonical order is the REGISTRY's, never the click order: two users who tick
 * the same boxes must produce the same string, or the canvas diffs for nothing. */
export function serializeEdgeTypes(types: string[], available: string[]): string {
  const chosen = new Set(types);
  const ordered = [...available.filter(t => chosen.has(t)), ...types.filter(t => !available.includes(t))];
  return [...new Set(ordered)].join(EDGE_TYPE_SEPARATOR);
}

/** The type a joined value RENDERS as: its first meaning. Styling one line two
 * colours is not available, so the primary wins and the panel lists the rest. */
export function primaryEdgeType(stored: string | undefined | null): string {
  return parseEdgeTypes(stored)[0] ?? UNMODELLED;
}

/** Does a stored value carry this meaning AT ALL -- primary or not?
 *
 * Every `=== 'iam'` on a stored value in `Canvas.tsx` had to become this: an edge
 * stored as `connection+iam` grants exactly as much as one stored `iam`, and
 * comparing the whole string would have hidden the permission label on the line
 * while `gateway/policy.py` went on enforcing it. That is the shape of bug this
 * repo names "the screen saying one thing and the engine doing another". */
export function includesEdgeType(stored: string | undefined | null, edgeType: string): boolean {
  return parseEdgeTypes(stored).includes(edgeType);
}

export function edgeStyle(edgeTypeId: string): React.CSSProperties {
  const def = edgeTypes[primaryEdgeType(edgeTypeId)] ?? edgeTypes[UNMODELLED];
  return {
    stroke: def.color,
    strokeWidth: 1.5,
    ...(def.dashed ? { strokeDasharray: '6 3' } : {}),
  };
}

/**
 * What a DRAWN edge means: its type, and for IAM the permissions it starts with.
 *
 * Extracted from `Canvas.tsx::onConnect` so it can be tested. The gesture that
 * produces it -- dragging between two 6px handles -- is not automatable here
 * (see .claude/CLAUDE.md: `pointerdown` arrives with a non-handle target even at
 * the handle's measured centre), so the drag itself has no coverage. Its RESULT
 * is pure logic, and this is that logic, where a test can reach it.
 *
 * The IAM rule worth pinning: permissions come from the end being ACCESSED, and
 * `ec2 -> s3` and `s3 -> ec2` must therefore produce the same S3 permissions,
 * because the user drew the same intent either way.
 */
export function edgeDataForConnection(
  sourceType: string, targetType: string,
): { edgeType: string; permissions: string[] } {
  const edgeType = detectDefaultEdgeType(sourceType, targetType);
  return { edgeType, permissions: defaultPermissionsFor(edgeType, sourceType, targetType) };
}

/**
 * The permissions a MEANING requires, for this pair. Empty for every meaning but
 * `iam`: a connection edge grants nothing, because connecting to a Postgres
 * container with a password consults no IAM at all, and odin does not implement
 * the one AWS action that would (`rds-db:connect` -- see `defaultPermissions`).
 * Ticking the IAM box beside it is what a workload calling the RDS control plane
 * does, and that is a separate, real choice.
 */
export function defaultPermissionsFor(
  edgeType: string, sourceType: string, targetType: string,
): string[] {
  if (edgeType !== 'iam') return [];
  // Which end is the RESOURCE being accessed: the end that IS an IAM target.
  //
  // This used to ask "which end is not compute", which has no answer when BOTH
  // ends are compute -- and two such pairs are explicitly supported by the
  // catalog: `ecs -> lambda` (lambda:Invoke) and `ec2 -> ecs` (ecs:RunTask).
  // For those the old rule produced `resourceType = ''` and therefore NO
  // permissions, so the user got a cyan IAM edge with nothing ticked, and
  // downstream `agent/hcl.py` reserved an auto-role and emitted an
  // `aws_iam_role` carrying no policy at all.
  //
  // Asking "is this end an IAM target" answers it for every pair, and it is
  // strictly better than a plain "the target end wins" tie-break: `ecs -> ec2`
  // has two compute ends but only ONE of them (ecs) is something you can be
  // granted actions on, so direction must NOT decide there. When both ends are
  // genuinely IAM targets -- `ecs <-> lambda` -- the arrow is the only thing
  // that says who calls whom, so the destination end wins.
  //
  // v0.8.15 REFINEMENT, found by a test rather than by reading. The rule above
  // asks the destination end first, and `rds` is now paired with `ecs` -- both
  // of which are IAM targets -- so `rds -> ecs` answered `ecs:RunTask`,
  // GRANTING THE DATABASE PERMISSION TO RUN ECS TASKS. It also broke this
  // function's own documented property, that the two orderings of one pair
  // produce the same permissions "because the user drew the same intent either
  // way": `rds -> ecs` and `ecs -> rds` disagreed.
  //
  // The PRINCIPAL is what was actually missing. Only a compute kind can hold a
  // role (`agent/hcl.py::_GRANTABLE_KINDS` plus lambda), so where exactly one
  // end is compute, THAT end is the principal and the other is the resource,
  // whichever way the line was drawn. The arrow only decides when both ends are
  // compute, which is the one case where it is the only thing that can.
  const principalType = computeTypes.has(sourceType) === computeTypes.has(targetType)
    ? sourceType                                   // both compute, or neither: the arrow says
    : computeTypes.has(sourceType) ? sourceType : targetType;
  const otherType = principalType === sourceType ? targetType : sourceType;
  // The resource is the far end when it is something you can be granted actions
  // on, else the principal end (`ecs -> ec2`: ec2 is no target, ecs is).
  const resourceType = iamTargetTypes.has(otherType) ? otherType
    : iamTargetTypes.has(principalType) ? principalType : '';
  return [...(defaultPermissions[resourceType] ?? [])];
}

// --- what the picker in `ConfigPanel.tsx` decides -----------------------------
//
// Extracted here for the same reason `edgeDataForConnection` was: the panel is
// TSX and this repo has no React test runner, so logic left inside the component
// has no coverage at all. That mattered more than usual for this one -- the
// `<select>` it replaces was gated on `availableTypes.length > 1` and, since
// every pair meant exactly one thing until v0.8.15, had NEVER ONCE RENDERED. It
// was unproven code that looked like working code.

/**
 * Which meanings an edge currently claims, as the panel should show them.
 *
 * A STORED value is honoured verbatim, and it is worth saying why it is not
 * filtered against `available`: an `iam` edge compiles to a real policy whichever
 * pair it sits on, because `gateway/policy.py` reads the edge's kind and not the
 * kinds of its endpoints. Dropping a stored meaning the registry no longer
 * suggests for that pair would therefore HIDE A LIVE GRANT -- the panel would
 * show no permissions while the gateway went on enforcing them. `ConfigPanel`
 * handles the one value that IS overridden (`network`, the pre-rename catch-all,
 * which decides nothing anywhere) before calling this.
 *
 * Nothing stored falls back to the pair's default meaning, which is what a fresh
 * drag would have written.
 */
export function selectedEdgeTypes(
  stored: string | undefined | null, available: string[],
): string[] {
  const parsed = parseEdgeTypes(stored);
  return parsed.length > 0 ? parsed : available.slice(0, 1);
}

/** Every box the picker shows: what this pair can mean, plus anything the edge
 * already claims that it no longer suggests -- so a stored meaning is always
 * visible and always removable, never a silent extra. */
export function edgeTypeChoices(
  stored: string | undefined | null, available: string[],
): string[] {
  return [...new Set([...available, ...selectedEdgeTypes(stored, available)])];
}

/**
 * The edge data after ticking or unticking one meaning.
 *
 * NEVER EMPTY: unticking the last remaining meaning is a no-op, because an edge
 * with no meaning is a line whose stored kind nothing can read -- it would come
 * back as `unmodelled` on the next load and quietly lose whatever the user had
 * chosen. The panel disables that box rather than letting the click look like it
 * did something.
 *
 * Permissions FOLLOW the meaning, which is the whole reason the picker is
 * multi-select: ticking `iam` seeds the defaults for the resource end (a grant
 * with nothing ticked reserves a role and emits a policy-less `aws_iam_role`),
 * and unticking it clears them, because `permissions` on a non-`iam` edge is
 * read by nothing on the Python side.
 */
export function toggleEdgeType(
  stored: string | undefined | null, available: string[],
  edgeType: string, on: boolean, sourceType: string, targetType: string,
  permissions: string[] = [],
): { edgeType: string; permissions: string[] } {
  const current = selectedEdgeTypes(stored, available);
  const next = on
    ? [...current, edgeType]
    : current.filter(t => t !== edgeType);
  const kept = next.length > 0 ? next : current;
  const iamOn = kept.includes('iam');
  const wasIam = current.includes('iam');
  return {
    edgeType: serializeEdgeTypes(kept, available),
    permissions: iamOn
      ? (wasIam ? permissions : defaultPermissionsFor('iam', sourceType, targetType))
      : [],
  };
}
