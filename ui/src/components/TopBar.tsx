import { useState, useEffect, useRef } from 'react';

const API = '';

type LedState = 'green' | 'yellow' | 'red';

const ledStyles: Record<LedState, string> = {
  green: 'bg-neon-green shadow-[0_0_6px_rgba(0,255,136,0.5)]',
  yellow: 'bg-neon-yellow shadow-[0_0_6px_rgba(255,221,0,0.5)] animate-pulse',
  red: 'bg-neon-red shadow-[0_0_6px_rgba(255,51,85,0.5)]',
};

function Led({ state }: { state: LedState }) {
  return <div className={`w-1.5 h-1.5 rounded-full ${ledStyles[state]}`} />;
}

type Busy = null | 'apply';

interface TopBarProps {
  wsConnected?: boolean;
  env?: string;
  onEnvChange?: (env: string) => void;
  onApply?: () => Promise<void>;
  onViewCode?: () => void;
  codeOpen?: boolean;
}

export default function TopBar({ wsConnected, env, onEnvChange, onApply, onViewCode, codeOpen }: TopBarProps) {
  const [busy, setBusy] = useState<Busy>(null);
  const [backendUp, setBackendUp] = useState(false);
  // Reconcilers that have stopped converging (GET /health's `reconcilers`).
  // A dead loop leaves every badge on the canvas frozen at its last reading
  // while the Backend LED stays green -- which is exactly what made it
  // invisible -- so it gets its own chip, off the live 5s /health poll rather
  // than the /world read the canvas only makes on mount.
  const [deadLoops, setDeadLoops] = useState<{ env: string; verdict: string }[]>([]);
  const [envs, setEnvs] = useState<string[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const poll = async () => {
      const res = await fetch(`${API}/health`).then(r => r.json()).catch(() => null);
      if (!mountedRef.current) return;
      setBackendUp(!!res);
      const loops: { env: string; ticking: boolean; verdict: string | null }[] = res?.reconcilers ?? [];
      setDeadLoops(
        loops.filter(l => !l.ticking).map(l => ({ env: l.env, verdict: l.verdict ?? 'not converging' })),
      );
    };
    poll();
    const interval = setInterval(poll, 5000);
    return () => { mountedRef.current = false; clearInterval(interval); };
  }, []);

  // Discover existing environments so the env field can autocomplete them.
  const loadEnvs = () => fetch(`${API}/envs`).then(r => r.json()).then(d => setEnvs(d.envs ?? [])).catch(() => {});
  useEffect(() => { loadEnvs(); }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as HTMLElement)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [menuOpen]);

  const run = (action: Exclude<Busy, null>, fn?: () => Promise<void>) => async () => {
    if (busy) return;
    setBusy(action);
    try { await fn?.(); } finally { if (mountedRef.current) setBusy(null); }
  };

  // Cmd/Ctrl+Enter is the commit chord -> Apply.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !busy) run('apply', onApply)();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  });

  // The agent's conversation lives in the SERVER's memory, per env, and is
  // cleared by a restart or by this. Tucked in the overflow menu on purpose
  // (owner: "I don't want to see it unless I actually want to go there") --
  // it is a rare, deliberate act, not a control to keep in view.
  const [cleared, setCleared] = useState<number | null>(null);
  const clearChat = async () => {
    setMenuOpen(false);
    const body = await fetch(`${API}/chat/clear?env=${encodeURIComponent(env || 'default')}`, {
      method: 'POST',
    }).then(r => r.json()).catch(() => null);
    if (!body) return;
    // Say what happened. A menu item that silently does nothing visible is
    // indistinguishable from one that failed.
    setCleared(body.turns_forgotten ?? 0);
    setTimeout(() => setCleared(null), 4000);
  };

  const exportCanvas = async () => {
    setMenuOpen(false);
    const canvas = await fetch(`${API}/canvas`).then(r => r.json()).catch(() => null);
    if (!canvas) return;
    const blob = new Blob([JSON.stringify(canvas, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${env || 'default'}-canvas.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const backendLed: LedState = backendUp ? 'green' : 'red';
  const wsLed: LedState = wsConnected ? 'green' : backendUp ? 'yellow' : 'red';

  const disabled = !!busy || !backendUp;
  const dim = disabled ? 'opacity-40 cursor-not-allowed' : '';

  return (
    <div className="bg-bg-secondary border-b border-border-bright flex items-center px-4 gap-4 h-12">
      <div className="font-mono font-bold text-lg text-neon-green tracking-[4px] uppercase [text-shadow:0_0_10px_rgba(0,255,136,0.3)]">
        Odin
      </div>
      <div className="w-px h-6 bg-border"></div>
      <div className="flex gap-3 font-mono text-[11px]">
        <div className="flex items-center gap-1.5 text-text-secondary" title={backendUp ? 'Backend reachable' : 'Backend unreachable'}>
          <Led state={backendLed} />
          Backend
        </div>
        <div className="flex items-center gap-1.5 text-text-secondary" title={wsConnected ? 'Live status connected' : 'WebSocket reconnecting'}>
          <Led state={wsLed} />
          WebSocket
        </div>
        {deadLoops.length > 0 && (
          <div
            title={deadLoops.map(l => l.verdict).join('\n\n')}
            className="flex items-center gap-1.5 h-5 px-2 border border-neon-red bg-[rgba(255,51,85,0.08)] text-neon-red uppercase tracking-[1px] text-[10px] leading-5"
          >
            <Led state="red" />
            Reconciler down: {deadLoops.map(l => l.env).join(', ')}
          </div>
        )}
      </div>
      <div className="flex-1"></div>
      <input
        value={env ?? 'default'}
        list="env-list"
        onChange={(e) => onEnvChange?.(e.target.value)}
        onFocus={loadEnvs}
        onBlur={(e) => { if (!e.target.value.trim()) onEnvChange?.('default'); }}
        title="Environment — an isolated copy (own AWS state). Type a new name to fork one."
        className="font-mono text-xs py-1.5 px-2 w-28 bg-bg-tertiary border border-border-bright text-text-secondary focus:text-neon-green focus:border-neon-green outline-none"
      />
      <datalist id="env-list">
        {envs.map(e => <option key={e} value={e} />)}
      </datalist>
      <button
        onClick={run('apply', onApply)}
        disabled={disabled}
        title="Run the canvas for real (⌘↵): containers via Colima, AWS-shaped resources on real open-source backings"
        className={`font-mono text-xs py-1.5 px-4 border border-neon-green bg-bg-tertiary text-neon-green uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(0,255,136,0.1)] hover:shadow-[0_0_12px_rgba(0,255,136,0.2)] ${busy === 'apply' ? 'opacity-50 cursor-wait' : dim || 'cursor-pointer'}`}
      >
        {busy === 'apply' ? 'Applying…' : 'Apply'}
      </button>
      <button
        onClick={onViewCode}
        disabled={!backendUp}
        title="View the generated Terraform for this canvas"
        className={`font-mono text-xs py-1.5 px-3 border bg-bg-tertiary transition-all duration-200 ${codeOpen ? 'border-neon-purple text-neon-purple bg-[rgba(170,85,255,0.1)]' : 'border-border-bright text-text-muted hover:bg-bg-hover hover:text-text-primary'} ${!backendUp ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
      >
        {'{ }'}
      </button>
      <div className="relative" ref={menuRef}>
        <button
          onClick={() => setMenuOpen(!menuOpen)}
          className="font-mono text-xs py-1.5 px-3 border border-border-bright bg-bg-tertiary text-text-muted cursor-pointer transition-all duration-200 hover:bg-bg-hover hover:text-text-primary hover:border-border-bright"
        >
          &middot;&middot;&middot;
        </button>
        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 bg-bg-secondary border border-border-bright z-50 min-w-[180px] shadow-lg">
            <button
              onClick={exportCanvas}
              className="w-full text-left font-mono text-xs py-2 px-4 text-text-secondary hover:bg-bg-tertiary hover:text-neon-blue transition-colors uppercase tracking-[1px]"
            >
              Export Canvas
            </button>
            <button
              onClick={clearChat}
              title="Forget the agent's conversation for this environment. Your canvas is untouched."
              className="w-full text-left font-mono text-xs py-2 px-4 text-text-secondary hover:bg-bg-tertiary hover:text-neon-purple transition-colors uppercase tracking-[1px]"
            >
              Clear Agent Session
            </button>
          </div>
        )}
        {cleared !== null && (
          <div className="absolute right-0 top-full mt-1 bg-bg-secondary border border-neon-purple z-50 py-2 px-4 whitespace-nowrap font-mono text-xs text-neon-purple uppercase tracking-[1px]">
            forgot {cleared} turn{cleared === 1 ? '' : 's'} — canvas untouched
          </div>
        )}
      </div>
    </div>
  );
}
