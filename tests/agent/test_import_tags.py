"""Tags survive an import for EVERY kind -- the ratchet, not four fixes.

`docs/limits.md` reported this as "sqs/sns tags are dropped on import". Measured
against the real generator before the fix, it was FOUR kinds -- `sqs`, `sns`,
`dynamodb` and `iam_role` -- because `_CARRIED_ATTRS` listed `tags` per kind and
those four sets stopped at `name`. Fixing the two that were reported would have
left the other two, which is this repo's most-repeated bug shape.

It was also two defects wearing one coat. `hcl.py::_tags_block` stamps an
`odin:node` tag on every primary resource, so those four kinds ALSO printed
`imported without unmodeled attribute(s): tags` on every import of odin's own
output -- a warning about a tag odin itself had just written. Warning noise is
not harmless in a module whose entire value is that its warnings are worth
reading.

So the assertions below are driven by `_KIND` itself rather than by a list a
human maintains: adding a kind to the importer without tags working fails here.
"""
from __future__ import annotations

import pytest

from odin.iac.hcl import generate_tf
from odin.iac.import_tf import _KIND, _NAME_ATTR, parse_hcl_text
from odin.spec.translate import canvas_to_stack

# The one argument each type needs before `_label` will call it by name. Kinds
# with no `name` argument at all (`aws_instance`) fall back to the `odin:node`
# tag, which is why it is in the fixture below.
_TAGS_HCL = '  tags = {\n    "team"      = "platform"\n    "odin:node" = "{label}"\n  }\n'


def _one_resource(rtype: str, label: str) -> str:
    name_attr = _NAME_ATTR.get(rtype)
    name_line = f'  {name_attr} = "{label}"\n' if name_attr else ""
    return (
        f'resource "{rtype}" "probe" {{\n'
        f"{name_line}"
        f"{_TAGS_HCL.replace('{label}', label)}"
        "}\n"
    )


@pytest.mark.parametrize("rtype,kind", sorted(_KIND.items()))
def test_every_kind_carries_its_user_tags_onto_the_canvas(rtype: str, kind: str):
    """The tags reach `data.tags`. Driven by `_KIND`, so a new kind is covered
    the moment it is added rather than when someone remembers to add a case."""
    result = parse_hcl_text(_one_resource(rtype, "probe-node"))
    assert result.parse_error is None
    (node,) = result.nodes
    assert node["type"] == kind
    assert node["data"].get("tags") == {"team": "platform"}, (
        f"{rtype} -> {kind} dropped its user tags"
    )


@pytest.mark.parametrize("rtype,kind", sorted(_KIND.items()))
def test_no_kind_reports_tags_as_an_unmodeled_attribute(rtype: str, kind: str):
    """The other half. `odin:node` is odin's OWN tag, so a `tags` drop-warning
    fires on every import of a file odin generated -- four kinds did exactly
    that. Asserting on the absence of the word is deliberate: the drop and the
    warning were a single bug and could regress together."""
    result = parse_hcl_text(_one_resource(rtype, "probe-node"))
    offending = [w for w in result.warnings if "tags" in w]
    assert offending == [], f"{rtype} warns about tags it did carry: {offending}"


def test_the_four_reported_kinds_round_trip_their_tags_through_generate():
    """End to end through the REAL generator, not just the parser: canvas ->
    HCL -> canvas. `limits.md` named sqs and sns; dynamodb and iam_role had the
    identical defect and are here so closing two of four cannot pass as done."""
    canvas = {"nodes": [
        {"id": "n1", "type": "sqs", "data": {"label": "jobs", "tags": {"team": "core"}}},
        {"id": "n2", "type": "sns", "data": {"label": "alerts", "tags": {"team": "core"}}},
        {"id": "n3", "type": "dynamodb",
         "data": {"label": "sessions", "hashKey": "id", "tags": {"tier": "gold"}}},
        {"id": "n4", "type": "iam_role", "data": {"label": "worker-role", "tags": {"owner": "ops"}}},
    ], "edges": []}

    imported = parse_hcl_text(generate_tf(canvas_to_stack(canvas)).files["main.tf"])
    by_label = {n["data"]["label"]: n["data"] for n in imported.nodes}
    assert by_label["jobs"]["tags"] == {"team": "core"}
    assert by_label["alerts"]["tags"] == {"team": "core"}
    assert by_label["sessions"]["tags"] == {"tier": "gold"}
    assert by_label["worker-role"]["tags"] == {"owner": "ops"}

    # ...and the SECOND generation still carries them, which is the property a
    # user relying on `import -> edit -> apply` actually depends on.
    regenerated = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": []})).files["main.tf"]
    assert regenerated.count('"team"      = "core"') == 2
    assert '"tier"      = "gold"' in regenerated
    assert '"owner"     = "ops"' in regenerated


def test_odins_own_management_tag_is_never_surfaced_as_a_user_tag():
    """`odin:node` is machinery (`reconcile/tf_status.py`, `gateway/keys.py`
    both key off it). Carrying it back as a user tag would show it in the config
    panel and let a user edit the thing two subsystems match on."""
    result = parse_hcl_text(_one_resource("aws_sqs_queue", "jobs"))
    (node,) = result.nodes
    assert node["data"]["tags"] == {"team": "platform"}
    assert "odin:node" not in node["data"]["tags"]
