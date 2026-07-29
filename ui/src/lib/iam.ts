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
  rds: ['rds-db:connect'],
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

export function edgeStyle(edgeTypeId: string): React.CSSProperties {
  const def = edgeTypes[edgeTypeId] ?? edgeTypes[UNMODELLED];
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
  if (edgeType !== 'iam') return { edgeType, permissions: [] };
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
  const resourceType = iamTargetTypes.has(targetType) ? targetType
    : iamTargetTypes.has(sourceType) ? sourceType : '';
  return { edgeType, permissions: [...(defaultPermissions[resourceType] ?? [])] };
}
