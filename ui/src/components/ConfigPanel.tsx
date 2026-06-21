import { useState, useEffect, useRef, useCallback } from 'react';
import type { Node, Edge } from '@xyflow/react';
import StatusBadge from './nodes/StatusBadge';
import { iamActionsForTarget, edgeTypes, detectEdgeTypes } from '../lib/iam';
import { catalogTypeConfig, catalogFields } from '../lib/catalog';

interface ConfigPanelProps {
  nodes: Node[];
  selectedEdge?: Edge | null;
  onNodeUpdate?: (nodeId: string, data: Record<string, string>) => void;
  onEdgeUpdate?: (edgeId: string, data: Record<string, unknown>) => void;
  onCollapse?: () => void;
  onValidate?: () => void;
}

const typeConfig: Record<string, { label: string; neonColor: string; neonBg: string }> = {
  vpc: { label: 'VPC', neonColor: 'text-neon-purple', neonBg: 'bg-[rgba(170,85,255,0.1)] border-neon-purple' },
  subnet: { label: 'Subnet', neonColor: 'text-neon-blue', neonBg: 'bg-[rgba(0,187,255,0.1)] border-neon-blue' },
  ec2: { label: 'EC2', neonColor: 'text-neon-orange', neonBg: 'bg-[rgba(255,136,0,0.1)] border-neon-orange' },
  lambda: { label: 'Lambda', neonColor: 'text-neon-yellow', neonBg: 'bg-[rgba(255,221,0,0.1)] border-neon-yellow' },
  s3: { label: 'S3', neonColor: 'text-neon-green', neonBg: 'bg-[rgba(0,255,136,0.1)] border-neon-green' },
  sg: { label: 'Security Group', neonColor: 'text-neon-red', neonBg: 'bg-[rgba(255,51,85,0.1)] border-neon-red' },
  dynamodb: { label: 'DynamoDB', neonColor: 'text-neon-cyan', neonBg: 'bg-[rgba(34,211,238,0.1)] border-neon-cyan' },
  ...catalogTypeConfig,
};

type FieldDef = { key: string; label: string; editable?: boolean; select?: string[] };

const ec2InstanceTypes = ['t2.micro', 't2.small', 't2.medium'];

const fieldsForType: Record<string, FieldDef[]> = {
  vpc: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'cidr', label: 'CIDR Block', editable: true },
    { key: 'resourceId', label: 'Resource ID' },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  subnet: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'cidr', label: 'CIDR Block', editable: true },
    { key: 'resourceId', label: 'Resource ID' },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  ec2: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'instanceType', label: 'Instance Type', editable: true, select: ec2InstanceTypes },
    { key: 'resourceId', label: 'Instance ID' },
    { key: 'privateIp', label: 'Private IP' },
    { key: 'overlayIp', label: 'Overlay IP' },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  lambda: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'runtime', label: 'Runtime', editable: true },
    { key: 'handler', label: 'Handler', editable: true },
    { key: 'memory', label: 'Memory', editable: true },
    { key: 'timeout', label: 'Timeout', editable: true },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  s3: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'arn', label: 'ARN' },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  sg: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'groupId', label: 'Group ID' },
    { key: 'vpcId', label: 'VPC ID' },
    { key: 'inboundRules', label: 'Inbound Rules', editable: true },
    { key: 'outboundRules', label: 'Outbound Rules', editable: true },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  dynamodb: [
    { key: 'label', label: 'Name', editable: true },
    { key: 'hashKey', label: 'Partition Key', editable: true },
    { key: 'billingMode', label: 'Billing Mode', editable: true, select: ['PAY_PER_REQUEST', 'PROVISIONED'] },
    { key: 'arn', label: 'ARN' },
    { key: 'status', label: 'Status' },
    { key: 'error', label: 'Error' },
  ],
  ...catalogFields,
};

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div className="mb-2.5">
      <label className="block text-[11px] text-text-secondary mb-1 font-mono">{label}</label>
      <input
        type="text"
        value={value}
        readOnly
        className="w-full py-1.5 px-2.5 bg-bg-tertiary border border-border text-text-muted font-mono text-xs outline-none cursor-not-allowed"
      />
    </div>
  );
}

function EditableField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="mb-2.5">
      <label className="block text-[11px] text-text-secondary mb-1 font-mono">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full py-1.5 px-2.5 bg-bg-primary border border-border text-text-primary font-mono text-xs outline-none transition-colors duration-200 focus:border-neon-blue focus:ring-1 focus:ring-neon-blue/30"
      />
    </div>
  );
}

function SelectField({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <div className="mb-2.5">
      <label className="block text-[11px] text-text-secondary mb-1 font-mono">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full py-1.5 px-2.5 bg-bg-primary border border-border text-text-primary font-mono text-xs outline-none transition-colors duration-200 focus:border-neon-blue focus:ring-1 focus:ring-neon-blue/30 appearance-none cursor-pointer"
      >
        {options.map(opt => <option key={opt} value={opt}>{opt}</option>)}
      </select>
    </div>
  );
}


function PermissionCheckbox({ action, checked, onChange }: { action: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 py-0.5 font-mono text-xs text-text-secondary hover:text-text-primary cursor-pointer">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="accent-[#00e5ff]" />
      {action}
    </label>
  );
}

function EdgeConfigView({ edge, onEdgeUpdate, onCollapse }: { edge: Edge; onEdgeUpdate?: (edgeId: string, data: Record<string, unknown>) => void; onCollapse?: () => void }) {
  const panelBase = "bg-bg-secondary border-l border-border-bright p-0 overflow-y-auto h-full";
  const currentType = (edge.data?.edgeType as string) ?? 'network';
  const typeDef = edgeTypes[currentType] ?? edgeTypes.network;
  const isIam = currentType === 'iam';

  const sourceType = edge.source.split('-')[0];
  const targetType = edge.target.split('-')[0];
  const permissions: string[] = (edge.data?.permissions as string[]) ?? [];

  // Available edge types for this node pair
  const availableTypes = detectEdgeTypes(sourceType, targetType);
  const iamAvailable = availableTypes.includes('iam');

  // Build per-resource permission groups (only for types that have IAM actions)
  const permissionGroups: { resourceType: string; label: string; neonColor: string; actions: string[] }[] = [];
  for (const rType of [sourceType, targetType]) {
    const actions = iamActionsForTarget[rType];
    if (!actions) continue;
    // Avoid duplicates if both ends are same type
    if (permissionGroups.some(g => g.resourceType === rType)) continue;
    const cfg = typeConfig[rType];
    permissionGroups.push({
      resourceType: rType,
      label: cfg?.label ?? rType.toUpperCase(),
      neonColor: cfg?.neonColor ?? 'text-text-muted',
      actions,
    });
  }

  const togglePermission = (action: string, checked: boolean) => {
    const newPermissions = checked
      ? [...permissions, action]
      : permissions.filter(p => p !== action);
    onEdgeUpdate?.(edge.id, { permissions: newPermissions, edgeType: 'iam' });
  };

  const changeEdgeType = (newType: string) => {
    const base: Record<string, unknown> = { edgeType: newType };
    if (newType !== 'iam') base.permissions = [];
    onEdgeUpdate?.(edge.id, base);
  };

  return (
    <div className={panelBase}>
      {/* Header */}
      <div onClick={onCollapse} className="px-4 py-3 border-b border-border flex items-center gap-2 cursor-pointer hover:opacity-70 transition-opacity" title="Hide Configuration">
        <span
          className="font-mono text-[10px] py-0.5 px-2 border uppercase"
          style={{ backgroundColor: `${typeDef.color}15`, borderColor: typeDef.color, color: typeDef.color }}
        >
          {typeDef.label}
        </span>
        <span className="font-semibold text-sm truncate text-text-primary">{isIam ? 'Permission' : 'Connection'}</span>
      </div>

      {/* Connection type selector — only show if multiple types available */}
      {availableTypes.length > 1 && (
        <div className="px-4 py-3 border-b border-border">
          <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
            Type
          </div>
          <select
            value={currentType}
            onChange={(e) => changeEdgeType(e.target.value)}
            className="w-full py-1.5 px-2.5 bg-bg-primary border border-border text-text-primary font-mono text-xs outline-none transition-colors duration-200 focus:border-neon-blue focus:ring-1 focus:ring-neon-blue/30 appearance-none cursor-pointer"
          >
            {availableTypes.map(t => {
              const def = edgeTypes[t];
              return <option key={t} value={t}>{def?.label ?? t}</option>;
            })}
          </select>
        </div>
      )}

      {/* Connection endpoints */}
      <div className="px-4 py-3 border-b border-border">
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
          Connection
        </div>
        <ReadOnlyField label="Endpoint" value={edge.source} />
        <ReadOnlyField label="Endpoint" value={edge.target} />
      </div>

      {/* Permissions — grouped by resource type, only when IAM */}
      {isIam && iamAvailable && permissionGroups.map(group => (
        <div key={group.resourceType} className="px-4 py-3 border-b border-border">
          <div className="font-mono text-[10px] uppercase tracking-[2px] mb-2.5 flex items-center gap-2">
            <span className={group.neonColor}>{group.label}</span>
            <span className="text-text-muted">Permissions</span>
          </div>
          <div className="space-y-0.5">
            {group.actions.map(action => (
              <PermissionCheckbox
                key={action}
                action={action}
                checked={permissions.includes(action)}
                onChange={(checked) => togglePermission(action, checked)}
              />
            ))}
          </div>
        </div>
      ))}

      {/* Edge ID */}
      <div className="px-4 py-3 border-t border-border">
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
          Internal
        </div>
        <ReadOnlyField label="Edge ID" value={edge.id} />
      </div>
    </div>
  );
}

function MultiSelectView({ nodes, onCollapse, onValidate }: { nodes: Node[]; onCollapse?: () => void; onValidate?: () => void }) {
  const panelBase = "bg-bg-secondary border-l border-border-bright p-0 overflow-y-auto h-full";

  const typeCounts: Record<string, number> = {};
  for (const n of nodes) {
    const t = n.type ?? 'unknown';
    typeCounts[t] = (typeCounts[t] ?? 0) + 1;
  }
  const breakdown = Object.entries(typeCounts)
    .map(([t, count]) => `${count} ${typeConfig[t]?.label ?? t}`)
    .join(', ');

  // Status counts for summary
  const statusCounts: Record<string, number> = {};
  for (const n of nodes) {
    const s = (n.data as Record<string, string>).status ?? 'draft';
    statusCounts[s] = (statusCounts[s] ?? 0) + 1;
  }

  return (
    <div className={panelBase}>
      {/* Header */}
      <div onClick={onCollapse} className="px-4 py-3 border-b border-border flex items-center justify-between cursor-pointer hover:opacity-70 transition-opacity" title="Hide Configuration">
        <span className="font-semibold text-sm text-text-primary">{nodes.length} resources selected</span>
      </div>

      {/* Type breakdown */}
      <div className="px-4 py-3 border-b border-border">
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
          Breakdown
        </div>
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(typeCounts).map(([t, count]) => {
            const cfg = typeConfig[t];
            return (
              <span key={t} className={`font-mono text-[10px] py-0.5 px-2 border uppercase ${cfg?.neonBg ?? 'bg-bg-tertiary border-border'} ${cfg?.neonColor ?? 'text-text-muted'}`}>
                {count} {cfg?.label ?? t}
              </span>
            );
          })}
        </div>
        <p className="text-text-secondary text-xs font-mono mt-2">{breakdown}</p>
      </div>

      {/* Status summary */}
      <div className="px-4 py-3 border-b border-border">
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
          Status
        </div>
        <div className="space-y-1.5">
          {Object.entries(statusCounts).map(([s, count]) => (
            <p key={s} className={`text-xs font-mono ${s === 'validated' ? 'text-neon-blue' : s === 'live' ? 'text-neon-green' : s === 'error' ? 'text-neon-red' : 'text-text-muted'}`}>
              {count} {s}
            </p>
          ))}
        </div>
      </div>

      {/* Batch actions */}
      <div className="px-4 py-3 space-y-2">
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
          Actions
        </div>
        <button
          onClick={() => onValidate?.()}
          className="w-full font-mono text-xs py-1.5 px-4 border border-neon-blue bg-bg-tertiary text-neon-blue cursor-pointer uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(51,153,255,0.1)] hover:shadow-[0_0_12px_rgba(51,153,255,0.2)]"
        >
          Validate
        </button>
      </div>
    </div>
  );
}

export default function ConfigPanel({ nodes, selectedEdge, onNodeUpdate, onEdgeUpdate, onCollapse, onValidate }: ConfigPanelProps) {
  const [localData, setLocalData] = useState<Record<string, string>>({});

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const debouncedUpdate = useCallback((nodeId: string, data: Record<string, string>) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      onNodeUpdate?.(nodeId, data);
    }, 300);
  }, [onNodeUpdate]);

  const node = nodes.length === 1 ? nodes[0] : null;

  // Sync local editable state when the selected node changes
  useEffect(() => {
    setLocalData(node ? { ...(node.data as Record<string, string>) } : {});
  }, [node?.id, node?.data]);

  const panelBase = "bg-bg-secondary border-l border-border-bright p-0 overflow-y-auto h-full";

  // Multi-select view
  if (nodes.length > 1) {
    return <MultiSelectView nodes={nodes} onCollapse={onCollapse} onValidate={onValidate} />;
  }

  // No node selected -- empty state
  const nodeType = node?.type ?? '';
  const config = typeConfig[nodeType];
  const fields = fieldsForType[nodeType];

  if (!node || !config || !fields) {
    // Edge config view: show when no node selected but an edge is selected
    if (selectedEdge) {
      return <EdgeConfigView edge={selectedEdge} onEdgeUpdate={onEdgeUpdate} onCollapse={onCollapse} />;
    }

    return (
      <div className={panelBase}>
        <div onClick={onCollapse} className="flex items-center justify-between px-4 py-3 border-b border-border cursor-pointer hover:opacity-70 transition-opacity" title="Hide Configuration">
          <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px]">Configuration</div>
        </div>
        <div className="px-4 py-8 text-center">
          <p className="text-text-muted text-xs font-mono">Select a node to view its configuration</p>
        </div>
      </div>
    );
  }

  const data = localData;
  const status = (node.data as Record<string, string>).status ?? 'draft';

  const updateField = (key: string, value: string) => {
    const updated = { ...localData, [key]: value };
    setLocalData(updated);
    debouncedUpdate(node.id, updated);
  };

  return (
    <div className={panelBase}>
      {/* Header */}
      <div onClick={onCollapse} className="px-4 py-3 border-b border-border flex items-center gap-2 cursor-pointer hover:opacity-70 transition-opacity" title="Hide Configuration">
        <span className={`font-mono text-[10px] py-0.5 px-2 border uppercase ${config.neonBg} ${config.neonColor}`}>
          {config.label}
        </span>
        <span className="font-semibold text-sm truncate">{data.label ?? node.id}</span>
        <div className="ml-auto">
          <StatusBadge status={status} />
        </div>
      </div>

      {/* Fields */}
      <div className="px-4 py-3">
        {fields.map((field) => {
          const value = data[field.key] ?? '';
          // Skip empty read-only fields (no value to show)
          if (!field.editable && !value) return null;
          // Status field uses the StatusBadge instead of an input
          if (field.key === 'status') return null;

          if (field.select) {
            return <SelectField key={field.key} label={field.label} value={value} options={field.select} onChange={(v) => updateField(field.key, v)} />;
          }
          return field.editable
            ? <EditableField key={field.key} label={field.label} value={value} onChange={(v) => updateField(field.key, v)} />
            : <ReadOnlyField key={field.key} label={field.label} value={value} />;
        })}
      </div>

      {/* Node ID (always shown, read-only) */}
      <div className="px-4 py-3 border-t border-border">
        <div className="font-mono text-[10px] text-text-muted uppercase tracking-[2px] mb-2.5">
          Internal
        </div>
        <ReadOnlyField label="Node ID" value={node.id} />
      </div>

      {/* Actions */}
      <div className="px-4 py-3 border-t border-border space-y-2">
        <button
          onClick={() => onValidate?.()}
          className="w-full font-mono text-xs py-1.5 px-4 border border-neon-blue bg-bg-tertiary text-neon-blue cursor-pointer uppercase tracking-[1px] transition-all duration-200 hover:bg-[rgba(51,153,255,0.1)] hover:shadow-[0_0_12px_rgba(51,153,255,0.2)]"
        >
          {status === 'validated' ? 'Re-validate' : 'Validate'}
        </button>
      </div>
    </div>
  );
}
