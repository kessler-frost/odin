import { Handle, Position, type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';

export type LambdaNodeData = {
  label: string;
  runtime: string;
  handler: string;
  memory: string;
  timeout: string;
  status: string;
};

export default function LambdaNode({ data, selected }: NodeProps) {
  const { label, runtime, handler, memory, timeout, status } = data as LambdaNodeData;
  return (
    <div className="w-full h-full border border-neon-yellow bg-bg-secondary shadow-[0_0_15px_rgba(255,221,0,0.08)]">
      <NodeResizer
        isVisible={selected}
        minWidth={200}
        minHeight={80}
        lineClassName="!border-neon-yellow"
        handleClassName="!bg-neon-yellow !border-none !w-2 !h-2"
      />
      <Handle id="left" type="source" position={Position.Left} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <Handle id="right" type="source" position={Position.Right} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <Handle id="top" type="source" position={Position.Top} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <Handle id="bottom" type="source" position={Position.Bottom} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <div className="flex items-center gap-2 px-3 h-10 border-b border-[rgba(255,221,0,0.3)] text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-yellow shrink-0">Lambda</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} error={(data as { error?: string }).error} />
      </div>
      <div className="flex flex-col justify-center px-3 h-10 font-mono text-[10px] text-text-secondary leading-tight">
        <span>{runtime} &bull; {handler}</span>
        <span>{memory} &bull; {timeout}</span>
      </div>
    </div>
  );
}
