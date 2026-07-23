import { Handle, Position, type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';

// V3c: adapted from the pre-ripout Ec2Node (git show 82a064e~1) for the
// real-Lima-VM model -- instanceType/ami/key/userData/securityGroups are
// USER fields (hcl.py's aws_instance/aws_key_pair builders read them);
// vpc/subnet are read-only containment stamps (lib/containment.ts), same
// convention as SgNode.
export type Ec2NodeData = {
  label: string;
  instanceType: string;
  ami: string;
  key: string;
  userData: string;
  securityGroups: string;
  status: string;
  vpc?: string;
  subnet?: string;
};

export default function Ec2Node({ data, selected }: NodeProps) {
  const { label, instanceType, status, vpc, subnet } = data as Ec2NodeData;
  const containedIn = subnet ?? vpc;
  return (
    <div className="w-full h-full border border-neon-orange bg-bg-secondary shadow-[0_0_15px_rgba(255,136,0,0.08)]">
      <NodeResizer
        isVisible={selected}
        minWidth={200}
        minHeight={60}
        lineClassName="!border-neon-orange"
        handleClassName="!bg-neon-orange !border-none !w-2 !h-2"
      />
      <Handle id="left" type="source" position={Position.Left} className="!bg-neon-orange !border-none !w-1.5 !h-1.5" />
      <Handle id="right" type="source" position={Position.Right} className="!bg-neon-orange !border-none !w-1.5 !h-1.5" />
      <Handle id="top" type="source" position={Position.Top} className="!bg-neon-orange !border-none !w-1.5 !h-1.5" />
      <Handle id="bottom" type="source" position={Position.Bottom} className="!bg-neon-orange !border-none !w-1.5 !h-1.5" />
      <div className="flex items-center gap-2 px-3 h-10 border-b border-[rgba(255,136,0,0.3)] text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-orange shrink-0">EC2</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} error={(data as { error?: string }).error} />
      </div>
      <div className="flex flex-col justify-center px-3 h-10 font-mono text-[10px] text-text-secondary leading-tight">
        <span>{instanceType || 't3.micro'}</span>
        {containedIn && <span className="text-neon-purple/70">in {containedIn}</span>}
      </div>
    </div>
  );
}
