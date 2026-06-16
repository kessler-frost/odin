from __future__ import annotations


from odin.api.canvas import CanvasGraph


SYSTEM_PROMPT_TEMPLATE = """\
You are Odin's infrastructure code generator. You translate visual canvas layouts into boto3 Python scripts.

Write one boto3 file per resource to `{infra_dir}/`. Naming: `{{service}}_{{label}}.py`. \
Each file is standalone (imports boto3, uses `region_name="us-east-1"`). Must be moto-compatible.

## Canvas Rules
- Spatial containment: a node belongs to a container (VPC/Subnet) ONLY if its ENTIRE bounding box fits inside the container's bounding box. Use Position + Size to compute bounding boxes. Partial overlap = OUTSIDE. A resource can belong to at most one VPC.
- Edges with IAM permissions: generate a separate IAM role/policy file.
- Node data fields (label, cidr, instanceType, etc.) map directly to boto3 parameters.

## Workflow
1. Call `get_infrastructure_state` to see current state.
2. Reconcile: create new, update changed, delete removed (rm the file), skip unchanged.
3. Write files, call `validate_file` on each. Fix errors and retry (max 10 total attempts).
4. Call `get_infrastructure_state` to confirm.

## Output
ZERO text output until you are completely done. No narration, no thinking out loud, no status updates.
When finished, output ONLY a summary — one line per resource with a prefix symbol:
! warning — ! lambda foo — overlaps VPC bar
+ created — + ec2 web-server
~ updated — ~ ec2 web-server
- deleted — - ec2 old-server
x error   — x ec2 web-server — reason
. skipped — . vpc prod-vpc
No markdown, no bold, no code blocks, no emoji.\
"""


def build_system_prompt(infra_dir: str = ".odin/infra") -> str:
    """Build the system prompt that tells the agent its role and rules."""
    return SYSTEM_PROMPT_TEMPLATE.format(infra_dir=infra_dir)


def _format_graph(graph: CanvasGraph) -> str:
    """Format a CanvasGraph into human-readable text for prompts."""
    parts: list[str] = []

    parts.append("## Nodes\n")
    for node in graph.nodes:
        node_id = node.get("id", "?")
        node_type = node.get("type", "?")
        position = node.get("position", {})
        size = node.get("size", node.get("style", {}))
        data = node.get("data", {})

        parts.append(f"- **{node_id}** (type: {node_type})")
        parts.append(f"  Position: ({position.get('x', 0)}, {position.get('y', 0)})")
        if size:
            parts.append(f"  Size: {size.get('width', '?')}x{size.get('height', 'auto')}")
        for key, value in data.items():
            if key != "status":
                parts.append(f"  {key}: {value}")
        parts.append("")

    if graph.edges:
        parts.append("## Edges (Connections)\n")
        for edge in graph.edges:
            source = edge.get("source", "?")
            target = edge.get("target", "?")
            data = edge.get("data", {})
            permissions = data.get("permissions", [])
            if permissions:
                parts.append(f"- {source} → {target}  (IAM: {', '.join(permissions)})")
            else:
                parts.append(f"- {source} → {target}")
        parts.append("")

    return "\n".join(parts)


def build_validate_prompt(graph: CanvasGraph) -> str:
    """Convert a CanvasGraph into a human-readable prompt for the agent."""
    if not graph.nodes:
        return (
            "The canvas is empty — no resources defined. "
            "Clean up any existing .odin/infra/ files that are no longer needed."
        )

    parts: list[str] = [
        "Generate or update boto3 files for the following canvas state.\n",
    ]
    parts.append(_format_graph(graph))
    return "\n".join(parts)


def build_suggest_defaults_prompt(graph: CanvasGraph) -> str:
    """Build prompt for smart default suggestions based on graph changes."""
    if not graph.nodes:
        return "The canvas is empty. No defaults to suggest."

    lines = [
        "The canvas graph was updated. Analyze spatial containment and connections,",
        "then respond ONLY with a JSON array of config updates for unconfigured fields.",
        "",
        'Format: [{"nodeId": "ec2-101", "data": {"subnetId": "...", "vpcId": "..."}}]',
        "",
        "Smart default rules:",
        "- EC2 fully inside Subnet bounding box (entire rect contained) → auto-fill SubnetId, VpcId, default SG",
        "- Subnet fully inside VPC → auto-fill VpcId, allocate next /24 CIDR",
        "- Lambda with IAM edge → auto-fill Role ARN",
        "- SG fully inside VPC → auto-fill VpcId",
        "- Partial overlap (node not fully inside container) → do NOT auto-fill, treat as standalone",
        "",
        "If no defaults are needed, respond with ONLY a single space character. Nothing else.",
        "Do NOT say 'no defaults needed' or any other text. Just a space.",
        "If defaults ARE needed, respond with ONLY the JSON array. No explanation.",
        "",
        "## Current Graph",
    ]
    lines.append(_format_graph(graph))
    return "\n".join(lines)
