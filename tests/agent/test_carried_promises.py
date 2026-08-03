"""Every entry in `import_tf`'s carried/companion/fixed registries is a PROMISE,
and until v0.8.22 nothing checked them as a set.

Membership is not a passive label: `_attribute_notes` reports an argument as
`dropped` only when it is NOT in the carried set, so an entry SUPPRESSES the one
warning that would otherwise name a loss. A false entry is therefore strictly
worse than a missing one, and it is invisible from the inside — `alb -> ecs` lost
its whole `load_balancer` block with `unsupported == []` and no warning at all,
precisely because `load_balancer` was listed.

## Why a default-valued round trip proves nothing

`generate -> import -> generate` over odin's own defaults is byte-identical even
when import drops the field entirely, because `hcl.py` refills the default. So
`CANVAS` below sets EVERY canvas field to a value odin's generator cannot
reproduce, and `_MUTATIONS` then rewrites each emitted argument again. Byte
equality is paired with `unsupported == []` and a per-argument verdict; on its
own it is not evidence.

## Where the expectations come from (rule 5)

Three independent sources, deliberately:

* `_VERDICTS` is a hand-written literal. Parametrization runs over IT, never over
  the registry, so DELETING a registry entry cannot delete a case — the quietest
  mutation, and the one that reads as success.
* the mutation VALUES come from parsing what `hcl.py` actually emits, which is a
  second producer and not import_tf's own opinion of itself.
* `test_the_registry_promises_and_this_files_verdicts_are_the_same_set` ties the
  two together in both directions, so a registry entry with no verdict fails and
  a verdict for an entry that no longer exists fails.
"""
from __future__ import annotations

import re

import pytest

from odin.iac import hcl, import_tf
from odin.iac.hcl import generate_tf
from odin.iac.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

# Every field NON-DEFAULT. Do not tidy any of these towards odin's own defaults:
# a default round-trips byte-identically even with import broken.
CANVAS = {
    "nodes": [
        {"id": "v1", "type": "vpc", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-vpc", "cidr": "172.20.0.0/16"}},
        {"id": "sn1", "type": "subnet", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-subnet", "vpc": "probe-vpc", "cidr": "172.20.7.0/24"}},
        {"id": "g1", "type": "sg", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-sg", "vpc": "probe-vpc",
                  "ingressRules": "tcp:8443:0.0.0.0/0",
                  "egressRules": "tcp:5432:10.9.0.0/16"}},
        {"id": "e1", "type": "ec2", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-ec2", "subnet": "probe-subnet", "securityGroups": "probe-sg",
                  "ami": "ami-0aaaabbbbccccdddd", "instanceType": "m5.4xlarge",
                  "key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIProbeKeyMaterial probe@odin",
                  "userData": "#!/bin/sh\necho probe-user-data\n"}},
        {"id": "b1", "type": "s3", "position": {"x": 0, "y": 0}, "data": {"label": "probe-bucket"}},
        {"id": "q1", "type": "sqs", "position": {"x": 0, "y": 0}, "data": {"label": "probe-queue"}},
        {"id": "t1", "type": "sns", "position": {"x": 0, "y": 0}, "data": {"label": "probe-topic"}},
        # A COMPOSITE key with two different attribute types, so `attribute {}`
        # blocks and `range_key` are both really exercised.
        {"id": "dd1", "type": "dynamodb", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-table", "hashKey": "probePk", "hashKeyType": "S",
                  "rangeKey": "probeSk", "rangeKeyType": "N"}},
        {"id": "r1", "type": "iam_role", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-role"}},
        {"id": "lg1", "type": "logs", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-logs", "retentionInDays": "14"}},
        {"id": "sec1", "type": "secret", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-secret", "description": "probe secret description",
                  "secretString": "probe-secret-value"}},
        {"id": "ssm1", "type": "ssm", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-param", "paramType": "SecureString",
                  "paramValue": "probe-param-value", "description": "probe ssm description"}},
        {"id": "ec1", "type": "elasticache", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-cache", "nodeType": "cache.m5.4xlarge"}},
        {"id": "db1", "type": "rds", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-db", "instanceClass": "db.m5.4xlarge",
                  "allocatedStorage": "500", "dbName": "probedbname",
                  "username": "probeuser", "password": "probePassw0rd"}},
        # listenerPort != port ON PURPOSE: they are both ports, they both land on
        # the alb node, and equal values would hide a swap between them.
        {"id": "lb1", "type": "alb", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-alb", "vpc": "probe-vpc", "subnet": "probe-subnet",
                  "listenerPort": "8080", "port": "9000", "healthCheckPath": "/probe-health"}},
        {"id": "reg1", "type": "ecr", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-repo"}},
        {"id": "c1", "type": "ecs", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-svc", "image": "nginx:1.25", "count": "3", "port": "9000"}},
        {"id": "f1", "type": "lambda", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-fn", "runtime": "python3.13", "handler": "lambda_function.probe",
                  "code": "def probe(event, context):\n    return 'probe'\n"}},
        {"id": "d1", "type": "ebs", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-vol", "az": "eu-west-2b", "size": "77"}},
        {"id": "z1", "type": "route53", "position": {"x": 0, "y": 0},
         "data": {"label": "probe.example.com"}},
        {"id": "fs1", "type": "efs", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-fs", "path": "/mnt/probe"}},
        {"id": "api1", "type": "apigateway", "position": {"x": 0, "y": 0},
         "data": {"label": "probe-api"}},
    ],
    # Every companion odin can emit needs the EDGE that produces it, or the
    # sweep below runs against a file that never contained the resource whose
    # promises it is checking -- the failure `test_import_coverage_is_honest`
    # has already paid for twice.
    "edges": [
        {"id": "ge1", "source": "c1", "target": "b1",
         "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}},
        {"id": "ge2", "source": "e1", "target": "b1",
         "data": {"edgeType": "iam", "permissions": ["s3:PutObject"]}},
        {"id": "ve1", "source": "d1", "target": "e1", "data": {"edgeType": "volume"}},
        {"id": "de1", "source": "z1", "target": "e1", "data": {"edgeType": "dns"}},
        {"id": "me1", "source": "fs1", "target": "c1", "data": {"edgeType": "mount"}},
        {"id": "me2", "source": "f1", "target": "fs1", "data": {"edgeType": "mount"}},
        {"id": "te1", "source": "lb1", "target": "e1", "data": {"edgeType": "target"}},
        {"id": "te2", "source": "lb1", "target": "c1", "data": {"edgeType": "target"}},
        {"id": "ae1", "source": "api1", "target": "f1", "data": {"edgeType": "target"}},
        # BOTH integration shapes. odin emits a different argument set per target
        # kind (`AWS_PROXY` + `payload_format_version` for a lambda, `HTTP_PROXY`
        # + `integration_method` for a service), so a lambda-only fixture leaves
        # `integration_method` emitted by nothing and unchecked by everything --
        # which is exactly how it was missing when this file was first written.
        {"id": "ae2", "source": "api1", "target": "c1", "data": {"edgeType": "target"}},
        {"id": "se1", "source": "t1", "target": "q1", "data": {"edgeType": "subscription"}},
    ],
}

# The two warnings odin's OWN output legitimately produces, and the only two: an
# `iam_role` node carries an `assume_role_policy` the canvas has no field for,
# and a function's code lives in a zip beside main.tf rather than in the HCL.
# Spelled out as a count plus two substrings so a NEW warning on odin's own
# output fails here rather than becoming background noise every reader learns to
# skip.
_OWN_OUTPUT_WARNINGS = ("assume_role_policy", "its CODE could not be imported")

# --- verdicts ---------------------------------------------------------------
#
# `(aws_* type, argument) -> verdict`, HAND-WRITTEN. This is the independently
# owned side of the check: the cases below are parametrized over THIS dict, so
# deleting a registry entry cannot silently delete a case.
#
#   "carried"  the source's own value reaches the canvas and is re-emitted
#              verbatim -- the strongest promise.
#   "named"    odin re-emits its OWN value (a `_FIXED_VALUES` entry, or one
#              `_derived_changes` computes per resource), and a source that
#              disagrees is reported BY NAME. The argument survives; its meaning
#              does not, and saying so is the promise.
#   "declined" the value reaches the canvas and `generate_tf` then REFUSES the
#              node rather than emitting something else. Also honest, and the
#              only verdict where the round trip legitimately produces no HCL.
_CARRIED, _NAMED, _DECLINED = "carried", "named", "declined"
_VERDICTS: dict[tuple[str, str], str] = {
    ("aws_apigatewayv2_api", "name"): _CARRIED,
    ("aws_apigatewayv2_api", "protocol_type"): _NAMED,
    ("aws_apigatewayv2_integration", "integration_method"): _NAMED,
    ("aws_apigatewayv2_integration", "integration_type"): _NAMED,
    ("aws_apigatewayv2_integration", "payload_format_version"): _NAMED,
    ("aws_apigatewayv2_route", "route_key"): _NAMED,
    ("aws_apigatewayv2_route", "target"): _NAMED,
    ("aws_apigatewayv2_stage", "auto_deploy"): _NAMED,
    ("aws_apigatewayv2_stage", "name"): _NAMED,
    ("aws_cloudwatch_log_group", "name"): _CARRIED,
    ("aws_cloudwatch_log_group", "retention_in_days"): _CARRIED,
    ("aws_db_instance", "allocated_storage"): _CARRIED,
    ("aws_db_instance", "db_name"): _CARRIED,
    # odin runs a real Postgres container and nothing else, so a different
    # engine is refused rather than quietly imported as postgres.
    ("aws_db_instance", "engine"): _DECLINED,
    ("aws_db_instance", "identifier"): _CARRIED,
    ("aws_db_instance", "instance_class"): _CARRIED,
    ("aws_db_instance", "password"): _CARRIED,
    ("aws_db_instance", "skip_final_snapshot"): _NAMED,
    ("aws_db_instance", "username"): _CARRIED,
    ("aws_dynamodb_table", "billing_mode"): _NAMED,
    ("aws_dynamodb_table", "hash_key"): _CARRIED,
    ("aws_dynamodb_table", "name"): _CARRIED,
    ("aws_dynamodb_table", "range_key"): _CARRIED,
    ("aws_ebs_volume", "availability_zone"): _CARRIED,
    ("aws_ebs_volume", "size"): _CARRIED,
    ("aws_ebs_volume", "type"): _NAMED,
    ("aws_ecr_repository", "name"): _CARRIED,
    ("aws_ecs_cluster", "name"): _NAMED,
    ("aws_ecs_service", "deployment_maximum_percent"): _NAMED,
    ("aws_ecs_service", "deployment_minimum_healthy_percent"): _NAMED,
    ("aws_ecs_service", "desired_count"): _CARRIED,
    ("aws_ecs_service", "launch_type"): _NAMED,
    ("aws_ecs_service", "name"): _CARRIED,
    ("aws_ecs_service", "wait_for_steady_state"): _NAMED,
    ("aws_ecs_task_definition", "container_definitions"): _NAMED,
    ("aws_ecs_task_definition", "family"): _NAMED,
    ("aws_ecs_task_definition", "network_mode"): _NAMED,
    ("aws_efs_file_system", "creation_token"): _CARRIED,
    ("aws_elasticache_cluster", "cluster_id"): _CARRIED,
    ("aws_elasticache_cluster", "engine"): _NAMED,
    ("aws_elasticache_cluster", "node_type"): _CARRIED,
    ("aws_elasticache_cluster", "num_cache_nodes"): _NAMED,
    ("aws_iam_instance_profile", "name"): _NAMED,
    ("aws_iam_role", "name"): _CARRIED,
    ("aws_iam_role_policy", "name"): _NAMED,
    ("aws_iam_role_policy", "policy"): _NAMED,
    ("aws_instance", "ami"): _CARRIED,
    ("aws_instance", "instance_type"): _CARRIED,
    ("aws_instance", "user_data"): _CARRIED,
    ("aws_key_pair", "key_name"): _NAMED,
    ("aws_key_pair", "public_key"): _CARRIED,
    # A function's body lives in the zip beside main.tf, so reading HCL TEXT
    # alone can never recover it -- the warning that says so names the filename
    # it looked for, which is what makes this `named` and not a silent default.
    ("aws_lambda_function", "filename"): _NAMED,
    ("aws_lambda_function", "function_name"): _CARRIED,
    ("aws_lambda_function", "handler"): _CARRIED,
    ("aws_lambda_function", "runtime"): _CARRIED,
    ("aws_lb", "internal"): _NAMED,
    ("aws_lb", "load_balancer_type"): _NAMED,
    ("aws_lb", "name"): _CARRIED,
    ("aws_lb_listener", "port"): _CARRIED,
    ("aws_lb_listener", "protocol"): _NAMED,
    ("aws_lb_target_group", "name"): _NAMED,
    ("aws_lb_target_group", "port"): _CARRIED,
    ("aws_lb_target_group", "protocol"): _NAMED,
    ("aws_lb_target_group", "target_type"): _NAMED,
    ("aws_lb_target_group_attachment", "port"): _NAMED,
    ("aws_route53_record", "name"): _NAMED,
    ("aws_route53_record", "ttl"): _NAMED,
    ("aws_route53_record", "type"): _NAMED,
    ("aws_route53_zone", "name"): _CARRIED,
    ("aws_s3_bucket", "bucket"): _CARRIED,
    ("aws_s3_bucket", "force_destroy"): _NAMED,
    ("aws_secretsmanager_secret", "description"): _CARRIED,
    ("aws_secretsmanager_secret", "name"): _CARRIED,
    ("aws_secretsmanager_secret", "recovery_window_in_days"): _NAMED,
    ("aws_secretsmanager_secret_version", "secret_string"): _CARRIED,
    ("aws_security_group", "name"): _CARRIED,
    ("aws_sns_topic", "name"): _CARRIED,
    ("aws_sns_topic_subscription", "protocol"): _NAMED,
    # CARRIED since the limits-six branch made it authorable per edge; this
    # table was written while it was still hardcoded `true` and import
    # substituted it, which is what _NAMED recorded. Two branches, each
    # correct on its own tree. Measured on the merged tree: emitting
    # `raw_message_delivery = false` imports back as
    # `{"rawMessageDelivery": False}` on the subscription edge.
    ("aws_sns_topic_subscription", "raw_message_delivery"): _CARRIED,
    ("aws_sqs_queue", "name"): _CARRIED,
    ("aws_ssm_parameter", "description"): _CARRIED,
    ("aws_ssm_parameter", "name"): _CARRIED,
    # The value reaches the canvas as `paramType`; `hcl.py::_ssm` then refuses a
    # type AWS does not have rather than substituting `String`.
    ("aws_ssm_parameter", "type"): _DECLINED,
    ("aws_ssm_parameter", "value"): _CARRIED,
    ("aws_subnet", "cidr_block"): _CARRIED,
    ("aws_volume_attachment", "device_name"): _NAMED,
    ("aws_vpc", "cidr_block"): _CARRIED,
}
# The literal every mutation run must still see. Rule 5's quietest failure is a
# mutation whose test COUNT drops, which reads as success -- so the count is
# pinned to a number a reader can check against the dict above.
_VERDICT_COUNT = 87

# Arguments odin emits that carry no per-argument promise, each with the reason
# it is out. Listed rather than filtered by a rule, so adding one is a decision
# somebody wrote down.
_NOT_PROMISED = {
    # Pure REFERENCES. Their promise is the EDGE or the containment stamp they
    # rebuild, which the round trip below proves wholesale; mutating the
    # reference text checks HCL naming, not what survives.
    ("aws_apigatewayv2_integration", "api_id"),
    ("aws_apigatewayv2_integration", "integration_uri"),
    ("aws_apigatewayv2_route", "api_id"),
    ("aws_apigatewayv2_stage", "api_id"),
    ("aws_ecs_service", "cluster"),
    ("aws_ecs_service", "task_definition"),
    ("aws_ecs_task_definition", "task_role_arn"),
    ("aws_efs_access_point", "file_system_id"),
    ("aws_iam_instance_profile", "role"),
    ("aws_iam_role_policy", "role"),
    ("aws_instance", "iam_instance_profile"),
    ("aws_instance", "key_name"),
    ("aws_instance", "subnet_id"),
    ("aws_instance", "vpc_security_group_ids"),
    ("aws_lambda_function", "role"),
    ("aws_lb", "subnets"),
    ("aws_lb_listener", "load_balancer_arn"),
    ("aws_lb_target_group", "vpc_id"),
    ("aws_lb_target_group_attachment", "target_group_arn"),
    ("aws_lb_target_group_attachment", "target_id"),
    ("aws_route53_record", "records"),
    ("aws_route53_record", "zone_id"),
    ("aws_secretsmanager_secret_version", "secret_id"),
    ("aws_security_group", "vpc_id"),
    ("aws_sns_topic_subscription", "endpoint"),
    ("aws_sns_topic_subscription", "topic_arn"),
    ("aws_subnet", "vpc_id"),
    ("aws_volume_attachment", "instance_id"),
    ("aws_volume_attachment", "volume_id"),
    # BLOCKS, not scalars -- each has its own pass and its own tests
    # (`_stamp_sg_rules`, `_dropped_health_check_attrs`, `_ecs_alb_targets`,
    # `_lambda_efs_mounts`, `_efs_volume_notes`, `_uncarried_attribute_blocks`,
    # `_default_action_notes`, `_ecs_timeout_notes`, `_root_directory_lines`).
    ("aws_ecs_service", "load_balancer"),
    ("aws_ecs_service", "placement_constraints"),
    ("aws_ecs_service", "timeouts"),
    ("aws_ecs_task_definition", "volume"),
    ("aws_dynamodb_table", "attribute"),
    ("aws_efs_access_point", "root_directory"),
    ("aws_lambda_function", "file_system_config"),
    ("aws_lb_listener", "default_action"),
    ("aws_lb_target_group", "health_check"),
    ("aws_security_group", "egress"),
    ("aws_security_group", "ingress"),
    # Re-derived from something already covered.
    ("aws_ecs_task_definition", "requires_compatibilities"),  # a one-element list
    ("aws_instance", "depends_on"),
    ("aws_ecs_service", "depends_on"),
    ("aws_lambda_function", "depends_on"),
    ("aws_lambda_function", "source_code_hash"),  # derives from `filename`
    ("aws_iam_role", "assume_role_policy"),  # the canvas has no field; reported dropped
    # `tags` is one promise for every kind at once and has its own file
    # (`test_import_tags.py`), which pins it against `_KIND`.
    ("*", "tags"),
    # cpu/memory: only emitted when the canvas says so, and this canvas does not
    # (`test_ecs_task_resources.py` owns them).
    ("aws_ecs_task_definition", "cpu"),
    ("aws_ecs_task_definition", "memory"),
}


def _project():
    return generate_tf(canvas_to_stack(CANVAS))


_RESOURCE = re.compile(r'^resource "([a-z0-9_]+)" "([A-Za-z0-9_]+)" \{$', re.M)


def _blocks(text: str) -> list[tuple[str, str, int, int]]:
    return [(m.group(1), m.group(2), m.start(), text.index("\n}\n", m.start()) + 3)
            for m in _RESOURCE.finditer(text)]


def _emitted_scalars(text: str) -> list[tuple[str, str, str, str, str]]:
    """`(type, hcl name, argument, replacement HCL, replacement value)` for every
    top-level SCALAR argument in the generated file.

    Read out of `hcl.py`'s own output rather than out of a table here: the
    generator is a second producer, so an argument it starts emitting shows up
    as an unverdicted case rather than being quietly skipped."""
    out = []
    for rtype, rname, start, end in _blocks(text):
        for m in re.finditer(r"^  ([a-z0-9_]+)(\s*)= (.*)$", text[start:end], re.M):
            attr, value = m.group(1), m.group(3).strip()
            if value.startswith('"'):
                new = f'"zzmut{attr}"'
            elif value in ("true", "false"):
                new = {"true": "false", "false": "true"}[value]
            elif re.fullmatch(r"-?\d+", value):
                new = "4242"
            else:  # a reference, a list, a function call, a heredoc
                continue
            out.append((rtype, rname, attr, m.group(0), f"  {attr}{m.group(2)}= {new}"))
    return out


def _promised_scalars() -> list[tuple[str, str, str, str, str]]:
    return [row for row in _emitted_scalars(_project().files["main.tf"])
            if (row[0], row[2]) in _VERDICTS]


def _mutate(text: str, rtype: str, rname: str, old: str, new: str) -> str:
    for t, n, start, end in _blocks(text):
        if (t, n) == (rtype, rname):
            body = text[start:end]
            assert old in body, f"{rtype}.{rname}: {old!r} not in its own block"
            return text[:start] + body.replace(old, new, 1) + text[end:]
    raise AssertionError(f"no block {rtype}.{rname}")


def _word(attr: str) -> re.Pattern:
    """`attr` as a whole identifier. A bare `in` matched `port` inside `imported`
    and `policy` inside `assume_role_policy`, which turned two silent losses into
    two false passes while this file was being written."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(attr)}(?![A-Za-z0-9_])")


# --- the round trip itself --------------------------------------------------


def test_odins_own_output_round_trips_with_non_default_values_everywhere():
    """The baseline the per-argument cases below are mutations OF.

    Byte equality is NOT the assertion on its own -- it was `True` while
    `aws_iam_instance_profile` was coming back `unsupported`, because the
    generator re-derives the profile from the grant. It is only evidence when
    paired with an empty `unsupported` and a warning list that holds no
    surprises, which is why all three are here."""
    main_tf = _project().files["main.tf"]
    result = parse_hcl_text(main_tf)

    assert result.parse_error is None
    assert result.unsupported == [], [(u.type, u.name) for u in result.unsupported]
    assert len(result.warnings) == len(_OWN_OUTPUT_WARNINGS), result.warnings
    for expected in _OWN_OUTPUT_WARNINGS:
        assert any(expected in w for w in result.warnings), (expected, result.warnings)

    again = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": result.edges}))
    assert again.files["main.tf"] == main_tf


def test_the_non_default_values_really_reach_the_canvas():
    """The guard on the guard: if `CANVAS` were quietly tidied back to odin's
    defaults, the test above would still pass and every case below would go
    vacuous. So a sample of the values is asserted ON THE IMPORTED NODES, and
    each is checked to differ from the generator's own default for that field."""
    nodes = {n["data"]["label"]: n["data"] for n in parse_hcl_text(_project().files["main.tf"]).nodes}
    for label, field, value, default in (
        ("probe-ec2", "instanceType", "m5.4xlarge", hcl._DEFAULT_INSTANCE_TYPE),
        ("probe-db", "allocatedStorage", "500", hcl._DEFAULT_ALLOCATED_STORAGE),
        ("probe-db", "password", "probePassw0rd", hcl._DEFAULT_DB_PASSWORD),
        ("probe-vol", "size", "77", hcl._DEFAULT_EBS_SIZE),
        ("probe-vol", "az", "eu-west-2b", hcl._DEFAULT_EBS_AZ),
        ("probe-svc", "image", "nginx:1.25", hcl._DEFAULT_ECS_IMAGE),
        ("probe-svc", "count", "3", hcl._DEFAULT_ECS_COUNT),
        ("probe-fn", "runtime", "python3.13", hcl._DEFAULT_LAMBDA_RUNTIME),
        ("probe-fs", "path", "/mnt/probe", hcl._DEFAULT_EFS_PATH),
        ("probe-cache", "nodeType", "cache.m5.4xlarge", hcl._DEFAULT_CACHE_NODE_TYPE),
        ("probe-alb", "listenerPort", "8080", hcl._DEFAULT_ALB_LISTENER_PORT),
        ("probe-alb", "port", "9000", hcl._DEFAULT_ALB_TARGET_PORT),
        ("probe-alb", "healthCheckPath", "/probe-health", hcl._DEFAULT_ALB_HEALTH_CHECK_PATH),
    ):
        assert value != default, f"{label}.{field} was tidied back to odin's own default"
        assert nodes[label][field] == value, (label, field, nodes[label])


@pytest.mark.parametrize(
    ("rtype", "rname", "attr", "old", "new"), _promised_scalars(),
    ids=lambda v: v if isinstance(v, str) and not v.startswith("  ") else "",
)
def test_a_promised_argument_is_carried_or_named(rtype, rname, attr, old, new):
    """Set one argument to a value the generator cannot reproduce, round-trip,
    and hold it to the verdict `_VERDICTS` promises for it.

    The failure this catches is the SILENT one: neither reproduced nor
    mentioned. That is what `alb -> ecs`, `aws_iam_instance_profile`,
    `aws_lb_target_group_attachment` and the eight found by the v0.8.22 audit all
    looked like, and in every case the carried-set entry was what hid it."""
    src = _mutate(_project().files["main.tf"], rtype, rname, old, new)
    result = parse_hcl_text(src)
    assert result.parse_error is None

    verdict = _VERDICTS[(rtype, attr)]
    marker = new.split("= ", 1)[1]
    again = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": result.edges}))
    # Anchored to `<attr> = <value>`. A bare `false` matches `"readOnly": false`
    # inside an unrelated `container_definitions` blob, which reported five
    # boolean substitutions as carried on the first sweep of this audit.
    reproduced = re.search(
        rf"^\s*{re.escape(attr)}\s*= {re.escape(marker)}\s*$", again.files.get("main.tf", ""), re.M,
    )
    notes = list(result.warnings) + [f"{u.type} {u.name}: {u.reason}" for u in result.unsupported]
    named = [n for n in notes if _word(attr).search(n) or marker.strip('"') in n]
    # A DECLINED verdict is not "something mentioned the attribute" -- odin's
    # decline messages name the CANVAS field (`paramType`), not the HCL argument
    # (`type`). The real promise is that the resource is REFUSED rather than
    # emitted carrying odin's own value, so that is what is asserted: the block
    # is gone from the regenerated file and something was reported.
    refused = (list(again.unsupported) + list(again.not_in_terraform)
               and f'resource "{rtype}"' not in again.files.get("main.tf", ""))

    if verdict == _CARRIED:
        assert reproduced, f"{rtype}.{attr}: promised CARRIED, not in the regenerated file"
    elif verdict == _NAMED:
        assert named, f"{rtype}.{attr}: promised NAMED, nothing said it. notes={notes}"
    else:
        assert refused, f"{rtype}.{attr}: promised DECLINED, generate_tf emitted something instead"


# --- the registry <-> verdict tie, both directions --------------------------


def test_the_verdict_table_has_not_shrunk():
    """Rule 5's quietest mutation: DELETE a registry entry and a test
    parametrized over the registry loses the case rather than failing, so the
    run goes green with fewer tests. The cases above are parametrized over
    `_VERDICTS` for that reason, and this pins its size to a literal."""
    assert len(_VERDICTS) == _VERDICT_COUNT


def test_every_argument_the_generator_emits_has_a_verdict_or_a_written_reason():
    """The INDEPENDENT-PRODUCER half. `hcl.py`'s output is the authority on what
    exists; this file is the authority on what it costs. An argument the
    generator starts emitting is unverdicted until somebody decides, which is the
    check a registry-derived test can never make -- it would only ever see
    entries somebody had already thought of."""
    unverdicted = {
        (rtype, attr)
        for rtype, _rname, attr, _old, _new in _emitted_scalars(_project().files["main.tf"])
        if (rtype, attr) not in _VERDICTS and (rtype, attr) not in _NOT_PROMISED
        and ("*", attr) not in _NOT_PROMISED
    }
    assert unverdicted == set(), f"generate_tf emits these and nothing says what a round trip costs: {sorted(unverdicted)}"


def test_the_registry_promises_and_this_files_verdicts_are_the_same_set():
    """A carried-set entry with no verdict is an unchecked promise; a verdict for
    an entry that no longer exists is a test measuring nothing. Both fail here.

    Reference and block arguments are excluded through `_NOT_PROMISED`, which is
    a LIST of decisions rather than a rule -- a rule would silently absorb the
    next entry somebody adds."""
    registry = {
        (hcl._TF_TYPES[kind], attr)
        for kind, attrs in import_tf._CARRIED_ATTRS.items() for attr in attrs
    } | {
        (rtype, attr)
        for rtype, attrs in import_tf._CARRIED_COMPANION_ATTRS.items() for attr in attrs
    }
    excluded = {pair for pair in _NOT_PROMISED if pair[0] != "*"}
    tags = {attr for _t, attr in _NOT_PROMISED if _t == "*"}
    registry = {(t, a) for t, a in registry if (t, a) not in excluded and a not in tags}

    assert registry - set(_VERDICTS) == set(), (
        "carried-set entries with no verdict -- an entry SUPPRESSES the warning that would name "
        f"the loss, so an unchecked one is a promise nothing keeps: {sorted(registry - set(_VERDICTS))}"
    )
    assert set(_VERDICTS) - registry == set(
        # `aws_ecs_cluster` folds away with nothing carried onto a node, so its
        # `name` is a promise made by the type's presence in `_ECS_COMPANION_TYPES`
        # rather than by a carried-set entry -- and it is a real one: a cluster
        # named `production` came back `odin` in silence before v0.8.22.
    ) | {("aws_ecs_cluster", "name")} - registry, sorted(set(_VERDICTS) - registry)


def test_every_type_import_recognises_is_accounted_for_somewhere():
    """The level above an argument: a TYPE recognised by `parse_hcl`'s dispatch
    is kept out of `unsupported`, which is the same suppression one scope up.
    Four types folded away with no honesty pass at all until v0.8.22
    (`aws_ecs_cluster`, `aws_ecs_task_definition`, `aws_key_pair`,
    `aws_iam_role_policy`) -- each measured silent on a project that renamed it.
    """
    recognised = (
        set(import_tf._ECS_COMPANION_TYPES) | set(import_tf._ALB_COMPANION_TYPES)
        | set(import_tf._APIGW_COMPANION_TYPES)
        | {import_tf._IAM_POLICY_TYPE, import_tf._EFS_ACCESS_POINT_TYPE,
           import_tf._INSTANCE_PROFILE_TYPE, "aws_key_pair",
           "aws_sns_topic_subscription", "aws_secretsmanager_secret_version",
           "aws_volume_attachment", "aws_route53_record"}
    )
    missing = recognised - set(import_tf._CARRIED_COMPANION_ATTRS)
    assert missing == set(), (
        "these types are recognised by parse_hcl -- so never reported unsupported -- and have no "
        f"carried set, so nothing computes what folding them away costs: {sorted(missing)}"
    )


# --- the shapes a generated-file sweep cannot reach --------------------------
#
# Everything above mutates odin's OWN output, which by construction only ever
# contains references that resolve and documents odin itself wrote. The losses
# below need a project odin did not generate, so each is spelled out as literal
# HCL. All nine were measured SILENT on develop before v0.8.22.

def _notes(tf: str) -> list[str]:
    result = parse_hcl_text(tf)
    assert result.parse_error is None, result.parse_error
    return list(result.warnings) + [f"{u.type} {u.name}: {u.reason}" for u in result.unsupported]


_LAMBDA_AND_BUCKET = '''
resource "aws_s3_bucket" "uploads" { bucket = "uploads" }
resource "aws_iam_role" "worker_role" { name = "worker-role" }
resource "aws_lambda_function" "worker" {
  function_name = "worker"
  role          = aws_iam_role.worker_role.arn
  handler       = "app.handler"
  runtime       = "python3.12"
  filename      = "w.zip"
}
'''


def test_a_policy_written_the_way_real_terraform_writes_one_is_not_dropped_in_silence():
    """THE WORST ONE. `jsonencode({...})` is how a hand-written project spells a
    policy document, and python-hcl2 hands it back as `${jsonencode(...)}` -- not
    a literal, so `json.loads` was never reached and the `except` was a bare
    `continue`. Measured on develop: this file imported with `unsupported == []`,
    `warnings == []` and ZERO edges. Every drawn permission gone, reported as a
    clean import."""
    notes = _notes(_LAMBDA_AND_BUCKET + '''
resource "aws_iam_role_policy" "worker_grants" {
  name = "worker-grants"
  role = aws_iam_role.worker_role.name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["s3:GetObject"], Resource = ["arn:aws:s3:::uploads/*"] }]
  })
}
''')
    assert any("THE PERMISSION IS LOST" in n and "not a literal document" in n for n in notes), notes


def test_a_policy_on_a_role_no_workload_carries_is_not_dropped_in_silence():
    notes = _notes('''
resource "aws_s3_bucket" "uploads" { bucket = "uploads" }
resource "aws_iam_role" "ci" { name = "ci" }
resource "aws_iam_role_policy" "ci_grants" {
  name   = "ci-grants"
  role   = aws_iam_role.ci.name
  policy = "{\\"Version\\": \\"2012-10-17\\", \\"Statement\\": [{\\"Effect\\": \\"Allow\\", \\"Action\\": [\\"s3:GetObject\\"], \\"Resource\\": [\\"arn:aws:s3:::uploads\\"]}]}"
}
''')
    assert any("THE PERMISSION IS LOST" in n and "belongs to no imported workload" in n
               for n in notes), notes


def test_a_policy_document_that_is_not_json_is_not_dropped_in_silence():
    notes = _notes(_LAMBDA_AND_BUCKET + '''
resource "aws_iam_role_policy" "worker_grants" {
  name   = "worker-grants"
  role   = aws_iam_role.worker_role.name
  policy = "not json at all"
}
''')
    assert any("THE PERMISSION IS LOST" in n and "not valid JSON" in n for n in notes), notes


_ECS_PROJECT = '''
resource "aws_ecs_cluster" "odin" {{ name = "odin" }}
resource "aws_ecs_task_definition" "web_taskdef" {{
  family                   = "web"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  {containers}
}}
resource "aws_ecs_service" "web" {{
  name            = "web"
  cluster         = aws_ecs_cluster.odin.id
  task_definition = aws_ecs_task_definition.web_taskdef.arn
  desired_count   = 1
}}
'''


@pytest.mark.parametrize("containers", [
    'container_definitions = "definitely not json"',
    # The spelling that matters: real Terraform writes this, python-hcl2 returns
    # `${jsonencode(...)}`, and the service came back carrying odin's DEFAULT
    # nginx image where the user's own container was.
    'container_definitions = jsonencode([{ name = "web", image = "ghcr.io/acme/web:3.1" }])',
])
def test_a_container_definition_odin_cannot_read_does_not_substitute_nginx_in_silence(containers):
    notes = _notes(_ECS_PROJECT.format(containers=containers))
    assert any("container_definitions" in n and hcl._DEFAULT_ECS_IMAGE in n for n in notes), notes


def test_a_cluster_the_user_named_is_not_renamed_in_silence():
    notes = _notes(_ECS_PROJECT.format(
        containers='container_definitions = "[]"',
    ).replace('"odin" { name = "odin" }', '"odin" { name = "production" }'))
    assert any("ecs cluster" in n and "production" in n and hcl._ECS_CLUSTER_NAME in n
               for n in notes), notes


def test_a_task_definitions_own_arguments_are_not_re_derived_in_silence():
    """`family`/`network_mode`/`requires_compatibilities` were left unchecked on
    the reasoning that a pass "would immediately warn on every ecs import". True
    of a plain dropped-attribute pass, FALSE of the derived comparison used
    everywhere else here -- which the byte-identical round trip above proves, by
    producing no such warning for odin's own output."""
    notes = _notes(_ECS_PROJECT.format(containers='container_definitions = "[]"')
                   .replace('family                   = "web"', 'family                   = "legacy-web"')
                   .replace('"bridge"', '"awsvpc"')
                   .replace('["EC2"]', '["FARGATE"]'))
    line = next(n for n in notes if "aws_ecs_task_definition" in n and "CHANGED" in n)
    for expected in ("legacy-web", "awsvpc", "FARGATE".lower()):
        assert expected in line, (expected, line)


def test_a_shared_key_pair_is_not_renamed_in_silence():
    """The `aws_iam_instance_profile` defect, in the type beside it. `key_name`
    is re-derived as `<ec2 label>-key`, so a `deploy-key` shared across a fleet
    comes back as a DIFFERENT AWS key pair -- and a `tags` block on it vanished
    outright."""
    notes = _notes('''
resource "aws_vpc" "v" { cidr_block = "10.0.0.0/16" }
resource "aws_subnet" "s" {
  vpc_id     = aws_vpc.v.id
  cidr_block = "10.0.1.0/24"
}
resource "aws_key_pair" "shared" {
  key_name   = "deploy-key"
  public_key = "ssh-ed25519 AAAAKEY user@host"
  tags       = { Team = "platform" }
}
resource "aws_instance" "api" {
  ami           = "ami-1"
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.s.id
  key_name      = aws_key_pair.shared.key_name
}
''')
    assert any("aws_key_pair" in n and "deploy-key" in n and "api-key" in n for n in notes), notes
    assert any("aws_key_pair" in n and "tags" in n for n in notes), notes


def test_a_websocket_api_does_not_import_as_http_in_silence():
    notes = _notes('''
resource "aws_apigatewayv2_api" "pub" {
  name          = "pub"
  protocol_type = "WEBSOCKET"
}
''')
    assert any("protocol_type" in n and "websocket" in n.lower() for n in notes), notes


def test_a_listener_that_redirects_does_not_come_back_forwarding_in_silence():
    """The direction is what makes this worth a warning: a closed door imported
    as an open one."""
    notes = _notes('''
resource "aws_vpc" "v" { cidr_block = "10.0.0.0/16" }
resource "aws_subnet" "s" {
  vpc_id     = aws_vpc.v.id
  cidr_block = "10.0.1.0/24"
}
resource "aws_lb" "front" {
  name               = "front"
  internal           = true
  load_balancer_type = "application"
  subnets            = [aws_subnet.s.id]
}
resource "aws_lb_target_group" "front_tg" {
  name        = "front-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.v.id
  target_type = "instance"
}
resource "aws_lb_listener" "front_listener" {
  load_balancer_arn = aws_lb.front.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "redirect"
    target_group_arn = aws_lb_target_group.front_tg.arn

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
''')
    assert any("default_action.type" in n and "redirect" in n for n in notes), notes
    assert any("default_action.redirect" in n for n in notes), notes


def test_a_target_group_in_a_different_vpc_is_not_moved_in_silence():
    """odin RE-DERIVES a target group's `vpc_id` from the load balancer's own
    containment, so a source that put it elsewhere comes back in the alb's VPC.
    Two imported VPCs on purpose: with only one there is nothing for the source
    to disagree WITH, and the check would pass vacuously."""
    notes = _notes('''
resource "aws_vpc" "v" { cidr_block = "10.0.0.0/16" }
resource "aws_vpc" "other" { cidr_block = "192.168.0.0/16" }
resource "aws_subnet" "s" {
  vpc_id     = aws_vpc.v.id
  cidr_block = "10.0.1.0/24"
}
resource "aws_lb" "front" {
  name               = "front"
  internal           = true
  load_balancer_type = "application"
  subnets            = [aws_subnet.s.id]

  tags = {
    "odin:node" = "front"
  }
}
resource "aws_lb_target_group" "front_tg" {
  name        = "front-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.other.id
  target_type = "instance"
}
resource "aws_lb_listener" "front_listener" {
  load_balancer_arn = aws_lb.front.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.front_tg.arn
  }
}
''')
    assert any("aws_lb_target_group" in n and "vpc_id" in n and "CHANGED" in n
               for n in notes), notes


def test_a_slow_rollouts_timeouts_are_not_cut_to_sixty_seconds_in_silence():
    """odin emits `hcl._ECS_CONVERGE_TIMEOUT` for all three, so a service given
    twenty minutes to roll out gets sixty seconds -- and a `tofu apply` that then
    reports a timeout for a deploy that was simply still going."""
    notes = _notes(_ECS_PROJECT.format(containers='container_definitions = "[]"').replace(
        "  desired_count   = 1",
        '  desired_count   = 1\n\n  timeouts {\n    create = "20m"\n    update = "20m"\n    delete = "20m"\n  }',
    ))
    line = next(n for n in notes if "timeouts" in n and "CHANGED" in n)
    assert "20m" in line and hcl._ECS_CONVERGE_TIMEOUT in line, line


def test_a_service_pointing_at_a_cluster_outside_the_file_is_not_moved_in_silence():
    """`_ecs_cluster_warnings` covers the cluster that IS in the file; this is
    the reference that leaves it. odin puts every service in its one shared
    cluster, so the regenerated project moves it."""
    notes = _notes(_ECS_PROJECT.format(containers='container_definitions = "[]"')
                   .replace('resource "aws_ecs_cluster" "odin" { name = "odin" }', "")
                   .replace("aws_ecs_cluster.odin.id", "aws_ecs_cluster.shared.id"))
    assert any("`cluster`" in n and hcl._ECS_CLUSTER_NAME in n for n in notes), notes


@pytest.mark.parametrize("companion", [
    '''resource "aws_apigatewayv2_stage" "other" {
  api_id      = aws_apigatewayv2_api.absent.id
  name        = "$default"
  auto_deploy = true
}''',
    '''resource "aws_apigatewayv2_route" "other" {
  api_id    = aws_apigatewayv2_api.absent.id
  route_key = "ANY /orders"
  target    = "integrations/${aws_apigatewayv2_integration.pub_orders.id}"
}''',
])
def test_a_route_or_stage_on_an_api_outside_the_file_is_not_folded_away_in_silence(companion):
    """Both fold AWAY entirely -- the canvas keeps neither -- so the only thing
    that brings one back is odin regenerating it for an API node it has. One
    attached to an API this import did not see does not come back at all."""
    notes = _notes(_LAMBDA_AND_BUCKET + '''
resource "aws_apigatewayv2_api" "pub" {
  name          = "pub"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "pub_orders" {
  api_id                 = aws_apigatewayv2_api.pub.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.worker.invoke_arn
  payload_format_version = "2.0"
}
''' + companion)
    assert any("`api_id`" in n and "names no imported aws_apigatewayv2_api" in n
               for n in notes), notes
