import { Handle, Position, type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';
import NodeMeta from './NodeMeta';

export type SgNodeData = {
  label: string;
  groupId: string;
  vpcId: string;
  ingressRules: string;
  status: string;
  vpc?: string;    // containment-stamped: the VPC whose rect holds this node's center
  subnet?: string; // containment-stamped: the deepest subnet holding this node's center
};

export default function SgNode({ data, selected }: NodeProps) {
  const { label, groupId, vpcId, status, vpc, subnet } = data as SgNodeData;
  const containedIn = subnet ?? vpc;
  return (
    <div className="w-full min-h-full border border-neon-red bg-bg-secondary shadow-[0_0_15px_rgba(255,51,85,0.08)]">
      <NodeResizer
        isVisible={selected}
        minWidth={200}
        minHeight={40}
        lineClassName="!border-neon-red"
        handleClassName="!bg-neon-red !border-none !w-2 !h-2"
      />
      <Handle id="left" type="source" position={Position.Left} className="!bg-neon-red !border-none !w-1.5 !h-1.5" />
      <Handle id="right" type="source" position={Position.Right} className="!bg-neon-red !border-none !w-1.5 !h-1.5" />
      <Handle id="top" type="source" position={Position.Top} className="!bg-neon-red !border-none !w-1.5 !h-1.5" />
      <Handle id="bottom" type="source" position={Position.Bottom} className="!bg-neon-red !border-none !w-1.5 !h-1.5" />
      <div className="flex items-center gap-2 px-3 h-10 text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-red shrink-0">SG</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} error={(data as { error?: string }).error} />
      </div>
      <NodeMeta
        rgb="255,51,85"
        rows={[
          `${groupId || '—'} • ${vpcId || '—'}`,
          containedIn && <span className="text-neon-purple/70" title={`in ${containedIn}`}>in {containedIn}</span>,
        ]}
      />
    </div>
  );
}
