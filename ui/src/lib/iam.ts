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
};

// Workload kinds act as IAM principals; permission edges run workload → resource.
export const computeTypes = new Set(['service', 'app', 'batch', 'llm']);

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
