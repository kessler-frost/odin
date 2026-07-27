import type { ReactNode } from 'react';

// The block under a node's header. Nodes size themselves to their CONTENT, so
// this renders one 20px grid row per line it was given and renders NOTHING at
// all when every line is empty -- a node with nothing to say is a 40px header
// with no orphan separator under it.
//
// Why it matters: node heights used to be frozen at whatever ReactFlow
// measured on first render (Canvas persisted `measured.height`), so a box that
// later GAINED a line clipped it -- `lambda_function.lambda_handler` rendered
// below its own border -- while a box that had nothing kept a dead 20px strip.
//
// The separator is an INSET SHADOW, not a border, because a 1px border on an
// auto-height element adds a 1px row to the node and puts it off the 20px grid
// (measured: a two-row lambda came to 81px). A shadow draws the same line and
// costs no layout, so heights stay exactly 40 + 20*rows.
//
// Rows truncate rather than wrap, so one long ARN cannot silently redefine the
// node's geometry; the full value stays reachable via the title tooltip.
export default function NodeMeta({ rows, rgb }: { rows: ReactNode[]; rgb: string }) {
  const shown = rows.filter(Boolean);
  if (!shown.length) return null;
  return (
    <div className="px-3 font-mono text-[10px] text-text-secondary" style={{ boxShadow: `inset 0 1px 0 rgba(${rgb},0.3)` }}>
      {shown.map((row, i) => (
        <div key={i} className="h-5 leading-5 truncate" title={typeof row === 'string' ? row : undefined}>{row}</div>
      ))}
    </div>
  );
}
