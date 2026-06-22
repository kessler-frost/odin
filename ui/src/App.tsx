import { useState, useCallback, useRef } from 'react';
import type { Node, Edge } from '@xyflow/react';
import TopBar from './components/TopBar';
import Sidebar from './components/Sidebar';
import Canvas from './components/Canvas';
import ConfigPanel from './components/ConfigPanel';
import BottomPanel from './components/BottomPanel';

export type BottomState = 'default' | 'collapsed' | 'half';

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
  const statusUpdateFnRef = useRef<((name: string, status: string, error?: string) => void) | null>(null);
  const [configUpdate, setConfigUpdate] = useState<{ nodeId: string; data: Record<string, any> } | null>(null);
  const resetDraftsRef = useRef<(() => void) | null>(null);
  const [clearLogSignal, setClearLogSignal] = useState(0);
  // EXPERIMENTAL: smart defaults disabled until agent reliability improves
  // const suggestTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleNodeUpdate = useCallback((nodeId: string, data: Record<string, string>) => {
    setNodeUpdates({ nodeId, data });
  }, []);

  const handleEdgeUpdate = useCallback((edgeId: string, data: Record<string, unknown>) => {
    setEdgeUpdates({ edgeId, data });
  }, []);

  // Apply: send the canvas as desired state; the Reconciler runs it for real and
  // streams live status back over the WebSocket (world_delta -> node phase).
  const handleApply = useCallback(async () => {
    const canvas = await fetch('/canvas').then(r => r.json()).catch(() => null);
    if (!canvas) return;
    await fetch(`/apply?env=${encodeURIComponent(env)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(canvas),
    }).catch(() => {});
  }, [env]);

  const handleValidateSelected = handleApply;

  const handleDestroy = useCallback(async () => {
    await fetch(`/destroy?env=${encodeURIComponent(env)}`, { method: 'POST' }).catch(() => {});
  }, [env]);

  const handleResourceStatus = useCallback((name: string, status: string, error?: string) => {
    statusUpdateFnRef.current?.(name, status, error);
  }, []);

  const handleConfigUpdate = useCallback((nodeId: string, data: Record<string, any>) => {
    setConfigUpdate({ nodeId, data });
  }, []);

  // EXPERIMENTAL: smart defaults disabled until agent reliability improves
  // const lastFingerprintRef = useRef('');
  // const handleCanvasSave = useCallback((graph: { nodes: any[]; edges: any[] }) => {
  //   const fingerprint = JSON.stringify(
  //     graph.nodes.map(n => ({ id: n.id, type: n.type, label: n.data?.label }))
  //       .concat(graph.edges.map((e: any) => ({ s: e.source, t: e.target })) as any)
  //   );
  //   if (fingerprint === lastFingerprintRef.current) return;
  //   lastFingerprintRef.current = fingerprint;
  //   if (suggestTimerRef.current) clearTimeout(suggestTimerRef.current);
  //   suggestTimerRef.current = setTimeout(async () => {
  //     try {
  //       await fetch('/suggest-defaults', {
  //         method: 'POST',
  //         headers: { 'Content-Type': 'application/json' },
  //         body: JSON.stringify(graph),
  //       });
  //     } catch {}
  //   }, 500);
  // }, []);

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
      <div className="col-span-full"><TopBar wsConnected={wsConnected} env={env} onEnvChange={setEnv} onApply={handleApply} onDestroy={handleDestroy} onReset={() => { resetDraftsRef.current?.(); setClearLogSignal(s => s + 1); }} /></div>

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
    </div>
  );
}
