// Catalog-driven service definitions: AWS-shaped resources, mostly on real
// open-source backings (sqs/sns/rds live today; the rest are future-coverage
// placeholders — see NORTHSTAR.md directive 5). Adding a service = one entry
// here (plus a backend ResourceSpec). The bespoke S3/DynamoDB nodes stay
// as-is; everything here is rendered by the generic ServiceNode and merged
// into the Canvas/ConfigPanel/Sidebar/IAM maps.
//
// Class strings are written out in full (not constructed) so Tailwind's scanner
// keeps them.
//
// PLACEHOLDERS -- the one invariant this file owes the user. A tile whose `type`
// is not in spec/translate.py's `_KIND` can never become a Stack resource: Apply
// reports it under `skipped_node_types` and touches nothing. Drawn beside a real
// one it is indistinguishable, so every such entry MUST say `(placeholder)` at
// the end of its `sublabel` and MUST NOT declare `iamActions` (see the kms note
// below for why the permissions are the sharper half of the lie). The rule reads
// both ways: `(placeholder)` in a sublabel means Apply skips it, and nothing
// else does. When a placeholder becomes real, the marker comes off in the same
// commit that adds it to `_KIND`.
// Today: kinesis, kms, route53, apigateway, efs, events, ebs, eip, igw.

// `multiline`/`placeholder` mirror ConfigPanel's own FieldDef (catalogFields
// spreads straight into it), so a catalog entry can declare a textarea field
// -- e.g. an rds node's one-SG-label-per-line `securityGroups`.
export type CatalogField = {
  key: string; label: string; editable?: boolean; select?: string[];
  multiline?: boolean; placeholder?: string;
};

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

// The seven BESPOKE resources: real, modelled services that render through their
// own node components instead of the generic ServiceNode, which is why they are
// not CATALOG entries. They live here anyway for two reasons.
//
// One: the sidebar renders `[...BUILTINS, ...CATALOG]`, so the honesty invariant
// this file owes the user -- a tile either models a real service or says
// `(placeholder)` -- has to hold over all 27 tiles, not the 20 in CATALOG. That
// needs a `type` to check against translate.py's `_KIND`, which the sidebar's own
// tile shape never carried.
//
// Two: Sidebar.tsx and Canvas.tsx each used to keep their own hardcoded copy of
// this list, and Canvas's copy is the load-bearing one -- its `nodeTypeMap`
// spreads the catalog AFTER these keys, so a catalog entry declaring abbr 'S3'
// would silently override the bespoke tile and drop the wrong node type. Two
// copies of a list where one silently wins is a bug waiting for its second
// author; both now derive from here.
//
// `iconClass` is spelled out in full rather than built from COLORS for the same
// reason every other class string in this file is: Tailwind's scanner only sees
// literal text.
export type BuiltinDef = {
  type: string;         // node type (matches backend node_type)
  abbr: string;         // sidebar drag key
  label: string;
  sublabel: string;
  category: string;
  iconClass: string;
};

export const BUILTINS: BuiltinDef[] = [
  { type: 'vpc', abbr: 'VPC', label: 'VPC', sublabel: 'Virtual Private Cloud', category: 'Networking', iconClass: 'text-neon-purple border-neon-purple' },
  { type: 'subnet', abbr: 'SUB', label: 'Subnet', sublabel: 'Network partition', category: 'Networking', iconClass: 'text-neon-blue border-neon-blue' },
  { type: 'sg', abbr: 'SG', label: 'Security Group', sublabel: 'Firewall rules', category: 'Networking', iconClass: 'text-neon-red border-neon-red' },
  { type: 'ec2', abbr: 'EC2', label: 'EC2 Instance', sublabel: 'Real Lima VM', category: 'Compute', iconClass: 'text-neon-orange border-neon-orange' },
  { type: 'lambda', abbr: 'LAM', label: 'Lambda Function', sublabel: 'Real RIE container', category: 'Compute', iconClass: 'text-neon-yellow border-neon-yellow' },
  { type: 's3', abbr: 'S3', label: 'S3 Bucket', sublabel: 'Object storage', category: 'Storage', iconClass: 'text-neon-green border-neon-green' },
  { type: 'dynamodb', abbr: 'DDB', label: 'DynamoDB', sublabel: 'NoSQL table', category: 'Database', iconClass: 'text-neon-cyan border-neon-cyan' },
];

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
  // kinesis: an UNBACKED placeholder (see PLACEHOLDERS at the top). It used to
  // declare `iamActions` for kinesis:PutRecord/GetRecords/* -- the only skipped
  // tile that did -- which let a user draw an IAM edge to a node Apply never
  // creates and tick permissions against a namespace the gateway does not
  // classify at all (there is no kinesis handler in gateway/models/). Removed for
  // exactly the reason kms has never had any: a permission odin can neither
  // enforce nor reach is a promise the engine cannot keep, and offering it is
  // worse than offering nothing. They come back with a real Kinesis model.
  {
    type: 'kinesis', abbr: 'KIN', label: 'Kinesis Stream', sublabel: 'Data stream (placeholder)',
    category: 'Integration', color: 'fuchsia', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'arn', label: 'ARN' }],
    defaultData: { label: 'new-stream', arn: '' },
  },
  {
    // W2.7: a real `aws_db_instance` (a real Postgres container behind it), so
    // every argument the HCL builder emits is editable here. `engine` lists
    // only postgres: that IS the substrate, and an honest Apply declines any
    // other engine rather than quietly handing you a Postgres (agent/hcl.py).
    // The name must be a valid RDS identifier (lowercase, hyphen-separated).
    type: 'rds', abbr: 'RDS', label: 'RDS Database', sublabel: 'Relational DB',
    category: 'Database', color: 'sky', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'engine', label: 'Engine', editable: true, select: ['postgres'] },
      { key: 'instanceClass', label: 'Instance Class', editable: true },
      { key: 'allocatedStorage', label: 'Storage (GiB)', editable: true },
      { key: 'dbName', label: 'Database', editable: true },
      { key: 'username', label: 'Username', editable: true },
      { key: 'password', label: 'Password', editable: true },
      // W2.6: the DB really is gated by the SGs named here -- they become the
      // `aws_db_instance`'s `vpc_security_group_ids` (agent/hcl.py), and the
      // gateway puts its Postgres on the env's Nebula mesh behind their
      // compiled firewall (gateway/models/rdsctl.py::_db_firewall). Same
      // one-label-per-line convention as an ec2 node's.
      { key: 'securityGroups', label: 'Security Groups (one label per line)', editable: true, multiline: true },
      { key: 'arn', label: 'ARN' },
    ],
    defaultData: {
      label: 'app-db', engine: 'postgres', instanceClass: 'db.t3.micro',
      allocatedStorage: '20', dbName: 'postgres', username: 'app',
      password: 'apppass123', securityGroups: '', arn: '',
    },
    primary: { key: 'engine', label: 'Engine' },
    iamActions: ['rds-db:connect', 'rds:DescribeDBInstances', 'rds:*'],
  },
  // elasticache (W2.8) is a REAL, gateway-modeled service (NORTHSTAR directive
  // 5): the drawn node IS one `aws_elasticache_cluster`, backed by a real
  // redis:7-alpine container whose published port is advertised as the
  // cluster's node endpoint (${{<node>.REDIS_URL}} from a container consumer,
  // ${{<node>.REDIS_URL_VM}} from an EC2 one). SINGLE NODE in v1 — no node-count
  // field, because the gateway rejects anything but 1 (see ROADMAP's limits).
  // The `iamActions` below gate the CONTROL plane only: Redis's own wire
  // protocol isn't AWS-signed, so no IAM edge can ever gate a GET/SET.
  // Renders via the generic ServiceNode, no bespoke component.
  {
    type: 'elasticache', abbr: 'ELC', label: 'ElastiCache', sublabel: 'Redis cache cluster',
    category: 'Database', color: 'teal', width: 220,
    fields: [
      { key: 'label', label: 'Cluster ID', editable: true },
      { key: 'nodeType', label: 'Node Type', editable: true },
    ],
    defaultData: { label: 'cache', nodeType: 'cache.t3.micro' },
    primary: { key: 'nodeType', label: 'Node' },
    iamActions: ['elasticache:DescribeCacheClusters', 'elasticache:ModifyCacheCluster', 'elasticache:*'],
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
  // kms: an UNBACKED placeholder -- odin has no KMS substitute and the gateway
  // classifies no `kms:*` action, so it is not in translate.py's _KIND and Apply
  // skips it (see skipped_node_types).
  // Deliberately NO `iamActions`: advertising kms:Encrypt/Decrypt let a user
  // draw an IAM edge and tick permissions that could never be enforced or
  // even reached -- a promise the engine cannot keep. They come back with a
  // real KMS model, not before.
  {
    type: 'kms', abbr: 'KMS', label: 'KMS Key', sublabel: 'Encryption key (placeholder)',
    category: 'Security', color: 'teal', width: 200,
    fields: [{ key: 'label', label: 'Description', editable: true }, { key: 'arn', label: 'Key ARN' }],
    defaultData: { label: 'new-key', arn: '' },
  },
  // iam_role/ecr (V2c) are REAL, gateway-modeled services (NORTHSTAR directive
  // 5). Both render via the generic ServiceNode, no bespoke component.
  //
  // There used to be a SECOND tile here, `type: 'iamrole'` (abbr IAM, sublabel
  // "Identity role"), also labelled "IAM Role", also amber, also in Security --
  // an unwired placeholder absent from translate.py's _KIND. Two tiles with the
  // same label in the same group, one of which silently does nothing on Apply,
  // is a coin flip the sidebar gave the user no way to win: sublabels are the
  // only thing that differed. Deleted rather than marked `(placeholder)`, because
  // unlike kms/route53/efs it duplicated a service odin ALREADY models -- the
  // "future coverage" it stood in for is `iam_role`, and it is right here. A
  // stale saved canvas still holding `type: 'iamrole'` degrades honestly: ReactFlow
  // falls back to its default node (error003) and Apply reports the type under
  // skipped/not_covered, the same path a typo'd type takes.
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
    // The image-PULL path a workload needs. `gateway/classify.py::_classify_ecr`
    // builds its action as `ecr:<op>` straight from `x-amz-target`, so these are
    // the op names a real SDK sends -- which is what makes the grant bite rather
    // than decorate (`tests/gateway/test_iam_vocabulary_is_enforceable.py`).
    iamActions: ['ecr:GetAuthorizationToken', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability', 'ecr:*'],
  },
  {
    type: 'route53', abbr: 'DNS', label: 'Route 53 Zone', sublabel: 'Hosted zone (placeholder)',
    category: 'Networking', color: 'indigo', width: 200,
    fields: [{ key: 'label', label: 'Domain', editable: true }, { key: 'zoneId', label: 'Zone ID' }],
    defaultData: { label: 'example.com', zoneId: '' },
  },
  {
    type: 'apigateway', abbr: 'API', label: 'API Gateway', sublabel: 'REST API (placeholder)',
    category: 'Networking', color: 'fuchsia', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'apiId', label: 'API ID' }],
    defaultData: { label: 'new-api', apiId: '' },
  },
  {
    type: 'efs', abbr: 'EFS', label: 'EFS', sublabel: 'Elastic file system (placeholder)',
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
      // Memory is a HARD cap odin enforces (`compute/tasks.py::_memory_mib`
      // sets the container's limit), and it was unauthorable until v0.8.2 --
      // every task ran at the 512 MiB fallback, so a container needing more was
      // OOM-killed with no field to say otherwise. It is also the number
      // `spec/capacity.py` refuses an over-subscribed instance with, so leaving
      // it blank and leaving it at 512 have to mean the same thing.
      { key: 'memory', label: 'Task Memory (MiB)', editable: true, placeholder: '512' },
      // cpu is a SHARE, not a cap -- same plumbing, no admission guard needed.
      { key: 'cpu', label: 'Task CPU (units, 1024 = 1 vCPU)', editable: true, placeholder: '256' },
      // Read-only, exactly like sg/ec2's `vpc`/`subnet` stamps: authored by
      // DRAGGING this box inside an EC2 box (lib/containment.ts), never typed.
      //
      // Without it the placement was the one containment stamp odin acted on
      // and never showed. It is not cosmetic -- it emits a real
      // `placement_constraints { memberOf }` and runs the task in that VM
      // (`agent/hcl.py`, `gateway/models/ecsctl.py::runtime_for_service`) --
      // so a user who dragged a box a few pixels short of "fully inside" had
      // no way to tell the placement had not taken. The strict containment
      // rule makes that a real possibility, which is exactly why the answer
      // has to be legible somewhere.
      { key: 'host', label: 'Instance (placement)' },
    ],
    defaultData: { label: 'new-service', image: 'nginx:alpine', count: '1', port: '80' },
    primary: { key: 'count', label: 'tasks' },
    // ECS is a workload AND an IAM target: one service legitimately controls
    // another's tasks. `gateway/classify.py::_classify_ecs` builds its action as
    // `ecs:<op>` from `x-amz-target`, so these are real SDK op names.
    iamActions: ['ecs:RunTask', 'ecs:StopTask', 'ecs:DescribeTasks', 'ecs:ListTasks', 'ecs:*'],
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
    type: 'events', abbr: 'EVT', label: 'EventBridge', sublabel: 'Event rule (placeholder)',
    category: 'Integration', color: 'sky', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'schedule', label: 'Schedule', editable: true },
    ],
    defaultData: { label: 'new-rule', schedule: 'rate(5 minutes)' },
    primary: { key: 'schedule', label: 'Schedule' },
  },
  {
    type: 'ebs', abbr: 'EBS', label: 'EBS Volume', sublabel: 'Block storage (placeholder)',
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
    type: 'eip', abbr: 'EIP', label: 'Elastic IP', sublabel: 'Static IP (placeholder)',
    category: 'Networking', color: 'teal', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'publicIp', label: 'Public IP' }],
    defaultData: { label: 'new-eip', publicIp: '' },
  },
  {
    type: 'igw', abbr: 'IGW', label: 'Internet Gateway', sublabel: 'VPC internet access (placeholder)',
    category: 'Networking', color: 'sky', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'igwId', label: 'Gateway ID' }],
    defaultData: { label: 'new-igw', igwId: '' },
  },
  // W2.5: a REAL load balancer -- one node expands to aws_lb +
  // aws_lb_target_group + aws_lb_listener (agent/hcl.py's `_alb`), and the
  // substrate is an actual nginx reverse-proxy container per load balancer
  // (compute/proxy.py) whose upstreams are the target group's registered
  // targets. Draw it INSIDE a Subnet (containment is what gives aws_lb its
  // subnets and the target group its vpc_id), then draw a NETWORK edge from it
  // to the ECS service it fronts -- that edge is what registers the service's
  // tasks as targets. Only `application` is supported in v1; `network` (an NLB)
  // reports itself unsupported on Apply rather than silently becoming an ALB.
  // The reachable address is NOT the ARN/DNS name: odin publishes the proxy on
  // a dynamic host port, surfaced as the node's ALB_ENDPOINT fact.
  {
    type: 'alb', abbr: 'ALB', label: 'Load Balancer', sublabel: 'Application LB (real proxy)',
    category: 'Networking', color: 'rose', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'lbType', label: 'Type', editable: true, select: ['application', 'network'] },
      { key: 'listenerPort', label: 'Listener Port', editable: true },
      { key: 'port', label: 'Target Port', editable: true },
      { key: 'healthCheckPath', label: 'Health Check Path', editable: true },
    ],
    defaultData: {
      label: 'new-lb', lbType: 'application', listenerPort: '80', port: '80', healthCheckPath: '/',
    },
    primary: { key: 'listenerPort', label: 'Listener' },
  },
];

export const catalogByType: Record<string, ServiceDef> = Object.fromEntries(
  CATALOG.map((s) => [s.type, s]),
);

export const catalogTypes = CATALOG.map((s) => s.type);

/**
 * A kind with no real substitute behind it. The `(placeholder)` marker in the
 * sublabel is the single source of truth (see the note at the top of this
 * file): it is what makes Apply skip the kind, and `catalog.test.ts` enforces
 * that any kind absent from translate.py carries it.
 *
 * These are hidden from the SIDEBAR palette (owner call, 2026-07-27) and come
 * back one at a time as each gains a substitute -- removing the marker
 * restores the tile in the same edit, with no second list to update.
 * `CATALOG` itself keeps every entry so an already-saved canvas containing one
 * still renders properly.
 */
export const isPlaceholder = (sublabel: string) => sublabel.includes('(placeholder)');

/** What the palette offers: everything with a real substitute behind it. */
export const PALETTE: ServiceDef[] = CATALOG.filter((s) => !isPlaceholder(s.sublabel));

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
