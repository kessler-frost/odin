import { CATALOG, COLORS } from '../lib/catalog';

type Item = { abbr: string; label: string; sublabel: string; category: string; iconClass: string };

// Built-in (bespoke-node) resources. Catalog (Phase-5) services are appended.
const builtins: Item[] = [
  { abbr: 'VPC', label: 'VPC', sublabel: 'Virtual Private Cloud', category: 'Networking', iconClass: 'text-neon-purple border-neon-purple' },
  { abbr: 'SUB', label: 'Subnet', sublabel: 'Network partition', category: 'Networking', iconClass: 'text-neon-blue border-neon-blue' },
  { abbr: 'SG', label: 'Security Group', sublabel: 'Firewall rules', category: 'Networking', iconClass: 'text-neon-red border-neon-red' },
  { abbr: 'EC2', label: 'EC2 Instance', sublabel: 'Virtual machine', category: 'Compute', iconClass: 'text-neon-orange border-neon-orange' },
  { abbr: 'FN', label: 'Lambda', sublabel: 'Serverless function', category: 'Compute', iconClass: 'text-neon-yellow border-neon-yellow' },
  { abbr: 'S3', label: 'S3 Bucket', sublabel: 'Object storage', category: 'Storage', iconClass: 'text-neon-green border-neon-green' },
  { abbr: 'DDB', label: 'DynamoDB', sublabel: 'NoSQL table', category: 'Database', iconClass: 'text-neon-cyan border-neon-cyan' },
];

const catalogItems: Item[] = CATALOG.map((s) => ({
  abbr: s.abbr,
  label: s.label,
  sublabel: s.sublabel,
  category: s.category,
  iconClass: `${COLORS[s.color].text} ${COLORS[s.color].border}`,
}));

const CATEGORY_ORDER = ['Networking', 'Compute', 'Storage', 'Database', 'Integration', 'Security', 'Monitoring'];

const allItems = [...builtins, ...catalogItems];
const groups = CATEGORY_ORDER
  .map((category) => ({ category, items: allItems.filter((i) => i.category === category) }))
  .filter((g) => g.items.length > 0);

function onDragStart(event: React.DragEvent, abbr: string) {
  event.dataTransfer.setData('application/odin-resource', abbr);
  event.dataTransfer.effectAllowed = 'move';
}

interface SidebarProps {
  onCollapse?: () => void;
}

export default function Sidebar({ onCollapse }: SidebarProps) {
  return (
    <div className="bg-bg-secondary border-r border-border-bright py-4 overflow-y-auto h-full">
      <div
        onClick={onCollapse}
        className="flex items-center justify-between px-4 pb-3 cursor-pointer hover:opacity-70 transition-opacity"
        title="Hide Resources"
      >
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px]">
          Resources
        </div>
      </div>
      {groups.map((group) => (
        <div key={group.category} className="mb-1">
          <div className="px-4 pt-2 pb-1 font-mono text-[9px] text-text-muted/70 uppercase tracking-[2px]">
            {group.category}
          </div>
          {group.items.map((r) => (
            <div
              key={r.abbr}
              draggable
              onDragStart={(e) => onDragStart(e, r.abbr)}
              className="flex items-center gap-3 py-2.5 px-4 cursor-grab border-l-2 border-transparent transition-all duration-150 hover:bg-bg-tertiary hover:border-l-neon-blue active:cursor-grabbing"
            >
              <div className={`w-8 h-8 border flex items-center justify-center font-mono text-[11px] font-semibold bg-bg-primary ${r.iconClass}`}>
                {r.abbr}
              </div>
              <div>
                <div className="text-[13px] font-medium">{r.label}</div>
                <div className="text-[10px] text-text-muted font-mono">{r.sublabel}</div>
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
