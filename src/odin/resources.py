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
    moto_service: str       # boto3 / Moto service name (e.g. "dynamodb")
    hint: str = ""          # Moto-compat HCL guidance appended to the agent prompt
    provider_key: str = ""  # AWS-provider endpoint key when it differs from
                            # moto_service (e.g. logs → "cloudwatchlogs")

    @property
    def endpoint_key(self) -> str:
        return self.provider_key or self.moto_service


RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec("vpc", "aws_vpc", "ec2"),
    ResourceSpec(
        "subnet", "aws_subnet", "ec2",
        '`aws_subnet` needs `vpc_id` and a `cidr_block` within the VPC range. When '
        'a VPC has multiple subnets, give each a DISTINCT, non-overlapping '
        '`cidr_block` (e.g. 10.0.1.0/24, 10.0.2.0/24, …) — identical CIDRs fail at '
        'apply — and spread them across different `availability_zone`s.',
    ),
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
        'path or try to create one. If the node has `memory`/`timeout`, set '
        '`memory_size` (MB) and `timeout` (seconds) — use just the numbers.',
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
    ResourceSpec(
        "rds", "aws_db_instance", "rds",
        '`aws_db_instance` needs `identifier` (= the label), `engine` (e.g. '
        '"postgres"/"mysql"), `instance_class` (e.g. "db.t3.micro"), '
        '`allocated_storage` (e.g. 20), `username`, `password` (any placeholder), '
        'and `skip_final_snapshot = true`.',
    ),
    ResourceSpec(
        "secret", "aws_secretsmanager_secret", "secretsmanager",
        '`aws_secretsmanager_secret` needs `name`.',
    ),
    ResourceSpec(
        "kms", "aws_kms_key", "kms",
        '`aws_kms_key` takes an optional `description` (use the label); it has no '
        '`name` argument.',
    ),
    ResourceSpec(
        "iamrole", "aws_iam_role", "iam",
        '`aws_iam_role` needs `name` and an `assume_role_policy` — a `jsonencode` '
        'of a single `sts:AssumeRole` statement with a sensible service principal.',
    ),
    ResourceSpec(
        "route53", "aws_route53_zone", "route53",
        '`aws_route53_zone` needs `name` — a DNS domain like "example.com".',
    ),
    ResourceSpec(
        "apigateway", "aws_api_gateway_rest_api", "apigateway",
        '`aws_api_gateway_rest_api` needs `name`.',
    ),
    ResourceSpec(
        "efs", "aws_efs_file_system", "efs",
        '`aws_efs_file_system` has no required args; set `creation_token` to the label.',
    ),
    ResourceSpec(
        "ssm", "aws_ssm_parameter", "ssm",
        '`aws_ssm_parameter` needs `name`, `type = "String"`, and `value` '
        '(any placeholder).',
    ),
    ResourceSpec(
        "kinesis", "aws_kinesis_stream", "kinesis",
        '`aws_kinesis_stream` needs `name` and `shard_count = 1`.',
    ),
    ResourceSpec("ecs", "aws_ecs_cluster", "ecs", '`aws_ecs_cluster` needs `name`.'),
    ResourceSpec(
        "logs", "aws_cloudwatch_log_group", "logs",
        '`aws_cloudwatch_log_group` needs `name` (e.g. "/aws/lambda/fn"); '
        '`retention_in_days` is optional.',
        provider_key="cloudwatchlogs",
    ),
    ResourceSpec(
        "events", "aws_cloudwatch_event_rule", "events",
        '`aws_cloudwatch_event_rule` needs `name` and either `schedule_expression` '
        '(e.g. "rate(5 minutes)") or `event_pattern` (a jsonencode pattern).',
        provider_key="cloudwatchevents",
    ),
    ResourceSpec(
        "ebs", "aws_ebs_volume", "ec2",
        '`aws_ebs_volume` needs `availability_zone` (e.g. "us-east-1a") and '
        '`size` in GiB (e.g. 10).',
    ),
    ResourceSpec("eip", "aws_eip", "ec2", '`aws_eip` needs `domain = "vpc"`.'),
    ResourceSpec(
        "igw", "aws_internet_gateway", "ec2",
        '`aws_internet_gateway` needs no required args; set `vpc_id` if a VPC is present.',
    ),
    ResourceSpec(
        "alb", "aws_lb", "elbv2",
        '`aws_lb` needs `name`, `load_balancer_type` ("application"/"network"), and '
        '`subnets` — at least two subnet ids in different AZs (reference subnet nodes). '
        'Add `security_groups` for an application load balancer.',
    ),
)

# Always needed, not themselves canvas resources: `iam` (emitted for IAM edges)
# and `sts` (the provider's account lookups).
_EXTRA_SERVICES = ("iam", "sts")

# canvas node type → Terraform resource type
NODE_AWS_TYPE: dict[str, str] = {s.node_type: s.aws_type for s in RESOURCE_SPECS}

# boto3 / Moto service names — used by the engine's clients.
MOTO_SERVICES: tuple[str, ...] = tuple(
    dict.fromkeys((*(s.moto_service for s in RESOURCE_SPECS), *_EXTRA_SERVICES))
)

# AWS-provider endpoint keys — used for provider.tf endpoint overrides. Differs
# from MOTO_SERVICES only where a provider key isn't the boto3 name.
PROVIDER_SERVICES: tuple[str, ...] = tuple(
    dict.fromkeys((*(s.endpoint_key for s in RESOURCE_SPECS), *_EXTRA_SERVICES))
)


def node_type_lines() -> str:
    """The `node type → aws resource` list for the agent system prompt."""
    width = max(len(s.node_type) for s in RESOURCE_SPECS)
    return "\n".join(f"- {s.node_type:<{width}} → {s.aws_type}" for s in RESOURCE_SPECS)


def resource_hints() -> str:
    """Per-resource Moto-compat HCL hints for the agent system prompt."""
    return "\n".join(f"- {s.hint}" for s in RESOURCE_SPECS if s.hint)
