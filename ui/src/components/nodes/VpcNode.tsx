import { type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';

export type VpcNodeData = {
  label: string;
  resourceId: string;
  cidr: string;
  status: string;
};

export default function VpcNode({ data, selected }: NodeProps) {
  const { label, resourceId, cidr, status } = data as VpcNodeData;
  return (
    <div className="w-full h-full border border-neon-purple bg-[rgba(170,85,255,0.03)]">
      <NodeResizer
        isVisible={selected}
        minWidth={280}
        minHeight={100}
        lineClassName="!border-neon-purple"
        handleClassName="!bg-neon-purple !border-none !w-2 !h-2"
      />
      <div className="flex items-center gap-2 px-3 h-10 border-b border-[rgba(170,85,255,0.3)] text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-purple shrink-0">VPC</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} error={(data as { error?: string }).error} />
      </div>
      <div className="flex items-center px-3 h-5 font-mono text-[10px] text-text-secondary">
        {resourceId} &bull; {cidr}
      </div>
    </div>
  );
}
