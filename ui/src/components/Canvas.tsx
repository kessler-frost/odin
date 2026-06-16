import { useCallback, useRef, useEffect, useState } from 'react';
import {
  ReactFlow,
  Background,
  MiniMap,
  Controls,
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlowProvider,
  addEdge,
  type Node,
  type Edge,
  type NodeTypes,
  type Connection,
  type OnConnect,
  BackgroundVariant,
  ConnectionMode,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import VpcNode from './nodes/VpcNode';
import SubnetNode from './nodes/SubnetNode';
import Ec2Node from './nodes/Ec2Node';
import LambdaNode from './nodes/LambdaNode';
import S3Node from './nodes/S3Node';
import SgNode from './nodes/SgNode';
import { computeTypes, defaultPermissions, detectDefaultEdgeType, edgeStyle, edgeTypes } from '../lib/iam';

const nodeTypes: NodeTypes = {
  vpc: VpcNode,
  subnet: SubnetNode,
  ec2: Ec2Node,
  lambda: LambdaNode,
  s3: S3Node,
  sg: SgNode,
};

const nodeTypeMap: Record<string, string> = {
  VPC: 'vpc',
  SUB: 'subnet',
  EC2: 'ec2',
  FN: 'lambda',
  S3: 's3',
  SG: 'sg',
};

const defaultDataForType: Record<string, Record<string, string>> = {
  vpc: { label: 'new-vpc', resourceId: '', cidr: '10.0.0.0/16', status: 'draft' },
  subnet: { label: 'new-subnet', resourceId: '', cidr: '10.0.1.0/24', status: 'draft' },
  ec2: { label: 'new-instance', resourceId: '', instanceType: 't2.micro', privateIp: '—', overlayIp: '—', status: 'draft' },
  lambda: { label: 'new-function', runtime: 'python3.12', handler: 'handler.main', memory: '128MB', timeout: '30s timeout', status: 'draft' },
  s3: { label: 'new-bucket', arn: '', status: 'draft' },
  sg: { label: 'new-sg', groupId: '', vpcId: '', inboundRules: '', outboundRules: '', status: 'draft' },
};

const defaultStyleForType: Record<string, React.CSSProperties> = {
  vpc: { width: 560, height: 380 },
  subnet: { width: 520, height: 280 },
  ec2: { width: 220 },
  lambda: { width: 220 },
  s3: { width: 200 },
  sg: { width: 220 },
};


const zIndexForType: Record<string, number> = {
  vpc: 0,
  subnet: 1,
  ec2: 2,
  lambda: 2,
  s3: 2,
  sg: 2,
};

const API = '';

let idCounter = 100;
function nextId(type: string) {
  return `${type}-${++idCounter}`;
}

type HistoryEntry = { nodes: Node[]; edges: Edge[] };

interface CanvasProps {
  onNodeSelect?: (nodes: Node[]) => void;
  onEdgeSelect?: (edges: Edge[]) => void;
  nodeUpdates?: { nodeId: string; data: Record<string, string> } | null;
  edgeUpdates?: { edgeId: string; data: Record<string, unknown> } | null;
  onStatusUpdate?: React.MutableRefObject<((name: string, status: string, error?: string) => void) | null>;
  configUpdate?: { nodeId: string; data: Record<string, any> } | null;
  onCanvasSave?: (graph: { nodes: any[]; edges: any[] }) => void;
  onResetDrafts?: React.MutableRefObject<(() => void) | null>;
}

function InnerCanvas({ onNodeSelect, onEdgeSelect, nodeUpdates, edgeUpdates, onStatusUpdate, configUpdate, onCanvasSave, onResetDrafts }: CanvasProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [loaded, setLoaded] = useState(false);
  const { screenToFlowPosition, fitView } = useReactFlow();
  const [shiftHeld, setShiftHeld] = useState(false);

  useEffect(() => {
    const down = (e: KeyboardEvent) => { if (e.key === 'Shift') setShiftHeld(true); };
    const up = (e: KeyboardEvent) => { if (e.key === 'Shift') setShiftHeld(false); };
    document.addEventListener('keydown', down);
    document.addEventListener('keyup', up);
    return () => { document.removeEventListener('keydown', down); document.removeEventListener('keyup', up); };
  }, []);

  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  nodesRef.current = nodes;
  edgesRef.current = edges;

  // Expose full reset to parent via ref — clears canvas + backend
  useEffect(() => {
    if (!onResetDrafts) return;
    onResetDrafts.current = () => {
      setNodes([]);
      setEdges([]);
      // Reset backend: clears registry, infra files, canvas.json, agent session
      fetch(`${API}/reset`, { method: 'POST' }).catch(() => {});
    };
    return () => { onResetDrafts.current = null; };
  }, [onResetDrafts, setNodes, setEdges]);

  // --- Load canvas from backend on mount ---
  useEffect(() => {
    const load = async () => {
      const [canvasRes, stateRes] = await Promise.all([
        fetch(`${API}/canvas`).then(r => r.json()).catch(() => ({ nodes: [], edges: [] })),
        fetch(`${API}/state`).then(r => r.json()).catch(() => ({ resources: [] })),
      ]);

      const statusMap: Record<string, Record<string, string>> = {};
      for (const r of (stateRes.resources ?? [])) {
        statusMap[r.name] = { status: r.status, ...(r.metadata ?? {}) };
      }

      const rfNodes: Node[] = (canvasRes.nodes ?? []).map((n: any) => ({
        id: n.id,
        type: n.type,
        position: n.position,
        zIndex: zIndexForType[n.type] ?? 2,
        data: { ...defaultDataForType[n.type], ...n.data, ...(statusMap[`${n.type}_${n.data?.label}`] ?? statusMap[n.data?.label] ?? {}) },
        style: { ...defaultStyleForType[n.type], ...n.size },
      }));

      const rfEdges: Edge[] = (canvasRes.edges ?? []).map((e: any) => {
        const eType = e.data?.edgeType ?? 'network';
        const typeDef = edgeTypes[eType] ?? edgeTypes.network;
        const permissions = e.data?.permissions ?? [];
        const hasLabel = eType === 'iam' && permissions.length > 0;
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          sourceHandle: e.sourceHandle ?? null,
          targetHandle: e.targetHandle ?? null,
          data: e.data ?? {},
          style: edgeStyle(eType),
          label: hasLabel ? permissions.map((p: string) => p.split(':')[1]).join(', ') : undefined,
          labelStyle: hasLabel ? { fill: typeDef.color, fontSize: 10, fontFamily: 'monospace' } : undefined,
          labelBgStyle: hasLabel ? { fill: '#0a0a10', stroke: typeDef.color, strokeWidth: 0.5 } : undefined,
        };
      });

      setNodes(rfNodes);
      setEdges(rfEdges);

      historyRef.current = [{ nodes: structuredClone(rfNodes), edges: structuredClone(rfEdges) }];
      historyIndexRef.current = 0;
      setLoaded(true);
    };
    load();
  }, [setNodes, setEdges]);

  // --- Undo/redo via debounced history ---
  const historyRef = useRef<HistoryEntry[]>([{ nodes: [], edges: [] }]);
  const historyIndexRef = useRef(0);
  const isUndoingRef = useRef(false);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clipboardRef = useRef<Node[]>([]);

  const scheduleSnapshot = useCallback(() => {
    // Don't record history while undo/redo is restoring state
    if (isUndoingRef.current) return;
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = setTimeout(() => {
      const currentNodes = nodesRef.current;
      const currentEdges = edgesRef.current;
      const lastEntry = historyRef.current[historyIndexRef.current];
      // Only push if state actually changed (skip selection-only changes)
      const nodesChanged = JSON.stringify(currentNodes.map(n => ({ id: n.id, pos: n.position, style: n.style, data: n.data, parentId: n.parentId })))
        !== JSON.stringify(lastEntry.nodes.map(n => ({ id: n.id, pos: n.position, style: n.style, data: n.data, parentId: n.parentId })));
      const edgesChanged = JSON.stringify(currentEdges.map(e => ({ id: e.id, source: e.source, target: e.target })))
        !== JSON.stringify(lastEntry.edges.map(e => ({ id: e.id, source: e.source, target: e.target })));
      if (nodesChanged || edgesChanged) {
        historyRef.current = historyRef.current.slice(0, historyIndexRef.current + 1);
        historyRef.current.push({ nodes: structuredClone(currentNodes), edges: structuredClone(currentEdges) });
        historyIndexRef.current = historyRef.current.length - 1;
      }
    }, 300);
  }, []);

  const undo = useCallback(() => {
    if (historyIndexRef.current <= 0) return;
    // Flush any pending snapshot of current state before undoing
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
      const currentNodes = nodesRef.current;
      const currentEdges = edgesRef.current;
      const lastEntry = historyRef.current[historyIndexRef.current];
      const nodesChanged = JSON.stringify(currentNodes.map(n => ({ id: n.id, pos: n.position, style: n.style, data: n.data, parentId: n.parentId })))
        !== JSON.stringify(lastEntry.nodes.map(n => ({ id: n.id, pos: n.position, style: n.style, data: n.data, parentId: n.parentId })));
      const edgesChanged = JSON.stringify(currentEdges.map(e => ({ id: e.id, source: e.source, target: e.target })))
        !== JSON.stringify(lastEntry.edges.map(e => ({ id: e.id, source: e.source, target: e.target })));
      if (nodesChanged || edgesChanged) {
        historyRef.current = historyRef.current.slice(0, historyIndexRef.current + 1);
        historyRef.current.push({ nodes: structuredClone(currentNodes), edges: structuredClone(currentEdges) });
        historyIndexRef.current = historyRef.current.length - 1;
      }
    }
    isUndoingRef.current = true;
    historyIndexRef.current--;
    const entry = historyRef.current[historyIndexRef.current];
    setNodes(structuredClone(entry.nodes));
    setEdges(structuredClone(entry.edges));
    requestAnimationFrame(() => { isUndoingRef.current = false; });
  }, [setNodes, setEdges]);

  const redo = useCallback(() => {
    if (historyIndexRef.current >= historyRef.current.length - 1) return;
    isUndoingRef.current = true;
    historyIndexRef.current++;
    const entry = historyRef.current[historyIndexRef.current];
    setNodes(structuredClone(entry.nodes));
    setEdges(structuredClone(entry.edges));
    requestAnimationFrame(() => { isUndoingRef.current = false; });
  }, [setNodes, setEdges]);

  // Schedule snapshot whenever nodes or edges change
  useEffect(() => { scheduleSnapshot(); }, [nodes, edges, scheduleSnapshot]);

  // --- Debounced save to backend ---
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onCanvasSaveRef = useRef(onCanvasSave);
  onCanvasSaveRef.current = onCanvasSave;

  useEffect(() => {
    if (!loaded) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      const canvasData = {
        nodes: nodesRef.current.map(n => ({
          id: n.id,
          type: n.type,
          position: n.position,
          size: {
            width: n.width ?? (n.style as any)?.width ?? (defaultStyleForType[n.type ?? ''] as any)?.width,
            height: n.height ?? (n.style as any)?.height ?? n.measured?.height ?? (defaultStyleForType[n.type ?? ''] as any)?.height,
          },
          data: Object.fromEntries(
            Object.entries(n.data ?? {}).filter(([k]) => !['status', 'error'].includes(k))
          ),
        })),
        edges: edgesRef.current.map(e => ({
          id: e.id,
          source: e.source,
          target: e.target,
          sourceHandle: e.sourceHandle ?? null,
          targetHandle: e.targetHandle ?? null,
          data: e.data ?? {},
        })),
      };
      fetch(`${API}/canvas`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(canvasData),
      }).catch(() => {});
      onCanvasSaveRef.current?.(canvasData);
    }, 500);
  }, [nodes, edges, loaded]);

  // --- Register status update callback (called directly, avoids React batching loss) ---
  useEffect(() => {
    if (!onStatusUpdate) return;
    onStatusUpdate.current = (name: string, status: string, error?: string) => {
      setNodes(nds => {
        const updated = nds.map(n => {
          const matches = n.data?.label === name || n.id === name || `${n.type}_${n.data?.label}` === name;
          if (matches) return { ...n, data: { ...n.data, status, ...(error ? { error } : {}) } };
          return n;
        });
        // Re-fire selection so ConfigPanel sees updated status
        const sel = updated.filter(n => n.selected);
        if (sel.length > 0) queueMicrotask(() => onNodeSelect?.(sel));
        return updated;
      });
    };
    return () => { onStatusUpdate.current = null; };
  }, [onStatusUpdate, setNodes, onNodeSelect]);

  // --- Apply config updates from agent (via WebSocket → BottomPanel → App.tsx) ---
  useEffect(() => {
    if (!configUpdate) return;
    setNodes(nds => nds.map(n => {
      if (n.id === configUpdate.nodeId) {
        return { ...n, data: { ...n.data, ...configUpdate.data } };
      }
      return n;
    }));
  }, [configUpdate, setNodes]);

  // --- Apply config panel edits to nodes ---
  useEffect(() => {
    if (!nodeUpdates) return;
    const { nodeId, data } = nodeUpdates;
    setNodes(nds => nds.map(n =>
      n.id === nodeId ? { ...n, data: { ...n.data, ...data } } : n
    ));
  }, [nodeUpdates, setNodes]);

  // --- Apply config panel edits to edges ---
  useEffect(() => {
    if (!edgeUpdates) return;
    const { edgeId, data } = edgeUpdates;
    let updatedEdge: Edge | null = null;
    setEdges(eds => eds.map(e => {
      if (e.id !== edgeId) return e;
      const newData = { ...e.data, ...data };
      const eType = (newData.edgeType as string) ?? 'network';
      const typeDef = edgeTypes[eType] ?? edgeTypes.network;
      const permissions = (newData.permissions as string[]) ?? [];
      const hasLabel = eType === 'iam' && permissions.length > 0;
      updatedEdge = {
        ...e,
        data: newData,
        style: edgeStyle(eType),
        label: hasLabel ? permissions.map(p => p.split(':')[1]).join(', ') : undefined,
        labelStyle: hasLabel ? { fill: typeDef.color, fontSize: 10, fontFamily: 'monospace' } : undefined,
        labelBgStyle: hasLabel ? { fill: '#0a0a10', stroke: typeDef.color, strokeWidth: 0.5 } : undefined,
      };
      return updatedEdge;
    }));
    // Re-fire selection so ConfigPanel gets the updated edge data
    if (updatedEdge) onEdgeSelect?.([updatedEdge]);
  }, [edgeUpdates, setEdges, onEdgeSelect]);

  const onConnect: OnConnect = useCallback(
    (connection: Connection) => {
      const sourceNode = nodesRef.current.find(n => n.id === connection.source);
      const targetNode = nodesRef.current.find(n => n.id === connection.target);
      const sourceType = sourceNode?.type ?? '';
      const targetType = targetNode?.type ?? '';
      const detectedType = detectDefaultEdgeType(sourceType, targetType);
      const typeDef = edgeTypes[detectedType] ?? edgeTypes.network;
      const isIam = detectedType === 'iam';
      // For IAM, pick the non-compute end (the resource being accessed) for defaults
      const iamResourceType = isIam
        ? (!computeTypes.has(sourceType) ? sourceType : !computeTypes.has(targetType) ? targetType : '')
        : '';
      const perms = isIam ? [...(defaultPermissions[iamResourceType] ?? [])] : [];
      const hasLabel = isIam && perms.length > 0;

      setEdges((eds) =>
        addEdge(
          {
            ...connection,
            data: { edgeType: detectedType, ...(isIam ? { permissions: perms } : {}) },
            style: edgeStyle(detectedType),
            label: hasLabel ? perms.map(p => p.split(':')[1]).join(', ') : undefined,
            labelStyle: hasLabel ? { fill: typeDef.color, fontSize: 10, fontFamily: 'monospace' } : undefined,
            labelBgStyle: hasLabel ? { fill: '#0a0a10', stroke: typeDef.color, strokeWidth: 0.5 } : undefined,
          },
          eds,
        ),
      );
    },
    [setEdges],
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const abbr = event.dataTransfer.getData('application/odin-resource');
      const type = nodeTypeMap[abbr];
      if (!type || !defaultDataForType[type]) return;

      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      position.x = Math.round(position.x / 20) * 20;
      position.y = Math.round(position.y / 20) * 20;
      setNodes((nds) => [
        ...nds,
        {
          id: nextId(type),
          type,
          position,
          zIndex: zIndexForType[type] ?? 2,
          data: { ...defaultDataForType[type] },
          style: { ...defaultStyleForType[type] },
        },
      ]);
    },
    [setNodes, screenToFlowPosition],
  );

  const dblClickTypeRef = useRef(0);
  const typeOrder = ['ec2', 'lambda', 's3', 'sg', 'vpc', 'subnet'];

  const onPaneDoubleClick = useCallback(
    (event: React.MouseEvent) => {
      const type = typeOrder[dblClickTypeRef.current % typeOrder.length];
      dblClickTypeRef.current++;
      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      position.x = Math.round(position.x / 20) * 20;
      position.y = Math.round(position.y / 20) * 20;
      setNodes((nds) => [
        ...nds,
        {
          id: nextId(type),
          type,
          position,
          zIndex: zIndexForType[type] ?? 2,
          data: { ...defaultDataForType[type] },
          style: { ...defaultStyleForType[type] },
        },
      ]);
    },
    [setNodes, screenToFlowPosition],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

      const mod = e.metaKey || e.ctrlKey;

      if (e.key === 'Delete' || e.key === 'Backspace') {
        const currentNodes = nodesRef.current;
        const currentEdges = edgesRef.current;
        const selectedNodeIds = new Set(currentNodes.filter((n) => n.selected).map((n) => n.id));
        const selectedEdgeIds = new Set(currentEdges.filter((ed) => ed.selected).map((ed) => ed.id));
        if (selectedNodeIds.size === 0 && selectedEdgeIds.size === 0) return;
        const allNodeIds = new Set(selectedNodeIds);
        for (const node of currentNodes) {
          if (node.parentId && allNodeIds.has(node.parentId)) allNodeIds.add(node.id);
        }
        setNodes((nds) => nds.filter((n) => !allNodeIds.has(n.id)));
        setEdges((eds) => eds.filter((ed) => !selectedEdgeIds.has(ed.id) && !allNodeIds.has(ed.source) && !allNodeIds.has(ed.target)));
        onEdgeSelect?.([]);
        e.preventDefault();
        return;
      }

      if (mod && e.key === 'a') {
        setNodes((nds) => nds.map((n) => ({ ...n, selected: true })));
        e.preventDefault();
        return;
      }

      if (mod && e.key === 'z' && !e.shiftKey) {
        undo();
        e.preventDefault();
        return;
      }

      if (mod && e.key === 'z' && e.shiftKey) {
        redo();
        e.preventDefault();
        return;
      }

      if (mod && e.key === 'c') {
        clipboardRef.current = structuredClone(nodesRef.current.filter((n) => n.selected));
        e.preventDefault();
        return;
      }

      if (mod && e.key === 'v') {
        if (clipboardRef.current.length === 0) return;
        const pasted = clipboardRef.current.map((n) => ({
          ...structuredClone(n),
          id: nextId(n.type ?? 'node'),
          position: { x: n.position.x + 40, y: n.position.y + 40 },
          selected: true,
          parentId: undefined,
          extent: undefined,
        }));
        setNodes((nds) => [...nds.map((n) => ({ ...n, selected: false })), ...pasted]);
        e.preventDefault();
        return;
      }

      if (mod && e.key === 'f') {
        fitView({ padding: 0.6, duration: 300 });
        e.preventDefault();
        return;
      }
    };

    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [setNodes, setEdges, fitView, undo, redo]);

  const handleSelectionChange = useCallback(({ nodes: selNodes, edges: selEdges }: { nodes: Node[]; edges: Edge[] }) => {
    onNodeSelect?.(selNodes);
    onEdgeSelect?.(selEdges);
  }, [onNodeSelect, onEdgeSelect]);

  const handleEdgeClick = useCallback((_event: React.MouseEvent, edge: Edge) => {
    onNodeSelect?.([]);
    onEdgeSelect?.([edge]);
  }, [onNodeSelect, onEdgeSelect]);

  const handleNodeClick = useCallback((_event: React.MouseEvent, node: Node) => {
    onEdgeSelect?.([]);
    onNodeSelect?.([node]);
  }, [onNodeSelect, onEdgeSelect]);

  return (
    <div className="bg-bg-primary relative overflow-hidden h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onEdgeClick={handleEdgeClick}
        onNodeClick={handleNodeClick}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onDoubleClick={onPaneDoubleClick}
        onSelectionChange={handleSelectionChange}
        nodeTypes={nodeTypes}
        defaultViewport={{ x: 0, y: 0, zoom: 1 }}
        minZoom={0.3}
        maxZoom={2}
        snapToGrid
        snapGrid={[20, 20]}
        nodesDraggable={!shiftHeld}
        panActivationKeyCode="Shift"
        selectionKeyCode="Meta"
        multiSelectionKeyCode="Meta"
        deleteKeyCode={null}
        connectionMode={ConnectionMode.Loose}
        elevateNodesOnSelect={false}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{
          style: { stroke: '#4a4a60', strokeWidth: 1.5 },
        }}
        connectionLineStyle={{ stroke: '#00bbff', strokeWidth: 1.5 }}
      >
        <Background
          variant={BackgroundVariant.Lines}
          gap={20}
          lineWidth={1}
          color="rgba(50, 50, 70, 0.4)"
        />
        <Controls showInteractive={true} />
        <MiniMap
          nodeColor={(node) => {
            const colors: Record<string, string> = {
              vpc: 'rgba(170,85,255,0.4)',
              subnet: 'rgba(0,187,255,0.4)',
              ec2: 'rgba(255,136,0,0.6)',
              lambda: 'rgba(255,221,0,0.6)',
              s3: 'rgba(0,255,136,0.6)',
              sg: 'rgba(255,51,85,0.6)',
            };
            return colors[node.type ?? ''] ?? '#333';
          }}
          maskColor="rgba(5,5,8,0.85)"
          className="!bg-bg-secondary !border-border-bright"
          style={{ width: 140, height: 90 }}
        />
      </ReactFlow>
    </div>
  );
}

export default function Canvas({ onNodeSelect, onEdgeSelect, nodeUpdates, edgeUpdates, onStatusUpdate, configUpdate, onCanvasSave, onResetDrafts }: CanvasProps) {
  return (
    <ReactFlowProvider>
      <InnerCanvas onNodeSelect={onNodeSelect} onEdgeSelect={onEdgeSelect} nodeUpdates={nodeUpdates} edgeUpdates={edgeUpdates} onStatusUpdate={onStatusUpdate} configUpdate={configUpdate} onCanvasSave={onCanvasSave} onResetDrafts={onResetDrafts} />
    </ReactFlowProvider>
  );
}
