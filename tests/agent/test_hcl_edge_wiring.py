"""v0.8.15 — the three edges that granted a permission and wired nothing.

Each one was the decorative-edge bug: a line you draw on the canvas, that
compiles to an IAM grant, and that does not connect the thing the grant is
for. They are fixed in the same direction the rest of odin's edges work --
the edge AUTHORS a value a builder reads -- and the tests here are written
against what the file now CARRIES, never against a builder's return value.

THE HAZARD THIS FILE EXISTS TO PIN (`test_a_legacy_network_typed_edge_*`):
none of these passes may gate on `edge.kind`. Every canvas saved before the
edge-type registry types its edges `"network"`
(`spec/translate.py::LEGACY_UNMODELLED`), so a builder that required a new
kind name would drop the resource from the generated HCL for those canvases
-- and `tofu destroy` the live one on the next Apply -- while a reconciler
test stayed green through it, because `_desired_subs` only ever ADDS.
"""
from __future__ import annotations

import json
import shutil

import pytest

from odin.agent import hcl
from odin.agent.hcl import generate_tf, resource_attrs, unquote
from odin.agent.translate import validate_refinement
from odin.spec.models import Edge, FieldValue, ResourceDesired, Stack


def _fields(**kwargs: str) -> dict[str, FieldValue]:
    return {k: FieldValue(value=v, provenance="user") for k, v in kwargs.items()}


def _group_names(project) -> list[str]:
    return sorted(
        unquote(attrs["name"])
        for (rtype, _name), attrs in resource_attrs(project.files).items()
        if rtype == "aws_cloudwatch_log_group"
    )


def _policy_resources(project) -> list[str]:
    out: list[str] = []
    for (rtype, _name), attrs in resource_attrs(project.files).items():
        if rtype != "aws_iam_role_policy":
            continue
        for statement in json.loads(unquote(attrs["policy"]))["Statement"]:
            out += statement["Resource"]
    return sorted(out)


def _container_definitions(project, taskdef_name: str) -> list[dict]:
    attrs = resource_attrs(project.files)[("aws_ecs_task_definition", taskdef_name)]
    return json.loads(unquote(attrs["container_definitions"]))


# --- 1. logs -> workload: the drawn group is the one that receives -----------
#
# `lambdactl._ship_logs` writes to `/aws/lambda/{function}` and
# `ecsctl._ship_task_logs` to `/ecs/{service}`; neither reads a destination
# from anywhere. So a `/odin/logs` tile drawn to lambda `myfn` created TWO
# groups -- the drawn one, which the policy granted PutLogEvents on, and
# `/aws/lambda/myfn`, which got every line -- and the drawn one stayed empty
# forever. The only canvas that appeared to work was one whose label coincided.


def _logs_stack(workload: ResourceDesired, *, edge_kind: str = "iam", label: str = "/odin/logs") -> Stack:
    return Stack(
        resources=(workload, ResourceDesired(id=label, kind="logs")),
        edges=(Edge(src=workload.id, dst=label, kind=edge_kind, perms=("logs:PutLogEvents",)),),
    )


def test_a_log_group_drawn_to_a_lambda_is_created_at_the_functions_real_destination():
    stack = _logs_stack(ResourceDesired(id="myfn", kind="lambda"))
    project = generate_tf(stack)
    # ONE group, and it is the one the substrate ships into.
    assert _group_names(project) == ["/aws/lambda/myfn"]
    assert "/odin/logs" not in project.files["main.tf"].replace('"odin:node" = "/odin/logs"', "")


def test_a_log_group_drawn_to_an_ecs_service_takes_the_services_destination():
    stack = _logs_stack(ResourceDesired(id="api", kind="ecs"))
    assert _group_names(generate_tf(stack)) == ["/ecs/api"]


def test_the_renamed_group_still_carries_the_canvas_label_as_its_odin_node_tag():
    """The identity bridge that keeps the rename from stranding the node.

    `reconcile/tf_status.py::_log_groups` and `api/logs.py::_find_log_group`
    both resolve a log group through this tag, so /world keeps reporting the
    node under the label the user typed and `odin logs --node /odin/logs`
    keeps finding it. Without the tag the rename would make the drawn node
    unaddressable, which is a worse bug than the one being fixed."""
    project = generate_tf(_logs_stack(ResourceDesired(id="myfn", kind="lambda")))
    attrs = resource_attrs(project.files)[("aws_cloudwatch_log_group", "_odin_logs")]
    # python-hcl2 keeps a quoted literal's `"` on both keys and values.
    assert unquote(attrs["tags"]['"odin:node"']) == "/odin/logs"
    assert unquote(attrs["name"]) == "/aws/lambda/myfn"


def test_the_grant_follows_the_group_to_the_name_the_classifier_will_report():
    """THE HALF THAT MAKES THE PERMISSION REAL, and it is not cosmetic.

    The gateway authorizes from the applied IAM and `classify.py` reports the
    group name a request NAMES, so a Resource left on the old label would deny
    the very PutLogEvents this edge was drawn to allow -- swapping one
    decorative edge for another."""
    project = generate_tf(_logs_stack(ResourceDesired(id="myfn", kind="lambda")))
    assert _policy_resources(project) == [
        "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/myfn",
        "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/myfn:*",
    ]


def test_a_log_group_with_no_workload_edge_keeps_its_own_label():
    """Every canvas drawn before this existed is byte-identical to before."""
    stack = Stack(resources=(ResourceDesired(id="/odin/logs", kind="logs"),))
    assert _group_names(generate_tf(stack)) == ["/odin/logs"]


def test_a_log_group_edged_to_an_ec2_node_is_untouched():
    """Nothing ships a VM's output into CloudWatch Logs, so an ec2 -> logs edge
    is a grant for the code INSIDE the VM to call PutLogEvents itself -- which
    works exactly as drawn. Renaming the group would BREAK that."""
    stack = Stack(
        resources=(
            ResourceDesired(id="net", kind="vpc"),
            ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
            ResourceDesired(id="box", kind="ec2", fields=_fields(subnet="web")),
            ResourceDesired(id="/odin/logs", kind="logs"),
        ),
        edges=(Edge(src="box", dst="/odin/logs", kind="iam", perms=("logs:PutLogEvents",)),),
    )
    project = generate_tf(stack)
    assert _group_names(project) == ["/odin/logs"]
    assert _policy_resources(project) == [
        "arn:aws:logs:us-east-1:000000000000:log-group:/odin/logs",
        "arn:aws:logs:us-east-1:000000000000:log-group:/odin/logs:*",
    ]


def test_one_log_group_drawn_as_two_workloads_sink_is_declined_with_both_names():
    """A group has one name and the two substrates ship to two -- odin cannot
    make one node be both, and says which two rather than picking."""
    stack = Stack(
        resources=(
            ResourceDesired(id="myfn", kind="lambda"),
            ResourceDesired(id="api", kind="ecs"),
            ResourceDesired(id="/odin/logs", kind="logs"),
        ),
        edges=(
            Edge(src="myfn", dst="/odin/logs", kind="iam", perms=("logs:PutLogEvents",)),
            Edge(src="api", dst="/odin/logs", kind="iam", perms=("logs:PutLogEvents",)),
        ),
    )
    project = generate_tf(stack)
    assert "aws_cloudwatch_log_group" not in project.files["main.tf"]
    (reason,) = project.unsupported
    assert reason.startswith("/odin/logs (logs): drawn as the log sink for more than one workload")
    assert "/aws/lambda/myfn" in reason and "/ecs/api" in reason


def test_a_log_group_whose_label_already_is_the_destination_is_unchanged():
    """The name-coincidence case -- the only one that ever worked -- keeps
    working, and now by construction rather than by luck."""
    stack = _logs_stack(ResourceDesired(id="myfn", kind="lambda"), label="/aws/lambda/myfn")
    assert _group_names(generate_tf(stack)) == ["/aws/lambda/myfn"]


# --- 2. ecr -> ecs: the edge authors the image -------------------------------


def _ecr_stack(workload: ResourceDesired, *, edge_kind: str = "iam", repos: tuple[str, ...] = ("images",)) -> Stack:
    return Stack(
        resources=(workload, *(ResourceDesired(id=repo, kind="ecr") for repo in repos)),
        edges=tuple(
            Edge(src=workload.id, dst=repo, kind=edge_kind, perms=("ecr:BatchGetImage",))
            for repo in repos
        ),
    )


def test_an_ecr_edge_sets_the_services_image_to_the_repositorys_own_address():
    """A TERRAFORM interpolation, not an odin-only field and not a `${{...}}`
    canvas ref: tofu resolves it at apply time, so the taskdef `ecsctl` stores
    carries the REAL `127.0.0.1:{port}/{name}` address -- the port is minted
    per env and cannot be typed in advance. `compute/tasks.py` hands that
    string straight to `docker run`, so the consumer already existed."""
    project = generate_tf(_ecr_stack(ResourceDesired(id="api", kind="ecs")))
    (container,) = _container_definitions(project, "api_taskdef")
    assert container["image"] == "${aws_ecr_repository.images.repository_url}:latest"


def test_a_hand_typed_image_beats_the_edge():
    """`odin canvas set`, the README's JSON schema, `import-tf` and the
    translation agent all write `image` directly. An edge must never silently
    overwrite something a user typed -- `_merge_role_edges`' rule, since an
    image is single-valued and "add to it" is not available."""
    workload = ResourceDesired(id="api", kind="ecs", fields=_fields(image="ghcr.io/me/app:v3"))
    project = generate_tf(_ecr_stack(workload))
    (container,) = _container_definitions(project, "api_taskdef")
    assert container["image"] == "ghcr.io/me/app:v3"


def test_an_image_tag_field_is_honoured_over_latest():
    workload = ResourceDesired(id="api", kind="ecs", fields=_fields(imageTag="v2"))
    project = generate_tf(_ecr_stack(workload))
    (container,) = _container_definitions(project, "api_taskdef")
    assert container["image"] == "${aws_ecr_repository.images.repository_url}:v2"


def test_a_service_with_no_ecr_edge_keeps_the_default_image():
    stack = Stack(resources=(ResourceDesired(id="api", kind="ecs"),))
    (container,) = _container_definitions(generate_tf(stack), "api_taskdef")
    assert container["image"] == hcl._DEFAULT_ECS_IMAGE


def test_two_ecr_edges_on_one_service_are_declined_rather_than_guessed():
    project = generate_tf(_ecr_stack(ResourceDesired(id="api", kind="ecs"), repos=("images", "other")))
    (reason,) = project.unsupported
    assert reason.startswith("api (ecs): drawn to more than one ECR repository")
    assert "'images'" in reason and "'other'" in reason
    assert "aws_ecs_service" not in project.files["main.tf"]


def test_an_ecr_edge_to_a_lambda_is_a_wiring_error_and_never_unsupported():
    """`unsupported` feeds a CI coverage gate and would claim odin cannot build
    this function -- false, it is built and applied. What is missing is a
    MEANING for the edge, which is what `wiring_errors` is for."""
    project = generate_tf(_ecr_stack(ResourceDesired(id="myfn", kind="lambda")))
    assert project.unsupported == []
    (note,) = project.wiring_errors
    assert note.startswith("myfn (lambda): the edge to 'images' (ecr) does NOT set this function's image")
    assert "aws_lambda_function" in project.files["main.tf"]


# --- 3. alb -> ec2: the canvas half of a gateway half that already existed ----


def _alb_stack(target: ResourceDesired, *, edge_kind: str = "network") -> Stack:
    return Stack(
        resources=(
            ResourceDesired(id="net", kind="vpc"),
            ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
            ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net", subnet="web")),
            target,
        ),
        edges=(Edge(src="front", dst=target.id, kind=edge_kind),),
    )


def test_an_alb_edged_to_an_ec2_node_emits_an_attachment_naming_the_instance_id():
    """`elbv2ctl._target_host` resolves an `i-...` target Id through
    `stores.ec2compute` to the VM's real address. That branch was unreachable
    from the canvas until this, because `_ALB_TARGET_KINDS` excluded ec2."""
    stack = _alb_stack(ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")))
    project = generate_tf(stack)
    assert project.unsupported == []
    attrs = resource_attrs(project.files)[("aws_lb_target_group_attachment", "front_server_attach")]
    assert attrs["target_id"] == "${aws_instance.server.id}"
    assert attrs["target_group_arn"] == "${aws_lb_target_group.front_tg.arn}"


def test_an_alb_edged_to_an_ec2_node_that_was_declined_emits_no_dangling_attachment():
    """An attachment referencing an `aws_instance` pass 2 declined is an
    unresolvable reference, which fails `tofu plan` for the WHOLE project and
    stops every other resource on the canvas from applying. The instance's own
    decline already names the cause, so nothing is lost silently."""
    stack = _alb_stack(ResourceDesired(id="server", kind="ec2"))  # no subnet
    project = generate_tf(stack)
    assert project.unsupported == [f"server (ec2): {hcl._NOT_IN_SUBNET}"]
    assert "aws_lb_target_group_attachment" not in project.files["main.tf"]


def test_an_alb_edged_to_a_lambda_is_declined_with_the_shim_reason():
    """DECLINED ON PURPOSE. odin's load-balancer substrate is an nginx
    container whose upstreams are host:port; a lambda target needs HTTP
    translated into the RIE's invoke envelope -- the same shim an
    `apigateway -> lambda` route needs. It is built once, there."""
    project = generate_tf(_alb_stack(ResourceDesired(id="myfn", kind="lambda")))
    (reason,) = project.unsupported
    assert reason.startswith("front (alb): target edge to myfn (lambda) —")
    assert "invoke envelope" in reason and "apigateway" in reason
    assert "aws_lb_target_group_attachment" not in project.files["main.tf"]


def test_an_alb_edged_to_an_s3_bucket_still_reports_the_generic_limit():
    project = generate_tf(_alb_stack(ResourceDesired(id="uploads", kind="s3")))
    assert project.unsupported == [
        "front (alb): target edge to uploads (s3) — only ecs/ec2 nodes can be load-balancer "
        "targets in Simulate v1"
    ]


# --- THE HAZARD RATCHET ------------------------------------------------------


@pytest.mark.parametrize("edge_kind", ["network", "iam", "ref", "unmodelled"])
def test_a_legacy_network_typed_edge_authors_exactly_what_a_typed_one_does(edge_kind: str):
    """NONE of the three passes may gate on `edge.kind`.

    Every canvas saved before the edge-type registry carries `"network"` on
    every edge, and `Edge.kind`'s own default is `"ref"`. A pass that required
    a new kind name would silently stop authoring for those canvases: the log
    group would revert to its label, the service to `nginx:alpine`, the
    attachment would vanish -- and `tofu destroy`/replace them on the next
    Apply. This is the migration test the hazard demands, and it is the
    mutation target for all three passes: add `if edge.kind != "..."` to any
    of them and a row here fails.
    """
    assert _group_names(generate_tf(
        _logs_stack(ResourceDesired(id="myfn", kind="lambda"), edge_kind=edge_kind),
    )) == ["/aws/lambda/myfn"]

    (container,) = _container_definitions(
        generate_tf(_ecr_stack(ResourceDesired(id="api", kind="ecs"), edge_kind=edge_kind)), "api_taskdef",
    )
    assert container["image"] == "${aws_ecr_repository.images.repository_url}:latest"

    attachments = generate_tf(_alb_stack(
        ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")), edge_kind=edge_kind,
    )).files["main.tf"]
    assert 'resource "aws_lb_target_group_attachment" "front_server_attach"' in attachments


@pytest.mark.parametrize("drawn_backwards", [False, True])
def test_direction_is_not_significant_for_any_of_the_three(drawn_backwards: bool):
    """Which end the user started the drag from carries no meaning
    (`spec/translate.py::_merge_sg_edges`' own rule), so both orders author the
    same thing rather than one silently doing nothing."""
    def edge(src: str, dst: str) -> Edge:
        return Edge(src=dst, dst=src, kind="network") if drawn_backwards else Edge(src=src, dst=dst, kind="network")

    logs = Stack(
        resources=(ResourceDesired(id="myfn", kind="lambda"), ResourceDesired(id="/odin/logs", kind="logs")),
        edges=(edge("myfn", "/odin/logs"),),
    )
    assert _group_names(generate_tf(logs)) == ["/aws/lambda/myfn"]

    ecr = Stack(
        resources=(ResourceDesired(id="api", kind="ecs"), ResourceDesired(id="images", kind="ecr")),
        edges=(edge("api", "images"),),
    )
    (container,) = _container_definitions(generate_tf(ecr), "api_taskdef")
    assert container["image"] == "${aws_ecr_repository.images.repository_url}:latest"


@pytest.mark.skipif(shutil.which("tofu") is None, reason="tofu not on PATH")
async def test_all_three_survive_the_real_tofu_validate():
    """THE ONLY CHECK THAT PROVES THE ARGUMENT NAMES ARE REAL, and the reason
    it is here rather than left to review: every claim these passes make about
    Terraform is a claim about a schema this repo does not own --
    that `aws_lb_target_group_attachment` takes `target_group_arn`/`target_id`/
    `port`, that `aws_ecr_repository` really exposes `repository_url`, and that
    an interpolation survives inside the JSON string `container_definitions`
    is. A unit test comparing generated text to expected text would agree with
    itself about all three.

    `validate_refinement` shells out to the same `tofu` binary an apply uses
    and loads the real hashicorp/aws provider schema. MEASURED on OpenTofu
    1.12.3: `REASON: None`, and `tofu fmt -check -diff` exit 0 with no diff.

    NO LAMBDA on this canvas, deliberately: `source_code_hash =
    filebase64sha256("myfn.zip")` fails validate because the archive is
    materialized by the apply path, not by `validate_refinement` (the same
    exclusion `test_granted_workload_hcl_validates.py` records). The ecs node
    carries the log-sink edge instead, which exercises the identical rename.
    """
    stack = Stack(
        resources=(
            ResourceDesired(id="net", kind="vpc"),
            ResourceDesired(id="web", kind="subnet", fields=_fields(vpc="net")),
            ResourceDesired(id="front", kind="alb", fields=_fields(vpc="net", subnet="web")),
            ResourceDesired(id="server", kind="ec2", fields=_fields(subnet="web")),
            ResourceDesired(id="api", kind="ecs"),
            ResourceDesired(id="images", kind="ecr"),
            ResourceDesired(id="/odin/logs", kind="logs", fields=_fields(retentionInDays="14")),
        ),
        edges=(
            Edge(src="front", dst="server", kind="network"),
            Edge(src="api", dst="images", kind="iam", perms=("ecr:BatchGetImage",)),
            Edge(src="api", dst="/odin/logs", kind="iam", perms=("logs:PutLogEvents",)),
        ),
    )
    files = generate_tf(stack).files
    # The three things being validated are really in the file (a validate that
    # passed because the resource was missing would prove nothing).
    assert 'resource "aws_lb_target_group_attachment" "front_server_attach"' in files["main.tf"]
    assert "${aws_ecr_repository.images.repository_url}:latest" in files["main.tf"]
    assert '"/ecs/api"' in files["main.tf"]
    reason, _formatted = await validate_refinement(files, files)
    assert reason is None, reason


def test_neither_pass_records_an_edge_twice_when_both_directions_match():
    """`_kind_pair_edges` tries BOTH orders per edge, so a pair that happens to
    match either way must still be one partner, not two -- otherwise a single
    edge would look like the two-workload ambiguity and decline the node."""
    stack = _logs_stack(ResourceDesired(id="myfn", kind="lambda"))
    partners = hcl._kind_pair_edges(
        stack, {r.id: r for r in stack.resources}, ("logs",), hcl._LOG_SHIPPING_KINDS,
    )
    assert partners == {"/odin/logs": ["myfn"]}
