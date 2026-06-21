import { Handle, Position, type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';
import { catalogByType, COLORS } from '../../lib/catalog';

// One component renders every catalog (Phase-5) leaf service; appearance comes
// from the service's catalog entry, not a bespoke component per service.
export default function ServiceNode({ type, data, selected }: NodeProps) {
  const def = catalogByType[type as string];
  const c = COLORS[def.color];
  const d = data as Record<string, string>;
  const detail = def.primary ? `${def.primary.label}: ${d[def.primary.key] || '—'}` : (d.arn || '');
  return (
    <div className={`w-full h-full border ${c.border} bg-bg-secondary ${c.shadow}`}>
      <NodeResizer
        isVisible={selected}
        minWidth={def.width}
        minHeight={60}
        lineClassName={c.line}
        handleClassName={`${c.handle} !border-none !w-2 !h-2`}
      />
      <Handle id="left" type="source" position={Position.Left} className={`${c.handle} !border-none !w-1.5 !h-1.5`} />
      <Handle id="right" type="source" position={Position.Right} className={`${c.handle} !border-none !w-1.5 !h-1.5`} />
      <Handle id="top" type="source" position={Position.Top} className={`${c.handle} !border-none !w-1.5 !h-1.5`} />
      <Handle id="bottom" type="source" position={Position.Bottom} className={`${c.handle} !border-none !w-1.5 !h-1.5`} />
      <div
        className="flex items-center gap-2 px-3 h-10 border-b text-xs font-semibold overflow-hidden whitespace-nowrap"
        style={{ borderColor: `rgba(${c.rgb},0.3)` }}
      >
        <span className={`${c.text} shrink-0`}>{def.abbr}</span>
        <span className="truncate">{d.label}</span>
        <StatusBadge status={d.status} error={d.error} />
      </div>
      <div className="flex items-center px-3 h-5 font-mono text-[10px] text-text-secondary truncate">
        {detail}
      </div>
    </div>
  );
}
