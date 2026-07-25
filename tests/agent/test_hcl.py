"""S3a — deterministic canvas -> Terraform skeleton generator."""
from __future__ import annotations

import io
import shutil
import subprocess
import time
import zipfile


from odin.agent.hcl import _ALB_NLB_UNSUPPORTED, generate_tf, resource_attrs, resource_set, unquote
from odin.spec.models import Edge, FieldValue, ResourceDesired, Stack
from odin.spec.translate import canvas_to_stack

_FULL_CANVAS = {
    "nodes": [
        {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
        {"id": "n3", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n4", "type": "dynamodb", "data": {"label": "items", "hashKey": "pk"}},
        {"id": "n5", "type": "rds", "data": {"label": "app-db", "engine": "postgres"}},
    ],
    "edges": [{"source": "n3", "target": "n2"}],
}

# Captured from a real run and verified byte-for-byte canonical via
# `tofu fmt -check -diff` (OpenTofu 1.12.3) — zero diff, exit 0.
_GOLDEN_MAIN_TF = '''terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_dynamodb_table" "items" {
  name         = "items"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  tags = {
    "odin:node" = "items"
  }
}

resource "aws_db_instance" "app_db" {
  identifier          = "app-db"
  engine              = "postgres"
  instance_class      = "db.t3.micro"
  allocated_storage   = 20
  db_name             = "postgres"
  username            = "app"
  password            = "apppass123"
  skip_final_snapshot = true

  tags = {
    "odin:node" = "app-db"
  }
}

resource "aws_s3_bucket" "uploads" {
  bucket        = "uploads"
  force_destroy = true

  tags = {
    "odin:node" = "uploads"
  }
}

resource "aws_sns_topic" "alerts" {
  name = "alerts"

  tags = {
    "odin:node" = "alerts"
  }
}

resource "aws_sns_topic_subscription" "alerts_jobs" {
  topic_arn            = aws_sns_topic.alerts.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.jobs.arn
  raw_message_delivery = true
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs"

  tags = {
    "odin:node" = "jobs"
  }
}
'''


def test_golden_main_tf_for_full_canvas():
    stack = canvas_to_stack(_FULL_CANVAS)
    proj = generate_tf(stack)
    assert proj.files["main.tf"] == _GOLDEN_MAIN_TF


def test_s3_bucket_gets_force_destroy():
    # Finding #4: a non-empty bucket must tear down cleanly on `tofu destroy`
    # (empty canvas = full destroy), not error BucketNotEmpty.
    main_tf = generate_tf(Stack(resources=(ResourceDesired(id="uploads", kind="s3"),))).files["main.tf"]
    assert "force_destroy = true" in main_tf


def test_rds_is_a_real_aws_db_instance_now_not_an_unsupported_kind():
    """W2.7: the LAST kind outside Terraform came inside. `skip_final_snapshot`
    is what keeps `tofu destroy` (empty canvas + Apply) from refusing -- s3's
    `force_destroy` for databases."""
    proj = generate_tf(canvas_to_stack(_FULL_CANVAS))
    assert proj.unsupported == []
    attrs = resource_attrs(proj.files)[("aws_db_instance", "app_db")]
    assert unquote(attrs["identifier"]) == "app-db"
    assert unquote(attrs["engine"]) == "postgres"
    assert attrs["allocated_storage"] == 20
    assert attrs["skip_final_snapshot"] is True


def test_rds_carries_every_canvas_field_it_has():
    res = ResourceDesired(id="app-db", kind="rds", fields={
        "engine": FieldValue(value="postgres"),
        "instanceClass": FieldValue(value="db.t3.small"),
        "allocatedStorage": FieldValue(value="50"),
        "dbName": FieldValue(value="orders"),
        "username": FieldValue(value="svc"),
        "password": FieldValue(value="s3cr3t-pw", sensitive=True),
    })
    attrs = resource_attrs(generate_tf(Stack(resources=(res,))).files)[("aws_db_instance", "app_db")]
    assert unquote(attrs["instance_class"]) == "db.t3.small"
    assert attrs["allocated_storage"] == 50
    assert unquote(attrs["db_name"]) == "orders"
    assert unquote(attrs["username"]) == "svc"
    assert unquote(attrs["password"]) == "s3cr3t-pw"


def test_rds_security_groups_field_becomes_vpc_security_group_ids():
    """W2.6: the SG a canvas draws for its database travels to the gateway
    through TERRAFORM, exactly as an ec2 node's does (same field, same builder)
    -- which is what lets `rdsctl` gate the real Postgres container's mesh
    membership by those groups' compiled firewall, and what puts the attachment
    in `tofu plan`."""
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="db-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:5432:web-sg")),
        ResourceDesired(id="web-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:22:0.0.0.0/0")),
        ResourceDesired(id="app-db", kind="rds", fields=_fields(engine="postgres", securityGroups="db-sg")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "vpc_security_group_ids = [aws_security_group.db_sg.id]" in main_tf


def test_rds_with_no_security_groups_field_omits_the_argument():
    """An rds node with nothing drawn keeps compiling byte-identical HCL -- the
    gateway then joins it to the mesh ungated (nebula's allow-all), never
    deny-all."""
    main_tf = generate_tf(Stack(resources=(ResourceDesired(id="app-db", kind="rds"),))).files["main.tf"]
    assert "aws_db_instance" in main_tf and "vpc_security_group_ids" not in main_tf


def test_rds_with_an_unknown_security_group_label_lands_in_unsupported():
    res = ResourceDesired(id="app-db", kind="rds", fields=_fields(securityGroups="ghost"))
    proj = generate_tf(Stack(resources=(res,)))
    assert proj.unsupported == [
        "app-db (rds): securityGroups names something that isn't a Security Group on the canvas"
    ]
    assert "aws_db_instance" not in proj.files["main.tf"]


def test_rds_with_a_non_postgres_engine_is_declined_not_silently_postgres():
    """The pre-W2.7 reconciler path ran a Postgres container no matter what
    `engine` said. Honest now: no local substrate, no HCL."""
    res = ResourceDesired(id="app-db", kind="rds", fields={"engine": FieldValue(value="mysql")})
    proj = generate_tf(Stack(resources=(res,)))
    assert proj.unsupported == [
        "app-db (rds): engine 'mysql' has no local substrate — odin runs a real "
        "Postgres, so only postgres is supported",
    ]
    assert "aws_db_instance" not in proj.files["main.tf"]


def test_rds_label_that_is_not_a_valid_identifier_is_declined_with_the_fix():
    """terraform-provider-aws validates `identifier` client-side, so a bad
    label would fail at plan time with a raw provider error. Declined here
    instead, with a sentence that says what to rename it to -- never silently
    renamed (that would break the container name AND the ${{db.VAR}} ref)."""
    for label in ("app_db", "App-DB", "-db", "db-", "app--db", "1db"):
        proj = generate_tf(Stack(resources=(ResourceDesired(id=label, kind="rds"),)))
        assert proj.unsupported == [
            f"{label} (rds): an RDS name must be lowercase letters/digits separated by "
            "single hyphens and start with a letter (e.g. app-db) — rename the node",
        ], label
        assert "aws_db_instance" not in proj.files["main.tf"]


def test_rds_allocated_storage_must_be_a_number():
    res = ResourceDesired(id="app-db", kind="rds", fields={"allocatedStorage": FieldValue(value="lots")})
    proj = generate_tf(Stack(resources=(res,)))
    assert proj.unsupported == ["app-db (rds): allocatedStorage must be a whole number of GiB (e.g. 20)"]


def test_generic_unsupported_kind_gets_a_fallback_reason():
    # generate_tf consumes any Stack, not only ones canvas_to_stack can
    # produce today -- a future/unknown kind must still be listed, not dropped.
    stack = Stack(resources=(ResourceDesired(id="cache", kind="redis"),))
    proj = generate_tf(stack)
    assert proj.unsupported == ["cache (redis): redis — not supported in Simulate v1"]
    assert proj.files["main.tf"].strip().endswith('region = "us-east-1"\n}'.strip())


def test_determinism_two_calls_identical():
    stack = canvas_to_stack(_FULL_CANVAS)
    assert generate_tf(stack).files == generate_tf(stack).files


def test_determinism_independent_of_node_insertion_order():
    shuffled = dict(_FULL_CANVAS, nodes=list(reversed(_FULL_CANVAS["nodes"])))
    a = generate_tf(canvas_to_stack(_FULL_CANVAS)).files["main.tf"]
    b = generate_tf(canvas_to_stack(shuffled)).files["main.tf"]
    assert a == b


def test_sanitizer_lowercases_strips_punctuation_and_prefixes_leading_digit():
    stack = Stack(resources=(
        ResourceDesired(id="Data Lake!", kind="s3"),
        ResourceDesired(id="3buckets", kind="s3"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_s3_bucket" "data_lake_"' in main_tf
    assert 'resource "aws_s3_bucket" "_3buckets"' in main_tf
    # the raw label is preserved as the actual AWS-facing bucket name
    assert '= "Data Lake!"' in main_tf


def test_sanitizer_collision_gets_numeric_suffix():
    # "Data Lake!" and "data lake?" both sanitize to "data_lake_".
    stack = Stack(resources=(
        ResourceDesired(id="Data Lake!", kind="s3"),
        ResourceDesired(id="data lake?", kind="s3"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_s3_bucket" "data_lake_"' in main_tf
    assert 'resource "aws_s3_bucket" "data_lake__2"' in main_tf


def test_dynamodb_hash_key_defaults_to_id_when_field_absent():
    stack = Stack(resources=(ResourceDesired(id="items", kind="dynamodb"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "hash_key     = \"id\"" in main_tf
    assert '    name = "id"' in main_tf


def test_dynamodb_hash_key_honors_node_field():
    stack = Stack(resources=(
        ResourceDesired(id="items", kind="dynamodb",
                         fields={"hashKey": FieldValue(value="pk", provenance="user")}),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'hash_key     = "pk"' in main_tf


def test_sns_sqs_edge_becomes_topic_subscription_with_resource_references():
    stack = canvas_to_stack(_FULL_CANVAS)
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_sns_topic_subscription" "alerts_jobs"' in main_tf
    assert "topic_arn            = aws_sns_topic.alerts.arn" in main_tf
    assert "endpoint             = aws_sqs_queue.jobs.arn" in main_tf
    assert "raw_message_delivery = true" in main_tf


def test_edge_between_non_sns_sqs_kinds_produces_no_subscription():
    canvas = {
        "nodes": [
            {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
            {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],  # s3 -> sqs, not sns -> sqs
    }
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert "aws_sns_topic_subscription" not in main_tf


def test_generated_hcl_never_contains_local_endpoints_or_credentials():
    # Global Constraint (2026-07-22-s-simulate-translation.md): portable TF
    # only -- no `endpoints {}` provider overrides, no skip_* flags, no local
    # URLs, no creds. Those live in odin's runtime-generated override.tf,
    # never in agent/generator output. (Two RESOURCE arguments that merely
    # look like the banned provider ones are excluded, being legitimate TF
    # schema fields: the subscription's `endpoint = aws_sqs_queue...`, and
    # `aws_db_instance.skip_final_snapshot` -- so the `skip_*` check names the
    # four real PROVIDER args odin's own override.tf carries, not the prefix.)
    stack = canvas_to_stack(_FULL_CANVAS)
    main_tf = generate_tf(stack).files["main.tf"].lower()
    forbidden = (
        "endpoints {", "access_key", "secret_key", "127.0.0.1", "localhost",
        "skip_credentials_validation", "skip_metadata_api_check",
        "skip_region_validation", "skip_requesting_account_id",
    )
    for token in forbidden:
        assert token not in main_tf, token


def _fields(**kwargs: str) -> dict[str, FieldValue]:
    return {k: FieldValue(value=v, provenance="user") for k, v in kwargs.items()}


def test_vpc_alone_emits_aws_vpc_with_cidr():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc", fields=_fields(cidr="10.9.0.0/16")),
    ))
    proj = generate_tf(stack)
    assert 'resource "aws_vpc" "net"' in proj.files["main.tf"]
    assert 'cidr_block = "10.9.0.0/16"' in proj.files["main.tf"]
    assert proj.unsupported == []


def test_vpc_cidr_defaults_when_field_absent():
    main_tf = generate_tf(Stack(resources=(ResourceDesired(id="net", kind="vpc"),))).files["main.tf"]
    assert 'cidr_block = "10.0.0.0/16"' in main_tf


def test_subnet_references_its_containing_vpc_by_hcl_name():
    # V1c containment: the UI stamps data.vpc with the containing VPC's LABEL
    # (labels are Stack resource ids); the builder resolves it to the HCL name.
    stack = Stack(resources=(
        ResourceDesired(id="Net Prod!", kind="vpc"),  # sanitizes to net_prod_
        ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="Net Prod!", cidr="10.0.2.0/24")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_subnet" "web"' in main_tf
    assert "vpc_id     = aws_vpc.net_prod_.id" in main_tf
    assert 'cidr_block = "10.0.2.0/24"' in main_tf


def test_two_pass_naming_survives_subnet_sorting_before_vpc():
    # Regression guard for the single-pass bug: kinds sort alphabetically and
    # "subnet" < "vpc", so a subnet's builder runs before the vpc is visited.
    # With incremental (pass-interleaved) name assignment the vpc would have
    # no HCL name yet and the reference would be lost — pass 1 must assign
    # ALL names before pass 2 builds any block.
    stack = Stack(resources=(
        ResourceDesired(id="app", kind="subnet", fields=_fields(vpc="net")),
        ResourceDesired(id="net", kind="vpc"),
    ))
    proj = generate_tf(stack)
    assert "vpc_id     = aws_vpc.net.id" in proj.files["main.tf"]
    assert proj.unsupported == []


def test_subnet_without_containing_vpc_lands_in_unsupported():
    proj = generate_tf(Stack(resources=(ResourceDesired(id="orphan", kind="subnet"),)))
    assert proj.unsupported == [
        "orphan (subnet): not contained inside a VPC on the canvas (drag it into a VPC box)"
    ]
    assert "aws_subnet" not in proj.files["main.tf"]


def test_subnet_contained_in_a_non_vpc_resource_lands_in_unsupported():
    stack = Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"),
        ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="uploads")),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == [
        "web (subnet): not contained inside a VPC on the canvas (drag it into a VPC box)"
    ]


def test_sg_with_two_ingress_rules_emits_both_blocks_plus_default_egress():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web-sg", kind="sg",
                        fields=_fields(vpc="net", ingressRules="tcp:443:0.0.0.0/0\ntcp:22:10.0.0.0/16")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_security_group" "web_sg"' in main_tf
    assert "vpc_id = aws_vpc.net.id" in main_tf
    assert "    from_port   = 443" in main_tf
    assert "    from_port   = 22" in main_tf
    assert '    cidr_blocks = ["10.0.0.0/16"]' in main_tf
    # AWS seeds allow-all egress; the provider removes it unless the config
    # states it — emitted explicitly so apply -> plan stays zero-drift.
    assert '    protocol    = "-1"' in main_tf


def test_sg_with_no_rules_emits_only_the_default_egress():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="quiet", kind="sg", fields=_fields(vpc="net")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "ingress {" not in main_tf
    assert "egress {" in main_tf


def test_sg_outside_a_vpc_lands_in_unsupported():
    proj = generate_tf(Stack(resources=(ResourceDesired(id="stray", kind="sg"),)))
    assert proj.unsupported == [
        "stray (sg): not contained inside a VPC on the canvas (drag it into a VPC box)"
    ]


def test_sg_with_malformed_ingress_rule_lands_in_unsupported():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="bad", kind="sg", fields=_fields(vpc="net", ingressRules="443 from anywhere")),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == [
        'bad (sg): invalid ingress rule — expected one "protocol:port:source" per line, e.g. tcp:443:0.0.0.0/0'
    ]


def test_sg_ingress_can_name_another_sg_as_its_source():
    """W2.6: "5432, from the web tier only" -- the AWS-idiomatic
    UserIdGroupPairs rule, which is the ONLY source form that gates by
    identity (a nebula `group:` rule matched against the peer's cert)
    rather than by address."""
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:80:0.0.0.0/0")),
        ResourceDesired(id="db-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:5432:web-sg")),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == []
    body = proj.files["main.tf"]
    assert "    security_groups = [aws_security_group.web_sg.id]" in body
    assert "tcp:5432:web-sg" not in body  # the rule is compiled, not pasted


def test_sg_ingress_naming_a_non_sg_source_lands_in_unsupported():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="db-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:5432:web-tier")),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == [
        "db-sg (sg): ingress rule source 'web-tier' is neither a CIDR (like 10.0.0.0/16) "
        "nor the name of another Security Group node on the canvas"
    ]


def test_sg_ingress_naming_itself_is_unsupported_not_a_tf_cycle():
    """A same-SG self-reference is real AWS (and needs TF's `self = true`) --
    unmodeled, so it must be REPORTED, never emitted as an HCL self-reference
    (which tofu rejects as a cycle)."""
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="app-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:5432:app-sg")),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported and proj.unsupported[0].startswith("app-sg (sg): ingress rule source 'app-sg'")
    assert "aws_security_group" not in proj.files["main.tf"]


def test_iam_role_emits_name_and_the_lambda_trust_policy():
    stack = Stack(resources=(ResourceDesired(id="lambda-exec", kind="iam_role"),))
    proj = generate_tf(stack)
    main_tf = proj.files["main.tf"]
    assert 'resource "aws_iam_role" "lambda_exec"' in main_tf
    assert '  name = "lambda-exec"' in main_tf
    assert 'Service = "lambda.amazonaws.com"' in main_tf
    assert 'Action    = "sts:AssumeRole"' in main_tf
    assert proj.unsupported == []
    assert "aws_iam_role_policy" not in main_tf  # no inlinePolicy field -> no second block


def test_iam_role_with_inline_policy_emits_a_separate_role_policy_resource():
    doc = '{"Version": "2012-10-17", "Statement": []}'
    stack = Stack(resources=(
        ResourceDesired(id="lambda-exec", kind="iam_role", fields=_fields(inlinePolicy=doc)),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_iam_role" "lambda_exec"' in main_tf
    assert 'resource "aws_iam_role_policy" "lambda_exec_inline"' in main_tf
    assert "  role   = aws_iam_role.lambda_exec.name" in main_tf
    assert '  policy = "{\\"Version\\": \\"2012-10-17\\", \\"Statement\\": []}"' in main_tf


def test_iam_role_with_blank_inline_policy_emits_no_second_block():
    stack = Stack(resources=(
        ResourceDesired(id="lambda-exec", kind="iam_role", fields=_fields(inlinePolicy="   ")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "aws_iam_role_policy" not in main_tf


def test_ecr_emits_repository_name():
    stack = Stack(resources=(ResourceDesired(id="app-image", kind="ecr"),))
    proj = generate_tf(stack)
    main_tf = proj.files["main.tf"]
    assert 'resource "aws_ecr_repository" "app_image"' in main_tf
    assert 'name = "app-image"' in main_tf
    assert proj.unsupported == []


def test_tofu_fmt_accepts_iam_role_and_ecr_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(
        ResourceDesired(id="lambda-exec", kind="iam_role", fields=_fields(inlinePolicy='{"Version": "2012-10-17"}')),
        ResourceDesired(id="app-image", kind="ecr"),
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tofu_fmt_accepts_vpc_subnet_sg_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
        ResourceDesired(id="web-sg", kind="sg",
                        fields=_fields(vpc="net", ingressRules="tcp:443:0.0.0.0/0")),
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tofu_fmt_check_accepts_generated_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = canvas_to_stack(_FULL_CANVAS)
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- ec2 (V3c) -----------------------------------------------------------------


def _subnet_stack(*ec2_nodes: ResourceDesired) -> Stack:
    return Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
        *ec2_nodes,
    ))


def test_ec2_in_a_subnet_emits_ami_type_and_subnet_ref():
    stack = _subnet_stack(ResourceDesired(
        id="server", kind="ec2", fields=_fields(subnet="web", ami="ami-custom", instanceType="t3.small"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_instance" "server"' in main_tf
    assert 'ami           = "ami-custom"' in main_tf
    assert 'instance_type = "t3.small"' in main_tf
    assert "subnet_id     = aws_subnet.web.id" in main_tf


def test_ec2_defaults_ami_and_instance_type_when_fields_absent():
    stack = _subnet_stack(ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'ami           = "ami-0c101f26f147fa7fd"' in main_tf
    assert 'instance_type = "t3.micro"' in main_tf


def test_ec2_outside_a_subnet_lands_in_unsupported():
    proj = generate_tf(Stack(resources=(ResourceDesired(id="stray", kind="ec2"),)))
    assert proj.unsupported == [
        "stray (ec2): not contained inside a Subnet on the canvas (drag it into a Subnet box)"
    ]
    assert "aws_instance" not in proj.files["main.tf"]


def test_ec2_security_groups_field_references_sg_nodes():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
        ResourceDesired(id="web-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:22:0.0.0.0/0")),
        ResourceDesired(id="db-sg", kind="sg", fields=_fields(vpc="net", ingressRules="tcp:5432:10.0.0.0/16")),
        ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web", securityGroups="web-sg\ndb-sg")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "vpc_security_group_ids = [aws_security_group.web_sg.id, aws_security_group.db_sg.id]" in main_tf


def test_ec2_with_unknown_security_group_label_lands_in_unsupported():
    stack = _subnet_stack(ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web", securityGroups="ghost")))
    proj = generate_tf(stack)
    assert proj.unsupported == [
        "server (ec2): securityGroups names something that isn't a Security Group on the canvas"
    ]


def test_ec2_with_no_security_groups_field_omits_the_argument():
    stack = _subnet_stack(ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "vpc_security_group_ids" not in main_tf


def test_ec2_user_data_becomes_a_plain_string_argument():
    stack = _subnet_stack(ResourceDesired(
        id="server", kind="ec2", fields=_fields(subnet="web", userData="#!/bin/bash\necho hi\n"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert '  user_data = "#!/bin/bash\\necho hi\\n"' in main_tf


def test_ec2_key_field_emits_a_companion_key_pair_and_references_it():
    stack = _subnet_stack(ResourceDesired(
        id="server", kind="ec2", fields=_fields(subnet="web", key="ssh-ed25519 AAAAtest me@host"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_key_pair" "server_key"' in main_tf
    assert 'public_key = "ssh-ed25519 AAAAtest me@host"' in main_tf
    assert 'key_name   = "server-key"' in main_tf
    assert "  key_name = aws_key_pair.server_key.key_name" in main_tf


def test_ec2_without_key_field_emits_no_key_pair():
    stack = _subnet_stack(ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "aws_key_pair" not in main_tf
    assert "key_name" not in main_tf


def test_tofu_fmt_accepts_ec2_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = _subnet_stack(ResourceDesired(
        id="server", kind="ec2",
        fields=_fields(
            subnet="web", key="ssh-ed25519 AAAAtest me@host",
            userData="#!/bin/bash\necho hi\n", instanceType="t3.small",
        ),
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- lambda (V4c) ----------------------------------------------------------


def test_lambda_with_explicit_role_references_it_and_emits_no_companion_role():
    stack = Stack(resources=(
        ResourceDesired(id="lambda-exec", kind="iam_role"),
        ResourceDesired(id="fn1", kind="lambda", fields=_fields(role="lambda-exec")),
    ))
    proj = generate_tf(stack)
    main_tf = proj.files["main.tf"]
    assert 'resource "aws_lambda_function" "fn1"' in main_tf
    assert "role             = aws_iam_role.lambda_exec.arn" in main_tf
    # Only ONE aws_iam_role block -- the explicit one, no auto-generated companion.
    assert main_tf.count('resource "aws_iam_role"') == 1
    assert proj.unsupported == []


def test_lambda_without_role_field_auto_generates_a_companion_role():
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_iam_role" "fn1_role"' in main_tf
    assert '  name = "fn1-role"' in main_tf
    assert 'Service = "lambda.amazonaws.com"' in main_tf  # same default trust doc as _iam_role
    assert "role             = aws_iam_role.fn1_role.arn" in main_tf


def test_lambda_with_unknown_role_label_lands_in_unsupported():
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda", fields=_fields(role="ghost")),))
    proj = generate_tf(stack)
    assert proj.unsupported == [
        "fn1 (lambda): role names something that isn't an IAM Role on the canvas"
    ]
    assert "aws_lambda_function" not in proj.files["main.tf"]
    # No orphan companion role for a lambda that DID name a (bad) role.
    assert "aws_iam_role" not in proj.files["main.tf"]


def test_lambda_defaults_runtime_and_handler_when_fields_absent():
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'runtime          = "python3.12"' in main_tf
    assert 'handler          = "lambda_function.lambda_handler"' in main_tf


def test_lambda_honors_explicit_runtime_and_handler():
    stack = Stack(resources=(
        ResourceDesired(id="fn1", kind="lambda", fields=_fields(runtime="nodejs20.x", handler="index.custom")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'runtime          = "nodejs20.x"' in main_tf
    assert 'handler          = "index.custom"' in main_tf


def test_lambda_filename_and_hash_reference_its_own_zip():
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'filename         = "fn1.zip"' in main_tf
    assert "source_code_hash = filebase64sha256(\"fn1.zip\")" in main_tf


def test_lambda_materializes_a_real_zip_with_the_pasted_code():
    stack = Stack(resources=(
        ResourceDesired(id="fn1", kind="lambda", fields=_fields(code="def lambda_handler(e, c):\n    return 42\n")),
    ))
    proj = generate_tf(stack)
    assert set(proj.binary_files) == {"fn1.zip"}
    with zipfile.ZipFile(io.BytesIO(proj.binary_files["fn1.zip"])) as archive:
        assert archive.namelist() == ["lambda_function.py"]
        assert archive.read("lambda_function.py").decode() == "def lambda_handler(e, c):\n    return 42\n"


def test_lambda_with_no_code_field_defaults_to_the_echo_handler():
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda"),))
    proj = generate_tf(stack)
    with zipfile.ZipFile(io.BytesIO(proj.binary_files["fn1.zip"])) as archive:
        assert "return event" in archive.read("lambda_function.py").decode()


def test_lambda_zip_is_byte_identical_across_translates():
    """Field-test 2 finding HIGH-4: the zip is what `source_code_hash =
    filebase64sha256(...)` hashes, so a zip whose bytes move between two
    translates of the SAME canvas makes every plan report `1 to change` and
    every Apply redeploy the function -- and `tofu plan -detailed-exitcode`
    useless as a drift check for any canvas with a Lambda."""
    stack = Stack(resources=(
        ResourceDesired(id="fn1", kind="lambda", fields=_fields(code="def lambda_handler(e, c):\n    return 1\n")),
    ))
    first = generate_tf(stack).binary_files["fn1.zip"]
    time.sleep(1.1)  # long enough to cross a DOS-timestamp (2s) boundary
    second = generate_tf(stack).binary_files["fn1.zip"]
    assert first == second


def test_lambda_zip_entry_metadata_is_fixed_not_wall_clock():
    """The mechanism behind the test above, asserted directly: a fixed
    timestamp (the ZIP epoch) and fixed 0644 permissions, so nothing about
    WHEN or WHERE the translate ran leaks into the archive."""
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda"),))
    proj = generate_tf(stack)
    with zipfile.ZipFile(io.BytesIO(proj.binary_files["fn1.zip"])) as archive:
        (info,) = archive.infolist()
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.external_attr >> 16 == 0o100644
        assert info.create_system == 3  # unix, not "whatever host built it"


def test_lambda_nodejs_runtime_zips_index_js():
    stack = Stack(resources=(
        ResourceDesired(id="fn1", kind="lambda", fields=_fields(runtime="nodejs20.x", code="exports.handler = (e) => e;")),
    ))
    proj = generate_tf(stack)
    with zipfile.ZipFile(io.BytesIO(proj.binary_files["fn1.zip"])) as archive:
        assert archive.namelist() == ["index.js"]


def test_tofu_fmt_accepts_lambda_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(ResourceDesired(id="fn1", kind="lambda"),))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- canvas wiring: depends_on, no values (field test 2, the product hole) ----

_WIRED_CANVAS = {
    "nodes": [
        {"id": "n1", "type": "rds", "data": {"label": "app-db", "password": "pw123"}},
        {"id": "n2", "type": "elasticache", "data": {"label": "cache"}},
        {"id": "n3", "type": "ecs", "data": {
            "label": "web", "image": "nginx:alpine",
            "env": {"DATABASE_URL": "${{app-db.DATABASE_URL}}", "REDIS_URL": "${{cache.REDIS_URL}}"},
        }},
    ],
    "edges": [],
}


def test_a_wired_ecs_service_depends_on_its_producers_but_carries_no_values():
    """The values are injected at container launch (`gateway/wiring.py`) so a
    resolved DATABASE_URL -- which embeds the DB PASSWORD -- never lands in
    main.tf or terraform.tfstate. `depends_on` buys back the one thing an
    interpolated value would have given for free: ordering."""
    proj = generate_tf(canvas_to_stack(_WIRED_CANVAS))
    main_tf = proj.files["main.tf"]
    assert "depends_on = [aws_db_instance.app_db, aws_elasticache_cluster.cache]" in main_tf
    assert "environment" not in main_tf, "no env values in the HCL -- that is the whole point"
    # The rds `password` argument is the ONE legitimate place the plaintext
    # appears (tofu has to send it); the resolved DATABASE_URL would have been a
    # second copy, in the service's own block and in tofu state.
    assert main_tf.count("pw123") == 1, main_tf
    assert proj.unsupported == []


def test_a_wired_lambda_depends_on_its_producers():
    canvas = {
        "nodes": [
            {"id": "n1", "type": "rds", "data": {"label": "app-db"}},
            {"id": "n2", "type": "lambda", "data": {
                "label": "fn1", "env": {"DATABASE_URL": "${{app-db.DATABASE_URL}}"}}},
        ],
        "edges": [],
    }
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert "depends_on = [aws_db_instance.app_db]" in main_tf


def test_an_unwired_ecs_node_emits_no_depends_on():
    stack = Stack(resources=(ResourceDesired(id="app", kind="ecs"),))
    assert "depends_on" not in generate_tf(stack).files["main.tf"]


def test_a_ref_to_a_node_that_is_not_on_the_canvas_is_reported_not_dropped():
    """Northstar directive 5: a typo'd producer name can never be wired, so say
    so in the apply response instead of silently omitting the variable and
    letting the container fail far from the cause. The service itself is still
    built -- the rest of it is valid."""
    canvas = {
        "nodes": [{"id": "n1", "type": "ecs", "data": {
            "label": "web", "env": {"DATABASE_URL": "${{typo-db.DATABASE_URL}}"}}}],
        "edges": [],
    }
    proj = generate_tf(canvas_to_stack(canvas))
    assert 'resource "aws_ecs_service" "web"' in proj.files["main.tf"]
    assert "depends_on" not in proj.files["main.tf"]
    (note,) = proj.unsupported
    assert "typo-db" in note and "DATABASE_URL" in note and "web" in note


def test_tofu_fmt_accepts_a_wired_service(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"])
    result = subprocess.run([tofu, "fmt", "-check", "-diff", str(main_tf)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# --- ecs (V5c) ---------------------------------------------------------------


def test_ecs_emits_service_taskdef_and_one_shared_cluster():
    stack = Stack(resources=(
        ResourceDesired(id="app", kind="ecs", fields=_fields(image="nginx:alpine", count="2", port="80")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_ecs_cluster" "odin"' in main_tf
    assert 'name = "odin"' in main_tf
    assert 'resource "aws_ecs_service" "app"' in main_tf
    assert "cluster                            = aws_ecs_cluster.odin.id" in main_tf
    assert "task_definition                    = aws_ecs_task_definition.app_taskdef.arn" in main_tf
    assert "desired_count                      = 2" in main_tf
    assert 'launch_type                        = "EC2"' in main_tf
    # finding #3: apply must wait for the service to converge and fail fast
    # (bounded) if a bad image / crash-on-start keeps it from running.
    assert "wait_for_steady_state              = true" in main_tf
    # Field test 3: the rolling-update contract is emitted EXPLICITLY -- it is
    # what keeps the PREVIOUS revision's tasks serving when a new image fails
    # (gateway/models/ecsctl.py's `_retire_stale`), so it must not be left to
    # an implicit provider default.
    assert "deployment_minimum_healthy_percent = 100" in main_tf
    assert "deployment_maximum_percent         = 200" in main_tf
    assert "timeouts {" in main_tf
    assert 'create = "60s"' in main_tf
    assert 'resource "aws_ecs_task_definition" "app_taskdef"' in main_tf
    assert 'family                   = "app"' in main_tf
    assert '\\"image\\": \\"nginx:alpine\\"' in main_tf
    assert '\\"containerPort\\": 80' in main_tf


def test_ecs_defaults_image_count_and_port_when_fields_absent():
    stack = Stack(resources=(ResourceDesired(id="app", kind="ecs"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "desired_count                      = 1" in main_tf
    assert '\\"image\\": \\"nginx:alpine\\"' in main_tf
    assert '\\"containerPort\\": 80' in main_tf


def test_ecs_multiple_nodes_share_one_cluster():
    stack = Stack(resources=(
        ResourceDesired(id="app-a", kind="ecs"),
        ResourceDesired(id="app-b", kind="ecs"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert main_tf.count('resource "aws_ecs_cluster"') == 1
    assert 'resource "aws_ecs_service" "app_a"' in main_tf
    assert 'resource "aws_ecs_service" "app_b"' in main_tf
    assert 'resource "aws_ecs_task_definition" "app_a_taskdef"' in main_tf
    assert 'resource "aws_ecs_task_definition" "app_b_taskdef"' in main_tf
    assert "cluster                            = aws_ecs_cluster.odin.id" in main_tf.split('"aws_ecs_service" "app_a"')[1]
    assert "cluster                            = aws_ecs_cluster.odin.id" in main_tf.split('"aws_ecs_service" "app_b"')[1]


def test_ecs_with_non_numeric_count_lands_in_unsupported():
    stack = Stack(resources=(ResourceDesired(id="app", kind="ecs", fields=_fields(count="two")),))
    proj = generate_tf(stack)
    assert proj.unsupported == ["app (ecs): count must be a whole number (e.g. 2)"]
    assert "aws_ecs_service" not in proj.files["main.tf"]


def test_ecs_with_non_numeric_port_lands_in_unsupported():
    stack = Stack(resources=(ResourceDesired(id="app", kind="ecs", fields=_fields(port="http")),))
    proj = generate_tf(stack)
    assert proj.unsupported == ["app (ecs): port must be a whole number (e.g. 80)"]
    assert "aws_ecs_service" not in proj.files["main.tf"]


# --- logs (W2.1) -------------------------------------------------------------


def test_logs_emits_the_group_name_and_the_odin_node_tag():
    # The canvas label IS the log group name -- the gateway classifies every
    # logs:* call by bare group name, so an IAM edge drawn to this node only
    # enforces while the two are the same string.
    stack = Stack(resources=(ResourceDesired(id="/odin/app", kind="logs"),))
    proj = generate_tf(stack)
    main_tf = proj.files["main.tf"]
    assert 'resource "aws_cloudwatch_log_group" "_odin_app"' in main_tf
    assert '  name = "/odin/app"' in main_tf
    assert '"odin:node" = "/odin/app"' in main_tf
    assert proj.unsupported == []


def test_logs_without_a_retention_field_omits_retention_in_days():
    # AWS's own default is "never expire" -- an invented number would silently
    # start deleting the user's logs.
    main_tf = generate_tf(Stack(resources=(ResourceDesired(id="app-logs", kind="logs"),))).files["main.tf"]
    assert "retention_in_days" not in main_tf


def test_logs_retention_field_becomes_retention_in_days():
    stack = Stack(resources=(ResourceDesired(id="app-logs", kind="logs", fields=_fields(retentionInDays="14")),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'name              = "app-logs"' in main_tf
    assert "retention_in_days = 14" in main_tf


def test_logs_with_non_numeric_retention_lands_in_unsupported():
    stack = Stack(resources=(ResourceDesired(id="app-logs", kind="logs", fields=_fields(retentionInDays="two weeks")),))
    proj = generate_tf(stack)
    assert proj.unsupported == ["app-logs (logs): retentionInDays must be a whole number of days (e.g. 14)"]
    assert "aws_cloudwatch_log_group" not in proj.files["main.tf"]


def test_logs_with_fractional_retention_lands_in_unsupported():
    stack = Stack(resources=(ResourceDesired(id="app-logs", kind="logs", fields=_fields(retentionInDays="14.5")),))
    proj = generate_tf(stack)
    assert proj.unsupported == ["app-logs (logs): retentionInDays must be a whole number of days (e.g. 14)"]


def test_logs_block_parses_back_as_a_log_group_resource():
    # Zero-drift structural check: S3b's guardrail compares the skeleton's
    # resource SET against the agent's refinement, so a logs block must read
    # back through parse_tf under the identity generate_tf assigned it.
    stack = Stack(resources=(ResourceDesired(id="app-logs", kind="logs", fields=_fields(retentionInDays="14")),))
    files = generate_tf(stack).files
    assert ("aws_cloudwatch_log_group", "app_logs") in resource_set(files)
    attrs = resource_attrs(files)[("aws_cloudwatch_log_group", "app_logs")]
    assert unquote(attrs["name"]) == "app-logs"
    assert attrs["retention_in_days"] == 14  # hcl2 parses an unquoted number as an int


def test_tofu_fmt_accepts_logs_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(
        ResourceDesired(id="/odin/app", kind="logs", fields=_fields(retentionInDays="14")),
        ResourceDesired(id="app-logs", kind="logs"),  # the no-retention (single-attr) shape too
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- odin:node tagging (fix-wave 2b finding #2 prerequisite) ---------------
#
# Every primary node-backed resource carries `tags = { "odin:node" = <label> }`
# -- the ONE mechanism both the reconciler's TF-owned-status projection
# (vpc/subnet/ec2 have no other queryable label) and the gateway's
# substrate-launch credential issuance (EC2 cloud-init, ECS task containers,
# Lambda RIE containers) key off. Companion resources (a lambda's
# auto-generated role, an ec2's key pair, an sns->sqs subscription, an ecs
# node's task definition + the one shared cluster, an iam_role's inline
# policy) are NOT canvas nodes themselves, so they get no tag of their own.


def test_vpc_subnet_and_ec2_get_the_odin_node_tag():
    # These three kinds carry NO other AWS-native name/label field (real
    # CreateVpc/CreateSubnet/RunInstances have no such argument) -- the tag
    # is their ONLY way back to the canvas label.
    stack = _subnet_stack(ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_vpc" "net"' in main_tf
    vpc_block = main_tf.split('resource "aws_vpc" "net"')[1].split("\nresource")[0]
    assert '"odin:node" = "net"' in vpc_block
    subnet_block = main_tf.split('resource "aws_subnet" "web"')[1].split("\nresource")[0]
    assert '"odin:node" = "web"' in subnet_block
    ec2_block = main_tf.split('resource "aws_instance" "server"')[1].split("\nresource")[0]
    assert '"odin:node" = "server"' in ec2_block


def test_every_named_kind_with_a_builder_gets_the_tag():
    stack = Stack(resources=(
        ResourceDesired(id="uploads", kind="s3"),
        ResourceDesired(id="jobs", kind="sqs"),
        ResourceDesired(id="alerts", kind="sns"),
        ResourceDesired(id="items", kind="dynamodb"),
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web-sg", kind="sg", fields=_fields(vpc="net")),
        ResourceDesired(id="lambda-exec", kind="iam_role"),
        ResourceDesired(id="app-image", kind="ecr"),
        ResourceDesired(id="fn1", kind="lambda", fields=_fields(role="lambda-exec")),
        ResourceDesired(id="svc", kind="ecs"),
        ResourceDesired(id="app-logs", kind="logs"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    for label in ("uploads", "jobs", "alerts", "items", "web-sg", "lambda-exec",
                  "app-image", "fn1", "svc", "app-logs"):
        assert f'"odin:node" = "{label}"' in main_tf, label
    assert '"odin:node" = "net"' in main_tf


def test_companion_resources_get_no_odin_node_tag_of_their_own():
    stack = Stack(resources=(
        ResourceDesired(id="net", kind="vpc"),
        ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
        ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web", key="ssh-ed25519 AAAAtest me@host")),
        ResourceDesired(id="fn1", kind="lambda"),  # no role field -> auto-generated companion role
        ResourceDesired(id="app", kind="ecs"),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    for companion in ("aws_key_pair", "aws_iam_role", "aws_ecs_cluster", "aws_ecs_task_definition"):
        block = main_tf.split(f'resource "{companion}"')[1].split("\nresource")[0]
        assert "tags" not in block, companion


def test_tofu_fmt_accepts_ecs_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(
        ResourceDesired(id="app", kind="ecs", fields=_fields(image="nginx:alpine", count="2", port="80")),
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- secret + ssm (W2.4) -----------------------------------------------------


def test_secret_emits_the_name_an_immediate_recovery_window_and_the_odin_tag():
    # The canvas label IS the secret name -- the gateway classifies every
    # secretsmanager:* call by bare name, so an IAM edge drawn to this node only
    # enforces while the two are the same string.
    proj = generate_tf(Stack(resources=(ResourceDesired(id="db-password", kind="secret"),)))
    main_tf = proj.files["main.tf"]
    assert 'resource "aws_secretsmanager_secret" "db_password"' in main_tf
    assert 'name                    = "db-password"' in main_tf
    # odin's DeleteSecret is immediate, and the HCL says so rather than
    # implying a 30-day window odin doesn't have.
    assert "recovery_window_in_days = 0" in main_tf
    assert '"odin:node" = "db-password"' in main_tf
    assert proj.unsupported == []


def test_a_secret_with_a_value_emits_a_companion_version_resource():
    stack = Stack(resources=(
        ResourceDesired(id="db-password", kind="secret", fields=_fields(secretString="hunter2-and-then-some")),
    ))
    files = generate_tf(stack).files
    attrs = resource_attrs(files)[("aws_secretsmanager_secret_version", "db_password_version")]
    assert unquote(attrs["secret_string"]) == "hunter2-and-then-some"
    assert attrs["secret_id"] == "${aws_secretsmanager_secret.db_password.id}"


def test_a_secret_with_no_value_emits_no_version_resource_at_all():
    # An existing-but-valueless secret is a real AWS state; `secret_string = ""`
    # would assert a value nobody typed.
    main_tf = generate_tf(Stack(resources=(ResourceDesired(id="db-password", kind="secret"),))).files["main.tf"]
    assert "aws_secretsmanager_secret_version" not in main_tf


def test_a_secret_value_keeps_its_surrounding_whitespace():
    # Every other optional field is `.strip()`ed for emptiness; a secret's
    # whitespace is part of the secret, so this one deliberately isn't.
    stack = Stack(resources=(ResourceDesired(id="s", kind="secret", fields=_fields(secretString="  padded  ")),))
    attrs = resource_attrs(generate_tf(stack).files)[("aws_secretsmanager_secret_version", "s_version")]
    assert unquote(attrs["secret_string"]) == "  padded  "


def test_secret_description_is_emitted_only_when_set():
    with_desc = generate_tf(Stack(resources=(
        ResourceDesired(id="s", kind="secret", fields=_fields(description="the db password")),
    ))).files["main.tf"]
    without = generate_tf(Stack(resources=(ResourceDesired(id="s", kind="secret"),))).files["main.tf"]
    assert 'description             = "the db password"' in with_desc
    assert "description" not in without


def test_ssm_emits_name_type_and_value():
    stack = Stack(resources=(
        ResourceDesired(id="/odin/api-key", kind="ssm", fields=_fields(paramType="SecureString", paramValue="abc123")),
    ))
    proj = generate_tf(stack)
    files = proj.files
    attrs = resource_attrs(files)[("aws_ssm_parameter", "_odin_api_key")]
    assert unquote(attrs["name"]) == "/odin/api-key"
    assert unquote(attrs["type"]) == "SecureString"
    assert unquote(attrs["value"]) == "abc123"
    assert '"odin:node" = "/odin/api-key"' in files["main.tf"]
    assert proj.unsupported == []


def test_ssm_defaults_to_a_plain_string_parameter():
    stack = Stack(resources=(ResourceDesired(id="flag", kind="ssm", fields=_fields(paramValue="on")),))
    attrs = resource_attrs(generate_tf(stack).files)[("aws_ssm_parameter", "flag")]
    assert unquote(attrs["type"]) == "String"


def test_ssm_without_a_value_lands_in_unsupported():
    proj = generate_tf(Stack(resources=(ResourceDesired(id="flag", kind="ssm"),)))
    assert proj.unsupported == ["flag (ssm): needs a Value (an SSM parameter can't exist without one)"]
    assert "aws_ssm_parameter" not in proj.files["main.tf"]


def test_ssm_with_an_unknown_type_lands_in_unsupported():
    stack = Stack(resources=(
        ResourceDesired(id="flag", kind="ssm", fields=_fields(paramType="Encrypted", paramValue="on")),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == ["flag (ssm): type must be one of String, StringList, SecureString"]


def test_a_secret_version_carries_no_odin_node_tag():
    # Companion resources aren't canvas nodes (and aws_secretsmanager_secret_
    # version has no tags argument at all).
    stack = Stack(resources=(ResourceDesired(id="s", kind="secret", fields=_fields(secretString="value123")),))
    main_tf = generate_tf(stack).files["main.tf"]
    version_block = main_tf.split('resource "aws_secretsmanager_secret_version"')[1].split("\nresource")[0]
    assert "tags" not in version_block


def test_tofu_fmt_accepts_secret_and_ssm_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(
        ResourceDesired(id="db-password", kind="secret", fields=_fields(secretString="v", description="d")),
        ResourceDesired(id="bare", kind="secret"),
        ResourceDesired(id="/odin/api-key", kind="ssm", fields=_fields(paramType="SecureString", paramValue="abc")),
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run([tofu, "fmt", "-check", "-diff", str(main_tf)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# --- W2.8: elasticache -----------------------------------------------------


def test_elasticache_emits_a_single_node_redis_cluster():
    stack = Stack(resources=(
        ResourceDesired(id="cache", kind="elasticache", fields=_fields(nodeType="cache.t3.small")),
    ))
    proj = generate_tf(stack)
    main_tf = proj.files["main.tf"]
    assert 'resource "aws_elasticache_cluster" "cache"' in main_tf
    assert '  cluster_id      = "cache"' in main_tf
    assert '  engine          = "redis"' in main_tf
    assert '  node_type       = "cache.t3.small"' in main_tf
    assert "  num_cache_nodes = 1" in main_tf  # v1 is single-node; the gateway rejects anything else
    assert '    "odin:node" = "cache"' in main_tf
    assert proj.unsupported == []


def test_elasticache_defaults_the_node_type_when_the_field_is_absent():
    stack = Stack(resources=(ResourceDesired(id="cache", kind="elasticache"),))
    assert '  node_type       = "cache.t3.micro"' in generate_tf(stack).files["main.tf"]


def test_elasticache_omits_port_and_engine_version_so_they_stay_computed():
    # Pinning `port` in the config while the API honestly reports the REAL
    # published host port is a guaranteed plan diff on every apply -- both are
    # Optional+Computed, so leaving them out is what keeps plan zero-drift.
    main_tf = generate_tf(Stack(resources=(ResourceDesired(id="cache", kind="elasticache"),))).files["main.tf"]
    assert "port" not in main_tf
    assert "engine_version" not in main_tf


def test_tofu_fmt_accepts_elasticache_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = Stack(resources=(
        ResourceDesired(id="cache", kind="elasticache", fields=_fields(nodeType="cache.t3.micro")),
    ))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run(
        [tofu, "fmt", "-check", "-diff", str(main_tf)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- alb (W2.5) --------------------------------------------------------------
#
# ONE `alb` canvas node expands to THREE tf resources -- the primary `aws_lb`
# plus a companion `aws_lb_target_group` and `aws_lb_listener`, built in their
# own pass (the same one-canvas-node-to-N-tf-resources shape as an ecs task
# definition or a secret's version). Containment is a SUBNET, like `_ec2`: the
# subnet is what gives `aws_lb.subnets` a value and, transitively, the target
# group its `vpc_id` (the canvas stamps BOTH `vpc` and `subnet` on a leaf drawn
# inside a subnet box).


def test_alb_in_a_subnet_expands_into_a_load_balancer_target_group_and_listener():
    stack = _subnet_stack(ResourceDesired(
        id="front", kind="alb",
        fields=_fields(vpc="net", subnet="web", listenerPort="8080", port="3000", healthCheckPath="/healthz"),
    ))
    proj = generate_tf(stack)
    main_tf = proj.files["main.tf"]
    # exactly three -- one primary + two companions, no more
    assert sorted(r for r in resource_set(proj.files) if r[0].startswith("aws_lb")) == [
        ("aws_lb", "front"), ("aws_lb_listener", "front_listener"), ("aws_lb_target_group", "front_tg"),
    ]
    assert 'resource "aws_lb" "front"' in main_tf
    assert 'name               = "front"' in main_tf
    # odin has no internet gateway, so an internet-facing scheme would be a
    # claim nothing backs -- `internal` is emitted explicitly, always.
    assert "internal           = true" in main_tf
    assert 'load_balancer_type = "application"' in main_tf
    assert "subnets = [aws_subnet.web.id]" in main_tf
    assert 'resource "aws_lb_target_group" "front_tg"' in main_tf
    assert 'name        = "front-tg"' in main_tf
    assert "port        = 3000" in main_tf
    assert "vpc_id      = aws_vpc.net.id" in main_tf
    assert 'target_type = "instance"' in main_tf
    assert '  health_check {\n    path = "/healthz"\n  }' in main_tf
    assert 'resource "aws_lb_listener" "front_listener"' in main_tf
    assert "load_balancer_arn = aws_lb.front.arn" in main_tf
    assert "port              = 8080" in main_tf
    assert '    type             = "forward"' in main_tf
    assert "    target_group_arn = aws_lb_target_group.front_tg.arn" in main_tf
    assert proj.unsupported == []


def test_alb_defaults_both_ports_to_80_and_the_health_check_to_root():
    stack = _subnet_stack(ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net", subnet="web")))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "port              = 80" in main_tf  # the listener
    assert "port        = 80" in main_tf        # the target group
    assert '  health_check {\n    path = "/"\n  }' in main_tf


def test_only_the_load_balancer_carries_the_odin_node_tag():
    # The two companions aren't canvas nodes, so they get no tag of their own
    # (same rule as a secret's version / an ecs task definition) -- `aws_lb` is
    # the ONE block that carries the trio back to the canvas label.
    stack = _subnet_stack(ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net", subnet="web")))
    main_tf = generate_tf(stack).files["main.tf"]
    lb_block = main_tf.split('resource "aws_lb" "front"')[1].split("\nresource")[0]
    assert '"odin:node" = "front"' in lb_block
    for companion in ("aws_lb_target_group", "aws_lb_listener"):
        block = main_tf.split(f'resource "{companion}"')[1].split("\nresource")[0]
        assert "tags" not in block, companion


def test_alb_outside_a_subnet_lands_in_unsupported():
    proj = generate_tf(Stack(resources=(ResourceDesired(id="stray", kind="alb"),)))
    assert proj.unsupported == [
        "stray (alb): not contained inside a Subnet on the canvas (drag it into a Subnet box)"
    ]
    assert 'resource "aws_lb"' not in proj.files["main.tf"]


def test_alb_of_type_network_lands_in_unsupported():
    # An NLB would need nginx's stream module and a TCP-only proxy shape, so a
    # `network` node reports itself rather than quietly getting an ALB.
    stack = _subnet_stack(ResourceDesired(
        id="front", kind="alb", fields=_fields(vpc="net", subnet="web", lbType="network"),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == [f"front (alb): {_ALB_NLB_UNSUPPORTED}"]
    assert 'resource "aws_lb"' not in proj.files["main.tf"]


def test_an_opted_out_alb_emits_no_orphan_companion_resources():
    """Regression, found in review: the companion pass used to re-derive `_alb`'s
    opt-out conditions instead of asking whether pass 2 had actually emitted the
    `aws_lb`, so a WITHHELD load balancer still got its target group + listener
    -- and `aws_lb_listener.load_balancer_arn` then pointed at a block that was
    never written, which fails `tofu plan` for the WHOLE project rather than just
    dropping one node. `generate_tf`'s `built_ids` set is the fix."""
    # Both ways `_alb` can opt a node out while the ports still parse and a vpc
    # ref still resolves -- a `network` type, and a node drawn inside a VPC but
    # not inside a Subnet.
    stacks = (
        _subnet_stack(ResourceDesired(
            id="front", kind="alb", fields=_fields(vpc="net", subnet="web", lbType="network"),
        )),
        Stack(resources=(
            ResourceDesired(id="net", kind="vpc"),
            ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net")),
        )),
    )
    for stack in stacks:
        main_tf = generate_tf(stack).files["main.tf"]
        assert "aws_lb_target_group" not in main_tf
        assert "aws_lb_listener" not in main_tf


def test_alb_with_non_numeric_listener_port_lands_in_unsupported():
    stack = _subnet_stack(ResourceDesired(
        id="front", kind="alb", fields=_fields(vpc="net", subnet="web", listenerPort="http"),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == ["front (alb): listenerPort must be a whole number (e.g. 80)"]
    assert 'resource "aws_lb"' not in proj.files["main.tf"]


def test_alb_with_non_numeric_target_port_lands_in_unsupported():
    stack = _subnet_stack(ResourceDesired(
        id="front", kind="alb", fields=_fields(vpc="net", subnet="web", port="eighty"),
    ))
    proj = generate_tf(stack)
    assert proj.unsupported == ["front (alb): port must be a whole number (e.g. 80)"]
    assert 'resource "aws_lb"' not in proj.files["main.tf"]


def _alb_ecs_stack(edge: Edge) -> Stack:
    return Stack(
        resources=(
            ResourceDesired(id="net", kind="vpc"),
            ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
            ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net", subnet="web")),
            ResourceDesired(id="app", kind="ecs", fields=_fields(port="3000")),
        ),
        edges=(edge,),
    )


def test_an_alb_target_edge_puts_a_load_balancer_block_on_the_ecs_service():
    # In real AWS the fronting relationship is a `load_balancer` block on the
    # SERVICE (the ECS scheduler registers each task itself), not an
    # `aws_lb_target_group_attachment` tofu would have to know the tasks for.
    proj = generate_tf(_alb_ecs_stack(Edge(src="front", dst="app", kind="network")))
    main_tf = proj.files["main.tf"]
    assert (
        "  load_balancer {\n"
        "    target_group_arn = aws_lb_target_group.front_tg.arn\n"
        '    container_name   = "app"\n'
        "    container_port   = 3000\n"
        "  }"
    ) in main_tf
    assert proj.unsupported == []


def test_an_alb_target_edge_drawn_the_other_way_round_means_the_same_thing():
    # Which end the user started dragging from carries no meaning, so both
    # directions produce byte-identical HCL.
    forward = generate_tf(_alb_ecs_stack(Edge(src="front", dst="app", kind="network")))
    reverse = generate_tf(_alb_ecs_stack(Edge(src="app", dst="front", kind="network")))
    assert "target_group_arn = aws_lb_target_group.front_tg.arn" in reverse.files["main.tf"]
    assert reverse.files["main.tf"] == forward.files["main.tf"]
    assert reverse.unsupported == []


def test_an_ecs_service_with_no_alb_edge_gets_no_load_balancer_block():
    stack = Stack(resources=(ResourceDesired(id="app", kind="ecs"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "load_balancer {" not in main_tf
    assert "aws_lb_target_group" not in main_tf


def test_an_alb_target_edge_to_an_ec2_node_lands_in_unsupported_exactly_once():
    # An ec2 target would need an `aws_lb_target_group_attachment` -- recorded
    # as an unbuilt limit instead of silently doing nothing with the edge the
    # user drew. Pass 1.5 tries BOTH edge directions, so the reason must not be
    # recorded twice for one edge.
    stack = Stack(
        resources=(
            ResourceDesired(id="net", kind="vpc"),
            ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
            ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net", subnet="web")),
            ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")),
        ),
        edges=(Edge(src="front", dst="server", kind="network"),),
    )
    proj = generate_tf(stack)
    assert proj.unsupported == [
        "front (alb): target edge to server (ec2) — only ecs nodes can be load-balancer targets in Simulate v1"
    ]
    assert "load_balancer {" not in proj.files["main.tf"]


def test_tofu_fmt_accepts_alb_output(tmp_path):
    tofu = shutil.which("tofu")
    if tofu is None:
        return  # skip cleanly -- no tofu on PATH in this environment
    stack = _alb_ecs_stack(Edge(src="front", dst="app", kind="network"))
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(generate_tf(stack).files["main.tf"])
    result = subprocess.run([tofu, "fmt", "-check", "-diff", str(main_tf)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
