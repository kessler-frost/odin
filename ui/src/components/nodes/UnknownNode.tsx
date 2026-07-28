import { Handle, Position } from '@xyflow/react';

/**
 * A node whose `type` odin does not recognise.
 *
 * Without this, ReactFlow falls back to its own `default` node and draws a
 * blank white rectangle carrying only the label — indistinguishable from a
 * rendering bug in odin. Measured while regenerating the README hero: a canvas
 * saying `"type": "role"` (the catalog kind is `iam_role`) produced an
 * unlabelled white box, and I took it for odin mis-rendering the IAM role
 * before finding the typo in my own input.
 *
 * A canvas is a FIRST-CLASS hand-authored input — `odin canvas set` today, the
 * translation agent next — so an unrecognised kind has to announce itself. It
 * is deliberately drawn, not refused: an unknown kind is applied-and-skipped by
 * design (`skipped`/`not_covered` in the apply response), so the canvas is
 * valid, it just cannot be built. What was missing was any way to SEE that.
 *
 * The handles stay so an edge already pointing at this node still has somewhere
 * to attach; losing them would make the node's edges disappear too, turning one
 * legible problem into two confusing ones.
 */
export default function UnknownNode({ data }: { data: Record<string, unknown> }) {
  const label = (data?.label as string) || 'unnamed';
  const kind = (data?.__type as string) || 'unknown';
  return (
    <div className="bg-bg-secondary border border-neon-red" style={{ width: 200 }}>
      {(['left', 'right', 'top', 'bottom'] as const).map((side) => (
        <Handle
          key={side}
          id={side}
          type="source"
          position={Position[(side[0].toUpperCase() + side.slice(1)) as 'Left' | 'Right' | 'Top' | 'Bottom']}
          className="!bg-neon-red !border-none !w-1.5 !h-1.5"
        />
      ))}
      <div className="h-10 px-3 flex items-center gap-2">
        <span className="font-mono text-[10px] text-neon-red uppercase tracking-[1px]">?</span>
        <span className="font-mono text-[11px] text-text-primary truncate">{label}</span>
      </div>
      <div
        className="px-3 h-5 leading-5 font-mono text-[10px] text-neon-red truncate"
        style={{ boxShadow: 'inset 0 1px 0 rgba(255,51,85,0.3)' }}
        title={`odin does not recognise the kind "${kind}"`}
      >
        unknown kind: {kind}
      </div>
    </div>
  );
}
