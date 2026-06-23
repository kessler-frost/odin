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

type Busy = null | 'apply' | 'preview' | 'destroy';

interface TopBarProps {
  wsConnected?: boolean;
  env?: string;
  onEnvChange?: (env: string) => void;
  onPreview?: () => Promise<void>;
  onApply?: () => Promise<void>;
  onDestroy?: () => Promise<void>;
  onReset?: () => void;
}

export default function TopBar({ wsConnected, env, onEnvChange, onPreview, onApply, onDestroy, onReset }: TopBarProps) {
  const [busy, setBusy] = useState<Busy>(null);
  const [armed, setArmed] = useState<null | 'destroy' | 'reset'>(null);
  const [backendUp, setBackendUp] = useState(false);
  const [agentUp, setAgentUp] = useState(false);
  const [envs, setEnvs] = useState<string[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  const armTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    const poll = async () => {
      const res = await fetch(`${API}/health`).then(r => r.json()).catch(() => null);
      if (!mountedRef.current) return;
      setBackendUp(!!res);
      setAgentUp(res?.agent ?? false);
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

  // Click-to-arm for the two irreversible actions: first click arms (~2.5s), second commits.
  const onDanger = (which: 'destroy' | 'reset', commit: () => void) => () => {
    if (armTimer.current) clearTimeout(armTimer.current);
    if (armed === which) { setArmed(null); commit(); return; }
    setArmed(which);
    armTimer.current = setTimeout(() => mountedRef.current && setArmed(null), 2500);
  };

  // Cmd/Ctrl+Enter is the commit chord -> Apply.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !busy) run('apply', onApply)();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  });

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
  const agentLed: LedState = agentUp ? 'green' : backendUp ? 'yellow' : 'red';

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
        <div className="flex items-center gap-1.5 text-text-secondary" title={agentUp ? 'Agent ready' : 'Agent unavailable'}>
          <Led state={agentLed} />
          Agent
        </div>
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
        onClick={run('preview', onPreview)}
        disabled={disabled}
        title="Preview the AI's proposed config changes before applying"
        className={`font-mono text-xs py-1.5 px-3 border border-border-bright bg-bg-tertiary text-text-secondary uppercase tracking-[1px] transition-all duration-200 hover:text-neon-blue hover:border-neon-blue ${busy === 'preview' ? 'opacity-50 cursor-wait' : dim || 'cursor-pointer'}`}
      >
        {busy === 'preview' ? 'Preview…' : 'Preview'}
      </button>
      <button
        onClick={run('apply', onApply)}
        disabled={disabled}
        title="Run the canvas for real (⌘↵): containers via Colima, AWS via embedded MiniStack"
        className={`font-mono text-xs py-1.5 px-4 border border-neon-green bg-bg-tertiary text-neon-green uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(0,255,136,0.1)] hover:shadow-[0_0_12px_rgba(0,255,136,0.2)] ${busy === 'apply' ? 'opacity-50 cursor-wait' : dim || 'cursor-pointer'}`}
      >
        {busy === 'apply' ? 'Applying…' : 'Apply'}
      </button>
      <button
        onClick={onDanger('destroy', () => run('destroy', onDestroy)())}
        disabled={!!busy || !backendUp}
        title="Tear down everything (containers + AWS resources) for this env"
        className={`font-mono text-xs py-1.5 px-4 border border-neon-red bg-bg-tertiary text-neon-red uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(255,51,85,0.1)] hover:shadow-[0_0_12px_rgba(255,51,85,0.2)] ${armed === 'destroy' ? 'bg-[rgba(255,51,85,0.15)] shadow-[0_0_12px_rgba(255,51,85,0.3)]' : ''} ${(!!busy || !backendUp) ? dim : 'cursor-pointer'}`}
      >
        {busy === 'destroy' ? 'Destroying…' : armed === 'destroy' ? `Confirm? (${env})` : 'Destroy'}
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
              onClick={onDanger('reset', () => { onReset?.(); setMenuOpen(false); })}
              className={`w-full text-left font-mono text-xs py-2 px-4 hover:bg-bg-tertiary transition-colors uppercase tracking-[1px] ${armed === 'reset' ? 'text-neon-red bg-[rgba(255,51,85,0.08)]' : 'text-text-secondary hover:text-neon-red'}`}
            >
              {armed === 'reset' ? 'Confirm reset?' : 'Reset Canvas'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
