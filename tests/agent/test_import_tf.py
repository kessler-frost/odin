"""S4 -- TF import (mode a: deterministic HCL -> canvas). Mode (b, live-state
import against a real gateway + backings) is exercised in
tests/simulate/test_import_tf_e2e.py (integration, needs Colima/tofu)."""
from __future__ import annotations

from odin.agent.import_tf import LiveResource, _import_id, parse_hcl_dir, parse_hcl_text

_FULL_TF = '''
resource "aws_s3_bucket" "uploads" {
  bucket = "uploads"
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs"
}

resource "aws_sns_topic" "alerts" {
  name = "alerts"
}

resource "aws_sns_topic_subscription" "alerts_jobs" {
  topic_arn            = aws_sns_topic.alerts.arn
  protocol              = "sqs"
  endpoint             = aws_sqs_queue.jobs.arn
  raw_message_delivery = true
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

resource "aws_lambda_function" "fn" {
  function_name = "fn"
}
'''


def test_full_project_parses_into_nodes_edges_and_unsupported():
    result = parse_hcl_text(_FULL_TF)

    by_id = {n["id"]: n for n in result.nodes}
    assert set(by_id) == {"uploads", "jobs", "alerts", "items"}
    assert by_id["uploads"]["type"] == "s3"
    assert by_id["jobs"]["type"] == "sqs"
    assert by_id["alerts"]["type"] == "sns"
    assert by_id["items"]["type"] == "dynamodb"
    assert by_id["items"]["data"]["hashKey"] == "pk"

    assert result.edges == [{"source": "alerts", "target": "jobs"}]

    assert len(result.unsupported) == 1
    assert result.unsupported[0].type == "aws_lambda_function"
    assert result.unsupported[0].name == "fn"


def test_grid_positions_are_on_the_20px_grid_in_220px_steps():
    result = parse_hcl_text(_FULL_TF)
    by_id = {n["id"]: n["position"] for n in result.nodes}
    xs = sorted(p["x"] for p in by_id.values())
    assert xs == [0, 220, 440, 660]
    assert all(p["y"] == 0 for p in by_id.values())
    assert all(p["x"] % 20 == 0 for p in by_id.values())


def test_unsupported_type_never_dropped_even_when_alone():
    result = parse_hcl_text('resource "aws_iam_role" "role" {\n  name = "role"\n}\n')
    assert result.nodes == []
    assert result.edges == []
    assert len(result.unsupported) == 1
    assert result.unsupported[0].type == "aws_iam_role"
    assert result.unsupported[0].name == "role"
    assert "not supported" in result.unsupported[0].reason


def test_subscription_referencing_an_unknown_resource_is_listed_unsupported_not_dropped():
    tf = '''
resource "aws_sns_topic_subscription" "orphan" {
  topic_arn = aws_sns_topic.missing.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.also_missing.arn
}
'''
    result = parse_hcl_text(tf)
    assert result.edges == []
    assert len(result.unsupported) == 1
    assert result.unsupported[0].type == "aws_sns_topic_subscription"
    assert result.unsupported[0].name == "orphan"


def test_malformed_hcl_reported_as_unsupported_not_raised():
    result = parse_hcl_text("not { valid hcl")
    assert result.nodes == []
    assert len(result.unsupported) == 1
    assert "failed to parse" in result.unsupported[0].reason


def test_dynamodb_hash_key_defaults_to_id_when_attribute_missing():
    tf = 'resource "aws_dynamodb_table" "t" {\n  name = "t"\n}\n'
    result = parse_hcl_text(tf)
    assert result.nodes[0]["data"]["hashKey"] == "id"


def test_bucket_with_computed_name_falls_back_to_hcl_resource_name_as_label():
    tf = 'resource "aws_s3_bucket" "generated" {\n  bucket = "${var.prefix}-uploads"\n}\n'
    result = parse_hcl_text(tf)
    # not a plain quoted literal -> the HCL resource name is used, not dropped
    assert result.nodes[0]["id"] == "generated"


def test_parse_hcl_dir_reads_and_merges_all_tf_files(tmp_path):
    (tmp_path / "s3.tf").write_text('resource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n}\n')
    (tmp_path / "sqs.tf").write_text('resource "aws_sqs_queue" "jobs" {\n  name = "jobs"\n}\n')
    result = parse_hcl_dir(tmp_path)
    assert {n["id"] for n in result.nodes} == {"uploads", "jobs"}


# --- mode (b) helpers (pure, no subprocess) -----------------------------------


def test_import_id_s3_and_dynamodb_use_the_bare_name():
    assert _import_id(LiveResource(type="s3", id="uploads"), gateway_port=4266) == "uploads"
    assert _import_id(LiveResource(type="dynamodb", id="items"), gateway_port=4266) == "items"


def test_import_id_sqs_builds_a_gateway_routed_queue_url():
    result = _import_id(LiveResource(type="sqs", id="jobs"), gateway_port=4266)
    assert result == "http://127.0.0.1:4266/000000000000/jobs"


def test_import_id_sns_builds_an_arn():
    result = _import_id(LiveResource(type="sns", id="alerts"), gateway_port=4266)
    assert result == "arn:aws:sns:us-east-1:000000000000:alerts"
