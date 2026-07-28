import { catalogIamActions } from './catalog';

export const iamActionsForTarget: Record<string, string[]> = {
  s3: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket', 's3:GetBucketLocation', 's3:*'],
  dynamodb: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:DeleteItem', 'dynamodb:*'],
  ...catalogIamActions,
};

export const defaultPermissions: Record<string, string[]> = {
  s3: ['s3:GetObject', 's3:PutObject'],
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
// that service" -- carried as the `network` edge type (agent/hcl.py's pass 1.5
// reads it and stamps a `load_balancer` block onto the aws_ecs_service, which
// is how real ECS attaches to a target group). Registered explicitly rather
// than relying on the `?? ['network']` fallback so the meaning is written down
// where the edge registry lives; keeping alb out of iamActionsForTarget above is
// what stops the IAM loop from claiming this pair first.
export const albTargetTypes = new Set(['ecs']);

// Compute kinds act as IAM principals; permission edges run compute → resource.
// The app-workload kinds (service/dep/batch/llm) are parked (see NORTHSTAR.md,
// git tag app-layer-parked) — ec2/lambda/ecs are the AWS compute placeholders
// that return per northstar directive 5, and all three are real, wired-up
// nodes on the canvas today, so edges from any of them to an IAM target do
// have a principal to draw from.
export const computeTypes = new Set(['ec2', 'lambda', 'ecs']);

// --- Edge type registry ---

export type EdgeTypeDef = {
  id: string;
  label: string;
  color: string;
  dashed: boolean;
};

export const edgeTypes: Record<string, EdgeTypeDef> = {
  iam: { id: 'iam', label: 'IAM Policy', color: '#00e5ff', dashed: true },
  network: { id: 'network', label: 'Network', color: '#4a4a60', dashed: false },
};

// Given a pair of node types (unordered), return which edge types are valid
// First entry is the auto-detected default
const pairKey = (a: string, b: string) => [a, b].sort().join(':');

// Workload → any IAM target (s3/dynamodb + every catalog entry with
// iamActions) is an IAM permission edge; everything else defaults to network.
const edgeTypesForPair: Record<string, string[]> = {};
for (const target of Object.keys(iamActionsForTarget)) {
  for (const workload of computeTypes) edgeTypesForPair[pairKey(workload, target)] = ['iam'];
}
// W2.5: alb <-> compute is a target edge (see `albTargetTypes` above).
for (const target of albTargetTypes) edgeTypesForPair[pairKey('alb', target)] = ['network'];

export function detectEdgeTypes(nodeTypeA: string, nodeTypeB: string): string[] {
  return edgeTypesForPair[pairKey(nodeTypeA, nodeTypeB)] ?? ['network'];
}

export function detectDefaultEdgeType(nodeTypeA: string, nodeTypeB: string): string {
  const types = detectEdgeTypes(nodeTypeA, nodeTypeB);
  return types[0] ?? 'network';
}

export function edgeStyle(edgeTypeId: string): React.CSSProperties {
  const def = edgeTypes[edgeTypeId] ?? edgeTypes.network;
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
 * The IAM rule worth pinning: permissions come from the NON-COMPUTE end, the
 * resource being accessed. `ec2 -> s3` and `s3 -> ec2` must therefore produce
 * the same S3 permissions, because the user drew the same intent either way.
 */
export function edgeDataForConnection(
  sourceType: string, targetType: string,
): { edgeType: string; permissions: string[] } {
  const edgeType = detectDefaultEdgeType(sourceType, targetType);
  if (edgeType !== 'iam') return { edgeType, permissions: [] };
  const resourceType = !computeTypes.has(sourceType)
    ? sourceType
    : !computeTypes.has(targetType) ? targetType : '';
  return { edgeType, permissions: [...(defaultPermissions[resourceType] ?? [])] };
}
