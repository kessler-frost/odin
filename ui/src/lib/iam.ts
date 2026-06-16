export const iamActionsForTarget: Record<string, string[]> = {
  s3: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket', 's3:GetBucketLocation', 's3:*'],
  lambda: ['lambda:InvokeFunction', 'lambda:GetFunction', 'lambda:ListFunctions', 'lambda:*'],
  ec2: ['ec2:DescribeInstances', 'ec2:StartInstances', 'ec2:StopInstances', 'ec2:*'],
};

export const defaultPermissions: Record<string, string[]> = {
  s3: ['s3:GetObject', 's3:PutObject'],
  lambda: ['lambda:InvokeFunction'],
};

export const computeTypes = new Set(['ec2', 'lambda']);

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

const edgeTypesForPair: Record<string, string[]> = {
  [pairKey('ec2', 's3')]: ['iam'],
  [pairKey('ec2', 'lambda')]: ['iam', 'network'],
  [pairKey('lambda', 's3')]: ['iam'],
  [pairKey('ec2', 'ec2')]: ['network'],
  [pairKey('lambda', 'lambda')]: ['iam'],
};

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
