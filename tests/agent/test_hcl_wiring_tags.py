"""A canvas `${{producer.ATTR}}` reference, carried by the generated Terraform.

The limit this closes read: "an imported ECS service loses its canvas wiring
entirely -- both the `${{producer.ATTR}}` env references and the ordering they
produced." The references were deliberately never written, because a resolved
`DATABASE_URL` carries the database password and would land in
`terraform.tfstate` in plaintext; odin then re-derives `depends_on` FROM those
references, so the ordering went with them.

THE DISTINCTION THE WHOLE CHANGE RESTS ON: a reference is not a secret, only its
resolved VALUE is. `${{db.DATABASE_URL}}` names a producer and an attribute and
carries no value at all, so it can be written down for free -- as an `odin:ref:`
tag on the workload's own resource (`hcl.py::_ref_tags`).

So this file's job is to prove the two halves separately:
  * the REFERENCE is carried, ordered, for every kind that can hold one;
  * NOTHING a resolver would produce is. That is the mutation target, and it is
    tested against a REAL resolved value -- `gateway/wiring.py::db_facts`, the
    function that actually builds `DATABASE_URL` at container launch -- rather
    than a string this test invented and could therefore get wrong.
"""
from __future__ import annotations

import json

import pytest

from odin.iac.hcl import generate_tf
from odin.gateway.wiring import db_facts
from odin.spec.translate import canvas_to_stack

# The password is the thing that must not travel. It appears in TWO places on a
# canvas: the rds node's own `password` field (which tofu legitimately has to
# send, so the `aws_db_instance` block carries it) and, invisibly, inside the
# `DATABASE_URL` a resolver builds from it. Only the second is a leak.
#
# DELIBERATELY LOW-ENTROPY, spelled out because the obvious instinct is the
# opposite: a realistic-looking credential here trips the repo's own gitleaks
# pre-commit hook (measured -- `generic-api-key`, entropy 3.92, commit refused).
# Nothing about these tests needs the fixture to look real; they need it to be a
# string that occurs nowhere else, so they can COUNT its occurrences.
PASSWORD = "canvas-password-fixture"
# A literal env value, which is NOT a ref. A user may well have typed a secret
# into one, so no static env entry is emitted at all -- and the mutation that
# breaks this rule ("emit every env entry, not just the refs") is exactly the
# one an implementer reaches for.
STATIC_SECRET = "canvas-token-fixture"


def _canvas(kind: str) -> dict:
    workload = {
        "ecs": {"label": "api", "image": "nginx:alpine"},
        "lambda": {"label": "resizer", "runtime": "python3.12"},
    }[kind]
    return {
        "nodes": [
            {"id": "d", "type": "rds", "position": {"x": 0, "y": 0},
             "data": {"label": "app-db", "password": PASSWORD, "dbName": "appdb", "username": "app"}},
            {"id": "c", "type": "elasticache", "position": {"x": 0, "y": 0}, "data": {"label": "hot"}},
            {"id": "w", "type": kind, "position": {"x": 0, "y": 0},
             "data": {**workload, "env": {
                 "DATABASE_URL": "${{app-db.DATABASE_URL}}",
                 "REDIS_URL": "${{hot.REDIS_URL}}",
                 "API_TOKEN": STATIC_SECRET,
             }}},
        ],
        "edges": [],
    }


def _main_tf(kind: str) -> str:
    return generate_tf(canvas_to_stack(_canvas(kind))).files["main.tf"]


_TF_TYPE = {"ecs": "aws_ecs_service", "lambda": "aws_lambda_function"}


def _resource_block(main_tf: str, tf_type: str) -> str:
    """One `resource "<type>" ... { ... }` block. Scoping matters: every
    resource carries an `odin:node` tag, so an assertion about ORDER inside the
    workload's own tag map has to read that block and not the whole file."""
    rest = main_tf[main_tf.index(f'resource "{tf_type}"'):]
    return rest[:rest.index("\n}\n")]


@pytest.mark.parametrize("kind", ["ecs", "lambda"])
def test_every_ref_is_carried_as_a_tag_naming_producer_and_attribute(kind: str):
    main_tf = _main_tf(kind)
    assert '"odin:ref:DATABASE_URL" = "app-db.DATABASE_URL"' in main_tf
    assert '"odin:ref:REDIS_URL"    = "hot.REDIS_URL"' in main_tf


@pytest.mark.parametrize("kind", ["ecs", "lambda"])
def test_the_ref_tags_are_ordered_so_a_round_trip_is_byte_stable(kind: str):
    """`depends_on` is already a sorted set, so ordering is not a correctness
    question -- it is a byte-stability one: generate -> import -> generate has to
    produce the same file, and an env dict's iteration order must not decide it."""
    block = _resource_block(_main_tf(kind), _TF_TYPE[kind])
    assert block.index('"odin:ref:DATABASE_URL"') < block.index('"odin:ref:REDIS_URL"')
    # ...and `odin:node` stays last, where every other kind already has it.
    assert block.index('"odin:ref:REDIS_URL"') < block.index('"odin:node"')


@pytest.mark.parametrize("kind", ["ecs", "lambda"])
def test_a_static_env_value_is_not_emitted_at_all(kind: str):
    """THE MUTATION TARGET. A literal env entry is not a reference, and a user
    may have typed a credential into one. Emitting `env` wholesale instead of
    `res.refs` would put it in `main.tf` and, from there, in the state file."""
    main_tf = _main_tf(kind)
    assert STATIC_SECRET not in main_tf
    assert "API_TOKEN" not in main_tf


@pytest.mark.parametrize("kind", ["ecs", "lambda"])
def test_the_resolved_value_of_a_ref_reaches_the_file_nowhere(kind: str):
    """The claim that matters, measured against the REAL resolver.

    `db_facts` is the function `gateway/wiring.py` uses to build the value a
    `${{app-db.DATABASE_URL}}` ref resolves to at container launch. Building the
    expected string here by hand would prove only that this test can concatenate;
    calling the real thing proves the value odin would really inject is absent.
    """
    resolved = db_facts({
        "endpoint_port": 54321, "master_username": "app",
        "master_password": PASSWORD, "db_name": "appdb",
    })["DATABASE_URL"]
    assert PASSWORD in resolved, "the fixture stopped exercising the leak it exists for"

    main_tf = _main_tf(kind)
    assert resolved not in main_tf
    # And the password itself appears ONCE: in the `aws_db_instance` block, which
    # is the one place tofu has to send it. Counting rather than asserting
    # absence is deliberate -- "the password is not in the file" would be a false
    # claim, and a test that asserts a false claim gets weakened later.
    assert main_tf.count(PASSWORD) == 1
    assert PASSWORD in _resource_block(main_tf, "aws_db_instance")


def test_a_ref_to_a_producer_that_is_not_on_the_canvas_is_still_recorded():
    """It is a wiring ERROR, reported as one -- but the tag is what an import
    needs to give the user their typo back to fix, instead of silently
    discarding the reference along with the mistake."""
    canvas = _canvas("ecs")
    canvas["nodes"][2]["data"]["env"]["GHOST"] = "${{nowhere.ENDPOINT}}"
    project = generate_tf(canvas_to_stack(canvas))
    assert '"odin:ref:GHOST"' in project.files["main.tf"]
    assert any("nowhere" in error for error in project.wiring_errors)


def test_the_tag_value_is_valid_where_the_literal_ref_text_would_not_be():
    """Why the tag holds `producer.attr` and not `${{producer.attr}}`.

    Measured against OpenTofu 1.12.3: `value = "${{db.DATABASE_URL}}"` is a
    PARSE error ("Missing key/value separator"), which fails the whole project
    rather than one resource. `"$${{...}}"` does parse, but `$`/`{`/`}` are
    outside AWS's documented tag-value character set, so it would break on the
    real Amazon this file is meant to be portable to. Pinned here because
    "portable" is exactly the kind of claim that rots quietly.
    """
    main_tf = _main_tf("ecs")
    assert "${{" not in main_tf, "an un-escaped ref would be a tofu PARSE error"
    assert "$${" not in main_tf, "an escaped ref would be an invalid AWS tag value"


def test_the_ordering_the_refs_produce_is_still_derived_from_them():
    """The other half of the limit: odin re-derives `depends_on` FROM the refs,
    so recovering the refs recovers the ordering. Recorded here so the two halves
    cannot drift -- a change that kept the tags but stopped deriving ordering
    would leave the doc claiming something untrue."""
    main_tf = _main_tf("ecs")
    depends = next(line for line in main_tf.splitlines() if "depends_on" in line)
    assert "aws_db_instance.app_db" in depends
    assert "aws_elasticache_cluster.hot" in depends


def test_a_lambdas_refs_do_not_become_an_environment_block():
    """The reference is carried as a TAG, never as a value the runtime reads.

    A `container_definitions`/`environment` entry holding the ref TEXT would be
    handed to the real container as a literal `${{...}}` string -- worse than
    absent, because the workload would start with a plausible-looking wrong
    value instead of failing. The values still arrive at launch, from
    `gateway/wiring.py`.
    """
    assert "environment" not in _resource_block(_main_tf("lambda"), "aws_lambda_function")


def test_an_ecs_services_container_definitions_carry_no_ref_at_all():
    main_tf = _main_tf("ecs")
    line = next(line for line in main_tf.splitlines() if "container_definitions" in line)
    definitions = json.loads(json.loads(line.split(" = ", 1)[1]))
    assert all("environment" not in container for container in definitions)
    assert "DATABASE_URL" not in line
