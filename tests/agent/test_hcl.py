"""S3a — deterministic canvas -> Terraform skeleton generator."""
from __future__ import annotations

import shutil
import subprocess

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
}

resource "aws_s3_bucket" "uploads" {
  bucket = "uploads"
}

resource "aws_sns_topic" "alerts" {
  name = "alerts"
}

resource "aws_sns_topic_subscription" "alerts_jobs" {
  topic_arn            = aws_sns_topic.alerts.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.jobs.arn
  raw_message_delivery = true
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs"
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
