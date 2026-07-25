"""S4 -- TF import (mode a: deterministic HCL -> canvas). Mode (b, live-state
import against a real gateway + backings) is exercised in
tests/simulate/test_import_tf_e2e.py (integration, needs Colima/tofu)."""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import LiveResource, _import_id, parse_hcl_dir, parse_hcl_text
from odin.spec.translate import canvas_to_stack

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
    result = parse_hcl_text('resource "aws_elasticache_cluster" "cache" {\n  cluster_id = "cache"\n}\n')
    assert result.nodes == []
    assert result.edges == []
    assert len(result.unsupported) == 1
    assert result.unsupported[0].type == "aws_elasticache_cluster"
    assert result.unsupported[0].name == "cache"
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


def test_malformed_hcl_sets_parse_error_not_unsupported():
    # Finding #7: a genuine parse failure is a distinct, hard error (parse_error),
    # NOT an "unsupported resource" -- so the CLI can exit non-zero on it while a
    # well-formed-but-unsupported file stays a success.
    result = parse_hcl_text("not { valid hcl")
    assert result.nodes == []
    assert result.unsupported == []
    assert result.parse_error is not None and "failed to parse" in result.parse_error


def test_valid_file_with_only_unsupported_resources_has_no_parse_error():
    result = parse_hcl_text('resource "aws_elasticache_cluster" "cache" {\n  cluster_id = "cache"\n}\n')
    assert result.parse_error is None
    assert len(result.unsupported) == 1


def test_dynamodb_hash_key_defaults_to_id_when_attribute_missing():
    tf = 'resource "aws_dynamodb_table" "t" {\n  name = "t"\n}\n'
    result = parse_hcl_text(tf)
    assert result.nodes[0]["data"]["hashKey"] == "id"


# --- finding #6: import carries load-bearing in-resource attributes ----------

_COMPOSITE_TF = '''
resource "aws_dynamodb_table" "orders" {
  name      = "orders"
  hash_key  = "userId"
  range_key = "createdAt"

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "createdAt"
    type = "N"
  }
}

resource "aws_s3_bucket" "data" {
  bucket = "data"

  tags = {
    Team = "platform"
    Env  = "prod"
  }
}

resource "aws_iam_role" "exec" {
  name               = "exec"
  assume_role_policy = "{}"
}
'''


def test_composite_dynamodb_carries_range_key_and_types():
    (node,) = [n for n in parse_hcl_text(_COMPOSITE_TF).nodes if n["type"] == "dynamodb"]
    assert node["data"]["hashKey"] == "userId"
    assert node["data"]["hashKeyType"] == "S"
    assert node["data"]["rangeKey"] == "createdAt"
    assert node["data"]["rangeKeyType"] == "N"


def test_s3_carries_user_tags_but_not_odins_own():
    tf = 'resource "aws_s3_bucket" "b" {\n  bucket = "b"\n  tags = { Team = "x"\n    "odin:node" = "b" }\n}\n'
    (node,) = parse_hcl_text(tf).nodes
    assert node["data"]["tags"] == {"Team": "x"}  # odin:node excluded


def test_iam_role_is_imported_not_listed_unsupported_with_a_warning():
    result = parse_hcl_text(_COMPOSITE_TF)
    (role,) = [n for n in result.nodes if n["type"] == "iam_role"]
    assert role["id"] == "exec"
    assert not any(u.type == "aws_iam_role" for u in result.unsupported)
    # the attribute it can't carry is WARNED, not silently dropped
    assert any("exec" in w and "assume_role_policy" in w for w in result.warnings)


def test_round_trip_preserves_composite_key_and_tags():
    imported = parse_hcl_text(_COMPOSITE_TF)
    regenerated = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges})).files["main.tf"]

    # dynamodb composite key survives (hash+range, correct types), not hash-only
    assert 'range_key    = "createdAt"' in regenerated
    assert regenerated.count("attribute {") == 2
    assert 'name = "createdAt"\n    type = "N"' in regenerated
    # s3 user tags survive (alongside odin's own management tag)
    assert '"Team"      = "platform"' in regenerated
    assert '"Env"       = "prod"' in regenerated
    # iam_role survives as an aws_iam_role
    assert 'resource "aws_iam_role" "exec"' in regenerated


# --- logs (W2.1): aws_cloudwatch_log_group <-> the `logs` canvas kind --------


_LOGS_TF = '''
resource "aws_cloudwatch_log_group" "app" {
  name              = "/odin/app"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "forever" {
  name = "/odin/forever"
}
'''


def test_log_group_imports_as_a_logs_node_with_the_group_name_as_its_label():
    result = parse_hcl_text(_LOGS_TF)
    by_id = {n["id"]: n for n in result.nodes}
    assert set(by_id) == {"/odin/app", "/odin/forever"}
    assert by_id["/odin/app"]["type"] == "logs"
    assert by_id["/odin/app"]["data"]["retentionInDays"] == "14"
    # No retention on the wire = AWS's "never expire"; the canvas field stays unset.
    assert "retentionInDays" not in by_id["/odin/forever"]["data"]
    assert result.unsupported == []
    assert result.warnings == []


def test_log_group_round_trip_reproduces_name_and_retention():
    imported = parse_hcl_text(_LOGS_TF)
    regenerated = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges})).files["main.tf"]
    assert 'name              = "/odin/app"' in regenerated
    assert "retention_in_days = 14" in regenerated
    assert 'name = "/odin/forever"' in regenerated
    assert regenerated.count("retention_in_days") == 1  # the never-expire group stays unset


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
