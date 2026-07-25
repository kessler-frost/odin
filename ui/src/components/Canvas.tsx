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
  type NodeChange,
  type Connection,
  type OnConnect,
  BackgroundVariant,
  ConnectionMode,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import VpcNode from './nodes/VpcNode';
import SubnetNode from './nodes/SubnetNode';
import SgNode from './nodes/SgNode';
import Ec2Node from './nodes/Ec2Node';
import LambdaNode from './nodes/LambdaNode';
import S3Node from './nodes/S3Node';
import DynamodbNode from './nodes/DynamodbNode';
import ServiceNode from './nodes/ServiceNode';
import RegionAsk from './RegionAsk';
import { CATALOG, catalogNodeTypeMap, catalogDefaultData, catalogDefaultStyle, catalogZIndex, catalogByType, COLORS } from '../lib/catalog';
import { withContainment, isInsideContainer } from '../lib/containment';
import { placeUnpositioned } from '../lib/placement';
import { computeTypes, defaultPermissions, detectDefaultEdgeType, edgeStyle, edgeTypes } from '../lib/iam';

const nodeTypes: NodeTypes = {
  vpc: VpcNode,
  subnet: SubnetNode,
  sg: SgNode,
  ec2: Ec2Node,
  lambda: LambdaNode,
  s3: S3Node,
  dynamodb: DynamodbNode,
  // Every catalog service renders with the generic ServiceNode.
  ...Object.fromEntries(CATALOG.map((s) => [s.type, ServiceNode])),
};

const nodeTypeMap: Record<string, string> = {
  VPC: 'vpc',
  SUB: 'subnet',
  SG: 'sg',
  EC2: 'ec2',
  LAM: 'lambda',
  S3: 's3',
  DDB: 'dynamodb',
  ...catalogNodeTypeMap,
};

// V4c: the same "return event" default hcl.py's own `_lambda` builder
// falls back to when the code field is blank -- a freshly-dropped node is
// already a real, working (if trivial) function, not an empty shell.
const DEFAULT_LAMBDA_CODE = 'def lambda_handler(event, context):\n    return event\n';

const defaultDataForType: Record<string, Record<string, string>> = {
  vpc: { label: 'new-vpc', resourceId: '', cidr: '10.0.0.0/16', status: 'draft' },
  subnet: { label: 'new-subnet', resourceId: '', cidr: '10.0.1.0/24', status: 'draft' },
  sg: { label: 'new-sg', groupId: '', vpcId: '', ingressRules: '', status: 'draft' },
  ec2: { label: 'new-instance', instanceType: 't3.micro', ami: '', key: '', userData: '', securityGroups: '', status: 'draft' },
  lambda: { label: 'new-function', runtime: 'python3.12', handler: 'lambda_function.lambda_handler', code: DEFAULT_LAMBDA_CODE, role: '', status: 'draft' },
  s3: { label: 'new-bucket', arn: '', status: 'draft' },
  dynamodb: { label: 'new-table', hashKey: 'id', billingMode: 'PAY_PER_REQUEST', arn: '', status: 'draft' },
  ...catalogDefaultData,
};

const defaultStyleForType: Record<string, React.CSSProperties> = {
  vpc: { width: 560, height: 380 },
  subnet: { width: 520, height: 280 },
  sg: { width: 200 },
  ec2: { width: 200 },
  lambda: { width: 220 },
  s3: { width: 200 },
  dynamodb: { width: 200 },
  ...catalogDefaultStyle,
};


// Containers layer under their contents: containment is spatial + z-index,
// never ReactFlow parent-child (elevateNodesOnSelect stays false).
const zIndexForType: Record<string, number> = {
  vpc: 0,
  subnet: 1,
  sg: 2,
  ec2: 2,
  lambda: 2,
  s3: 2,
  dynamodb: 2,
  ...catalogZIndex,
};

const API = '';

let idCounter = 100;
function nextId(type: string) {
  return `${type}-${++idCounter}`;
}

// Nudge a drop/double-click spot so a new node never overlaps an existing one.
// Step is >= the default node width (200px), kept on the 20px grid.
const DECOLLIDE_STEP = 220;
function deCollide(pos: { x: number; y: number }, nodes: Node[]) {
  let { x, y } = pos;
  const occupied = () => nodes.some(n => Math.abs(n.position.x - x) < DECOLLIDE_STEP && Math.abs(n.position.y - y) < DECOLLIDE_STEP);
  for (let i = 0; i < 50 && occupied(); i++) { x += DECOLLIDE_STEP; y += DECOLLIDE_STEP; }
  return { x, y };
}

// Read the live endpoint / DATABASE_URL off a World resource's facts — the
// same extraction the live world_delta handler uses, reused for rehydration.
function endpointFromFacts(facts?: Record<string, unknown>): string {
  return (facts?.endpoint as string) || (facts?.DATABASE_URL as string) || '';
}

type HistoryEntry = { nodes: Node[]; edges: Edge[] };

// A node as it comes off `/canvas`: everything a ReactFlow node needs EXCEPT a
// guaranteed `position` — the one field a hand-authored canvas keeps omitting.
type LoadedNode = Omit<Node, 'position'> & { position?: { x: number; y: number } };

interface CanvasProps {
  env?: string;
  onNodeSelect?: (nodes: Node[]) => void;
  onEdgeSelect?: (edges: Edge[]) => void;
  onNodeLabelsChange?: (entries: { id: string; label?: string }[]) => void;
  nodeUpdates?: { nodeId: string; data: Record<string, string> } | null;
  edgeUpdates?: { edgeId: string; data: Record<string, unknown> } | null;
  onStatusUpdate?: React.MutableRefObject<((name: string, status: string, error?: string, facts?: Record<string, unknown>) => void) | null>;
  configUpdate?: { nodeId: string; data: Record<string, any> } | null;
  onCanvasSave?: (graph: { nodes: any[]; edges: any[] }) => void;
  onResetDrafts?: React.MutableRefObject<(() => void) | null>;
  onNotice?: (text: string) => void;
}

function InnerCanvas({ env, onNodeSelect, onEdgeSelect, onNodeLabelsChange, nodeUpdates, edgeUpdates, onStatusUpdate, configUpdate, onCanvasSave, onResetDrafts, onNotice }: CanvasProps) {
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

  // --- Load canvas from backend on mount (status is seeded separately, from /world) ---
  useEffect(() => {
    const load = async () => {
      const canvasRes = await fetch(`${API}/canvas`).then(r => r.json()).catch(() => ({ nodes: [], edges: [] }));

      // A canvas authored outside the UI (`odin canvas set`, an agent, the
      // README's own example) may carry no `position`. ReactFlow dereferences
      // node.position.x, so one such node used to blank the WHOLE canvas to
      // solid black with nothing said anywhere (fresh-user BLOCK-3). Lay those
      // out on the 20px grid, in canvas order, and say so — a node the user
      // can see and drag beats a black rectangle and a console TypeError.
      // The grid v0.7.3 laid them out on was blind, though: field test 4's
      // DynamoDB table landed exactly on top of an SQS queue the author HAD
      // positioned, hiding it completely. `placeUnpositioned` skips space
      // that is already taken.
      const fromDisk: LoadedNode[] = (canvasRes.nodes ?? []).map((n: any) => ({
        id: n.id,
        type: n.type,
        position: (typeof n.position?.x === 'number' && typeof n.position?.y === 'number') ? n.position : undefined,
        zIndex: zIndexForType[n.type] ?? 2,
        data: { ...defaultDataForType[n.type], ...n.data },
        style: { ...defaultStyleForType[n.type], ...n.size },
      }));
      const { nodes: rfNodes, placed } = placeUnpositioned(fromDisk);
      if (placed > 0) {
        onNotice?.(`${placed} node${placed === 1 ? '' : 's'} had no "position" — laid out on the grid. Move one and it sticks.`);
      }

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
  }, [setNodes, setEdges, onNotice]);

  // --- Rehydrate node badges from the observed World, on mount and on env change ---
  // (a live world_delta over the WebSocket always arrives after this and wins).
  const worldForEnv = useCallback((envName: string) => {
    fetch(`${API}/world?env=${encodeURIComponent(envName)}`)
      .then(r => r.json())
      .then((world: { resources?: { id: string; phase: string; facts?: Record<string, unknown>; verdict?: string | null }[] }) => {
        const byId = new Map((world.resources ?? []).map(r => [r.id, r]));
        setNodes(nds => nds.map(n => {
          const label = (n.data as Record<string, string>)?.label;
          const resource = label ? byId.get(label) : undefined;
          const endpoint = endpointFromFacts(resource?.facts);
          return {
            ...n,
            data: {
              ...n.data,
              status: resource?.phase ?? 'draft',
              ...(resource?.verdict ? { error: resource.verdict } : {}),
              ...(endpoint ? { endpoint } : {}),
            },
          };
        }));
      })
      .catch(() => {});
  }, [setNodes]);

  useEffect(() => {
    if (!loaded) return;
    worldForEnv(env ?? 'default');
  }, [env, loaded, worldForEnv]);

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
    onStatusUpdate.current = (name: string, status: string, error?: string, facts?: Record<string, unknown>) => {
      // The live endpoint / DATABASE_URL is the actual deliverable — surface it on the tile.
      const endpoint = endpointFromFacts(facts);
      setNodes(nds => {
        const updated = nds.map(n => {
          const matches = n.data?.label === name || n.id === name || `${n.type}_${n.data?.label}` === name;
          if (matches) return { ...n, data: { ...n.data, status, ...(error ? { error } : {}), ...(endpoint ? { endpoint } : {}) } };
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

  // --- Surface live labels to the parent so the config panel can guard against duplicates ---
  useEffect(() => {
    onNodeLabelsChange?.(nodes.map(n => ({ id: n.id, label: (n.data as Record<string, string>)?.label })));
  }, [nodes, onNodeLabelsChange]);

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
  // Re-stamp containment after edits: renaming a VPC/Subnet must refresh the
  // data.vpc/data.subnet labels stamped on everything drawn inside it.
  useEffect(() => {
    if (!nodeUpdates) return;
    const { nodeId, data } = nodeUpdates;
    setNodes(nds => withContainment(nds.map(n =>
      n.id === nodeId ? { ...n, data: { ...n.data, ...data } } : n
    )));
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
      setNodes((nds) => {
        // Keep labels unique — the registry keys on `{type}_{label}`, so two
        // nodes of a type with the same default label would otherwise collide.
        const base = defaultDataForType[type].label;
        const taken = new Set(nds.map((n) => (n.data as { label?: string })?.label));
        let label = base;
        for (let i = 2; taken.has(label); i++) label = `${base}-${i}`;
        return withContainment([
          ...nds,
          {
            id: nextId(type),
            type,
            // A drop point already inside a VPC/Subnet is a deliberate nesting
            // gesture — deCollide's proximity shove would otherwise push the
            // new node outside the container it was just dropped into.
            position: isInsideContainer(position, nds) ? position : deCollide(position, nds),
            zIndex: zIndexForType[type] ?? 2,
            data: { ...defaultDataForType[type], label },
            style: { ...defaultStyleForType[type] },
          },
        ]);
      });
    },
    [setNodes, screenToFlowPosition],
  );

  const dblClickTypeRef = useRef(0);
  const typeOrder = ['s3', 'sqs', 'dynamodb', 'rds', 'vpc', 'subnet', 'sg', 'ec2', 'lambda'];

  const onPaneDoubleClick = useCallback(
    (event: React.MouseEvent) => {
      const type = typeOrder[dblClickTypeRef.current % typeOrder.length];
      dblClickTypeRef.current++;
      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      position.x = Math.round(position.x / 20) * 20;
      position.y = Math.round(position.y / 20) * 20;
      setNodes((nds) => {
        // Keep labels unique — the registry keys on `{type}_{label}`, so two
        // nodes of a type with the same default label would otherwise collide.
        const base = defaultDataForType[type].label;
        const taken = new Set(nds.map((n) => (n.data as { label?: string })?.label));
        let label = base;
        for (let i = 2; taken.has(label); i++) label = `${base}-${i}`;
        return withContainment([
          ...nds,
          {
            id: nextId(type),
            type,
            // A drop point already inside a VPC/Subnet is a deliberate nesting
            // gesture — deCollide's proximity shove would otherwise push the
            // new node outside the container it was just dropped into.
            position: isInsideContainer(position, nds) ? position : deCollide(position, nds),
            zIndex: zIndexForType[type] ?? 2,
            data: { ...defaultDataForType[type], label },
            style: { ...defaultStyleForType[type] },
          },
        ]);
      });
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
        setNodes((nds) => withContainment([...nds.map((n) => ({ ...n, selected: false })), ...pasted]));
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

  // --- Spatial containment (owner rule: geometry compiles to infrastructure) ---
  // Re-stamp data.vpc/data.subnet whenever geometry settles: drag-stop below,
  // drop/double-click/paste at creation, and resize via the dimension changes
  // NodeResizer reports through the standard node-change channel (which also
  // covers the first post-render measure of freshly created nodes).
  // withContainment returns the same array on no-op, so these never push
  // spurious history entries. When stamps change on a selected node, re-fire
  // selection so the ConfigPanel sees the fresh data (same pattern as the
  // onStatusUpdate handler above).
  const restampContainment = useCallback(() => {
    setNodes((nds) => {
      const updated = withContainment(nds);
      if (updated !== nds) {
        const sel = updated.filter((n) => n.selected);
        if (sel.length > 0) queueMicrotask(() => onNodeSelect?.(sel));
      }
      return updated;
    });
  }, [setNodes, onNodeSelect]);

  const handleNodesChange = useCallback((changes: NodeChange<Node>[]) => {
    onNodesChange(changes);
    if (changes.some((c) => c.type === 'dimensions')) restampContainment();
  }, [onNodesChange, restampContainment]);

  const handleNodeDragStop = useCallback(() => {
    restampContainment();
  }, [restampContainment]);

  // W2.9/M8: the selected region, as node LABELS — what /agent/debug (and
  // everything else server-side) keys resources by, not ReactFlow's ids.
  const [selectedLabels, setSelectedLabels] = useState<string[]>([]);

  const handleSelectionChange = useCallback(({ nodes: selNodes, edges: selEdges }: { nodes: Node[]; edges: Edge[] }) => {
    onNodeSelect?.(selNodes);
    onEdgeSelect?.(selEdges);
    setSelectedLabels(selNodes.map(n => (n.data as Record<string, string>)?.label).filter(Boolean));
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
        onNodesChange={handleNodesChange}
        onNodeDragStop={handleNodeDragStop}
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
        multiSelectionKeyCode={['Meta', 'Shift']}
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
        {nodes.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-0">
            <p className="font-mono text-xs text-text-muted/70 text-center leading-relaxed">
              Drag a resource from the left, or double-click the canvas.<br />
              Then press <span className="text-neon-green">Apply</span> to run it.
            </p>
          </div>
        )}
        <MiniMap
          nodeColor={(node) => {
            const bespoke: Record<string, string> = {
              vpc: 'rgba(170,85,255,0.4)',
              subnet: 'rgba(0,187,255,0.4)',
              sg: 'rgba(255,51,85,0.6)',
              s3: 'rgba(0,255,136,0.6)',
            };
            const t = node.type ?? '';
            if (bespoke[t]) return bespoke[t];
            const def = catalogByType[t];
            return def ? `rgba(${COLORS[def.color].rgb},0.5)` : 'rgba(120,120,140,0.5)';
          }}
          maskColor="rgba(5,5,8,0.85)"
          className="!bg-bg-secondary !border-border-bright"
          style={{ width: 140, height: 90 }}
        />
      </ReactFlow>
      {/* Outside <ReactFlow> so canvas pan/zoom gestures never eat its clicks. */}
      <RegionAsk selectedIds={selectedLabels} env={env ?? 'default'} />
    </div>
  );
}

export default function Canvas({ env, onNodeSelect, onEdgeSelect, onNodeLabelsChange, nodeUpdates, edgeUpdates, onStatusUpdate, configUpdate, onCanvasSave, onResetDrafts, onNotice }: CanvasProps) {
  return (
    <ReactFlowProvider>
      <InnerCanvas env={env} onNodeSelect={onNodeSelect} onEdgeSelect={onEdgeSelect} onNodeLabelsChange={onNodeLabelsChange} nodeUpdates={nodeUpdates} edgeUpdates={edgeUpdates} onStatusUpdate={onStatusUpdate} configUpdate={configUpdate} onCanvasSave={onCanvasSave} onResetDrafts={onResetDrafts} onNotice={onNotice} />
    </ReactFlowProvider>
  );
}
