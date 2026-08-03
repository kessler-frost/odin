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
// Today: kinesis, apigateway, efs, events, eip, igw.
// (`kms`, `ebs` and `route53` each came OFF this list -- kms in W2.9, a real
// sealed-at-rest key; ebs in v0.8.18, a real `limactl disk` volume on a real
// VM; route53 in v0.8.19, a real hosts entry -- and each time the marker came
// off in the same commit that added the kind to `_KIND`, exactly as the rule
// above requires. There were TWO contradictory "Today" lines here until
// v0.8.19, a merge artifact from kms and ebs landing in one release out of two
// worktrees: each edit dropped its own kind and neither saw the other's, so one
// line still called ebs a placeholder and the other still called kms one. Both
// were wrong, and the test that pins this invariant could not see it, because
// it reads CATALOG rather than this comment. Keep it to ONE line. `develop`
// landed the same dedupe independently in 38d1a99 -- two people finding the
// same artifact separately, which is the argument for the ratchet that commit
// added rather than for either fix.)
// Today: kinesis, route53, events, eip, igw.
// `kms`, `ebs` and now `efs` came OFF this list; each marker came off in the
// same commit that added the kind to `_KIND`, exactly as the rule above
// requires.
//
// That one line was TWO contradictory lines for a merge -- one still naming
// `ebs`, the other still naming `kms`, both kept -- so it is worth saying how
// this one was arrived at: by grepping the file for `(placeholder)'` and
// checking each hit against `translate.py::_KIND`, not from memory. A
// hand-maintained list beside the thing it describes is prose that cannot
// fail a build; `catalog.test.ts` pins the INVARIANT (nothing marked
// `(placeholder)` reaches `PALETTE`), which is the half that matters.

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
  // the reason kms's were withheld until kms became real (W2.9): a permission
  // odin can neither enforce nor reach is a promise the engine cannot keep, and
  // offering it is worse than offering nothing. They come back with a real
  // Kinesis model -- which is exactly how kms got its list back.
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
    // `rds-db:connect` is TICKABLE and is no longer the DEFAULT, which is the
    // same split the ecr block below draws and for the same two reasons.
    //
    // It cannot bite locally, and unlike ecr's layer verbs it cannot even be
    // CLASSIFIED: `classify.py` builds every rds action as `rds:<Action>` out of
    // the query protocol's `Action` param, so every string it can emit starts
    // `rds:` and `rds-db:` is a different service prefix entirely. odin
    // implements no IAM database authentication at all -- the Postgres container
    // takes its password out of `DATABASE_URL` and consults nobody. classify.py's
    // own prose asserted the opposite ("compiles to a statement the gateway
    // enforces") and is corrected in the same change.
    //
    // It stays here because the generated Terraform is meant to be portable, and
    // taken to Amazon this is exactly the action IAM DB auth needs. It stops
    // being PRE-TICKED (`iam.ts::defaultPermissions` now offers
    // `rds:DescribeDBInstances`) because ticking something FOR the user that odin
    // cannot enforce is odin claiming a protection it has not got -- and what the
    // user drawing rds -> workload usually wants is the `connection` edge, which
    // authors that `DATABASE_URL` instead of granting a permission nobody checks.
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
  // the secret's initial version.
  //
  // ENCRYPTED AT REST as of W2.9. This paragraph said the opposite until then --
  // "stored CLEARTEXT ... there is no KMS in odin, so nothing here is encrypted
  // at rest" -- and that was true when written. The value is now sealed
  // AES-256-GCM by `gateway/kms.py` before it reaches the 0600 per-env sidecar,
  // under the key `KMS Key` names or the env's default key when it is blank.
  // Two consequences worth knowing before typing a real credential: the KEY
  // FILE (`.odin/{env}/kms.json`) sits beside the sidecar on the same disk, so
  // this defends against reading the sidecar, not against reading the
  // directory; and deleting the key DESTROYS this value, immediately.
  {
    type: 'secret', abbr: 'SEC', label: 'Secret', sublabel: 'Secrets Manager',
    category: 'Security', color: 'lime', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'description', label: 'Description', editable: true },
      { key: 'secretString', label: 'Value', editable: true },
      // Blank = the env's default key, which is a REAL key created on first
      // use, not "unencrypted". Naming a key that does not exist is a hard
      // error rather than a silent fallback, so a `kms -> secret` edge fills
      // this in (`spec/translate.py::_merge_encryption_edges`) instead of
      // asking the user to keep two places in step.
      { key: 'kmsKeyId', label: 'KMS Key', editable: true },
    ],
    defaultData: { label: 'new-secret', description: '', secretString: '', kmsKeyId: '' },
    iamActions: [
      'secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret',
      'secretsmanager:PutSecretValue', 'secretsmanager:*',
    ],
  },
  // W2.9: kms is REAL. It was an unbacked placeholder until then, and the
  // paragraph here said so at length -- that odin "has no KMS substitute" and
  // "the gateway classifies no `kms:*` action". Both are now false, which is the
  // caveat-outliving-its-fix failure .claude/CLAUDE.md's honesty rule 3 names,
  // so what it says instead is what the key actually does:
  //
  //   * WHAT IT ENCRYPTS: the two sidecars odin holds the plaintext of --
  //     Secrets Manager secrets and SSM parameters. `gateway/kms.py` keeps
  //     AES-256 material in a 0600 `.odin/{env}/kms.json` and every value is
  //     sealed AES-GCM before it reaches `secretsctl.json` / `ssmctl.json`.
  //     THIS IS ENCRYPTION WHERE THE DATA LIVES, not a claim that AWS would
  //     encrypt the same thing -- and the key file sits beside the sidecar on
  //     the same disk, so it defends against reading the sidecar, NOT against
  //     reading the directory. Say the smaller true thing.
  //   * WHAT IT DOES NOT: everything else. An S3 object, an RDS volume and a
  //     DynamoDB item are stored by real RustFS / Postgres / dynalite
  //     containers that odin does not hold the keys for, so drawing this at
  //     them encrypts nothing -- those pairs stay `unmodelled` on purpose.
  //     An RDS master password is likewise still stored and used in cleartext;
  //     nothing here changes that.
  //   * DELETING THE KEY DESTROYS THE DATA. `ScheduleKeyDeletion` is IMMEDIATE
  //     here (no 7-30 day window), and a secret sealed under a deleted key
  //     reads back as a 400 naming the key, never a blank value.
  //
  // The Name IS the key id: real `CreateKey` carries no name, so the label rides
  // in on the `odin:node` tag and the gateway keys by it. Rename the node and
  // you have a different key -- and any secret still naming the old one fails
  // loudly rather than falling back to the default.
  {
    type: 'kms', abbr: 'KMS', label: 'KMS Key', sublabel: 'Encryption key',
    category: 'Security', color: 'teal', width: 220,
    fields: [
      { key: 'label', label: 'Name (key ID)', editable: true },
      { key: 'description', label: 'Description', editable: true },
      // A FLAG, not a rotation: `kmsctl` records it and GetKeyRotationStatus
      // reports it back, but no material is re-derived and no ciphertext is
      // re-wrapped. Offered because the generated Terraform is portable, where
      // it means the real thing; never pre-ticked, because a default asserts a
      // protection odin has not got.
      { key: 'rotate', label: 'Key Rotation', editable: true, select: ['false', 'true'] },
    ],
    defaultData: { label: 'new-key', description: '', rotate: 'false' },
    // Every one of these has a real handler in `gateway/models/kmsctl.py` --
    // the bar `tests/gateway/test_iam_vocabulary_is_enforceable.py` and the ecr
    // precedent set. The grant/alias verbs odin models NO handler for
    // (CreateAlias, CreateGrant, ...) answer `InvalidAction` 400 and are
    // deliberately not offered: a tickable action the gateway cannot answer is
    // decorative, which is what got kinesis's list deleted outright.
    iamActions: ['kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey', 'kms:DescribeKey', 'kms:*'],
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
    // ONLY `ecr:GetAuthorizationToken` bites locally. The comment here used to
    // say the opposite -- that `_classify_ecr` building `ecr:<op>` from
    // `x-amz-target` "is what makes the grant bite rather than decorate" -- and
    // that was wrong twice over, in the direction this repo's honesty rules
    // exist for:
    //
    //   * `gateway/models/ecr.py::_HANDLERS` has SEVEN entries, and the three
    //     layer verbs below are not among them: the gateway answers
    //     `InvalidAction` 400. Classifiable is not the same as answerable.
    //   * The image bytes never reach the gateway at all. ecr.py's docstring:
    //     "The gateway does NOT proxy the registry's v2 HTTP protocol in this
    //     slice" -- a real `docker pull` dials the `registry:2` container's
    //     published port directly, `repositoryUri` merely making it findable.
    //     Nothing in `src/odin` ever sends any of the three (grep: zero hits).
    //
    // It cited `test_iam_vocabulary_is_enforceable.py` as proof, and that test
    // never checked it: its own docstring says only the SERVICE prefix can be
    // checked for target-derived services like ecr. So the claim was reviewed,
    // believed, and pinned by nothing -- honesty rule 1 exactly.
    // `tests/gateway/test_ecr_vocabulary_has_handlers.py` pins it properly now.
    //
    // The three stay TICKABLE rather than deleted because a drawn permission
    // becomes a real `aws_iam_role_policy` in the generated Terraform, and that
    // file is meant to be portable: taken to Amazon these are exactly the verbs
    // an image pull needs. They are not pre-ticked (`iam.ts::defaultPermissions`
    // offers only `GetAuthorizationToken`), because ticking something FOR the
    // user that odin cannot enforce is odin claiming a protection it has not got.
    iamActions: ['ecr:GetAuthorizationToken', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability', 'ecr:*'],
  },
  // REAL as of v0.8.19: an `aws_route53_zone` per node, plus an
  // `aws_route53_record` per edge drawn to an EC2 instance (`iam.ts`'s `dns`
  // edge). The substrate is real name resolution -- an `--add-host` entry on
  // every container in the env and an `/etc/hosts` line on every Lima VM in it.
  // The label IS the domain: `agent/hcl.py::_route53` emits `name = <label>` and
  // REFUSES a label that is not a valid DNS name rather than writing a record no
  // resolver could ever match.
  //
  // The sublabel STATES THE LIMIT rather than merely dropping `(placeholder)`,
  // because the limit is the design. A hosts entry is `<ip> <name>`: no port, no
  // scheme. Measured 2026-08-02 by running the real projectors in
  // `reconcile/tf_status.py`, exactly one kind publishes an address that shape
  // -- ec2's `PRIVATE_IP`. An alb publishes `http://127.0.0.1:<dynamic port>`
  // and rds a `host:port`, so a name pointing at either would resolve and then
  // fail to connect: a green resource that does not work, which is the failure
  // this repo's honesty rules exist to stop. Both are declined BY NAME on Apply
  // (`hcl.py::_dns_target_unsupported`), never silently dropped.
  //
  // "Resolves EC2" does NOT mean one address for everybody, and the sublabel is
  // careful to name the TARGET KIND rather than promise a value. The emitted
  // Terraform carries `aws_instance.<n>.private_ip` -- the portable, AWS-shaped
  // answer -- while odin's substrate resolves the name to whatever the CONSUMER
  // can reach: `private_ip` from a container or the host, the Nebula OVERLAY
  // address from another VM, and no hosts line at all for a VM in an env with no
  // mesh (a case odin REPORTS rather than silently skipping, since a name that
  // resolves nothing under a healthy badge is the bug this repo's honesty rules
  // are named after). That is forced rather than chosen: stock Lima `vz` NATs
  // each VM into its own isolated address space, so VM -> VM on a private_ip is
  // 100% packet loss before nebula is involved at all (`fabric/nebula.py`'s R5 note,
  // confirmed live with two real VMs). See `iam.ts`'s `dnsTargetTypes` note for
  // the full matrix; docs/limits.md states it as a measured limit.
  //
  // NO `iamActions`, deliberately, and both reasons are load-bearing:
  //   * HONESTY -- the argument `iam.ts` already records for `alb`. A workload
  //     does not resolve a name by making a signed AWS call; resolution reads a
  //     hosts file, which consults nobody and never reaches odin's gateway. The
  //     route53:* namespace is a CONTROL plane only tofu (the operator
  //     principal) ever touches, so a grant drawn here would gate nothing.
  //   * MECHANICAL -- `iam.ts::pairKey` SORTS, so `route53 -> ec2` and
  //     `ec2 -> route53` are ONE key. Declaring `iamActions` would put route53
  //     into `iamTargetTypes`, and the `computeTypes x iamTargets` loop would
  //     then register `iam` on that very key beside `dns`. The pair becomes
  //     AMBIGUOUS and `edge-ambiguity.test.ts` fails naming it. Verified by
  //     doing exactly that: it printed `ec2 -> route53: iam | dns` and
  //     `route53 -> ec2: iam | dns` and failed.
  //
  // The `zoneId` display field is GONE. Nothing ever wrote it -- there is no
  // route53 projector in `tf_status.py` and no hosted-zone id anywhere in
  // `src/odin` (grep: zero producers) -- because odin's substrate is a hosts
  // file and a hosts file has no zones. On a placeholder tile a permanently
  // blank read-only field was merely inert; on a REAL one it reads as "not
  // applied yet", which is the tile claiming a fact odin does not have. (`arn`
  // on the sqs/sns tiles is the same shape and equally unwritten: a pre-existing
  // defect, reported rather than propagated to one more tile.)
  {
    type: 'route53', abbr: 'DNS', label: 'Route 53 Zone',
    sublabel: 'Hosted zone (resolves EC2 only)',
    category: 'Networking', color: 'indigo', width: 200,
    fields: [{ key: 'label', label: 'Domain', editable: true }],
    defaultData: { label: 'example.com' },
  },
  {
    // REAL as of v0.8.19: a real nginx container per API (`compute/apigw.py`),
    // published on a real host port, whose `location` blocks are this API's
    // routes. Draw an edge to a lambda or an ecs service and `/<that node's
    // label>` is served -- for a lambda through odin's HTTP<->invoke-envelope
    // shim, for an ecs service as a plain reverse proxy to the running task.
    // The reachable address is the API's `api_endpoint`, NOT the stage's
    // `invoke_url` (the provider builds that one client-side from the api id
    // and it points at amazonaws.com -- measured; see docs/limits.md).
    //
    // `apiId` stays a read-only field: it is minted by the gateway on
    // CreateApi, so a user-typed value could only ever be ignored.
    type: 'apigateway', abbr: 'API', label: 'API Gateway', sublabel: 'HTTP API',
    category: 'Networking', color: 'fuchsia', width: 200,
    fields: [{ key: 'label', label: 'Name', editable: true }, { key: 'apiId', label: 'API ID' }],
    defaultData: { label: 'new-api', apiId: '' },
  },
  {
    // REAL as of v0.8.19, and what is really underneath is a shared HOST
    // DIRECTORY: one `.odin/<env>/gateway/efs/<fs-id>/` per file system,
    // bind-mounted into the container behind every ecs/lambda node the tile is
    // edged to (`iam.ts`'s `mount` edge -> `ContainerSpec.volumes` -> a real
    // `-v source:path`). Two tasks writing and reading one directory is what
    // "shared file system" has to mean, and that is what the e2e measures.
    //
    // `path` is the mount path INSIDE the consumer, and it is one field for the
    // whole node rather than one per edge, because a tile has nowhere to hang a
    // per-consumer value -- every consumer of one efs node mounts it at the same
    // place. Stated in docs/limits.md rather than left to be discovered.
    // The default is `/mnt/efs` because AWS's own `LocalMountPath` pattern
    // (`/mnt/[a-zA-Z0-9-_.]+`, read out of botocore rather than remembered)
    // allows exactly ONE segment under `/mnt`: `/mnt/efs` is legal, and
    // `/mnt/efs/data` is not.
    //
    // NO `iamActions`, and that is a decision rather than an omission -- the
    // same one `alb` and `iam_role` carry. An EFS mount is a kernel bind mount
    // performed by the container runtime; odin's gateway never sees a signed
    // request for it, so an `elasticfilesystem:ClientMount` offered here would
    // be a permission that cannot bite, which is the defect
    // `tests/gateway/test_iam_vocabulary_is_enforceable.py` exists to stop.
    // It is load-bearing a second way: the IAM loop in `iam.ts` registers every
    // `iamActionsForTarget` key against every compute kind, so declaring
    // actions here would give `efs <-> ecs` and `efs <-> lambda` a SECOND
    // meaning and fail `edge-ambiguity.test.ts` by name.
    //
    // The accent moved from `sky` to `fuchsia` in the same change, for a reason
    // that only exists once the tile is real: every edge type takes its colour
    // from its node's accent (`iam.ts`, the rule `sg` states for red and `role`
    // for amber), and `sky` (#38bdf8) is already the `target` edge. An
    // `alb -> ecs` target and an `efs -> ecs` mount meet on the same ECS node in
    // an ordinary canvas -- docs/SCENARIOS.md's S11 is exactly that graph -- and
    // two solid lines of one colour meaning two different things is the canvas
    // saying less than it knows. `fuchsia` is unused by any edge type.
    type: 'efs', abbr: 'EFS', label: 'EFS', sublabel: 'Elastic file system',
    category: 'Storage', color: 'fuchsia', width: 200,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'path', label: 'Mount Path', editable: true, placeholder: '/mnt/efs' },
    ],
    defaultData: { label: 'new-fs', path: '/mnt/efs' },
    primary: { key: 'path', label: 'Mount' },
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
  // match). A parameter can't exist without a Value, hence the placeholder
  // default rather than an empty one.
  //
  // ENCRYPTED AT REST as of W2.9, and the sentence this replaces was right
  // about the part that has NOT changed. It read: "SecureString is NOT
  // encrypted ... stored byte-for-byte like a String would be, CLEARTEXT".
  // Every value is now sealed AES-256-GCM before it reaches the 0600 per-env
  // sidecar -- but odin seals EVERY type, so `SecureString` still buys nothing
  // over `String`. The Type is AWS's own record of intent here, not the
  // protection; the KMS Key field is.
  {
    type: 'ssm', abbr: 'SSM', label: 'SSM Parameter', sublabel: 'Parameter store',
    category: 'Management', color: 'indigo', width: 220,
    fields: [
      { key: 'label', label: 'Name', editable: true },
      { key: 'paramType', label: 'Type', editable: true, select: ['String', 'StringList', 'SecureString'] },
      { key: 'paramValue', label: 'Value', editable: true },
      // Same as the secret's `kmsKeyId`, under the name the AWS API and the
      // terraform provider both use for a parameter (`key_id`).
      { key: 'keyId', label: 'KMS Key', editable: true },
    ],
    defaultData: { label: '/odin/param', paramType: 'String', paramValue: 'changeme', keyId: '' },
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
    // REAL as of v0.8.18: a real `limactl disk` volume, attached to a real
    // Lima VM through its `additionalDisks:`. Drawn to an ec2 node it emits an
    // `aws_volume_attachment` (`iam.ts`'s `volume` edge). What the guest
    // actually gets is `/dev/vdb`, not the `/dev/sdf` the attachment asks for
    // -- see docs/limits.md, which states the measurement rather than the
    // wish. `az` and `size` map straight onto `aws_ebs_volume`.
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
