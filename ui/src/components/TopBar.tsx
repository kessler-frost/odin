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

interface TopBarProps {
  wsConnected?: boolean;
  onValidate?: () => Promise<void>;
  onSimulate?: () => Promise<void>;
  onSimulateDestroy?: () => Promise<void>;
  onReset?: () => void;
}

export default function TopBar({ wsConnected, onValidate, onSimulate, onSimulateDestroy, onReset }: TopBarProps) {
  const [validating, setValidating] = useState(false);
  const [backendUp, setBackendUp] = useState(false);
  const [agentUp, setAgentUp] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);

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

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as HTMLElement)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [menuOpen]);

  const handleValidate = async () => {
    setValidating(true);
    await onValidate?.();
    setValidating(false);
  };

  const backendLed: LedState = backendUp ? 'green' : 'red';
  const wsLed: LedState = wsConnected ? 'green' : backendUp ? 'yellow' : 'red';
  const agentLed: LedState = agentUp ? 'green' : backendUp ? 'yellow' : 'red';

  return (
    <div className="bg-bg-secondary border-b border-border-bright flex items-center px-4 gap-4 h-12">
      <div className="font-mono font-bold text-lg text-neon-green tracking-[4px] uppercase [text-shadow:0_0_10px_rgba(0,255,136,0.3)]">
        Odin
      </div>
      <div className="w-px h-6 bg-border"></div>
      <div className="flex gap-3 font-mono text-[11px]">
        <div className="flex items-center gap-1.5 text-text-secondary">
          <Led state={backendLed} />
          Backend
        </div>
        <div className="flex items-center gap-1.5 text-text-secondary">
          <Led state={wsLed} />
          WebSocket
        </div>
        <div className="flex items-center gap-1.5 text-text-secondary">
          <Led state={agentLed} />
          Agent
        </div>
      </div>
      <div className="flex-1"></div>
      <button
        onClick={handleValidate}
        disabled={validating}
        className={`font-mono text-xs py-1.5 px-4 border border-neon-blue bg-bg-tertiary text-neon-blue cursor-pointer uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(51,153,255,0.1)] hover:shadow-[0_0_12px_rgba(51,153,255,0.2)] ${validating ? 'opacity-50 cursor-wait' : ''}`}
      >
        {validating ? 'Validating...' : 'Validate'}
      </button>
      <button
        onClick={() => onSimulate?.()}
        title="Run the canvas for real as local Lima VMs + containers (heavy)"
        className="font-mono text-xs py-1.5 px-4 border border-neon-purple bg-bg-tertiary text-neon-purple cursor-pointer uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(170,85,255,0.1)] hover:shadow-[0_0_12px_rgba(170,85,255,0.2)]"
      >
        Simulate
      </button>
      <button
        onClick={() => onSimulateDestroy?.()}
        title="Tear down everything Simulate created (VMs + containers)"
        className="font-mono text-xs py-1.5 px-4 border border-neon-red bg-bg-tertiary text-neon-red cursor-pointer uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(255,51,85,0.1)] hover:shadow-[0_0_12px_rgba(255,51,85,0.2)]"
      >
        Destroy
      </button>
      <div className="relative" ref={menuRef}>
        <button
          onClick={() => setMenuOpen(!menuOpen)}
          className="font-mono text-xs py-1.5 px-3 border border-border-bright bg-bg-tertiary text-text-muted cursor-pointer transition-all duration-200 hover:bg-bg-hover hover:text-text-primary hover:border-border-bright"
        >
          &middot;&middot;&middot;
        </button>
        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 bg-bg-secondary border border-border-bright z-50 min-w-[160px] shadow-lg">
            <button
              onClick={() => { onReset?.(); setMenuOpen(false); }}
              className="w-full text-left font-mono text-xs py-2 px-4 text-text-secondary hover:bg-bg-tertiary hover:text-neon-red transition-colors uppercase tracking-[1px]"
            >
              Reset
            </button>
            <button
              onClick={() => setMenuOpen(false)}
              className="w-full text-left font-mono text-xs py-2 px-4 text-text-secondary hover:bg-bg-tertiary hover:text-neon-blue transition-colors uppercase tracking-[1px]"
            >
              Export
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
