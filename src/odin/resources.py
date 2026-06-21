"""Single source of truth for the AWS resource types the canvas supports.

One `ResourceSpec` per node type keeps four things in lockstep that used to be
edited separately (and could drift): the canvas node → Terraform type map, the
provider endpoint overrides (a *missing* one silently routes tofu at real AWS),
the Moto engine's supported set, and the agent's per-resource HCL hints.

Adding a service to the backend = adding one entry here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceSpec:
    node_type: str          # canvas node type (e.g. "dynamodb")
    aws_type: str           # Terraform resource type (e.g. "aws_dynamodb_table")
    moto_service: str       # provider endpoint / Moto service (e.g. "dynamodb")
    hint: str = ""          # Moto-compat HCL guidance appended to the agent prompt


RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec("vpc", "aws_vpc", "ec2"),
    ResourceSpec("subnet", "aws_subnet", "ec2"),
    ResourceSpec("sg", "aws_security_group", "ec2"),
    ResourceSpec(
        "ec2", "aws_instance", "ec2",
        '`aws_instance` needs `ami` + `instance_type` — use a placeholder AMI '
        'like "ami-12345678" if none is given.',
    ),
    ResourceSpec(
        "lambda", "aws_lambda_function", "lambda",
        '`aws_lambda_function` needs a deployment package: set '
        '`filename = "placeholder.zip"` (Odin provides this file in the tf dir) '
        'plus `handler`, `runtime`, and a `role` ARN. Do NOT invent your own zip '
        'path or try to create one.',
    ),
    ResourceSpec("s3", "aws_s3_bucket", "s3"),
    ResourceSpec(
        "dynamodb", "aws_dynamodb_table", "dynamodb",
        '`aws_dynamodb_table` needs `name`, `billing_mode = "PAY_PER_REQUEST"`, '
        '`hash_key`, and a matching `attribute { name = <hash_key> type = "S" }` '
        'block (type "S"/"N"/"B"). Add `range_key` plus a second `attribute` '
        'block only if a sort key is given.',
    ),
    ResourceSpec(
        "sqs", "aws_sqs_queue", "sqs",
        '`aws_sqs_queue` needs `name`. For a FIFO queue set `fifo_queue = true` '
        'and end the name with ".fifo".',
    ),
    ResourceSpec("sns", "aws_sns_topic", "sns", '`aws_sns_topic` needs `name`.'),
)

# Always-needed Moto services that aren't themselves canvas resources.
_EXTRA_MOTO_SERVICES = ("sts",)

# canvas node type → Terraform resource type
NODE_AWS_TYPE: dict[str, str] = {s.node_type: s.aws_type for s in RESOURCE_SPECS}

# Ordered-unique provider/Moto services (resources + extras).
MOTO_SERVICES: tuple[str, ...] = tuple(
    dict.fromkeys(s.moto_service for s in RESOURCE_SPECS) | dict.fromkeys(_EXTRA_MOTO_SERVICES)
)


def node_type_lines() -> str:
    """The `node type → aws resource` list for the agent system prompt."""
    width = max(len(s.node_type) for s in RESOURCE_SPECS)
    return "\n".join(f"- {s.node_type:<{width}} → {s.aws_type}" for s in RESOURCE_SPECS)


def resource_hints() -> str:
    """Per-resource Moto-compat HCL hints for the agent system prompt."""
    return "\n".join(f"- {s.hint}" for s in RESOURCE_SPECS if s.hint)
