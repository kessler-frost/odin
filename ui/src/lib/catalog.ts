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
