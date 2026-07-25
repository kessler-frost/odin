"""S4 -- TF import (mode a: deterministic HCL -> canvas). Mode (b, live-state
import against a real gateway + backings) is exercised in
tests/simulate/test_import_tf_e2e.py (integration, needs Colima/tofu)."""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import _TF_TYPE, LiveResource, _import_id, parse_hcl_dir, parse_hcl_text
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
    result = parse_hcl_text('resource "aws_lambda_function" "fn" {\n  function_name = "fn"\n}\n')
    assert result.nodes == []
    assert result.edges == []
    assert len(result.unsupported) == 1
    assert result.unsupported[0].type == "aws_lambda_function"
    assert result.unsupported[0].name == "fn"
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
    result = parse_hcl_text('resource "aws_lambda_function" "fn" {\n  function_name = "fn"\n}\n')
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


# --- secret + ssm (W2.4): aws_secretsmanager_secret(+_version) <-> the
# `secret` kind, aws_ssm_parameter <-> the `ssm` kind. The VERSION resource is
# a COMPANION: it folds into its secret node's own value field (the exact
# inverse of hcl.py's companion pass), never a node of its own. ---------------


_SECRETS_TF = '''
resource "aws_secretsmanager_secret" "db_password" {
  name                    = "db-password"
  description             = "the db password"
  recovery_window_in_days = 0

  tags = {
    "odin:node" = "db-password"
    "team"      = "core"
  }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = "hunter2-and-then-some"
}

resource "aws_secretsmanager_secret" "empty" {
  name = "no-value-yet"
}

resource "aws_ssm_parameter" "api_key" {
  name  = "/odin/api-key"
  type  = "SecureString"
  value = "abc123456"
}
'''


def test_secret_and_ssm_import_as_their_canvas_kinds():
    result = parse_hcl_text(_SECRETS_TF)
    by_id = {n["id"]: n for n in result.nodes}
    assert set(by_id) == {"db-password", "no-value-yet", "/odin/api-key"}
    assert by_id["db-password"]["type"] == "secret"
    assert by_id["db-password"]["data"]["description"] == "the db password"
    # The user tag survives; odin's own management tag never surfaces as one.
    assert by_id["db-password"]["data"]["tags"] == {"team": "core"}
    assert by_id["/odin/api-key"]["type"] == "ssm"
    assert by_id["/odin/api-key"]["data"]["paramType"] == "SecureString"
    assert by_id["/odin/api-key"]["data"]["paramValue"] == "abc123456"
    assert result.unsupported == []
    assert result.warnings == []


# --- rds (W2.7): aws_db_instance <-> the `rds` canvas kind --------------------


_RDS_TF = '''
resource "aws_db_instance" "orders" {
  identifier          = "orders-db"
  engine              = "postgres"
  instance_class      = "db.t3.small"
  allocated_storage   = 50
  db_name             = "orders"
  username            = "svc"
  password            = "s3cr3t-pw"
  skip_final_snapshot = true

  tags = {
    "team" = "core"
  }
}
'''


def test_db_instance_imports_as_an_rds_node_keyed_by_its_identifier():
    result = parse_hcl_text(_RDS_TF)
    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node["id"] == "orders-db"
    assert node["type"] == "rds"
    assert node["data"] == {
        "label": "orders-db", "allocatedStorage": "50", "engine": "postgres",
        "instanceClass": "db.t3.small", "dbName": "orders", "username": "svc",
        "password": "s3cr3t-pw", "tags": {"team": "core"},
    }
    assert result.unsupported == []
    assert result.warnings == []


def test_a_secret_version_folds_into_its_secret_nodes_value_field():
    by_id = {n["id"]: n for n in parse_hcl_text(_SECRETS_TF).nodes}
    assert by_id["db-password"]["data"]["secretString"] == "hunter2-and-then-some"
    # A secret with no version stays valueless rather than gaining an empty one.
    assert "secretString" not in by_id["no-value-yet"]["data"]


def test_secret_and_ssm_round_trip_reproduces_the_value_resources():
    imported = parse_hcl_text(_SECRETS_TF)
    regenerated = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges})).files["main.tf"]
    assert 'resource "aws_secretsmanager_secret" "db_password"' in regenerated
    assert 'secret_string = "hunter2-and-then-some"' in regenerated
    assert 'resource "aws_ssm_parameter" "_odin_api_key"' in regenerated
    assert 'value = "abc123456"' in regenerated
    # ...and the valueless secret still emits no version block of its own.
    assert regenerated.count("aws_secretsmanager_secret_version") == 1


def test_a_secret_version_pointing_outside_the_supported_set_is_reported_not_dropped():
    tf = (
        'resource "aws_secretsmanager_secret_version" "orphan" {\n'
        '  secret_id     = aws_secretsmanager_secret.elsewhere.id\n'
        '  secret_string = "x"\n'
        "}\n"
    )
    result = parse_hcl_text(tf)
    assert result.nodes == []
    assert [(u.type, u.name) for u in result.unsupported] == [("aws_secretsmanager_secret_version", "orphan")]


def test_a_computed_secret_value_is_reported_rather_than_imported_verbatim():
    tf = (
        'resource "aws_secretsmanager_secret" "s" {\n  name = "s"\n}\n\n'
        'resource "aws_secretsmanager_secret_version" "s" {\n'
        "  secret_id     = aws_secretsmanager_secret.s.id\n"
        "  secret_string = var.db_password\n"
        "}\n"
    )
    result = parse_hcl_text(tf)
    assert "secretString" not in result.nodes[0]["data"]
    assert result.unsupported[0].type == "aws_secretsmanager_secret_version"


def test_secret_and_ssm_stay_out_of_the_live_import_path():
    # Neither has a backing to enumerate live resources from (both are pure
    # gateway models), so mode (b) reports them instead of pretending.
    assert "secret" not in _TF_TYPE
    assert "ssm" not in _TF_TYPE


def test_db_instance_round_trip_reproduces_every_argument_including_the_password():
    """A dropped `password` would silently substitute hcl.py's default on the
    next apply -- a real credential change, so it round-trips."""
    imported = parse_hcl_text(_RDS_TF)
    regenerated = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges})).files["main.tf"]
    for line in (
        'identifier          = "orders-db"', 'instance_class      = "db.t3.small"',
        "allocated_storage   = 50", 'db_name             = "orders"',
        'username            = "svc"', 'password            = "s3cr3t-pw"',
        "skip_final_snapshot = true", '"team"      = "core"',
    ):
        assert line in regenerated, line


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


# --- W2.8: elasticache -------------------------------------------------------

_CACHE_TF = '''
resource "aws_elasticache_cluster" "sessions" {
  cluster_id      = "sessions"
  engine          = "redis"
  node_type       = "cache.t3.small"
  num_cache_nodes = 1

  tags = {
    "odin:node" = "sessions"
  }
}
'''


# --- alb (W2.5): aws_lb(+_target_group +_listener) <-> the `alb` canvas kind.
# BOTH companions fold onto the aws_lb's node (the exact inverse of hcl.py's own
# companion pass, so one canvas node stays one canvas node instead of
# multiplying into three). The LISTENER is what ties the trio together: it names
# its load balancer directly and its target group through the forward action. ---


_ALB_TF = '''
resource "aws_lb" "front" {
  name               = "front"
  internal           = true
  load_balancer_type = "application"

  tags = {
    "odin:node" = "front"
    "team"      = "core"
  }
}

resource "aws_lb_target_group" "front_tg" {
  name        = "front-tg"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.net.id
  target_type = "instance"

  health_check {
    path = "/healthz"
  }
}

resource "aws_lb_listener" "front_listener" {
  load_balancer_arn = aws_lb.front.arn
  port              = 8080
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.front_tg.arn
  }
}
'''


def test_elasticache_cluster_imports_as_an_elasticache_node():
    result = parse_hcl_text(_CACHE_TF)
    (node,) = result.nodes
    assert (node["id"], node["type"]) == ("sessions", "elasticache")
    assert node["data"]["nodeType"] == "cache.t3.small"
    assert not any(u.type == "aws_elasticache_cluster" for u in result.unsupported)
    assert result.warnings == []  # every argument hcl.py emits is carried


def test_elasticache_round_trips_back_through_generate_tf():
    imported = parse_hcl_text(_CACHE_TF)
    canvas = {"nodes": imported.nodes, "edges": imported.edges}
    regenerated = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert 'resource "aws_elasticache_cluster" "sessions"' in regenerated
    assert '  cluster_id      = "sessions"' in regenerated
    assert '  node_type       = "cache.t3.small"' in regenerated
    assert "  num_cache_nodes = 1" in regenerated


def test_elasticache_label_falls_back_to_the_hcl_name_when_cluster_id_is_computed():
    tf = 'resource "aws_elasticache_cluster" "generated" {\n  cluster_id = "${var.prefix}-cache"\n}\n'
    assert parse_hcl_text(tf).nodes[0]["id"] == "generated"


def test_elasticache_stays_out_of_the_live_import_path():
    # No `_import_id` shape can resolve a cluster from outside a canvas Apply
    # (it exists only as a gateway-model record + a real container), so mode
    # (b) reports it unsupported rather than generating a bogus import block.
    assert "elasticache" not in _TF_TYPE


def test_the_alb_trio_imports_as_one_node_carrying_both_ports_and_the_health_check():
    result = parse_hcl_text(_ALB_TF)
    (node,) = result.nodes  # the two companions produce no node of their own
    assert node["id"] == "front"  # the aws_lb's `name`, not the HCL resource name
    assert node["type"] == "alb"
    assert node["data"]["listenerPort"] == "8080"  # from the listener
    assert node["data"]["port"] == "3000"          # from the target group
    assert node["data"]["healthCheckPath"] == "/healthz"
    assert result.unsupported == []


def test_alb_round_trips_through_generate_and_back_reproducing_every_field():
    canvas = {
        "nodes": [
            {"id": "n1", "type": "vpc", "data": {"label": "net"}},
            {"id": "n2", "type": "subnet", "data": {"label": "web", "vpc": "net"}},
            {"id": "n3", "type": "alb", "data": {
                "label": "front", "vpc": "net", "subnet": "web",
                "listenerPort": "8080", "port": "3000", "healthCheckPath": "/healthz",
            }},
        ],
        "edges": [],
    }
    generated = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    (node,) = [n for n in parse_hcl_text(generated).nodes if n["type"] == "alb"]
    assert node["id"] == "front"
    assert node["data"]["listenerPort"] == "8080"
    assert node["data"]["port"] == "3000"
    assert node["data"]["healthCheckPath"] == "/healthz"


def test_a_listener_naming_a_load_balancer_outside_the_supported_set_is_reported():
    tf = (
        'resource "aws_lb_listener" "orphan" {\n'
        "  load_balancer_arn = aws_lb.elsewhere.arn\n"
        "  port              = 80\n"
        "\n"
        "  default_action {\n"
        '    type             = "forward"\n'
        "    target_group_arn = aws_lb_target_group.tg.arn\n"
        "  }\n"
        "}\n"
    )
    result = parse_hcl_text(tf)
    assert result.nodes == []
    assert [(u.type, u.name) for u in result.unsupported] == [("aws_lb_listener", "orphan")]
    assert "outside the supported set" in result.unsupported[0].reason


def test_a_target_group_no_listener_forwards_to_is_reported_not_dropped():
    # It can't be attributed to any load balancer, so it's reported rather than
    # guessed at (the subscription pass's rule).
    tf = (
        'resource "aws_lb" "front" {\n  name = "front"\n}\n\n'
        'resource "aws_lb_target_group" "lonely" {\n  name = "lonely-tg"\n  port = 80\n}\n'
    )
    result = parse_hcl_text(tf)
    assert [n["id"] for n in result.nodes] == ["front"]
    (dropped,) = result.unsupported
    assert (dropped.type, dropped.name) == ("aws_lb_target_group", "lonely")
    assert "not the forward target of any imported listener" in dropped.reason


def test_alb_carries_user_tags_but_not_odins_own():
    (node,) = parse_hcl_text(_ALB_TF).nodes
    assert node["data"]["tags"] == {"team": "core"}  # odin:node excluded


def test_alb_name_scheme_type_and_tags_are_carried_while_other_arguments_warn():
    # `internal`/`load_balancer_type` are re-emitted by odin with ITS values, so
    # they only warn when the source disagrees (see the test below); a genuinely
    # unmodeled argument always has to be honest.
    tf = (
        'resource "aws_lb" "front" {\n'
        '  name               = "front"\n'
        "  internal           = true\n"
        '  load_balancer_type = "application"\n'
        "  idle_timeout       = 120\n"
        "\n"
        '  tags = {\n    "team" = "core"\n  }\n'
        "}\n"
    )
    result = parse_hcl_text(tf)
    assert "front (alb): imported without unmodeled attribute(s): idle_timeout" in result.warnings
    assert result.unsupported == []


# --- v0.7.1: no silent exceptions to the attribute-honesty rule (field test U3)


def test_an_internet_facing_load_balancer_warns_that_odin_makes_it_internal():
    """`internal = false` vanished in silence, quietly turning an
    internet-facing load balancer into an internal one -- the source argument is
    still THERE in the regenerated HCL, saying the opposite."""
    tf = 'resource "aws_lb" "front" {\n  name = "front"\n  internal = false\n}\n'
    (warning,) = [w for w in parse_hcl_text(tf).warnings if "internal" in w]
    assert "internal=false (odin always emits true)" in warning


def test_a_target_groups_matcher_and_other_health_check_members_warn():
    """`matcher = "200-299"` was dropped with no warning at all: nothing ever
    computed dropped attributes for the alb's companion resources."""
    tf = (
        'resource "aws_lb" "front" {\n  name = "front"\n}\n\n'
        'resource "aws_lb_target_group" "front_tg" {\n'
        '  name     = "front-tg"\n  port = 3000\n  protocol = "HTTPS"\n'
        "  deregistration_delay = 30\n"
        '  health_check {\n    path = "/healthz"\n    matcher = "200-299"\n    interval = 10\n  }\n'
        "}\n\n"
        'resource "aws_lb_listener" "front_listener" {\n'
        "  load_balancer_arn = aws_lb.front.arn\n  port = 8080\n"
        "  default_action {\n    type = \"forward\"\n"
        "    target_group_arn = aws_lb_target_group.front_tg.arn\n  }\n}\n"
    )
    (warning,) = [w for w in parse_hcl_text(tf).warnings if "aws_lb_target_group" in w]
    assert "health_check.matcher" in warning and "health_check.interval" in warning
    assert "deregistration_delay" in warning
    assert "protocol=https (odin always emits http)" in warning


def test_the_carried_alb_arguments_produce_no_warning_at_all():
    """The round trip's own output must stay warning-free, or the honesty rule
    turns into noise nobody reads."""
    canvas = {
        "nodes": [
            {"id": "n1", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
            {"id": "n2", "type": "subnet", "data": {"label": "web", "cidr": "10.0.1.0/24", "vpc": "net"}},
            {"id": "n3", "type": "alb", "data": {
                "label": "front", "vpc": "net", "subnet": "web",
                "listenerPort": "8080", "port": "3000", "healthCheckPath": "/healthz",
            }},
        ],
        "edges": [],
    }
    generated = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert parse_hcl_text(generated).warnings == []


# --- v0.7.1: containment, so an imported load balancer can actually be applied
# Field test U2: `import-tf` imported an `aws_lb`, warned that `subnets` was
# dropped, and produced a canvas node Apply then refused ("not contained inside
# a Subnet on the canvas") -- a defect created at import, surfaced at apply.

_NETWORK_TF = '''
resource "aws_vpc" "net" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true

  tags = {
    "Name" = "acme-net"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.net.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = true
}

resource "aws_lb" "acme_public" {
  name     = "acme-public"
  internal = false
  subnets  = [aws_subnet.public.id]
}

resource "aws_lb_target_group" "acme_tg" {
  name   = "acme-tg"
  port   = 8080
  vpc_id = aws_vpc.net.id
}

resource "aws_lb_listener" "acme_listener" {
  load_balancer_arn = aws_lb.acme_public.arn
  port              = 80

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.acme_tg.arn
  }
}
'''


def test_vpc_and_subnet_import_as_container_nodes_instead_of_unsupported():
    result = parse_hcl_text(_NETWORK_TF)
    by_id = {n["id"]: n for n in result.nodes}
    assert by_id["net"]["type"] == "vpc" and by_id["net"]["data"]["cidr"] == "10.0.0.0/16"
    assert by_id["public"]["type"] == "subnet" and by_id["public"]["data"]["cidr"] == "10.0.1.0/24"
    assert result.unsupported == []
    # ...and what they DON'T carry is still reported by name.
    assert any("enable_dns_hostnames" in w for w in result.warnings)
    assert any("availability_zone" in w and "map_public_ip_on_launch" in w for w in result.warnings)


def test_containment_is_rebuilt_from_vpc_id_and_subnets_references():
    by_id = {n["id"]: n for n in parse_hcl_text(_NETWORK_TF).nodes}
    assert by_id["public"]["data"]["vpc"] == "net"
    assert by_id["acme-public"]["data"]["subnet"] == "public"
    assert by_id["acme-public"]["data"]["vpc"] == "net"


def test_an_imported_load_balancer_can_now_be_applied():
    """The end of U2: translate the imported canvas and the `aws_lb` is really
    there, with no unsupported entry."""
    imported = parse_hcl_text(_NETWORK_TF)
    project = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert project.unsupported == []
    assert 'resource "aws_lb" "acme_public"' in project.files["main.tf"]
    assert 'resource "aws_subnet" "public"' in project.files["main.tf"]
    assert "subnets = [aws_subnet.public.id]" in project.files["main.tf"]


def test_a_load_balancer_whose_subnets_are_not_importable_says_so_at_import_time():
    tf = (
        'resource "aws_lb" "front" {\n  name = "front"\n'
        "  subnets = [aws_subnet.somewhere_else.id]\n}\n"
    )
    (warning,) = [w for w in parse_hcl_text(tf).warnings if "containment" in w]
    assert "not contained inside a Subnet on the canvas" in warning
    assert "draw a VPC + Subnet" in warning


def test_a_subnet_whose_vpc_is_not_importable_says_so_at_import_time():
    tf = 'resource "aws_subnet" "orphan" {\n  vpc_id = aws_vpc.elsewhere.id\n}\n'
    (warning,) = [w for w in parse_hcl_text(tf).warnings if "containment" in w]
    assert "`vpc_id` names no imported aws_vpc" in warning


def _rect(node: dict) -> tuple[float, float, float, float]:
    """The node's rect the way ui/src/lib/containment.ts computes it: explicit
    size when the importer set one, else the type's default leaf size."""
    size = node.get("size") or {"width": 220, "height": 120}
    x, y = node["position"]["x"], node["position"]["y"]
    return x, y, x + size["width"], y + size["height"]


def test_imported_nodes_are_geometrically_inside_their_containers():
    """The stamps alone aren't enough: the browser re-derives containment from
    geometry on every measure/drag and would strip a stamp whose node sits
    outside its box."""
    by_id = {n["id"]: n for n in parse_hcl_text(_NETWORK_TF).nodes}
    vx0, vy0, vx1, vy1 = _rect(by_id["net"])
    sx0, sy0, sx1, sy1 = _rect(by_id["public"])
    lx0, ly0, lx1, ly1 = _rect(by_id["acme-public"])
    assert (vx0 <= sx0 and vy0 <= sy0 and sx1 <= vx1 and sy1 <= vy1)  # containsRect
    center = ((lx0 + lx1) / 2, (ly0 + ly1) / 2)                       # containsPoint
    assert sx0 <= center[0] <= sx1 and sy0 <= center[1] <= sy1
    assert all(n["position"]["x"] % 20 == 0 and n["position"]["y"] % 20 == 0
               for n in by_id.values())


def test_a_project_with_no_containers_keeps_the_flat_row():
    positions = [n["position"] for n in parse_hcl_text(_FULL_TF).nodes]
    assert sorted(p["x"] for p in positions) == [0, 220, 440, 660]
    assert all(p["y"] == 0 for p in positions)


def test_alb_stays_out_of_the_live_import_path():
    # Mode (b) enumerates live resources from a BACKING; an alb's substrate is
    # the gateway's own nginx proxy, so it reports itself instead of pretending.
    assert "alb" not in _TF_TYPE
