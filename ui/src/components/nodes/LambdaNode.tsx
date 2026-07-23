import { Handle, Position, type NodeProps, NodeResizer } from '@xyflow/react';
import StatusBadge from './StatusBadge';

// V4c: adapted from the pre-ripout LambdaNode (git show 6411edf:ui/src/
// components/nodes/LambdaNode.tsx) for the real-RIE-container model --
// runtime/handler/code/role are USER fields (hcl.py's `_lambda` builder
// reads them; `code` is odin's own DX magic -- paste code, Apply, and odin
// zips + materializes it into the tofu workspace, no separate upload step).
// `role` is optional: leaving it blank gets an auto-generated basic
// execution role (see hcl.py's companion-role pass), same convention the
// pre-ripout node never had (that version predates V2's iam_role work).
export type LambdaNodeData = {
  label: string;
  runtime: string;
  handler: string;
  code: string;
  role: string;
  status: string;
};

export default function LambdaNode({ data, selected }: NodeProps) {
  const { label, runtime, handler, status } = data as LambdaNodeData;
  return (
    <div className="w-full h-full border border-neon-yellow bg-bg-secondary shadow-[0_0_15px_rgba(255,221,0,0.08)]">
      <NodeResizer
        isVisible={selected}
        minWidth={200}
        minHeight={60}
        lineClassName="!border-neon-yellow"
        handleClassName="!bg-neon-yellow !border-none !w-2 !h-2"
      />
      <Handle id="left" type="source" position={Position.Left} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <Handle id="right" type="source" position={Position.Right} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <Handle id="top" type="source" position={Position.Top} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <Handle id="bottom" type="source" position={Position.Bottom} className="!bg-neon-yellow !border-none !w-1.5 !h-1.5" />
      <div className="flex items-center gap-2 px-3 h-10 border-b border-[rgba(255,221,0,0.3)] text-xs font-semibold overflow-hidden whitespace-nowrap">
        <span className="text-neon-yellow shrink-0">λ</span>
        <span className="truncate">{label}</span>
        <StatusBadge status={status} error={(data as { error?: string }).error} />
      </div>
      <div className="flex flex-col justify-center px-3 h-10 font-mono text-[10px] text-text-secondary leading-tight">
        <span>{runtime || 'python3.12'}</span>
        <span className="text-text-muted truncate">{handler || 'lambda_function.lambda_handler'}</span>
      </div>
    </div>
  );
}
