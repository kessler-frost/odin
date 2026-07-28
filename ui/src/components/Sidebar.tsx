import { BUILTINS, COLORS, PALETTE } from '../lib/catalog';

type Item = { abbr: string; label: string; sublabel: string; category: string; iconClass: string };

// The bespoke-node resources, from the single list in catalog.ts (Canvas.tsx
// derives its `nodeTypeMap` from the same one). Catalog services are appended.
const builtins: Item[] = BUILTINS;

// `PALETTE` is `CATALOG` minus the `(placeholder)` kinds -- tiles that drag
// onto the canvas, draw like a real resource, and are then silently skipped by
// Apply. Hidden until each has a real substitute (owner call, 2026-07-27; see
// ROADMAP). Palette-only: `CATALOG` still holds them so an already-saved
// canvas containing one keeps rendering properly.
const catalogItems: Item[] = PALETTE.map((s) => ({
  abbr: s.abbr,
  label: s.label,
  sublabel: s.sublabel,
  category: s.category,
  iconClass: `${COLORS[s.color].text} ${COLORS[s.color].border}`,
}));

// Workloads first, then the data/AWS-shaped resources.
const CATEGORY_ORDER = ['Compute', 'Storage', 'Database', 'Integration', 'Networking', 'Security', 'Monitoring', 'Management'];

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
