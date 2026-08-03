"""What a drawn edge MEANS, and the two ways odin used to lose the answer.

Two defects, both the same shape -- odin showing the user one thing and doing
another -- and both measured before this file existed:

1. **The `iam_role -> workload` edge was INERT.** `iam_role` declares no
   `iamActions` (correctly: a role is not an IAM data-plane target), so the
   canvas never registered the pair, the edge fell through to the catch-all, and
   NOTHING in Python read it. Draw `admin-role -> my-lambda` while the lambda's
   `role` field says `other-role` and you got a dead edge, `other-role` in the
   generated file, and `other-role`'s statements enforced by the gateway --
   silently, and for every revision after.

2. **An sns/sqs subscription edge drawn BACKWARDS was a silent no-op.** Both
   consumers key on the drawn direction (`agent/hcl.py`'s subscription pass
   reads `edge.src` as the topic; `reconcile/reconciler.py::_desired_subs`
   filters `e.src == sns_id`), so `sqs -> sns` gave a grey line, a green Apply,
   no subscription, and no entry in `unsupported` or `wiring_errors`.

Everything here goes through the PRODUCT'S OWN PATH -- `canvas_to_stack` into
`generate_tf` -- rather than asserting on the intermediate field, because the
whole class of bug being fixed is a value that looks right in the Stack and is
read by nobody downstream.
"""
from __future__ import annotations

from odin.agent.hcl import generate_tf
from odin.spec.models import Edge, Stack
from odin.spec.translate import (
    EDGE_KINDS,
    ENCRYPTION,
    LEGACY_UNMODELLED,
    ROLE_ASSUMPTION,
    SNS_SUBSCRIPTION,
    UNMODELLED,
    canvas_to_stack,
)


def _edge(source: str, target: str, kind: str, **data) -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target,
            "data": {"edgeType": kind, **data}}


def _role_canvas(edges: list[dict], role_field: str = "") -> dict:
    return {
        "nodes": [
            {"id": "role-1", "type": "iam_role", "position": {"x": 0, "y": 0},
             "data": {"label": "admin-role"}},
            {"id": "role-2", "type": "iam_role", "position": {"x": 20, "y": 0},
             "data": {"label": "aaa-role"}},
            {"id": "lam-1", "type": "lambda", "position": {"x": 40, "y": 0},
             "data": {"label": "worker", "code": "def lambda_handler(e, c):\n    return e",
                      **({"role": role_field} if role_field else {})}},
            {"id": "ec2-1", "type": "ec2", "position": {"x": 60, "y": 0},
             "data": {"label": "box", "vpc": "net", "subnet": "web"}},
            {"id": "ecs-1", "type": "ecs", "position": {"x": 80, "y": 0},
             "data": {"label": "svc"}},
        ],
        "edges": edges,
    }


def _role_of(stack: Stack, resource_id: str) -> str:
    resource = next(r for r in stack.resources if r.id == resource_id)
    field = resource.fields.get("role")
    return str(field.value).strip() if field else ""


# --- 1. the role edge ---------------------------------------------------------


def test_a_role_edge_reaches_the_generated_terraform():
    """The whole defect: the edge must decide what `_lambda` emits, not merely
    sit in the Stack. Asserted on `main.tf` rather than on the field, because
    the old bug was precisely a value nothing downstream consumed."""
    stack = canvas_to_stack(_role_canvas([_edge("role-1", "lam-1", ROLE_ASSUMPTION)]))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_iam_role" "admin_role"' in main_tf
    # `_block` pads its keys into a column, so match on the value side only.
    assert "= aws_iam_role.admin_role.arn" in main_tf
    # ...and the auto-generated execution role a role-less lambda would have got
    # is NOT emitted, because the canvas named one.
    assert "worker_role" not in main_tf


def test_a_role_edge_is_direction_insensitive():
    forward = canvas_to_stack(_role_canvas([_edge("role-1", "lam-1", ROLE_ASSUMPTION)]))
    backward = canvas_to_stack(_role_canvas([_edge("lam-1", "role-1", ROLE_ASSUMPTION)]))
    assert _role_of(forward, "worker") == _role_of(backward, "worker") == "admin-role"


def test_a_hand_typed_role_wins_over_a_drawn_one():
    """`odin canvas set`, the README's JSON schema and the translation agent all
    write this field directly. A role is single-valued, so unlike
    `securityGroups` the edge cannot ADD to it -- and silently overwriting what
    a user typed would be the same lie in the other direction."""
    stack = canvas_to_stack(
        _role_canvas([_edge("role-1", "lam-1", ROLE_ASSUMPTION)], role_field="aaa-role"),
    )
    assert _role_of(stack, "worker") == "aaa-role"


def test_two_conflicting_role_edges_resolve_deterministically():
    """A contradiction the canvas can express and odin cannot resolve. The lowest
    name wins so the generated file never depends on edge ORDERING -- better than
    the old behaviour (both edges did nothing) and still recorded as an open
    limit in docs/limits.md rather than presented as an answer."""
    both = [_edge("role-1", "lam-1", ROLE_ASSUMPTION), _edge("role-2", "lam-1", ROLE_ASSUMPTION)]
    assert _role_of(canvas_to_stack(_role_canvas(both)), "worker") == "aaa-role"
    assert _role_of(canvas_to_stack(_role_canvas(both[::-1])), "worker") == "aaa-role"


def test_a_role_edge_to_ec2_or_ecs_authors_nothing():
    """Neither builder reads a `role` field -- they reach a role through an
    auto-generated one plus an instance profile / `task_role_arn`. Writing the
    field anyway would be the drawn-line-that-does-nothing bug this edge type
    exists to fix, one kind over. The canvas does not offer `role` for these
    pairs either (`ui/src/lib/iam.ts::roleHolderTypes`)."""
    stack = canvas_to_stack(_role_canvas([
        _edge("role-1", "ec2-1", ROLE_ASSUMPTION), _edge("role-1", "ecs-1", ROLE_ASSUMPTION),
    ]))
    assert _role_of(stack, "box") == "" and _role_of(stack, "svc") == ""


def test_only_a_role_KIND_edge_authors_the_field():
    """The kind carries the meaning: an IAM grant or an unmodelled line between
    the same two nodes must not quietly become the lambda's execution role."""
    for kind in ("iam", UNMODELLED, LEGACY_UNMODELLED, "sg"):
        stack = canvas_to_stack(_role_canvas([_edge("role-1", "lam-1", kind)]))
        assert _role_of(stack, "worker") == "", kind


def test_a_role_edge_to_something_that_is_not_a_role_is_ignored():
    stack = canvas_to_stack(_role_canvas([_edge("ec2-1", "lam-1", ROLE_ASSUMPTION)]))
    assert _role_of(stack, "worker") == ""


def test_the_role_edge_survives_into_the_stack():
    """The merge must not consume the edge -- `/world`, the IAM review and the
    UI all still read `stack.edges`."""
    stack = canvas_to_stack(_role_canvas([_edge("role-1", "lam-1", ROLE_ASSUMPTION)]))
    assert any(e.src == "admin-role" and e.dst == "worker" and e.kind == ROLE_ASSUMPTION
               for e in stack.edges)


# --- 2. subscription direction ------------------------------------------------


def _sub_canvas(edges: list[dict]) -> dict:
    return {
        "nodes": [
            {"id": "sns-1", "type": "sns", "position": {"x": 0, "y": 0},
             "data": {"label": "events"}},
            {"id": "sqs-1", "type": "sqs", "position": {"x": 20, "y": 0},
             "data": {"label": "jobs"}},
        ],
        "edges": edges,
    }


SUBSCRIPTION_BLOCK = 'resource "aws_sns_topic_subscription"'


def test_a_subscription_drawn_forwards_builds():
    stack = canvas_to_stack(_sub_canvas([_edge("sns-1", "sqs-1", SNS_SUBSCRIPTION)]))
    assert SUBSCRIPTION_BLOCK in generate_tf(stack).files["main.tf"]


def test_a_subscription_drawn_BACKWARDS_builds_too():
    """A subscription has exactly one possible direction: a topic fans out to a
    queue, never the reverse. Drawing it queue -> topic used to give a grey line,
    a green Apply, no subscription, and no entry in `unsupported` or
    `wiring_errors` -- a silent no-op with no test in either direction.

    hcl.py's ALB pass three hundred lines earlier already accepts both orderings
    "since which end the user started from carries no meaning"; the same
    reasoning is now applied here, in the spec, where both consumers see it."""
    stack = canvas_to_stack(_sub_canvas([_edge("sqs-1", "sns-1", SNS_SUBSCRIPTION)]))
    assert SUBSCRIPTION_BLOCK in generate_tf(stack).files["main.tf"]


def test_a_backwards_subscription_is_oriented_for_the_RECONCILER_too():
    """`reconciler.py::_desired_subs` filters `e.src == sns_id` -- the same
    directional read, in the path that fans a live topic out to its queues. It is
    fixed by the same orientation rather than by a second patch there."""
    stack = canvas_to_stack(_sub_canvas([_edge("sqs-1", "sns-1", SNS_SUBSCRIPTION)]))
    kind_of = {r.id: r.kind for r in stack.resources}
    desired = tuple(e.dst for e in stack.edges
                    if e.src == "events" and kind_of.get(e.dst) == "sqs")
    assert desired == ("jobs",)


def test_a_legacy_network_typed_subscription_still_builds():
    """THE destruction guard. Every canvas saved before edge types were named
    stores `edgeType: "network"` on this edge and works anyway, because both
    consumers key on the two NODE kinds and never read `edge.kind`.

    If a builder ever started REQUIRING `kind == "subscription"` without a
    migration landing in the same commit, every one of those canvases would
    silently drop its subscription from the generated HCL and `tofu` would
    DESTROY the live subscription on the next apply -- and the reconciler would
    stay quiet, because `_desired_subs` only ever ADDS a missing subscription
    and never unsubscribes. This test fails the moment that gate appears."""
    for kind in (LEGACY_UNMODELLED, "iam", UNMODELLED):
        stack = canvas_to_stack(_sub_canvas([_edge("sns-1", "sqs-1", kind)]))
        assert SUBSCRIPTION_BLOCK in generate_tf(stack).files["main.tf"], kind


def test_a_legacy_network_typed_subscription_drawn_backwards_builds_too():
    stack = canvas_to_stack(_sub_canvas([_edge("sqs-1", "sns-1", LEGACY_UNMODELLED)]))
    assert SUBSCRIPTION_BLOCK in generate_tf(stack).files["main.tf"]


def test_an_iam_edge_is_never_reoriented():
    """`hcl.py::_granted_ids` and `gateway/policy.py::compile_policies` both read
    `edge.src` as the PRINCIPAL, so flipping one would move a grant to a
    different node -- a silent authorization change."""
    canvas = {
        "nodes": [
            {"id": "sqs-1", "type": "sqs", "position": {"x": 0, "y": 0}, "data": {"label": "jobs"}},
            {"id": "sns-1", "type": "sns", "position": {"x": 20, "y": 0}, "data": {"label": "events"}},
        ],
        "edges": [_edge("sqs-1", "sns-1", "iam", permissions=["sns:Publish"])],
    }
    (edge,) = canvas_to_stack(canvas).edges
    assert (edge.src, edge.dst) == ("jobs", "events")


# --- 3. the kind vocabulary ---------------------------------------------------


def test_an_old_canvas_carrying_kind_network_round_trips_unchanged():
    """`Edge.kind` is a free `str` in spec/models.py, which is what lets every
    canvas ever saved keep parsing. Verified rather than assumed: the field
    accepts the legacy word, keeps it verbatim, and survives a model round trip."""
    assert Stack.model_fields["edges"] is not None
    legacy = Edge(src="a", dst="b", kind=LEGACY_UNMODELLED)
    assert legacy.kind == LEGACY_UNMODELLED
    assert Edge.model_validate_json(legacy.model_dump_json()) == legacy
    stack = Stack(env="e", resources=(), edges=(legacy,))
    assert Stack.model_validate_json(stack.model_dump_json()).edges == (legacy,)
    # ...and it still arrives from a canvas untouched.
    canvas = {"nodes": [{"id": "a", "type": "s3", "data": {"label": "a"}},
                        {"id": "b", "type": "s3", "data": {"label": "b"}}],
              "edges": [_edge("a", "b", LEGACY_UNMODELLED)]}
    assert canvas_to_stack(canvas).edges[0].kind == LEGACY_UNMODELLED


def test_an_edge_with_no_type_at_all_is_unmodelled():
    canvas = {"nodes": [{"id": "a", "type": "s3", "data": {"label": "a"}},
                        {"id": "b", "type": "kms", "data": {"label": "b"}}],
              "edges": [{"id": "e", "source": "a", "target": "b"}]}
    assert canvas_to_stack(canvas).edges[0].kind == UNMODELLED


# --- 4. the encryption edge (W2.9) --------------------------------------------


def _kms_canvas(edges: list[dict], **typed) -> dict:
    """A key, a lower-named key, a secret and a parameter -- plus an s3 bucket,
    which is the kind the edge must REFUSE to author onto (nothing encrypts a
    RustFS object; see `_ENCRYPTION_FIELDS`)."""
    return {
        "nodes": [
            {"id": "k-1", "type": "kms", "position": {"x": 0, "y": 0},
             "data": {"label": "app-key"}},
            {"id": "k-2", "type": "kms", "position": {"x": 20, "y": 0},
             "data": {"label": "aaa-key"}},
            {"id": "sec-1", "type": "secret", "position": {"x": 40, "y": 0},
             "data": {"label": "db-password", "secretString": "hunter2",
                      **({"kmsKeyId": typed["kmsKeyId"]} if "kmsKeyId" in typed else {})}},
            {"id": "ssm-1", "type": "ssm", "position": {"x": 60, "y": 0},
             "data": {"label": "/odin/db", "paramValue": "x",
                      **({"keyId": typed["keyId"]} if "keyId" in typed else {})}},
            {"id": "s3-1", "type": "s3", "position": {"x": 80, "y": 0},
             "data": {"label": "uploads"}},
        ],
        "edges": edges,
    }


def _field_of(stack: Stack, resource_id: str, field: str) -> str:
    resource = next(r for r in stack.resources if r.id == resource_id)
    value = resource.fields.get(field)
    return str(value.value).strip() if value else ""


def test_an_encryption_edge_reaches_the_generated_terraform():
    """The product's own path, for the same reason the role tests take it: the
    bug class is a value that looks right in the Stack and is read by nobody.
    The INTERPOLATED reference is the half that matters -- it is what orders the
    key ahead of the secret, and `kmsctl.seal` refuses a key that does not exist
    yet, so without the ordering the apply fails outright rather than retrying."""
    stack = canvas_to_stack(_kms_canvas([
        _edge("k-1", "sec-1", ENCRYPTION), _edge("k-1", "ssm-1", ENCRYPTION),
    ]))
    main_tf = generate_tf(stack).files["main.tf"]
    assert 'resource "aws_kms_key" "app_key"' in main_tf
    # `_block` pads its keys into a column, so match on the value side only.
    assert main_tf.count("= aws_kms_key.app_key.key_id") == 2


def test_the_key_carries_the_odin_node_tag_which_is_its_only_name():
    """Real `CreateKey` takes no name argument, so `gateway/models/kmsctl.py`
    keys a key by this tag and mints a uuid without it. Every reference to the
    key -- the secret's `kms_key_id`, an IAM grant's Resource, the World
    projection -- resolves through the label, so an untagged key is addressable
    from nothing. `_tags_block` stamps it on every primary, but this kind is the
    one where losing it is silent AND total."""
    main_tf = generate_tf(canvas_to_stack(_kms_canvas([]))).files["main.tf"]
    key_block = main_tf.split('resource "aws_kms_key" "app_key"')[1].split("\nresource ")[0]
    assert '"odin:node" = "app-key"' in key_block
    # ...and no `name`, because the AWS resource has no such argument at all.
    assert "name" not in key_block


def test_an_encryption_edge_is_direction_insensitive():
    forward = canvas_to_stack(_kms_canvas([_edge("k-1", "sec-1", ENCRYPTION)]))
    backward = canvas_to_stack(_kms_canvas([_edge("sec-1", "k-1", ENCRYPTION)]))
    assert _field_of(forward, "db-password", "kmsKeyId") == "app-key"
    assert _field_of(backward, "db-password", "kmsKeyId") == "app-key"


def test_a_hand_typed_key_wins_over_a_drawn_one():
    stack = canvas_to_stack(
        _kms_canvas([_edge("k-1", "sec-1", ENCRYPTION)], kmsKeyId="aaa-key"),
    )
    assert _field_of(stack, "db-password", "kmsKeyId") == "aaa-key"


def test_two_conflicting_encryption_edges_resolve_deterministically():
    both = [_edge("k-1", "sec-1", ENCRYPTION), _edge("k-2", "sec-1", ENCRYPTION)]
    assert _field_of(canvas_to_stack(_kms_canvas(both)), "db-password", "kmsKeyId") == "aaa-key"
    assert _field_of(canvas_to_stack(_kms_canvas(both[::-1])), "db-password", "kmsKeyId") == "aaa-key"


def test_an_encryption_edge_to_s3_authors_nothing():
    """odin holds no key for a RustFS object, a Postgres volume or a dynalite
    item, so there is no field to author and the pair stays `unmodelled` on the
    canvas too (`ui/src/lib/iam.ts::encryptionTargetTypes`)."""
    stack = canvas_to_stack(_kms_canvas([_edge("k-1", "s3-1", ENCRYPTION)]))
    assert _field_of(stack, "uploads", "kmsKeyId") == ""
    assert _field_of(stack, "uploads", "keyId") == ""


def test_only_an_encryption_KIND_edge_authors_the_field():
    for kind in ("iam", UNMODELLED, LEGACY_UNMODELLED, "sg", ROLE_ASSUMPTION):
        stack = canvas_to_stack(_kms_canvas([_edge("k-1", "sec-1", kind)]))
        assert _field_of(stack, "db-password", "kmsKeyId") == "", kind


def test_a_key_field_naming_a_node_that_is_not_a_key_is_DECLINED_not_ignored():
    """The one place this edge type must not fail soft. A silently dropped
    `kms_key_id` would seal the secret under the env's DEFAULT key while the
    canvas named another -- odin using one key and showing a different one. The
    resource is declined with the reason instead, which `/apply` reports."""
    canvas = _kms_canvas([], kmsKeyId="uploads")  # an s3 bucket, not a key
    project = generate_tf(canvas_to_stack(canvas))
    (declined,) = [u for u in project.unsupported if u.startswith("db-password")]
    assert "'uploads'" in declined and "kms node" in declined
    assert "aws_secretsmanager_secret" not in project.files["main.tf"]


def test_the_encryption_edge_survives_into_the_stack():
    stack = canvas_to_stack(_kms_canvas([_edge("k-1", "sec-1", ENCRYPTION)]))
    assert [(e.src, e.dst, e.kind) for e in stack.edges] == [("app-key", "db-password", ENCRYPTION)]


def test_a_kms_node_is_a_real_stack_resource_now():
    """It was skipped by Apply entirely until W2.9 -- `_KIND` had no entry, so
    `skipped_node_types` reported it and nothing was ever built."""
    from odin.spec.translate import skipped_node_types
    canvas = _kms_canvas([])
    assert "kms" not in skipped_node_types(canvas)
    assert "app-key" in {r.id: r.kind for r in canvas_to_stack(canvas).resources}


def test_the_kind_vocabulary_names_every_kind_the_canvas_can_author():
    """`EDGE_KINDS` is the ONE place Python knows which kinds exist, and it is
    what `agent/chat.py` validates against. Both catch-alls are in it: dropping
    the legacy one would make `odin chat` refuse an edge type that is sitting in
    every saved canvas on disk."""
    assert {
        "iam", "sg", "role", "target", "subscription", "connection", ENCRYPTION, "volume", "mount",
    } <= EDGE_KINDS
    assert UNMODELLED in EDGE_KINDS and LEGACY_UNMODELLED in EDGE_KINDS
    assert "ref" in EDGE_KINDS  # Edge.kind's own default
    # `<=` is a SUBSET check, so a kind added to `EDGE_KINDS` and forgotten here
    # passes vacuously -- exactly what happened to `connection` on the day it
    # landed. Pinning the whole set is what makes the test's own name true.
    assert EDGE_KINDS == {
        "iam", "sg", "role", "target", "subscription", "connection", ENCRYPTION, "volume", "mount",
        UNMODELLED, LEGACY_UNMODELLED, "ref",
    }
