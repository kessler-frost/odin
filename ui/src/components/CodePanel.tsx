import { useState, useEffect } from 'react';

// Shape of POST /translate?env= — renders the canvas to HCL without applying it.
interface TranslateResult {
  files: Record<string, string>;
  notes: string[];
  unsupported: string[];
  refined: boolean;
}

interface CodePanelProps {
  env: string;
  onClose?: () => void;
}

// Read-only viewer for the Terraform generated from the canvas. Viewing code
// never triggers an apply — /translate only renders HCL; /apply-full runs it.
export default function CodePanel({ env, onClose }: CodePanelProps) {
  const [result, setResult] = useState<TranslateResult | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setFailed(false);
    fetch(`/translate?env=${encodeURIComponent(env)}`, { method: 'POST' })
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(d => { if (!cancelled) setResult(d); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [env]);

  const files = Object.entries(result?.files ?? {});

  return (
    <div className="h-full bg-bg-secondary border-l border-border-bright flex flex-col overflow-hidden shadow-[-8px_0_24px_rgba(0,0,0,0.5)]">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border flex items-center gap-2 shrink-0">
        <span className="font-mono text-[10px] py-0.5 px-2 border border-neon-purple text-neon-purple bg-[rgba(170,85,255,0.1)] uppercase">
          TF
        </span>
        <span className="font-mono text-xs font-semibold uppercase tracking-[2px] text-text-primary">Terraform</span>
        <span className="font-mono text-[10px] text-text-muted">{env}</span>
        {result?.refined && (
          <span
            className="font-mono text-[9px] py-0.5 px-1.5 border border-neon-orange/50 text-neon-orange uppercase tracking-[1px]"
            title="The translation agent refined the deterministic skeleton"
          >
            AI-refined
          </span>
        )}
        <div className="flex-1" />
        <button
          onClick={onClose}
          title="Hide Terraform"
          className="font-mono text-xs py-0.5 px-2 text-text-muted border border-transparent hover:text-text-primary hover:border-border-bright cursor-pointer transition-colors"
        >
          &#x2715;
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {failed && (
          <div className="font-mono text-[11px] text-neon-red">Translate failed — backend unreachable.</div>
        )}
        {!failed && !result && (
          <div className="font-mono text-[11px] text-text-muted animate-pulse">Translating…</div>
        )}
        {result && result.unsupported.length > 0 && (
          <div className="space-y-1">
            {result.unsupported.map((u, i) => (
              <div key={i} className="font-mono text-[10px] text-neon-yellow/80">Not simulated: {u}</div>
            ))}
          </div>
        )}
        {result && result.notes.length > 0 && (
          <div className="space-y-1">
            {result.notes.map((n, i) => (
              <div key={i} className="font-mono text-[10px] text-text-muted">{n}</div>
            ))}
          </div>
        )}
        {result && files.length === 0 && (
          <div className="font-mono text-[11px] text-text-muted">
            Nothing to translate — no Terraform-supported resources on the canvas.
          </div>
        )}
        {files.map(([path, text]) => (
          <div key={path}>
            <div className="font-mono text-[10px] text-text-secondary tracking-[1px] pb-1 mb-1 border-b border-border">{path}</div>
            <pre className="bg-bg-primary border border-border p-3 font-mono text-[11px] leading-relaxed text-text-secondary overflow-x-auto whitespace-pre">
              {text}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}
