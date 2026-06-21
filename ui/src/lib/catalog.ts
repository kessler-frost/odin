// Catalog-driven AWS service definitions. Adding a frequently-used service =
// one entry here (plus a backend ResourceSpec + a Moto test). The existing
// bespoke nodes (vpc/subnet/ec2/lambda/s3/sg) stay as-is; everything here is
// rendered by the generic ServiceNode and merged into the Canvas/ConfigPanel/
// Sidebar/IAM maps.
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
    defaultData: { label: 'new-db', engine: 'postgres', instanceClass: 'db.t3.micro', arn: '' },
    primary: { key: 'engine', label: 'Engine' },
  },
  {
    type: 'secret', abbr: 'SEC', label: 'Secret', sublabel: 'Secrets Manager',
    category: 'Security', color: 'lime', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'arn', label: 'ARN' }],
    defaultData: { label: 'new-secret', arn: '' },
    iamActions: ['secretsmanager:GetSecretValue', 'secretsmanager:*'],
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
  {
    type: 'ecs', abbr: 'ECS', label: 'ECS Cluster', sublabel: 'Container cluster',
    category: 'Compute', color: 'lime', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'arn', label: 'ARN' }],
    defaultData: { label: 'new-cluster', arn: '' },
  },
  {
    type: 'ssm', abbr: 'SSM', label: 'SSM Parameter', sublabel: 'Parameter store',
    category: 'Management', color: 'indigo', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'paramValue', label: 'Value', editable: true },
    ],
    defaultData: { label: 'new-param', paramValue: 'changeme' },
    iamActions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:*'],
  },
  {
    type: 'logs', abbr: 'LOG', label: 'Log Group', sublabel: 'CloudWatch Logs',
    category: 'Monitoring', color: 'amber', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'arn', label: 'ARN' }],
    defaultData: { label: '/odin/logs', arn: '' },
    iamActions: ['logs:CreateLogStream', 'logs:PutLogEvents', 'logs:*'],
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
