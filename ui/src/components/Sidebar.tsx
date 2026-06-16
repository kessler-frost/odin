const resources = [
  { abbr: 'VPC', label: 'VPC', sublabel: 'Virtual Private Cloud', color: 'neon-purple' },
  { abbr: 'SUB', label: 'Subnet', sublabel: 'Network partition', color: 'neon-blue' },
  { abbr: 'SG', label: 'Security Group', sublabel: 'Firewall rules', color: 'neon-red' },
  { abbr: 'EC2', label: 'EC2 Instance', sublabel: 'Virtual machine', color: 'neon-orange' },
  { abbr: 'FN', label: 'Lambda', sublabel: 'Serverless function', color: 'neon-yellow' },
  { abbr: 'S3', label: 'S3 Bucket', sublabel: 'Object storage', color: 'neon-green' },
] as const;

const iconColors: Record<string, string> = {
  'neon-purple': 'text-neon-purple border-neon-purple',
  'neon-blue': 'text-neon-blue border-neon-blue',
  'neon-red': 'text-neon-red border-neon-red',
  'neon-orange': 'text-neon-orange border-neon-orange',
  'neon-yellow': 'text-neon-yellow border-neon-yellow',
  'neon-green': 'text-neon-green border-neon-green',
};

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
      {resources.map((r) => (
        <div
          key={r.abbr}
          draggable
          onDragStart={(e) => onDragStart(e, r.abbr)}
          className="flex items-center gap-3 py-2.5 px-4 cursor-grab border-l-2 border-transparent transition-all duration-150 hover:bg-bg-tertiary hover:border-l-neon-blue active:cursor-grabbing"
        >
          <div className={`w-8 h-8 border flex items-center justify-center font-mono text-[11px] font-semibold bg-bg-primary ${iconColors[r.color]}`}>
            {r.abbr}
          </div>
          <div>
            <div className="text-[13px] font-medium">{r.label}</div>
            <div className="text-[10px] text-text-muted font-mono">{r.sublabel}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
