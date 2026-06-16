import { Handle, Position, type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';

export type S3NodeData = {
  label: string;
  arn: string;
  status: string;
};

export default function S3Node({ data, selected }: NodeProps) {
  const { label, arn, status } = data as S3NodeData;
  return (
    <div className="w-full h-full border border-neon-green bg-bg-secondary shadow-[0_0_15px_rgba(0,255,136,0.08)]">
      <NodeResizer
        isVisible={selected}
        minWidth={180}
        minHeight={60}
        lineClassName="!border-neon-green"
        handleClassName="!bg-neon-green !border-none !w-2 !h-2"
      />
      <Handle id="left" type="source" position={Position.Left} className="!bg-neon-green !border-none !w-1.5 !h-1.5" />
      <Handle id="right" type="source" position={Position.Right} className="!bg-neon-green !border-none !w-1.5 !h-1.5" />
      <Handle id="top" type="source" position={Position.Top} className="!bg-neon-green !border-none !w-1.5 !h-1.5" />
      <Handle id="bottom" type="source" position={Position.Bottom} className="!bg-neon-green !border-none !w-1.5 !h-1.5" />
      <div className="flex items-center gap-2 px-3 h-10 border-b border-[rgba(0,255,136,0.3)] text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-green shrink-0">S3</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} />
      </div>
      <div className="flex items-center px-3 h-5 font-mono text-[10px] text-text-secondary">
        {arn}
      </div>
    </div>
  );
}
