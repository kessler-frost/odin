import { useState, useCallback, useRef, useEffect } from 'react';
import type { Node, Edge } from '@xyflow/react';
import TopBar from './components/TopBar';
import Sidebar from './components/Sidebar';
import Canvas from './components/Canvas';
import ConfigPanel from './components/ConfigPanel';
import BottomPanel from './components/BottomPanel';

export type BottomState = 'default' | 'collapsed' | 'half';

type Toast = { id: number; kind: 'success' | 'error' | 'info'; text: string };
type Diff = Record<string, Record<string, unknown>>;
type PreviewState = { open: boolean; loading: boolean; diff: Diff };

// --- Toasts: surface the result of real-infra actions instead of swallowing them ---
function Toasts({ toasts }: { toasts: Toast[] }) {
  const tone = {
    success: 'border-neon-green text-neon-green bg-[rgba(0,255,136,0.08)]',
    error: 'border-neon-red text-neon-red bg-[rgba(255,51,85,0.08)]',
    info: 'border-border-bright text-text-secondary bg-bg-secondary',
  };
  return (
    <div className="fixed bottom-4 right-4 z-[110] flex flex-col gap-2 items-end">
      {toasts.map(t => (
        <div key={t.id} className={`font-mono text-xs py-2 px-3 border shadow-lg animate-[fade-in_0.15s_ease-out] ${tone[t.kind]}`}>
          {t.text}
        </div>
      ))}
    </div>
  );
}

// --- Preview: the AI's staged changeset, as a themed drawer (was a window.alert) ---
function PreviewDrawer({ diff, onApply, onClose }: { diff: Diff; onApply: () => void; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);
  const entries = Object.entries(diff);
  const empty = entries.length === 0;
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60" onClick={onClose}>
      <div onClick={e => e.stopPropagation()} className="w-[460px] max-h-[70vh] flex flex-col bg-bg-secondary border border-border-bright shadow-2xl">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <div className="font-mono text-[11px] text-text-secondary uppercase tracking-[2px]">
            Staged changes <span className="text-neon-blue">[AI]</span>
          </div>
          <button onClick={onClose} className="font-mono text-[10px] text-text-muted hover:text-text-primary cursor-pointer uppercase tracking-[1px]">esc ✕</button>
        </div>
        <div className="px-4 py-3 overflow-y-auto flex-1">
          {empty ? (
            <p className="text-text-muted font-mono text-xs py-8 text-center leading-relaxed">
              No AI-proposed changes.<br />Apply will run the canvas as drawn.
            </p>
          ) : entries.map(([id, fields]) => (
            <div key={id} className="mb-3">
              <div className="font-mono text-xs text-text-primary mb-1 flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-neon-blue" />{id}
              </div>
              {Object.entries(fields).map(([k, v]) => (
                <div key={k} className="font-mono text-[11px] pl-3.5 leading-relaxed">
                  <span className="text-text-muted">{k}</span>
                  <span className="text-text-muted"> = </span>
                  <span className="text-neon-green">{String(v)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
        <div className="px-4 py-3 border-t border-border flex gap-2 justify-end">
          <button onClick={onClose} className="font-mono text-xs py-1.5 px-3 border border-border-bright bg-bg-tertiary text-text-secondary cursor-pointer uppercase tracking-[1px] hover:text-text-primary transition-colors">
            Dismiss
          </button>
          {!empty && (
            <button onClick={onApply} className="font-mono text-xs py-1.5 px-3 border border-neon-green bg-bg-tertiary text-neon-green cursor-pointer uppercase tracking-[1px] hover:bg-[rgba(0,255,136,0.1)] transition-colors">
              Apply changes
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [selectedNodes, setSelectedNodes] = useState<Node[]>([]);
  const [selectedEdges, setSelectedEdges] = useState<Edge[]>([]);
  const [nodeUpdates, setNodeUpdates] = useState<{ nodeId: string; data: Record<string, string> } | null>(null);
  const [edgeUpdates, setEdgeUpdates] = useState<{ edgeId: string; data: Record<string, unknown> } | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [configOpen, setConfigOpen] = useState(true);
  const [bottomState, setBottomState] = useState<BottomState>('default');
  const [wsConnected, setWsConnected] = useState(false);
  const [env, setEnv] = useState('default');
  const statusUpdateFnRef = useRef<((name: string, status: string, error?: string, facts?: Record<string, unknown>) => void) | null>(null);
  const [configUpdate, setConfigUpdate] = useState<{ nodeId: string; data: Record<string, any> } | null>(null);
  const resetDraftsRef = useRef<(() => void) | null>(null);
  const [clearLogSignal, setClearLogSignal] = useState(0);

  const [toasts, setToasts] = useState<Toast[]>([]);
  const [preview, setPreview] = useState<PreviewState>({ open: false, loading: false, diff: {} });
  const toastId = useRef(0);

  const pushToast = useCallback((kind: Toast['kind'], text: string) => {
    const id = ++toastId.current;
    setToasts(t => [...t, { id, kind, text }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4500);
  }, []);

  const handleNodeUpdate = useCallback((nodeId: string, data: Record<string, string>) => {
    setNodeUpdates({ nodeId, data });
  }, []);

  const handleEdgeUpdate = useCallback((edgeId: string, data: Record<string, unknown>) => {
    setEdgeUpdates({ edgeId, data });
  }, []);

  const readCanvas = useCallback(async () => {
    const canvas = await fetch('/canvas').then(r => r.json()).catch(() => null);
    if (!canvas) pushToast('error', 'Could not read the canvas');
    return canvas;
  }, [pushToast]);

  // Apply: send the canvas as desired state; the Reconciler runs it for real and
  // streams live status back over the WebSocket (world_delta -> node phase).
  const handleApply = useCallback(async () => {
    const canvas = await readCanvas();
    if (!canvas) return;
    try {
      const res = await fetch(`/apply?env=${encodeURIComponent(env)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(canvas),
      });
      if (!res.ok) throw new Error(String(res.status));
      const body = await res.json();
      pushToast('success', `Applied to ${body.env ?? env}`);
    } catch {
      pushToast('error', 'Apply failed — backend unreachable');
    }
  }, [env, readCanvas, pushToast]);

  const handleValidateSelected = handleApply;

  // Staged changeset: show what the AI would fill, in a drawer, before committing.
  const handlePreview = useCallback(async () => {
    const canvas = await readCanvas();
    if (!canvas) return;
    setPreview(p => ({ ...p, loading: true }));
    try {
      const res = await fetch(`/preview?env=${encodeURIComponent(env)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(canvas),
      });
      if (!res.ok) throw new Error(String(res.status));
      const body = await res.json();
      setPreview({ open: true, loading: false, diff: body.diff ?? {} });
    } catch {
      setPreview({ open: false, loading: false, diff: {} });
      pushToast('error', 'Preview failed — could not reach the AI');
    }
  }, [env, readCanvas, pushToast]);

  const handleDestroy = useCallback(async () => {
    try {
      const res = await fetch(`/destroy?env=${encodeURIComponent(env)}`, { method: 'POST' });
      if (!res.ok) throw new Error(String(res.status));
      pushToast('success', `Destroyed ${env}`);
    } catch {
      pushToast('error', 'Destroy failed — backend unreachable');
    }
  }, [env, pushToast]);

  const handleResourceStatus = useCallback((name: string, status: string, error?: string, facts?: Record<string, unknown>) => {
    statusUpdateFnRef.current?.(name, status, error, facts);
  }, []);

  const handleConfigUpdate = useCallback((nodeId: string, data: Record<string, any>) => {
    setConfigUpdate({ nodeId, data });
  }, []);

  const cycleBottom = useCallback(() => {
    const order: BottomState[] = ['default', 'half', 'collapsed'];
    setBottomState(prev => order[(order.indexOf(prev) + 1) % order.length]);
  }, []);

  const bottomRow = bottomState === 'collapsed' ? '0px'
    : bottomState === 'half' ? '50vh'
    : '200px';

  const gridCols = `${sidebarOpen ? '240px' : '0px'} 1fr ${configOpen ? '300px' : '0px'}`;

  return (
    <div
      className="h-screen overflow-hidden grid transition-[grid-template-columns,grid-template-rows] duration-200"
      style={{
        gridTemplateColumns: gridCols,
        gridTemplateRows: `48px 1fr ${bottomRow}`,
      }}
    >
      {/* Row 1: TopBar */}
      <div className="col-span-full"><TopBar wsConnected={wsConnected} env={env} onEnvChange={setEnv} onPreview={handlePreview} onApply={handleApply} onDestroy={handleDestroy} onReset={() => { resetDraftsRef.current?.(); setClearLogSignal(s => s + 1); }} /></div>

      {/* Row 2: Sidebar + Canvas + Config */}
      <div className="overflow-hidden">
        <Sidebar onCollapse={() => setSidebarOpen(false)} />
      </div>
      <div className="relative overflow-hidden">
        <Canvas onNodeSelect={setSelectedNodes} onEdgeSelect={setSelectedEdges} nodeUpdates={nodeUpdates} edgeUpdates={edgeUpdates} onStatusUpdate={statusUpdateFnRef} configUpdate={configUpdate} onResetDrafts={resetDraftsRef} />
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            className="absolute top-2 left-2 z-10 py-1 px-2 flex items-center justify-center bg-bg-secondary border border-border text-text-muted hover:text-text-primary hover:border-border-bright transition-colors font-mono text-[10px] uppercase tracking-[1px] cursor-pointer"
            title="Show Resources"
          >
            Resources
          </button>
        )}
        {!configOpen && (
          <button
            onClick={() => setConfigOpen(true)}
            className="absolute top-2 right-2 z-10 py-1 px-2 flex items-center justify-center bg-bg-secondary border border-border text-text-muted hover:text-text-primary hover:border-border-bright transition-colors font-mono text-[10px] uppercase tracking-[1px] cursor-pointer"
            title="Show Configuration"
          >
            Configuration
          </button>
        )}
        {bottomState === 'collapsed' && (
          <button
            onClick={cycleBottom}
            className="absolute bottom-2 left-1/2 -translate-x-1/2 z-10 py-1 px-2 flex items-center justify-center bg-bg-secondary border border-border text-text-muted hover:text-text-primary hover:border-border-bright transition-colors font-mono text-[10px] uppercase tracking-[1px] cursor-pointer"
            title="Show Console"
          >
            Console
          </button>
        )}
      </div>
      <div className="overflow-hidden">
        <ConfigPanel
          nodes={selectedNodes}
          selectedEdge={selectedEdges.length === 1 ? selectedEdges[0] : null}
          onNodeUpdate={handleNodeUpdate}
          onEdgeUpdate={handleEdgeUpdate}
          onCollapse={() => setConfigOpen(false)}
          onValidate={handleValidateSelected}
        />
      </div>

      {/* Row 3: Bottom panel */}
      <div className="col-span-full overflow-hidden">
        <BottomPanel bottomState={bottomState} activeEnv={env} onCycleBottom={cycleBottom} onWsStatusChange={setWsConnected} onResourceStatus={handleResourceStatus} onConfigUpdate={handleConfigUpdate} clearSignal={clearLogSignal} />
      </div>

      {preview.open && (
        <PreviewDrawer
          diff={preview.diff}
          onApply={() => { setPreview(p => ({ ...p, open: false })); handleApply(); }}
          onClose={() => setPreview(p => ({ ...p, open: false }))}
        />
      )}
      <Toasts toasts={toasts} />
    </div>
  );
}
