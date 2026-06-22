import { useState, useEffect, useRef, useMemo, useCallback } from 'react';

const tabs = ["Agent", "Logs", "Events"];

interface LogLine {
  time: string;
  source: string;
  msg: string;
  msgClass: string;
}

const tabFilter: Record<string, (line: LogLine) => boolean> = {
  Agent: (line) => line.source === 'agent',
  Logs: (line) => line.source !== 'agent' && line.source !== 'event',
  Events: (line) => line.source === 'event',
};

const msgColors: Record<string, string> = {
  '': 'text-text-secondary',
  'success': 'text-neon-green',
  'warn': 'text-neon-yellow',
  'error': 'text-neon-red',
};

const spinnerWords = [
  'Pondering', 'Noodling', 'Vibing', 'Percolating', 'Simmering',
  'Canoodling', 'Booping', 'Lollygagging', 'Gallivanting', 'Frolicking',
  'Meandering', 'Ruminating', 'Cerebrating', 'Concocting', 'Brewing',
  'Marinating', 'Fermenting', 'Sprouting', 'Moonwalking', 'Grooving',
  'Shimmying', 'Scampering', 'Puttering', 'Tinkering', 'Doodling',
  'Spelunking', 'Waddling', 'Zigzagging', 'Flummoxing', 'Combobulating',
  'Shenaniganing', 'Tomfoolering', 'Razzle-dazzling', 'Whirring',
  'Skedaddling', 'Smooshing', 'Flibbertigibbeting', 'Beboppin\'',
  'Boogieing', 'Discombobulating',
];

function parseWebSocketMessage(msg: Record<string, unknown>): LogLine[] {
  const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const type = msg.type as string | undefined;

  if (type === 'agent_message') {
    const data = msg.data as Record<string, unknown> | undefined;
    const text = (data?.text ?? msg.text ?? '') as string;
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    return lines.map(l => ({ time, source: 'agent', msg: l, msgClass: '' }));
  }

  if (type === 'agent_done') return [];
  if (type === 'tool_use') return [];

  if (type === 'log') {
    const text = (msg.text ?? '') as string;
    return text ? [{ time, source: (msg.source ?? 'system') as string, msg: text, msgClass: (msg.level ?? '') as string }] : [];
  }

  if (type?.startsWith('resource_')) {
    const text = `${msg.name}: ${type.replace('resource_', '')}`;
    const msgClass = type.includes('error') ? 'error' : type.includes('live') ? 'success' : '';
    return [{ time, source: 'event', msg: text, msgClass }];
  }

  if (type === 'world_delta') {
    const phase = msg.phase as string;
    const msgClass = phase === 'healthy' ? 'success' : (phase === 'crashed' || phase === 'error') ? 'error' : '';
    return [{ time, source: 'event', msg: `${msg.resource_id}: ${phase}`, msgClass }];
  }

  return [];
}

type WsStatus = 'connected' | 'reconnecting' | 'disconnected';

function useWebSocket(onMessage: (msg: Record<string, unknown>) => void) {
  const [status, setStatus] = useState<WsStatus>('disconnected');
  const delayRef = useRef(1000);
  const wsRef = useRef<WebSocket | null>(null);
  const mountedRef = useRef(true);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${location.host}/ws`);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      setStatus('connected');
      delayRef.current = 1000;
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      onMessageRef.current(msg);
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setStatus('reconnecting');
      const delay = delayRef.current;
      delayRef.current = Math.min(delay * 2, 30000);
      setTimeout(connect, delay);
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
    };
  }, [connect]);

  return status;
}

const statusConfig: Record<WsStatus, { color: string; label: string; pulse: boolean }> = {
  connected: { color: 'bg-neon-green shadow-[0_0_6px_rgba(0,255,136,0.5)]', label: 'Connected', pulse: false },
  reconnecting: { color: 'bg-neon-yellow shadow-[0_0_6px_rgba(255,221,0,0.5)]', label: 'Reconnecting...', pulse: true },
  disconnected: { color: 'bg-neon-red shadow-[0_0_6px_rgba(255,51,85,0.5)]', label: 'Disconnected', pulse: false },
};

interface BottomPanelProps {
  bottomState: string;
  activeEnv?: string;
  onCycleBottom: () => void;
  onWsStatusChange?: (connected: boolean) => void;
  onResourceStatus?: (name: string, status: string, error?: string) => void;
  onConfigUpdate?: (nodeId: string, data: Record<string, any>) => void;
  clearSignal?: number;
}

export default function BottomPanel({ bottomState, activeEnv, onCycleBottom, onWsStatusChange, onResourceStatus, onConfigUpdate, clearSignal }: BottomPanelProps) {
  const [activeTab, setActiveTab] = useState("Agent");
  const [logs, setLogs] = useState<LogLine[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [agentActive, setAgentActive] = useState(false);
  const [spinnerWord, setSpinnerWord] = useState(spinnerWords[0]);

  useEffect(() => {
    fetch('/events')
      .then(r => r.json())
      .then((events: Record<string, unknown>[]) => {
        const lines = events.flatMap(parseWebSocketMessage);
        setLogs(lines.reverse());
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (clearSignal) setLogs([]);
  }, [clearSignal]);

  const onResourceStatusRef = useRef(onResourceStatus);
  onResourceStatusRef.current = onResourceStatus;
  const activeEnvRef = useRef(activeEnv);
  activeEnvRef.current = activeEnv;
  const onConfigUpdateRef = useRef(onConfigUpdate);
  onConfigUpdateRef.current = onConfigUpdate;

  const handleMessage = useCallback((msg: Record<string, unknown>) => {
    const type = msg.type as string | undefined;
    if (type === 'agent_message' || type === 'tool_use') setAgentActive(true);
    if (type === 'agent_done') { setAgentActive(false); return; }
    if (type === 'node_config_update' && msg.nodeId) {
      onConfigUpdateRef.current?.(msg.nodeId as string, (msg.data ?? {}) as Record<string, any>);
    }
    if (type?.startsWith('resource_')) {
      const name = msg.name as string;
      const status = type.replace('resource_', '');
      onResourceStatusRef.current?.(name, status, msg.error as string | undefined);
    }
    if (type === 'world_delta') {
      const env = (msg.env as string | undefined) ?? 'default';
      if (env === (activeEnvRef.current ?? 'default')) {  // only the active env's status
        onResourceStatusRef.current?.(
          msg.resource_id as string, msg.phase as string, msg.verdict as string | undefined,
        );
      }
    }
    const logLines = parseWebSocketMessage(msg);
    if (logLines.length) setLogs(prev => [...logLines, ...prev]);
  }, []);

  useEffect(() => {
    if (!agentActive) return;
    const interval = setInterval(() => {
      setSpinnerWord(spinnerWords[Math.floor(Math.random() * spinnerWords.length)]);
    }, 1000);
    return () => clearInterval(interval);
  }, [agentActive]);

  const wsStatus = useWebSocket(handleMessage);

  useEffect(() => {
    onWsStatusChange?.(wsStatus === 'connected');
  }, [wsStatus, onWsStatusChange]);
  const sc = statusConfig[wsStatus];

  const filteredLogs = useMemo(() => logs.filter(tabFilter[activeTab]), [logs, activeTab]);

  return (
    <div className="bg-bg-secondary border-t border-border-bright flex flex-col overflow-hidden h-full">
      <div className="flex border-b border-border items-center">
        {tabs.map((tab) => (
          <div
            key={tab}
            className={`py-2 px-4 font-mono text-[11px] cursor-pointer border-b-2 uppercase tracking-[1px] ${
              activeTab === tab
                ? 'text-neon-green border-b-neon-green'
                : 'text-text-muted border-transparent'
            }`}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </div>
        ))}
        <div onClick={onCycleBottom} className="flex-1 cursor-pointer h-full" title="Cycle Console size" />
        <div className="pr-4 flex items-center gap-3 font-mono text-[10px] text-text-muted">
          <div className="flex items-center gap-1.5">
            <div className={`w-1.5 h-1.5 rounded-full ${sc.color} ${sc.pulse ? 'animate-pulse' : ''}`} />
            {sc.label}
          </div>
        </div>
      </div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto py-2 font-mono text-[11px] leading-relaxed">
        {activeTab === 'Agent' && agentActive && (
          <div className="py-1.5 px-4 flex items-center gap-2 animate-[pulse_1.5s_infinite]">
            <div className="w-1.5 h-1.5 rounded-full bg-neon-orange shadow-[0_0_6px_rgba(255,136,0,0.5)]" />
            <span className="font-mono text-[11px] text-neon-orange">{spinnerWord}...</span>
          </div>
        )}
        {filteredLogs.map((line, i) => (
          <div key={i} className="py-0.5 px-4 flex gap-3 transition-colors duration-100 hover:bg-bg-tertiary">
            <span className="text-text-muted shrink-0">{line.time}</span>
            <span className="text-neon-blue shrink-0 min-w-[80px]">{line.source}</span>
            <span className={msgColors[line.msgClass] || 'text-text-secondary'}>{line.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
