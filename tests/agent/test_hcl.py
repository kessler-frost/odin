"""S3a — deterministic canvas -> Terraform skeleton generator."""
from __future__ import annotations

import io
import shutil
import subprocess
import zipfile

from odin.agent.hcl import generate_tf
from odin.spec.models import FieldValue, ResourceDesired, Stack
from odin.spec.translate import canvas_to_stack

_FULL_CANVAS = {
    "nodes": [
        {"id": "n1", "type": "s3", "data": {"label": "uploads"}},
        {"id": "n2", "type": "sqs", "data": {"label": "jobs"}},
        {"id": "n3", "type": "sns", "data": {"label": "alerts"}},
        {"id": "n4", "type": "dynamodb", "data": {"label": "items", "hashKey": "pk"}},
        {"id": "n5", "type": "rds", "data": {"label": "db", "engine": "postgres"}},
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

resource "aws_s3_bucket" "uploads" {
  bucket = "uploads"

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


def test_rds_listed_unsupported_with_reason_never_dropped():
    stack = canvas_to_stack(_FULL_CANVAS)
    proj = generate_tf(stack)
    assert proj.unsupported == ["db (rds): Simulate v1 — stays on the reconciler path"]
    assert "aws_db_instance" not in proj.files["main.tf"]


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
    assert 'bucket = "Data Lake!"' in main_tf


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
    # never in agent/generator output. (The subscription resource's own
    # `endpoint = aws_sqs_queue...` argument is a legitimate TF schema field,
    # not a local-endpoint override, so it's excluded from this check.)
    stack = canvas_to_stack(_FULL_CANVAS)
    main_tf = generate_tf(stack).files["main.tf"].lower()
    forbidden = ("endpoints {", "skip_", "access_key", "secret_key", "127.0.0.1", "localhost")
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
        'bad (sg): invalid ingress rule — expected one "protocol:port:cidr" per line, e.g. tcp:443:0.0.0.0/0'
    ]


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


# --- ecs (V5c) ---------------------------------------------------------------


def test_ecs_emits_service_taskdef_and_one_shared_cluster():
    stack = Stack(resources=(
        ResourceDesired(id="app", kind="ecs", fields=_fields(image="nginx:alpine", count="2", port="80")),
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_ecs_cluster" "odin"' in main_tf
    assert 'name = "odin"' in main_tf
    assert 'resource "aws_ecs_service" "app"' in main_tf
    assert "cluster               = aws_ecs_cluster.odin.id" in main_tf
    assert "task_definition       = aws_ecs_task_definition.app_taskdef.arn" in main_tf
    assert "desired_count         = 2" in main_tf
    assert 'launch_type           = "EC2"' in main_tf
    # finding #3: apply must wait for the service to converge and fail fast
    # (bounded) if a bad image / crash-on-start keeps it from running.
    assert "wait_for_steady_state = true" in main_tf
    assert "timeouts {" in main_tf
    assert 'create = "60s"' in main_tf
    assert 'resource "aws_ecs_task_definition" "app_taskdef"' in main_tf
    assert 'family                   = "app"' in main_tf
    assert '\\"image\\": \\"nginx:alpine\\"' in main_tf
    assert '\\"containerPort\\": 80' in main_tf


def test_ecs_defaults_image_count_and_port_when_fields_absent():
    stack = Stack(resources=(ResourceDesired(id="app", kind="ecs"),))
    main_tf = generate_tf(stack).files["main.tf"]
    assert "desired_count         = 1" in main_tf
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
    assert "cluster               = aws_ecs_cluster.odin.id" in main_tf.split('"aws_ecs_service" "app_a"')[1]
    assert "cluster               = aws_ecs_cluster.odin.id" in main_tf.split('"aws_ecs_service" "app_b"')[1]


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


def test_s3_sqs_sns_dynamodb_sg_iam_role_ecr_lambda_ecs_all_get_the_tag():
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
    ))
    main_tf = generate_tf(stack).files["main.tf"]
    for label in ("uploads", "jobs", "alerts", "items", "web-sg", "lambda-exec", "app-image", "fn1", "svc"):
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
