import { useState, useCallback, useRef, useEffect } from 'react';
import type { Node, Edge } from '@xyflow/react';
import TopBar from './components/TopBar';
import Sidebar from './components/Sidebar';
import Canvas from './components/Canvas';
import ConfigPanel from './components/ConfigPanel';
import BottomPanel from './components/BottomPanel';
import CodePanel from './components/CodePanel';
import { readCanvas as readCanvasPayload } from './lib/canvasLoad';

export type BottomState = 'default' | 'collapsed' | 'half';

type Toast = { id: number; kind: 'success' | 'error' | 'info'; text: string };

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

export default function App() {
  const [selectedNodes, setSelectedNodes] = useState<Node[]>([]);
  const [selectedEdges, setSelectedEdges] = useState<Edge[]>([]);
  const [nodeUpdates, setNodeUpdates] = useState<{ nodeId: string; data: Record<string, string> } | null>(null);
  const [edgeUpdates, setEdgeUpdates] = useState<{ edgeId: string; data: Record<string, unknown> } | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [configOpen, setConfigOpen] = useState(true);
  const [codeOpen, setCodeOpen] = useState(false);
  const [bottomState, setBottomState] = useState<BottomState>('default');
  const [streamConnected, setStreamConnected] = useState(false);
  const [env, setEnv] = useState(() => localStorage.getItem('odin-active-env') || 'default');
  const statusUpdateFnRef = useRef<((name: string, status: string, error?: string, facts?: Record<string, unknown>) => void) | null>(null);
  // Another client saved the canvas. The canvas is global, so every tab must
  // converge on it rather than sit on a stale copy and later overwrite it.
  const canvasUpdatedFnRef = useRef<((rev: string, env: string) => void) | null>(null);
  const [configUpdate, setConfigUpdate] = useState<{ nodeId: string; data: Record<string, any> } | null>(null);
  const [nodeLabels, setNodeLabels] = useState<{ id: string; label?: string }[]>([]);

  // The active env survives a reload so world rehydration lands on the right one.
  useEffect(() => { localStorage.setItem('odin-active-env', env); }, [env]);

  const [toasts, setToasts] = useState<Toast[]>([]);
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

  // No `?env=` here, deliberately: the canvas is GLOBAL, one architecture
  // applied to many environments, which are isolated copies of AWS *state*
  // (`.odin/canvas.json` is a single file; `api/canvas.py`'s routes take no
  // env at all, and `GET /canvas?env=X` returns byte-identical bytes for
  // every X). Adding `?env=` here reads as a per-env canvas that does not
  // exist -- a claim the code cannot back.
  //
  // Shares `lib/canvasLoad.ts` with the canvas loader, and the sharing is the
  // point: this call site had the same defect in a more destructive form. It
  // was `fetch('/canvas').then(r => r.json())` with no status check, so a 500
  // handed `handleApply` FastAPI's `{"detail": ...}` body — an object, so
  // truthy — which `POST /apply-full` accepts as a canvas with ZERO nodes
  // (`CanvasGraph.nodes` defaults to `[]`). Measured: HTTP 200,
  // `{"status": "applied"}`, a real revision committed. Applying an empty
  // desired state tears the environment down. A failed read must abort Apply,
  // never apply emptiness.
  const readCanvas = useCallback(async () => {
    // `?env=` IS required now: the canvas is per-env, so Apply must send the
    // canvas of the environment the user is looking at.
    const canvas = await readCanvasPayload(fetch, `/canvas?env=${encodeURIComponent(env)}`);
    if (!canvas) pushToast('error', 'Could not read the canvas — nothing was applied');
    return canvas;
  }, [env, pushToast]);

  // Apply: send the canvas as desired state; the Reconciler runs it for real,
  // Terraform is generated + applied through the gateway, and live status
  // (world_delta + tf lines) streams back over the event stream.
  const handleApply = useCallback(async () => {
    const canvas = await readCanvas();
    if (!canvas) return;
    const res = await fetch(`/apply-full?env=${encodeURIComponent(env)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(canvas),
    }).catch(() => null);
    if (!res) { pushToast('error', 'Apply failed — backend unreachable'); return; }
    if (res.status === 409) { pushToast('info', `A Terraform run is already in progress for ${env} — try again shortly`); return; }
    if (!res.ok) { pushToast('error', `Apply failed (HTTP ${res.status})`); return; }
    const body = await res.json().catch(() => ({}));
    const tf = body.tf;
    // Field test 3: `tf: ok` is not the same as "it works". A service that
    // ended the apply short of its desired task count is an outage, and this
    // toast must not read green while one is on the canvas — same rule the CLI
    // now exits nonzero for.
    const unhealthy: { node: string; running: number; desired: number; reason?: string }[] = body.unhealthy ?? [];
    for (const svc of unhealthy) {
      pushToast('error', `${svc.node}: ${svc.running}/${svc.desired} tasks running${svc.reason ? ` — ${svc.reason}` : ''}`);
    }
    // tf === null: nothing TF-supported on the canvas — a clean success.
    if (unhealthy.length) pushToast('error', `Applied to ${body.env ?? env}, but a service is not running — see Events`);
    else if (!tf) pushToast('success', `Applied to ${body.env ?? env}`);
    else if (tf.status === 'ok') pushToast('success', `Applied to ${body.env ?? env} — Terraform converged`);
    else if (tf.status === 'failed') pushToast('error', `Applied to ${body.env ?? env}, but Terraform failed (exit ${tf.exit_code}) — see Events for output`);
    else pushToast('info', `Applied to ${body.env ?? env} — Terraform skipped: ${tf.error}. Fix: ${tf.fix}`);
    if (body.skipped?.length) pushToast('info', `Not runnable: ${body.skipped.join(', ')}`);
    if (body.unsupported?.length) pushToast('info', `Not in Terraform: ${body.unsupported.join('; ')}`);
  }, [env, readCanvas, pushToast]);

  const handleValidateSelected = handleApply;

  const handleResourceStatus = useCallback((name: string, status: string, error?: string, facts?: Record<string, unknown>) => {
    statusUpdateFnRef.current?.(name, status, error, facts);
  }, []);

  const handleCanvasUpdated = useCallback((rev: string, updatedEnv: string) => {
    canvasUpdatedFnRef.current?.(rev, updatedEnv);
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
      <div className="col-span-full"><TopBar streamConnected={streamConnected} env={env} onEnvChange={setEnv} onApply={handleApply} onViewCode={() => setCodeOpen(o => !o)} codeOpen={codeOpen} /></div>

      {/* Row 2: Sidebar + Canvas + Config */}
      <div className="overflow-hidden">
        <Sidebar onCollapse={() => setSidebarOpen(false)} />
      </div>
      <div className="relative overflow-hidden">
        <Canvas env={env} onNodeSelect={setSelectedNodes} onEdgeSelect={setSelectedEdges} onNodeLabelsChange={setNodeLabels} nodeUpdates={nodeUpdates} edgeUpdates={edgeUpdates} onStatusUpdate={statusUpdateFnRef} configUpdate={configUpdate} onCanvasUpdated={canvasUpdatedFnRef} />
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
        {codeOpen && (
          <div className="absolute top-0 right-0 bottom-0 z-20 w-[520px] max-w-[75%]">
            <CodePanel env={env} onClose={() => setCodeOpen(false)} />
          </div>
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
          allLabels={nodeLabels}
          onNodeUpdate={handleNodeUpdate}
          onEdgeUpdate={handleEdgeUpdate}
          onCollapse={() => setConfigOpen(false)}
          onValidate={handleValidateSelected}
        />
      </div>

      {/* Row 3: Bottom panel */}
      <div className="col-span-full overflow-hidden">
        <BottomPanel
          bottomState={bottomState} activeEnv={env}
          selectedNode={selectedNodes.length === 1 ? ((selectedNodes[0].data?.label as string) || selectedNodes[0].id) : undefined}
          onCycleBottom={cycleBottom} onStreamStatusChange={setStreamConnected} onResourceStatus={handleResourceStatus} onConfigUpdate={handleConfigUpdate}
          onCanvasUpdated={handleCanvasUpdated}
        />
      </div>

      <Toasts toasts={toasts} />
    </div>
  );
}
