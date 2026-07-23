import { type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';

export type SubnetNodeData = {
  label: string;
  resourceId: string;
  cidr: string;
  status: string;
  vpc?: string; // containment-stamped: the VPC whose rect fully encloses this subnet
};

export default function SubnetNode({ data, selected }: NodeProps) {
  const { label, resourceId, cidr, status, vpc } = data as SubnetNodeData;
  return (
    // pointer-events-none on the root (border + fill included) so edges/nodes
    // routed through the Subnet's rect stay clickable underneath it. pointer-events
    // is inherited, so the header/meta/resizer below each opt back IN with their
    // own `pointer-events-auto` — that's what keeps drag/select/resize working.
    <div className="relative w-full h-full border border-neon-blue bg-[rgba(0,187,255,0.02)] pointer-events-none">
      <div className="pointer-events-auto">
        <NodeResizer
          isVisible={selected}
          minWidth={260}
          minHeight={80}
          lineClassName="!border-neon-blue"
          handleClassName="!bg-neon-blue !border-none !w-2 !h-2"
        />
      </div>
      <div className="pointer-events-auto flex items-center gap-2 px-3 h-10 border-b border-[rgba(0,187,255,0.3)] text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-blue shrink-0">Subnet</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} error={(data as { error?: string }).error} />
      </div>
      <div className="pointer-events-auto flex items-center px-3 h-5 font-mono text-[10px] text-text-secondary whitespace-nowrap overflow-hidden">
        {resourceId} &bull; {cidr}
        {vpc && <span className="text-neon-purple/70">&nbsp;&bull; in {vpc}</span>}
      </div>
    </div>
  );
}
