// Catalog-driven service definitions: AWS-shaped resources, mostly on real
// open-source backings (sqs/sns/rds live today; the rest are future-coverage
// placeholders — see NORTHSTAR.md directive 5). Adding a service = one entry
// here (plus a backend ResourceSpec). The bespoke S3/DynamoDB nodes stay
// as-is; everything here is rendered by the generic ServiceNode and merged
// into the Canvas/ConfigPanel/Sidebar/IAM maps.
//
// Class strings are written out in full (not constructed) so Tailwind's scanner
// keeps them.

export type CatalogField = { key: string; label: string; editable?: boolean; select?: string[] };

export type ColorBundle = {
  text: string;     // badge / label text color
  border: string;   // node border
  line: string;     // NodeResizer line
  handle: string;   // connection handle bg
  shadow: string;   // node glow
  chipBg: string;   // config-panel header chip bg + border
  rgb: string;      // "r,g,b" for the header-divider tint
};

export const COLORS: Record<string, ColorBundle> = {
  cyan:    { text: 'text-neon-cyan',    border: 'border-neon-cyan',    line: '!border-neon-cyan',    handle: '!bg-neon-cyan',    shadow: 'shadow-[0_0_15px_rgba(34,211,238,0.08)]',  chipBg: 'bg-[rgba(34,211,238,0.1)] border-neon-cyan',   rgb: '34,211,238' },
  pink:    { text: 'text-neon-pink',    border: 'border-neon-pink',    line: '!border-neon-pink',    handle: '!bg-neon-pink',    shadow: 'shadow-[0_0_15px_rgba(244,114,182,0.08)]', chipBg: 'bg-[rgba(244,114,182,0.1)] border-neon-pink',  rgb: '244,114,182' },
  rose:    { text: 'text-neon-rose',    border: 'border-neon-rose',    line: '!border-neon-rose',    handle: '!bg-neon-rose',    shadow: 'shadow-[0_0_15px_rgba(251,113,133,0.08)]', chipBg: 'bg-[rgba(251,113,133,0.1)] border-neon-rose',  rgb: '251,113,133' },
  indigo:  { text: 'text-neon-indigo',  border: 'border-neon-indigo',  line: '!border-neon-indigo',  handle: '!bg-neon-indigo',  shadow: 'shadow-[0_0_15px_rgba(129,140,248,0.08)]', chipBg: 'bg-[rgba(129,140,248,0.1)] border-neon-indigo', rgb: '129,140,248' },
  lime:    { text: 'text-neon-lime',    border: 'border-neon-lime',    line: '!border-neon-lime',    handle: '!bg-neon-lime',    shadow: 'shadow-[0_0_15px_rgba(163,230,53,0.08)]',  chipBg: 'bg-[rgba(163,230,53,0.1)] border-neon-lime',   rgb: '163,230,53' },
  amber:   { text: 'text-neon-amber',   border: 'border-neon-amber',   line: '!border-neon-amber',   handle: '!bg-neon-amber',   shadow: 'shadow-[0_0_15px_rgba(251,191,36,0.08)]',  chipBg: 'bg-[rgba(251,191,36,0.1)] border-neon-amber',  rgb: '251,191,36' },
  teal:    { text: 'text-neon-teal',    border: 'border-neon-teal',    line: '!border-neon-teal',    handle: '!bg-neon-teal',    shadow: 'shadow-[0_0_15px_rgba(45,212,191,0.08)]',  chipBg: 'bg-[rgba(45,212,191,0.1)] border-neon-teal',   rgb: '45,212,191' },
  sky:     { text: 'text-neon-sky',     border: 'border-neon-sky',     line: '!border-neon-sky',     handle: '!bg-neon-sky',     shadow: 'shadow-[0_0_15px_rgba(56,189,248,0.08)]',  chipBg: 'bg-[rgba(56,189,248,0.1)] border-neon-sky',    rgb: '56,189,248' },
  fuchsia: { text: 'text-neon-fuchsia', border: 'border-neon-fuchsia', line: '!border-neon-fuchsia', handle: '!bg-neon-fuchsia', shadow: 'shadow-[0_0_15px_rgba(232,121,249,0.08)]', chipBg: 'bg-[rgba(232,121,249,0.1)] border-neon-fuchsia', rgb: '232,121,249' },
};

export type ServiceDef = {
  type: string;         // node type (matches backend node_type)
  abbr: string;         // sidebar drag key + node badge
  label: string;        // full display name
  sublabel: string;     // sidebar sub-text
  category: string;     // sidebar group
  color: keyof typeof COLORS;
  width: number;        // default node width (px, multiple of 20)
  fields: CatalogField[];               // config fields (status/error appended in panel)
  defaultData: Record<string, string>;  // initial node data
  primary?: { key: string; label: string }; // one-line node detail
  iamActions?: string[];                // if this is an IAM target
};

export const CATALOG: ServiceDef[] = [
  {
    type: 'sqs', abbr: 'SQS', label: 'SQS Queue', sublabel: 'Message queue',
    category: 'Integration', color: 'pink', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'arn', label: 'ARN' },
    ],
    defaultData: { label: 'new-queue', arn: '' },
    iamActions: ['sqs:SendMessage', 'sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:*'],
  },
  {
    type: 'sns', abbr: 'SNS', label: 'SNS Topic', sublabel: 'Pub/sub topic',
    category: 'Integration', color: 'rose', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'arn', label: 'ARN' },
    ],
    defaultData: { label: 'new-topic', arn: '' },
    iamActions: ['sns:Publish', 'sns:Subscribe', 'sns:*'],
  },
  {
    type: 'kinesis', abbr: 'KIN', label: 'Kinesis Stream', sublabel: 'Data stream',
    category: 'Integration', color: 'fuchsia', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'arn', label: 'ARN' }],
    defaultData: { label: 'new-stream', arn: '' },
    iamActions: ['kinesis:PutRecord', 'kinesis:GetRecords', 'kinesis:*'],
  },
  {
    type: 'rds', abbr: 'RDS', label: 'RDS Database', sublabel: 'Relational DB',
    category: 'Database', color: 'sky', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'engine', label: 'Engine', editable: true, select: ['postgres', 'mysql', 'mariadb'] },
      { key: 'instanceClass', label: 'Instance Class', editable: true },
      { key: 'arn', label: 'ARN' },
    ],
    defaultData: { label: 'db', engine: 'postgres', instanceClass: 'db.t3.micro', arn: '' },
    primary: { key: 'engine', label: 'Engine' },
    iamActions: ['rds-db:connect', 'rds:DescribeDBInstances', 'rds:*'],
  },
  // W2.4: real Secrets Manager -- the node's Name IS the secret name (the
  // gateway classifies every secretsmanager:* call by that bare name, so an
  // IAM edge drawn to this node only enforces while the two match). Value is
  // the secret's initial version; it is stored CLEARTEXT in a 0600 per-env
  // JSON sidecar -- there is no KMS in odin, so nothing here is encrypted at
  // rest. Read SECURITY.md's Secrets section before typing a real credential.
  {
    type: 'secret', abbr: 'SEC', label: 'Secret', sublabel: 'Secrets Manager',
    category: 'Security', color: 'lime', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'description', label: 'Description', editable: true },
      { key: 'secretString', label: 'Value', editable: true },
    ],
    defaultData: { label: 'new-secret', description: '', secretString: '' },
    iamActions: [
      'secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret',
      'secretsmanager:PutSecretValue', 'secretsmanager:*',
    ],
  },
  {
    type: 'kms', abbr: 'KMS', label: 'KMS Key', sublabel: 'Encryption key',
    category: 'Security', color: 'teal', width: 200,
    fields: [{ key: 'label', label: 'Description', editable: true }, { key: 'arn', label: 'Key ARN' }],
    defaultData: { label: 'new-key', arn: '' },
    iamActions: ['kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey', 'kms:*'],
  },
  {
    type: 'iamrole', abbr: 'IAM', label: 'IAM Role', sublabel: 'Identity role',
    category: 'Security', color: 'amber', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'arn', label: 'ARN' }],
    defaultData: { label: 'new-role', arn: '' },
  },
  // iam_role/ecr (V2c) are the REAL, gateway-modeled services (NORTHSTAR
  // directive 5) — distinct from the 'iamrole' placeholder above, which
  // stays an unwired future-coverage entry (not in translate.py's _KIND, so
  // Apply silently skips it; see skipped_node_types). Both render via the
  // generic ServiceNode, no bespoke component.
  {
    type: 'iam_role', abbr: 'ROLE', label: 'IAM Role', sublabel: 'Terraform-managed role',
    category: 'Security', color: 'amber', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'inlinePolicy', label: 'Inline Policy (JSON)', editable: true },
    ],
    defaultData: { label: 'new-role', inlinePolicy: '' },
  },
  {
    type: 'ecr', abbr: 'ECR', label: 'ECR Repository', sublabel: 'Container registry',
    category: 'Storage', color: 'sky', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }],
    defaultData: { label: 'new-repo' },
  },
  {
    type: 'route53', abbr: 'DNS', label: 'Route 53 Zone', sublabel: 'Hosted zone',
    category: 'Networking', color: 'indigo', width: 200,
    fields: [{ key: 'label', label: 'Domain', editable: true }, { key: 'zoneId', label: 'Zone ID' }],
    defaultData: { label: 'example.com', zoneId: '' },
  },
  {
    type: 'apigateway', abbr: 'API', label: 'API Gateway', sublabel: 'REST API',
    category: 'Networking', color: 'fuchsia', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'apiId', label: 'API ID' }],
    defaultData: { label: 'new-api', apiId: '' },
  },
  {
    type: 'efs', abbr: 'EFS', label: 'EFS', sublabel: 'Elastic file system',
    category: 'Storage', color: 'sky', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'fsId', label: 'File System ID' }],
    defaultData: { label: 'new-fs', fsId: '' },
  },
  // ecs (V5c) is the REAL, gateway-modeled ECS service (NORTHSTAR directive
  // 5): the drawn node IS the service+taskdef pair (v1 single-container
  // taskdefs), sharing ONE auto-generated cluster per canvas (agent/hcl.py's
  // `_ecs` builder) -- renders via the generic ServiceNode, no bespoke
  // component, same as iam_role/ecr/lambda.
  {
    type: 'ecs', abbr: 'ECS', label: 'ECS Service', sublabel: 'Container service',
    category: 'Compute', color: 'lime', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'image', label: 'Image', editable: true },
      { key: 'count', label: 'Task Count', editable: true },
      { key: 'port', label: 'Container Port', editable: true },
    ],
    defaultData: { label: 'new-service', image: 'nginx:alpine', count: '1', port: '80' },
    primary: { key: 'count', label: 'tasks' },
  },
  // W2.4: real SSM Parameter Store -- the node's Name IS the parameter name,
  // slashes and all (the gateway classifies every ssm:* call by that bare
  // name, so an IAM edge drawn to this node only enforces while the two
  // match). SecureString is NOT encrypted: there is no KMS in odin, so it is
  // stored byte-for-byte like a String would be, CLEARTEXT in a 0600 per-env
  // JSON sidecar -- see SECURITY.md's Secrets section. A parameter can't exist
  // without a Value, hence the placeholder default rather than an empty one.
  {
    type: 'ssm', abbr: 'SSM', label: 'SSM Parameter', sublabel: 'Parameter store',
    category: 'Management', color: 'indigo', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'paramType', label: 'Type', editable: true, select: ['String', 'StringList', 'SecureString'] },
      { key: 'paramValue', label: 'Value', editable: true },
    ],
    defaultData: { label: '/odin/param', paramType: 'String', paramValue: 'changeme' },
    primary: { key: 'paramType', label: 'Type' },
    iamActions: [
      'ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath',
      'ssm:PutParameter', 'ssm:*',
    ],
  },
  // W2.1: real CloudWatch Logs -- the node's Name IS the log group name (the
  // gateway classifies every logs:* call by bare group name, so an IAM edge
  // only enforces while the two match). Retention is left blank by default =
  // AWS's own "never expire"; a value is emitted as `retention_in_days`.
  {
    type: 'logs', abbr: 'LOG', label: 'Log Group', sublabel: 'CloudWatch Logs',
    category: 'Monitoring', color: 'amber', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'retentionInDays', label: 'Retention (days)', editable: true },
    ],
    defaultData: { label: '/odin/logs', retentionInDays: '' },
    iamActions: [
      'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:GetLogEvents',
      'logs:FilterLogEvents', 'logs:DescribeLogStreams', 'logs:*',
    ],
  },
  {
    type: 'events', abbr: 'EVT', label: 'EventBridge', sublabel: 'Event rule',
    category: 'Integration', color: 'sky', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'schedule', label: 'Schedule', editable: true },
    ],
    defaultData: { label: 'new-rule', schedule: 'rate(5 minutes)' },
    primary: { key: 'schedule', label: 'Schedule' },
  },
  {
    type: 'ebs', abbr: 'EBS', label: 'EBS Volume', sublabel: 'Block storage',
    category: 'Storage', color: 'lime', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'az', label: 'Availability Zone', editable: true },
      { key: 'size', label: 'Size (GiB)', editable: true },
    ],
    defaultData: { label: 'new-volume', az: 'us-east-1a', size: '10' },
    primary: { key: 'size', label: 'GiB' },
  },
  {
    type: 'eip', abbr: 'EIP', label: 'Elastic IP', sublabel: 'Static IP',
    category: 'Networking', color: 'teal', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'publicIp', label: 'Public IP' }],
    defaultData: { label: 'new-eip', publicIp: '' },
  },
  {
    type: 'igw', abbr: 'IGW', label: 'Internet Gateway', sublabel: 'VPC internet access',
    category: 'Networking', color: 'sky', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'igwId', label: 'Gateway ID' }],
    defaultData: { label: 'new-igw', igwId: '' },
  },
  {
    type: 'alb', abbr: 'ALB', label: 'Load Balancer', sublabel: 'Application/Network LB',
    category: 'Networking', color: 'rose', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'lbType', label: 'Type', editable: true, select: ['application', 'network'] },
      { key: 'arn', label: 'ARN' },
    ],
    defaultData: { label: 'new-lb', lbType: 'application', arn: '' },
    primary: { key: 'lbType', label: 'Type' },
  },
];

export const catalogByType: Record<string, ServiceDef> = Object.fromEntries(
  CATALOG.map((s) => [s.type, s]),
);

export const catalogTypes = CATALOG.map((s) => s.type);

// --- derived maps merged into the existing Canvas/ConfigPanel/Sidebar/IAM ---

export const catalogNodeTypeMap: Record<string, string> = Object.fromEntries(
  CATALOG.map((s) => [s.abbr, s.type]),
);

export const catalogDefaultData: Record<string, Record<string, string>> = Object.fromEntries(
  CATALOG.map((s) => [s.type, { ...s.defaultData, status: 'draft' }]),
);

export const catalogDefaultStyle: Record<string, { width: number }> = Object.fromEntries(
  CATALOG.map((s) => [s.type, { width: s.width }]),
);

export const catalogZIndex: Record<string, number> = Object.fromEntries(
  CATALOG.map((s) => [s.type, 2]),
);

export const catalogTypeConfig: Record<string, { label: string; neonColor: string; neonBg: string }> =
  Object.fromEntries(CATALOG.map((s) => [s.type, { label: s.label, neonColor: COLORS[s.color].text, neonBg: COLORS[s.color].chipBg }]));

export const catalogFields: Record<string, CatalogField[]> = Object.fromEntries(
  CATALOG.map((s) => [s.type, [...s.fields, { key: 'status', label: 'Status' }, { key: 'error', label: 'Error' }]]),
);

export const catalogIamActions: Record<string, string[]> = Object.fromEntries(
  CATALOG.filter((s) => s.iamActions).map((s) => [s.type, s.iamActions as string[]]),
);
