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
import { sizeOnLoad, sizeForSave } from '../lib/nodeSize';
import { BUILTINS, CATALOG, catalogNodeTypeMap, catalogDefaultData, catalogDefaultStyle, catalogZIndex, catalogByType, COLORS } from '../lib/catalog';
import { withContainment, isInsideContainer } from '../lib/containment';
import { placeUnpositioned } from '../lib/placement';
import { readCanvasWithRevision } from '../lib/canvasLoad';

// The canvas is PER-ENV (`.odin/<env>/canvas.json`). One helper so none of the
// three call sites can forget the parameter -- omitting it silently reads or
// writes the DEFAULT env's canvas, which is the class of bug that cost a
// canvas earlier in this release.
const canvasUrl = (env: string | undefined) => `${API}/canvas?env=${encodeURIComponent(env || 'default')}`;
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

// abbr -> node type, for both halves of the sidebar. The bespoke keys come from
// catalog.ts's BUILTINS rather than a second hardcoded copy: the catalog spread
// lands AFTER them, so a catalog entry declaring abbr 'S3' would silently
// override a bespoke tile and drop the wrong node type. One list, plus a test
// that no catalog abbr collides with a bespoke one.
const nodeTypeMap: Record<string, string> = {
  ...Object.fromEntries(BUILTINS.map((b) => [b.abbr, b.type])),
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

// Nudge a drop/double-click spot only when the new node would REALLY overlap
// an existing one, and then only as far as it takes.
//
// This used to treat a 220x220 halo around every node as occupied and, on a
// hit, jump +220 in BOTH axes -- up to 50 times. So a drop merely NEAR another
// node was flung diagonally away, which reads as "it went somewhere else
// entirely", and two nearby nodes compounded it. The halo was always wrong for
// height (a node is 40-80px tall now that leaves are content-sized, so 220
// reserved 3-5x its real size) and the diagonal step moved on an axis nothing
// had collided on.
//
// Real rectangle overlap plus one grid cell of breathing room, then a ring
// search outward on the 20px grid for the NEAREST free slot -- so a node that
// fits where you dropped it stays exactly there, and one that does not moves
// the minimum distance, usually a single cell.
const GAP = 20;

type Box = { x: number; y: number; w: number; h: number };

function boxOf(n: Node): Box {
  const style = (n.style ?? {}) as { width?: number; height?: number };
  return {
    x: n.position.x,
    y: n.position.y,
    w: n.width ?? style.width ?? 200,
    // Leaves are content-sized, so `measured` is the only truth for them;
    // fall back to the middle of the 40-80 range rather than the old 220.
    h: n.height ?? n.measured?.height ?? style.height ?? 60,
  };
}

function hits(a: Box, b: Box): boolean {
  return a.x < b.x + b.w + GAP && a.x + a.w + GAP > b.x
      && a.y < b.y + b.h + GAP && a.y + a.h + GAP > b.y;
}

function deCollide(pos: { x: number; y: number }, nodes: Node[], size = { w: 200, h: 60 }) {
  const boxes = nodes.map(boxOf);
  const free = (p: { x: number; y: number }) => !boxes.some(b => hits({ ...p, ...size }, b));
  if (free(pos)) return pos;
  for (let r = GAP; r <= 600; r += GAP) {
    for (const cand of [
      { x: pos.x + r, y: pos.y }, { x: pos.x, y: pos.y + r },
      { x: pos.x - r, y: pos.y }, { x: pos.x, y: pos.y - r },
      { x: pos.x + r, y: pos.y + r }, { x: pos.x - r, y: pos.y + r },
      { x: pos.x + r, y: pos.y - r }, { x: pos.x - r, y: pos.y - r },
    ]) if (free(cand)) return cand;
  }
  return pos;
}

// Read the live endpoint / DATABASE_URL off a World resource's facts — the
// same extraction the live world_delta handler uses, reused for rehydration.
function endpointFromFacts(facts?: Record<string, unknown>): string {
  return (facts?.endpoint as string) || (facts?.DATABASE_URL as string) || '';
}

// ReactFlow edges from a stored canvas.
//
// Shared by the initial load AND the `canvas_updated` convergence handler
// deliberately. The handler originally rebuilt only NODES, so a canvas change
// that added an IAM edge converged its nodes and silently dropped the edge --
// measured with `odin canvas set` adding an EC2 -> S3 edge, which an open tab
// showed as 2 nodes and 0 edges indefinitely. Two code paths reading one shape
// drift; one function cannot.
function edgesFromCanvas(canvas: { edges?: unknown[] }): Edge[] {
  return (canvas.edges ?? []).map((e: any) => {
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
  onCanvasUpdated?: React.MutableRefObject<((rev: string, env: string) => void) | null>;
}

function InnerCanvas({ env, onNodeSelect, onEdgeSelect, onNodeLabelsChange, nodeUpdates, edgeUpdates, onStatusUpdate, configUpdate, onCanvasSave, onResetDrafts, onCanvasUpdated }: CanvasProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [loaded, setLoaded] = useState(false);
  // How many nodes the canvas had to place itself on the last load. Stays on
  // screen until dismissed: the fix for it was a toast that faded after 4.5s,
  // which is the same as saying nothing to anyone who looked a moment later.
  const [placed, setPlaced] = useState(0);
  // The canvas could not be READ. Deliberately NOT dismissible: dismissing it
  // would leave an empty canvas that looks like a real one, which is the exact
  // confusion that made the data loss above so quiet.
  const [loadFailed, setLoadFailed] = useState(false);
  // Another tab saved a newer canvas than this page is editing. Not
  // dismissible: the page is now knowingly stale, and quietly continuing is
  // how the overwrite used to happen.
  const [conflict, setConflict] = useState(false);
  // The revision this page's canvas was loaded (or last saved) at.
  const revisionRef = useRef<string | null>(null);
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
      // A FAILED read is not an empty canvas — see `lib/canvasLoad.ts` for the
      // data loss that collapsing those two cases caused. `null` means COULD
      // NOT READ, and the loader returns WITHOUT setting `loaded`: nothing
      // renders, the debounced save below never arms, the file on disk is
      // untouched, and the banner says so.
      const read = await readCanvasWithRevision(fetch, canvasUrl(env));
      if (!read) {
        setLoadFailed(true);
        return;
      }
      const canvasRes = read.canvas;
      // Remember WHICH canvas this page is editing, so the save below can say
      // so and be refused rather than clobber a newer one.
      revisionRef.current = read.rev;

      // A canvas authored outside the UI (`odin canvas set`, an agent, the
      // README's own example) may carry no `position`. ReactFlow dereferences
      // node.position.x, so one such node used to blank the WHOLE canvas to
      // solid black with nothing said anywhere (fresh-user BLOCK-3).
      // `placeUnpositioned` lays those out on the 20px grid, in canvas order,
      // skipping space another node already occupies (field test 4: a table
      // dropped exactly on top of a queue, hiding it) — and the count comes
      // back so the canvas can SAY it moved them, instead of relocating a
      // user's nodes in silence.
      const fromDisk: LoadedNode[] = (canvasRes.nodes ?? []).map((n: any) => ({
        id: n.id,
        type: n.type,
        position: (typeof n.position?.x === 'number' && typeof n.position?.y === 'number') ? n.position : undefined,
        zIndex: zIndexForType[n.type] ?? 2,
        data: { ...defaultDataForType[n.type], ...n.data },
        style: { ...defaultStyleForType[n.type], ...sizeOnLoad(defaultStyleForType, n.type, n.size) },
      }));
      const { nodes: rfNodes, placed: placedCount } = placeUnpositioned(fromDisk);
      setPlaced(placedCount);

      const rfEdges = edgesFromCanvas(canvasRes);

      setNodes(rfNodes);
      setEdges(rfEdges);

      historyRef.current = [{ nodes: structuredClone(rfNodes), edges: structuredClone(rfEdges) }];
      historyIndexRef.current = 0;
      setLoaded(true);
    };
    load();
    // `env` is a dependency: the canvas is per-env now, so switching
    // environments must LOAD that env's canvas. Without it the previous env's
    // nodes stay on screen and the debounced save then writes them into the
    // env the user just switched to.
  }, [env, setNodes, setEdges]);

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
          size: sizeForSave(defaultStyleForType, n),
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
      // `If-Match` carries the revision this page loaded. The canvas is
      // global and every tab holds its own copy plus this debounced save, so
      // without the precondition the last tab to re-render silently overwrites
      // the others -- measured replacing three applied resources with a single
      // node from a tab left open in another window.
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (revisionRef.current) headers['If-Match'] = revisionRef.current;
      fetch(canvasUrl(env), { method: 'POST', headers, body: JSON.stringify(canvasData) })
        .then(r => {
          if (r.status === 409) { setConflict(true); return; }
          // Track the revision WE just wrote, so this page recognises its own
          // broadcast echo and does not reload itself mid-edit.
          if (r.ok) revisionRef.current = r.headers.get('ETag') ?? revisionRef.current;
        })
        .catch(() => {});
      onCanvasSaveRef.current?.(canvasData);
    }, 500);
  }, [nodes, edges, loaded]);

  // --- Another client saved the canvas: converge instead of drifting ---
  //
  // The canvas is GLOBAL by design -- one architecture, many environments --
  // so two tabs should show the same thing. They did not: each held its own
  // copy and the last to re-render overwrote the rest, silently. This is the
  // half that makes them agree; `If-Match` above is the half that makes the
  // remaining races refuse rather than clobber.
  useEffect(() => {
    if (!onCanvasUpdated) return;
    onCanvasUpdated.current = (rev: string, updatedEnv: string) => {
      // A tab showing `prod` must not reload because `staging` was saved.
      if ((updatedEnv || 'default') !== (env || 'default')) return;
      // Our OWN save echoes back. Reloading on it would throw away whatever
      // the user typed in the 500ms since, which is the same data loss wearing
      // a friendlier hat.
      if (!rev || rev === revisionRef.current) return;
      readCanvasWithRevision(fetch, canvasUrl(env)).then(read => {
        if (!read) return;
        revisionRef.current = read.rev;
        setConflict(false);
        const fromDisk: LoadedNode[] = (read.canvas.nodes ?? []).map((n: any) => ({
          id: n.id,
          type: n.type,
          position: (typeof n.position?.x === 'number' && typeof n.position?.y === 'number') ? n.position : undefined,
          zIndex: zIndexForType[n.type] ?? 2,
          data: { ...defaultDataForType[n.type], ...n.data },
          style: { ...defaultStyleForType[n.type], ...sizeOnLoad(defaultStyleForType, n.type, n.size) },
        }));
        const { nodes: rfNodes } = placeUnpositioned(fromDisk);
        setNodes(rfNodes);
        // Edges too -- see `edgesFromCanvas`. Omitting this made a CLI-added
        // IAM edge invisible in an already-open tab.
        setEdges(edgesFromCanvas(read.canvas));
      });
    };
    return () => { onCanvasUpdated.current = null; };
  }, [env, onCanvasUpdated, setNodes]);

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

      const position = centredOn(
        screenToFlowPosition({ x: event.clientX, y: event.clientY }), type,
      );
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
            position: isInsideContainer(position, nds) ? position : deCollide(position, nds, sizeFor(type)),
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
  // A drop/click point is where the CURSOR is, and `screenToFlowPosition` returns
// exactly that -- but ReactFlow reads `position` as the node's TOP-LEFT corner.
// Using it raw put every new node down and to the RIGHT of the cursor by half
// its own size, which reads as "it didn't land where I dropped it".
// Centring is the right correction rather than preserving the grab offset,
// because the thing being dragged is a small palette TILE, not the node, so an
// offset inside the tile has no meaningful mapping onto a 200px node.
// Heights are content-derived for leaf nodes now (40 + 20*rows), so the
// declared height is used when there is one (vpc/subnet) and a 60px middle of
// the leaf range otherwise -- half a row out is invisible, half a node is not.
const CENTRED_FALLBACK = { width: 200, height: 60 };

function sizeFor(type: string) {
  const style = (defaultStyleForType[type] ?? {}) as { width?: number; height?: number };
  return {
    w: typeof style.width === 'number' ? style.width : CENTRED_FALLBACK.width,
    h: typeof style.height === 'number' ? style.height : CENTRED_FALLBACK.height,
  };
}

function centredOn(point: { x: number; y: number }, type: string) {
  const style = (defaultStyleForType[type] ?? {}) as { width?: number; height?: number };
  const width = typeof style.width === 'number' ? style.width : CENTRED_FALLBACK.width;
  const height = typeof style.height === 'number' ? style.height : CENTRED_FALLBACK.height;
  // Snap AFTER centring, so the node still lands on the 20px grid.
  return {
    x: Math.round((point.x - width / 2) / 20) * 20,
    y: Math.round((point.y - height / 2) / 20) * 20,
  };
}

const typeOrder = ['s3', 'sqs', 'dynamodb', 'rds', 'vpc', 'subnet', 'sg', 'ec2', 'lambda'];

  const onPaneDoubleClick = useCallback(
    (event: React.MouseEvent) => {
      const type = typeOrder[dblClickTypeRef.current % typeOrder.length];
      dblClickTypeRef.current++;
      const position = centredOn(
        screenToFlowPosition({ x: event.clientX, y: event.clientY }), type,
      );
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
            position: isInsideContainer(position, nds) ? position : deCollide(position, nds, sizeFor(type)),
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
      {/* The canvas moved something the user didn't: say so, in the same pill
          bar RegionAsk uses (40px tall, 20px off the edge), and leave it up
          until it's dismissed. */}
      {loadFailed && (
        <div className="absolute top-0 left-0 right-0 z-30 flex items-center gap-2 px-3 py-2.5 bg-bg-secondary border-b border-neon-red shadow-lg">
          <span className="font-mono text-[10px] leading-5 text-neon-red uppercase tracking-[1px] whitespace-nowrap">Not loaded</span>
          <span className="flex-1 min-w-0 font-mono text-[11px] leading-5 text-text-secondary">
            odin could not read the saved canvas. <span className="text-text-primary">Nothing has been overwritten</span> — your
            saved canvas is intact on disk. Reload to try again; do not draw here until it loads.
          </span>
          <button
            onClick={() => window.location.reload()}
            title="Reload"
            className="font-mono text-[10px] h-5 px-2.5 border border-border bg-bg-tertiary text-text-muted uppercase tracking-[1px] cursor-pointer transition-colors duration-200 hover:text-text-primary hover:border-border-bright"
          >
            Reload
          </button>
        </div>
      )}
      {conflict && (
        <div className="absolute top-0 left-0 right-0 z-30 flex items-center gap-2 px-3 py-2.5 bg-bg-secondary border-b border-neon-amber shadow-lg">
          <span className="font-mono text-[10px] leading-5 text-neon-amber uppercase tracking-[1px] whitespace-nowrap">Stale</span>
          <span className="flex-1 min-w-0 font-mono text-[11px] leading-5 text-text-secondary">
            Another tab saved a newer canvas, so this page's changes were <span className="text-text-primary">not saved</span> —
            nothing was overwritten. Reload to continue from the current canvas.
          </span>
          <button
            onClick={() => window.location.reload()}
            title="Reload"
            className="font-mono text-[10px] h-5 px-2.5 border border-border bg-bg-tertiary text-text-muted uppercase tracking-[1px] cursor-pointer transition-colors duration-200 hover:text-text-primary hover:border-border-bright"
          >
            Reload
          </button>
        </div>
      )}
      {placed > 0 && (
        <div className="absolute top-0 left-0 right-0 z-30 flex items-center gap-2 px-3 py-2.5 bg-bg-secondary border-b border-border-bright shadow-lg">
          <span className="font-mono text-[10px] leading-5 text-neon-amber uppercase tracking-[1px] whitespace-nowrap">Placed</span>
          <span className="flex-1 min-w-0 font-mono text-[11px] leading-5 text-text-secondary">
            {placed} node{placed === 1 ? '' : 's'} had no <span className="text-text-primary">position</span> — odin
            put {placed === 1 ? 'it' : 'them'} on the grid, clear of your other nodes, and saved the layout.
          </span>
          <button
            onClick={() => setPlaced(0)}
            title="Dismiss"
            className="font-mono text-[10px] h-5 px-2.5 border border-border bg-bg-tertiary text-text-muted uppercase tracking-[1px] cursor-pointer transition-colors duration-200 hover:text-text-primary hover:border-border-bright"
          >
            Dismiss
          </button>
        </div>
      )}
      {/* Outside <ReactFlow> so canvas pan/zoom gestures never eat its clicks. */}
      <RegionAsk selectedIds={selectedLabels} env={env ?? 'default'} />
    </div>
  );
}

export default function Canvas({ env, onNodeSelect, onEdgeSelect, onNodeLabelsChange, nodeUpdates, edgeUpdates, onStatusUpdate, configUpdate, onCanvasSave, onResetDrafts, onCanvasUpdated }: CanvasProps) {
  return (
    <ReactFlowProvider>
      <InnerCanvas env={env} onNodeSelect={onNodeSelect} onEdgeSelect={onEdgeSelect} onNodeLabelsChange={onNodeLabelsChange} nodeUpdates={nodeUpdates} edgeUpdates={edgeUpdates} onStatusUpdate={onStatusUpdate} configUpdate={configUpdate} onCanvasSave={onCanvasSave} onResetDrafts={onResetDrafts} onCanvasUpdated={onCanvasUpdated} />
    </ReactFlowProvider>
  );
}
