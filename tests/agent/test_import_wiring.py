"""Canvas wiring and security-group EGRESS survive an import — by ROUND TRIP.

Two `docs/limits.md` entries, both closed against a format agreed with the agent
that owns `agent/hcl.py` rather than one guessed at:

* an imported ECS service lost its `${{producer.ATTR}}` env references entirely.
* an imported security group's OUTBOUND rules did not survive.

**Why these assert on a round trip and not on a parse.** The repo's last two
import defects were only ever visible end to end, and a parse-only test would
have passed for both. So the ECS tests here take a real canvas, run the REAL
`generate_tf`, import the result, and then run the REAL `generate_tf` again on
what came back -- the property a user relying on `import -> edit -> apply`
actually depends on is that the second file orders the same resources as the
first.

**The one simulated step, named because it is a real limit of this run.**
`hcl-generate`'s `odin:ref:` emission is not committed yet, so `_with_ref_tags`
stamps those tags onto the genuinely-generated file in the exact shape they
specified (sorted by VAR, ahead of `odin:node`). Everything else in the chain is
production code. `test_generate_does_not_yet_emit_ref_tags` is a TRIPWIRE that
fails the moment their change lands, so nobody can merge it believing this file
already proved the whole loop.
"""
from __future__ import annotations

import re

import pytest

from odin.agent.hcl import generate_tf
from odin.agent.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

_WIRED_CANVAS = {"nodes": [
    {"id": "d", "type": "rds", "data": {"label": "db", "engine": "postgres"}},
    {"id": "q", "type": "sqs", "data": {"label": "jobs"}},
    {"id": "e", "type": "ecs", "data": {
        "label": "api", "image": "nginx:1.27", "port": "8080", "count": "2",
        "env": {"DATABASE_URL": "${{db.DATABASE_URL}}", "QUEUE_URL": "${{jobs.QUEUE_URL}}"},
    }},
], "edges": []}


def _with_ref_tags(tf: str, resource: str, refs: dict[str, str]) -> str:
    """hcl-generate's agreed emission, stamped onto a real generated file."""
    lines = "".join(f'    "odin:ref:{var}" = "{value}"\n' for var, value in sorted(refs.items()))
    pattern = re.compile(rf'(resource "{resource}" "[^"]+" \{{.*?tags = \{{\n)', re.DOTALL)
    stamped, count = pattern.subn(rf"\g<1>{lines}", tf, count=1)
    assert count == 1, f"no tags block found on {resource} -- the fixture is stale"
    return stamped


def _depends_on(tf: str) -> list[str]:
    return sorted(line.strip() for line in tf.splitlines() if "depends_on" in line)


def _node(result, kind: str) -> dict:
    return next(n for n in result.nodes if n["type"] == kind)


# --------------------------------------------------------------------------
# ECS / lambda canvas wiring
# --------------------------------------------------------------------------

def test_an_ecs_services_env_refs_survive_the_round_trip():
    """THE reported bug: the service came back with no wiring at all."""
    generated = generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"]
    stamped = _with_ref_tags(generated, "aws_ecs_service", {
        "DATABASE_URL": "db.DATABASE_URL", "QUEUE_URL": "jobs.QUEUE_URL",
    })

    result = parse_hcl_text(stamped)
    assert result.parse_error is None
    assert _node(result, "ecs")["data"]["env"] == {
        "DATABASE_URL": "${{db.DATABASE_URL}}",
        "QUEUE_URL": "${{jobs.QUEUE_URL}}",
    }


def test_the_ordering_re_derives_from_the_imported_refs():
    """The half limits.md called separately lost. It is not: `depends_on` is a
    deterministic function of the ref target SET (`hcl.py::_depends_on_block` is
    `sorted(set(...))`), so recovering the refs recovers the ordering for free.
    Asserted by generating TWICE and comparing, not by reading the source."""
    generated = generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"]
    stamped = _with_ref_tags(generated, "aws_ecs_service", {
        "DATABASE_URL": "db.DATABASE_URL", "QUEUE_URL": "jobs.QUEUE_URL",
    })
    imported = parse_hcl_text(stamped)
    regenerated = generate_tf(
        canvas_to_stack({"nodes": imported.nodes, "edges": []})
    ).files["main.tf"]

    assert _depends_on(regenerated) == _depends_on(generated)
    assert _depends_on(regenerated) == ["depends_on = [aws_db_instance.db, aws_sqs_queue.jobs]"]


def test_a_recovered_service_warns_about_nothing():
    """A warning that fires on a CORRECT import is why people stop reading
    warnings. The old text ("its canvas wiring cannot be imported") was
    unconditional on `depends_on`."""
    generated = generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"]
    stamped = _with_ref_tags(generated, "aws_ecs_service", {
        "DATABASE_URL": "db.DATABASE_URL", "QUEUE_URL": "jobs.QUEUE_URL",
    })
    wiring = [w for w in parse_hcl_text(stamped).warnings if "canvas wiring" in w]
    assert wiring == []


def test_a_file_with_no_ref_tags_still_says_the_wiring_is_lost():
    """The honest half. A hand-authored project, or one generated before the
    tags existed, genuinely cannot have its wiring rebuilt -- and the ordering
    goes with it, because odin re-derives `depends_on` FROM the refs."""
    generated = generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"]
    (wiring,) = [w for w in parse_hcl_text(generated).warnings if "canvas wiring" in w]
    assert "api (ecs)" in wiring
    assert "db" in wiring and "jobs" in wiring
    assert "odin:ref:" in wiring  # names the mechanism, so the reader can check their file


def test_a_lambdas_env_refs_survive_too():
    """The SIBLING. `hcl.py::_WIRED_KINDS` is ecs AND lambda, so closing only the
    kind limits.md named would leave the identical defect standing next to it."""
    canvas = {"nodes": [
        {"id": "q", "type": "sqs", "data": {"label": "jobs"}},
        {"id": "f", "type": "lambda", "data": {
            "label": "thumbnailer", "runtime": "python3.12", "code": "def handler(e, c): pass",
            "env": {"QUEUE_URL": "${{jobs.QUEUE_URL}}"},
        }},
    ], "edges": []}
    generated = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    stamped = _with_ref_tags(generated, "aws_lambda_function", {"QUEUE_URL": "jobs.QUEUE_URL"})

    result = parse_hcl_text(stamped)
    assert _node(result, "lambda")["data"]["env"] == {"QUEUE_URL": "${{jobs.QUEUE_URL}}"}
    regenerated = generate_tf(
        canvas_to_stack({"nodes": result.nodes, "edges": []})
    ).files["main.tf"]
    assert _depends_on(regenerated) == _depends_on(generated)


def test_ref_tags_are_never_surfaced_as_user_tags():
    """They are odin's machinery. Surfacing them would put them in the config
    panel as editable text AND re-emit them as literal user tags beside the ones
    the generator writes itself -- doubling on every round trip."""
    generated = generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"]
    stamped = _with_ref_tags(generated, "aws_ecs_service", {"DATABASE_URL": "db.DATABASE_URL"})
    data = _node(parse_hcl_text(stamped), "ecs")["data"]
    assert "odin:ref:DATABASE_URL" not in (data.get("tags") or {})
    assert not any(key.startswith("odin:") for key in (data.get("tags") or {}))


def test_a_placement_only_depends_on_does_not_claim_lost_wiring():
    """`depends_on` has two sources in hcl.py -- env refs AND the placement host.
    A service drawn inside an ec2 box with no env refs at all must not be told to
    re-add references it never had."""
    canvas = {"nodes": [
        {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
        {"id": "s", "type": "subnet",
         "data": {"label": "app-a", "cidr": "10.0.1.0/24", "vpc": "net"}},
        {"id": "i", "type": "ec2",
         "data": {"label": "box", "vpc": "net", "subnet": "app-a"}},
        {"id": "e", "type": "ecs",
         "data": {"label": "api", "image": "nginx:1.27", "port": "8080", "host": "box"}},
    ], "edges": []}
    generated = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert "depends_on" in generated  # the fixture really does exercise the path
    assert [w for w in parse_hcl_text(generated).warnings if "canvas wiring" in w] == []


def test_generate_does_not_yet_emit_ref_tags():
    """TRIPWIRE -- delete this the moment `hcl-generate`'s emission lands.

    `_with_ref_tags` above simulates one production step. This pins the fact that
    it IS still simulated, so the merge that makes it real fails here and forces
    whoever lands it to re-run the loop against the genuine article instead of
    trusting a green file that quietly stopped testing the same thing."""
    generated = generate_tf(canvas_to_stack(_WIRED_CANVAS)).files["main.tf"]
    assert "odin:ref:" not in generated, (
        "hcl.py now emits odin:ref: tags -- delete this test, drop `_with_ref_tags`, "
        "and assert the round trip against the real generator"
    )


# --------------------------------------------------------------------------
# Security-group EGRESS
# --------------------------------------------------------------------------

_SG_HEAD = '''
resource "aws_vpc" "net" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_security_group" "cache" {
  name   = "cache"
  vpc_id = aws_vpc.net.id
'''


def _sg_tf(*blocks: str) -> str:
    return _SG_HEAD + "\n" + "\n".join(blocks) + "\n}\n"


_RESTRICTED_EGRESS = '''
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  egress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.0.2.0/24"]
  }
'''


def test_a_restricted_egress_is_carried_onto_the_canvas():
    """THE reported bug: outbound rules did not survive, so a group that
    restricted egress came back wide open."""
    result = parse_hcl_text(_sg_tf(_RESTRICTED_EGRESS))
    assert _node(result, "sg")["data"]["egressRules"] == "tcp:443:10.0.0.0/16\ntcp:5432:10.0.2.0/24"


def test_a_restricted_egress_no_longer_reports_itself_as_a_changed_argument():
    """A caveat outliving its fix (honesty rule 3). Reporting "a restricted one
    comes back UNRESTRICTED" is a false claim once the rules are carried."""
    warnings = parse_hcl_text(_sg_tf(_RESTRICTED_EGRESS)).warnings
    assert [w for w in warnings if "UNRESTRICTED" in w] == []
    assert [w for w in warnings if "egress" in w] == []


def test_an_identity_egress_rule_comes_back_as_the_other_groups_label():
    """The canvas has no `sg-` ids in it, so a group-to-group rule can only
    survive as the referenced node's LABEL -- the same form ingress uses."""
    tf = _sg_tf('''
  egress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.db.id]
  }
''') + '''
resource "aws_security_group" "db" {
  name   = "db-tier"
  vpc_id = aws_vpc.net.id
}
'''
    result = parse_hcl_text(tf)
    cache = next(n for n in result.nodes if n["data"]["label"] == "cache")
    assert cache["data"]["egressRules"] == "tcp:5432:db-tier"


def test_odins_own_default_egress_leaves_the_field_empty():
    """Agreed with `hcl-generate`: an EMPTY `egressRules` is what tells hcl.py to
    emit the wide-open default, so the honest canvas for a default group is one
    with no rules text -- it regenerates byte-identically and looks like every
    hand-drawn canvas. Writing a synthesized `-1:0:0.0.0.0/0` would generate the
    same bytes and read as a rule the user had authored."""
    tf = _sg_tf('''
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
''')
    result = parse_hcl_text(tf)
    assert "egressRules" not in _node(result, "sg")["data"]
    assert result.warnings == []


def test_generate_does_not_yet_emit_egress_rules():
    """TRIPWIRE -- delete this when `hcl-generate`'s `egress` emission lands.

    The import half is done and verified end to end (`odin import-tf` against a
    real server puts `tcp:443:10.0.0.0/16` on the node). The EMIT half is theirs
    and is not in this tree, so a canvas carrying `egressRules` still regenerates
    with the wide-open default -- measured through the real CLI:
    `odin translate --file <imported canvas>` produced `protocol = "-1"` /
    `cidr_blocks = ["0.0.0.0/0"]` for both groups.

    This exists so the loop cannot be reported as closed while only half of it
    is. When it fails, assert the real round trip instead: canvas with
    `egressRules` -> generate -> import -> the same `egressRules`.
    """
    canvas = {"nodes": [
        {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
        {"id": "g", "type": "sg", "data": {
            "label": "cache", "vpc": "net", "egressRules": "tcp:443:10.0.0.0/16",
        }},
    ], "edges": []}
    generated = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    assert 'cidr_blocks = ["10.0.0.0/16"]' not in generated, (
        "hcl.py now emits egress from egressRules -- delete this tripwire and assert "
        "the full canvas -> generate -> import -> canvas round trip for egress"
    )


def test_a_group_whose_egress_cannot_be_expressed_says_it_comes_back_wide_open():
    """The DANGEROUS direction, and the reason egress gets its own warning text
    rather than sharing ingress's. A dropped ingress rule makes the group more
    restrictive; a dropped egress rule empties the field, and an empty field is
    exactly what makes hcl.py emit allow-everything."""
    tf = _sg_tf('''
  egress {
    from_port   = 1024
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
''')
    (warning,) = [w for w in parse_hcl_text(tf).warnings if "egress" in w]
    assert "1 of 1 egress rule(s) could not be imported" in warning
    assert "UNRESTRICTED" in warning


def test_a_partly_expressible_egress_says_it_allows_LESS_not_more():
    """The other side of the same branch: when SOME rules survive, the field is
    non-empty, the default is not emitted, and the group is more restrictive --
    the opposite claim, so it must not share the sentence above."""
    tf = _sg_tf(_RESTRICTED_EGRESS + '''
  egress {
    from_port   = 1024
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
''')
    (warning,) = [w for w in parse_hcl_text(tf).warnings if "egress" in w]
    assert "1 of 3 egress rule(s) could not be imported" in warning
    assert "LESS outbound" in warning
    assert "UNRESTRICTED" not in warning


# --------------------------------------------------------------------------
# The half-inverse: a rule odin can WRITE and cannot READ BACK
# --------------------------------------------------------------------------

_IPV6_TF = '''
resource "aws_vpc" "net" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_security_group" "web" {
  name   = "web"
  vpc_id = aws_vpc.net.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["2001:db8::/32"]
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
'''


def test_an_ipv6_cidr_rule_does_not_silently_delete_the_whole_security_group():
    """`unquote` was "half an inverse" once -- it stripped quotes and left the
    escapes. This is the same shape one level up, and it was live: the rule
    writer is `":".join(...)` while `hcl.py::_ingress_rules` is `split(":")` with
    `len(parts) == 3`, and an IPv6 CIDR contains colons.

    MEASURED before the fix, on the group below: the import produced
    `ingressRules = 'tcp:443:2001:db8::/32'` and **zero warnings**, and
    regenerating then dropped the ENTIRE `aws_security_group` as unsupported --
    taking the perfectly good port-80 rule with it, since one unreadable line
    fails the whole field. A clean-looking import that deletes a security group
    on the next Apply.
    """
    result = parse_hcl_text(_IPV6_TF)
    sg = _node(result, "sg")
    # The rule odin CAN express survives; the one it cannot is left out.
    assert sg["data"]["ingressRules"] == "tcp:80:0.0.0.0/0"

    (warning,) = [w for w in result.warnings if "ingress rule" in w]
    assert "1 of 2 ingress rule(s) could not be imported" in warning
    assert "IPv6" in warning

    # ...and the group SURVIVES regeneration, which is the part that was broken.
    project = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": []}))
    assert project.unsupported == []
    assert 'resource "aws_security_group" "web"' in project.files["main.tf"]


@pytest.mark.parametrize("source", ["2001:db8::/32", "fe80::/10"])
def test_no_ipv6_source_ever_reaches_the_rules_field(source: str):
    tf = _IPV6_TF.replace("2001:db8::/32", source)
    sg = _node(parse_hcl_text(tf), "sg")
    assert source not in sg["data"].get("ingressRules", "")


# --------------------------------------------------------------------------
# IAM policy import: the incoming ARN change (`hcl-generate`'s item 3)
# --------------------------------------------------------------------------

def _policy_tf(resource_json: str) -> str:
    return (
        'resource "aws_s3_bucket" "uploads" {\n  bucket = "uploads"\n}\n\n'
        'resource "aws_iam_role" "api_role" {\n  name = "api-role"\n}\n\n'
        'resource "aws_lambda_function" "api" {\n'
        '  function_name = "api"\n'
        "  role          = aws_iam_role.api_role.arn\n"
        '  handler       = "main.handler"\n'
        '  runtime       = "python3.12"\n'
        '  filename      = "api.zip"\n'
        "}\n\n"
        'resource "aws_iam_role_policy" "api_grants" {\n'
        '  name = "api-grants"\n'
        "  role = aws_iam_role.api_role.id\n"
        f"  policy = {resource_json}\n"
        "}\n"
    )


_LABEL_POLICY = (
    r'"{\"Version\": \"2012-10-17\", \"Statement\": [{\"Effect\": \"Allow\", '
    r'\"Action\": [\"s3:GetObject\"], \"Resource\": \"uploads\"}]}"'
)
_LIST_POLICY = (
    r'"{\"Version\": \"2012-10-17\", \"Statement\": [{\"Effect\": \"Allow\", '
    r'\"Action\": [\"s3:GetObject\"], \"Resource\": [\"uploads\", \"uploads\"]}]}"'
)
_ARN_POLICY = (
    r'"{\"Version\": \"2012-10-17\", \"Statement\": [{\"Effect\": \"Allow\", '
    r'\"Action\": [\"s3:GetObject\"], \"Resource\": [\"arn:aws:s3:::uploads\", '
    r'\"arn:aws:s3:::uploads/*\"]}]}"'
)


def test_a_scalar_resource_still_imports_as_one_edge():
    """Today's shape, kept working."""
    result = parse_hcl_text(_policy_tf(_LABEL_POLICY))
    iam = [e for e in result.edges if (e.get("data") or {}).get("edgeType") == "iam"]
    assert iam == [{"source": "api", "target": "uploads",
                    "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}}]


def test_a_LIST_resource_does_not_crash_the_whole_import():
    """`hcl-generate` measured the break they were about to hand me: `Resource`
    becomes a list, and `target not in node_by_label` on a list raises
    `TypeError: unhashable type: 'list'` -- which fails the ENTIRE import, not
    one statement. A hand-authored project can carry a list today, so this is
    a live bug independent of their change."""
    result = parse_hcl_text(_policy_tf(_LIST_POLICY))
    assert result.parse_error is None
    iam = [e for e in result.edges if (e.get("data") or {}).get("edgeType") == "iam"]
    # ...and the duplicate collapses. Two ARNs reduce to one canvas node (s3
    # emits bucket AND bucket/*), so without de-duplication one drawn permission
    # returns as two identical edges and the round trip is not stable.
    assert len(iam) == 1


def test_an_ARN_resource_reduces_back_to_the_node_label():
    """`hcl-generate`'s item 3, wired. `Resource` is now a list of real ARNs, and
    the reduction goes through the GATEWAY'S OWN `arn_label` -- the same function
    its evaluator uses to match a policy against a classified request, so an edge
    this importer reconstructs is by construction one that would be enforced.

    The s3 pair (`arn:aws:s3:::uploads` for the bucket, `.../\\*` for its objects)
    reduces to ONE label, so one drawn permission comes back as exactly one edge
    rather than two identical ones."""
    result = parse_hcl_text(_policy_tf(_ARN_POLICY))
    assert result.parse_error is None
    iam = [e for e in result.edges if (e.get("data") or {}).get("edgeType") == "iam"]
    assert iam == [{"source": "api", "target": "uploads",
                    "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}}]
    assert [w for w in result.warnings if "(iam)" in w] == []


def test_a_role_another_workload_POINTS_AT_is_not_folded_away_as_an_auto_role():
    """Found only END TO END, through the real `odin import-tf`, after the unit
    tests were green -- which is the whole reason that run was required.

    odin folds a workload's AUTO-GENERATED execution role back in rather than
    importing it as a node the user never drew, and detects it by the
    `<workload>-role` naming convention. The ec2/ecs half of that pass tested the
    NAME and not the reference, so on a project with an ecs node called `api` and
    a lambda whose role happens to be `api-role`, the role was folded away as the
    service's auto-role -- and the lambda that actually used it then could not be
    regenerated at all:

        unsupported: worker (lambda): role names something that isn't an IAM Role
        on the canvas

    ...which silently took its IAM policy with it. A role something points at
    explicitly is by definition not auto-generated.
    """
    tf = (
        'resource "aws_ecs_cluster" "odin" {\n  name = "odin"\n}\n\n'
        'resource "aws_ecs_task_definition" "api_taskdef" {\n'
        '  family                = "api"\n'
        '  container_definitions = "[{\\"name\\": \\"api\\", \\"image\\": \\"nginx\\"}]"\n'
        "}\n\n"
        'resource "aws_ecs_service" "api" {\n'
        '  name            = "api"\n'
        "  cluster         = aws_ecs_cluster.odin.id\n"
        "  task_definition = aws_ecs_task_definition.api_taskdef.arn\n"
        "}\n\n"
        'resource "aws_iam_role" "api_role" {\n  name = "api-role"\n}\n\n'
        'resource "aws_lambda_function" "worker" {\n'
        '  function_name = "worker"\n'
        "  role          = aws_iam_role.api_role.arn\n"
        '  handler       = "main.handler"\n'
        '  runtime       = "python3.12"\n'
        '  filename      = "worker.zip"\n'
        "}\n"
    )
    result = parse_hcl_text(tf)
    labels = {n["data"]["label"] for n in result.nodes}
    assert "api-role" in labels, "a role the lambda points at was folded away"
    assert _node(result, "lambda")["data"]["role"] == "api-role"

    # ...and the proof that matters: it regenerates instead of being declined.
    project = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": result.edges}))
    assert project.unsupported == []
    assert 'resource "aws_lambda_function" "worker"' in project.files["main.tf"]


def test_a_resource_that_names_nothing_is_NAMED_rather_than_dropped_in_silence():
    """The second-order hazard `hcl-generate` flagged, and the one that matters
    more than the crash: a Resource that reduces to nothing on this canvas used
    to be skipped in silence, so the import reported clean while the permission
    was gone. It survives the reducer for the cases the reducer cannot handle --
    here a bucket that simply is not on this canvas.

    A crash is an honest failure; silence is not, and a lost IAM edge is a lost
    permission."""
    absent = _ARN_POLICY.replace("uploads", "not-on-this-canvas")
    result = parse_hcl_text(_policy_tf(absent))
    assert result.parse_error is None
    (warning,) = [w for w in result.warnings if "(iam)" in w]
    assert "PERMISSION IS LOST" in warning
    assert "not-on-this-canvas" in warning
    assert "s3:GetObject" in warning
