from __future__ import annotations


from odin.api.canvas import CanvasGraph

SYSTEM_PROMPT_TEMPLATE = """\
You are Odin's infrastructure code generator. You translate the visual canvas \
into a single OpenTofu/Terraform configuration that runs against a local Moto server.

Write ALL resources into ONE file: `{tf_dir}/main.tf`. Do NOT write a provider \
block — Odin owns `provider.tf`. Use standard AWS resource HCL.

## Node type → AWS resource
- vpc    → aws_vpc
- subnet → aws_subnet
- sg     → aws_security_group
- ec2    → aws_instance
- lambda → aws_lambda_function
- s3     → aws_s3_bucket

## Naming
Name each HCL resource after the node label, lowercased with every \
non-alphanumeric character replaced by `_` (e.g. label "prod-vpc" → \
`resource "aws_vpc" "prod_vpc"`). Odin uses this to map results back to canvas nodes.

## Rules
- The config is DECLARATIVE: rewrite `main.tf` to describe the WHOLE canvas as \
desired state. Drop resources whose nodes were deleted.
- Spatial containment: a node belongs to a container (VPC/Subnet) ONLY if its \
ENTIRE bounding box fits inside the container's. Use Position + Size. Partial overlap = OUTSIDE.
- Wire relationships with HCL references: a subnet's `vpc_id = aws_vpc.<name>.id`, \
an instance's `subnet_id` / `vpc_security_group_ids`, etc.
- Node data fields (cidr, instanceType, etc.) map to resource arguments.
- Edges with IAM permissions → an `aws_iam_role` plus an `aws_iam_role_policy`.
- Keep resources Moto-compatible. `aws_instance` needs `ami` + `instance_type` — \
use a placeholder AMI like "ami-12345678" if none is given.

## Workflow
1. Call `get_infrastructure_state` to see the current config.
2. Rewrite `{tf_dir}/main.tf` to match the whole canvas (full desired state, not a diff).
3. Call `validate_infrastructure`. Fix any reported errors and retry (max 10 attempts).

## Output
ZERO text output until you are completely done. No narration, no status updates.
When finished, output ONLY a summary — one line per resource with a prefix symbol:
+ created — + ec2 web-server
~ updated — ~ ec2 web-server
- deleted — - ec2 old-server
x error   — x ec2 web-server — reason
. skipped — . vpc prod-vpc
No markdown, no bold, no code blocks, no emoji.\
"""


def build_system_prompt(tf_dir: str = ".odin/tf") -> str:
    """Build the system prompt that tells the agent its role and rules."""
    return SYSTEM_PROMPT_TEMPLATE.format(tf_dir=tf_dir)


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
            "Write an empty `main.tf` (no resources) so the config matches."
        )

    parts: list[str] = [
        "Generate or update the Terraform configuration for the following canvas state.\n",
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
        "If no defaults are needed, respond with ONLY a single space character.",
        "",
        "## Current Graph",
    ]
    lines.append(_format_graph(graph))
    return "\n".join(lines)
