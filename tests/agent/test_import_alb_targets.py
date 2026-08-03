"""Terraform -> canvas for the two companions odin's OWN output could not read.

Measured on develop before this file existed, on a canvas with an `alb -> ec2`
target and a granted `ec2` -- exactly what a user draws to put an instance behind
a load balancer and let it reach a bucket:

    import unsupported:
      aws_iam_instance_profile     box_profile        -- not supported by odin's
                                                        import (yet)
      aws_lb_target_group_attachment front_box_attach -- not supported by odin's
                                                        import (yet)

So odin generated a project it could not read back. `test_import_coverage_is_
honest.py::test_odins_own_project_now_round_trips_with_nothing_unsupported` did
not catch it because its canvas contained neither shape -- the guard was real and
its FIXTURE was narrower than the generator, which is why that canvas is widened
in the same change as this file lands.

## The two companions take the two DIFFERENT treatments, on purpose

`import_tf` has exactly two things it can do with a resource that is not a node:
fold it onto a node, or turn it into an edge. Which one is not a taste question.

* **`aws_lb_target_group_attachment` becomes an EDGE** (the `aws_volume_
  attachment` precedent). It is a RELATIONSHIP between two canvas nodes and holds
  nothing the canvas has a field for -- its port is the target group's, which the
  alb node already carries. Drop it and the regenerated project registers nothing,
  so the next apply DEREGISTERS a live instance from the load balancer.
* **`aws_iam_instance_profile` FOLDS AWAY** (the `aws_ecs_cluster` precedent). It
  is odin's own machinery: `name` is `<ec2 label>-profile`, `role` is that
  instance's auto-role, and the thing it exists for -- the grant -- comes back as
  an `iam` edge from the `aws_iam_role_policy`. As a node it would invent a kind
  the canvas has not got; as an edge it would draw a second line for a permission
  already drawn.

## What a byte-identical round trip can and cannot prove here

It proves the ATTACHMENT and it cannot prove the PROFILE, and that difference is
worth stating because it looks like the same assertion. The generator re-derives
the profile from the grant edge, so `again == main_tf` held even in the broken
state, with the profile sitting in `unsupported`. The profile's proof is
therefore `unsupported == []` plus `warnings == []` plus the falsification tests
below -- a round trip cannot see a resource its own generator puts back.
"""
from __future__ import annotations

from odin.iac.hcl import generate_tf
from odin.iac.import_tf import parse_hcl_text
from odin.spec.translate import canvas_to_stack

_NETWORK = (
    'resource "aws_vpc" "net" {\n  cidr_block = "10.0.0.0/16"\n\n'
    '  tags = {\n    "odin:node" = "net"\n  }\n}\n'
    "\n"
    'resource "aws_subnet" "web" {\n  vpc_id     = aws_vpc.net.id\n'
    '  cidr_block = "10.0.1.0/24"\n\n'
    '  tags = {\n    "odin:node" = "web"\n  }\n}\n'
)
_ALB = (
    'resource "aws_lb" "front" {\n'
    '  name               = "front"\n'
    "  internal           = true\n"
    '  load_balancer_type = "application"\n'
    "\n"
    "  subnets = [aws_subnet.web.id]\n"
    "\n"
    '  tags = {\n    "odin:node" = "front"\n  }\n}\n'
    "\n"
    'resource "aws_lb_listener" "front_listener" {\n'
    "  load_balancer_arn = aws_lb.front.arn\n"
    "  port              = 8080\n"
    '  protocol          = "HTTP"\n'
    "\n"
    "  default_action {\n"
    '    type             = "forward"\n'
    "    target_group_arn = aws_lb_target_group.front_tg.arn\n"
    "  }\n}\n"
    "\n"
    'resource "aws_lb_target_group" "front_tg" {\n'
    '  name        = "front-tg"\n'
    "  port        = 9000\n"
    '  protocol    = "HTTP"\n'
    "  vpc_id      = aws_vpc.net.id\n"
    '  target_type = "instance"\n'
    "\n"
    "  health_check {\n"
    '    path = "/healthz"\n'
    "  }\n}\n"
)
_INSTANCE = (
    'resource "aws_instance" "box" {\n'
    '  ami           = "ami-00abcdef0123456789"\n'
    '  instance_type = "t3.large"\n'
    "  subnet_id     = aws_subnet.web.id\n"
    "\n"
    '  tags = {\n    "odin:node" = "box"\n  }\n}\n'
)
# The instance as odin writes it for a GRANTED node: the profile reference plus
# the ordering dependency on the policy that authorizes it.
_GRANTED_INSTANCE = (
    'resource "aws_instance" "box" {\n'
    '  ami           = "ami-00abcdef0123456789"\n'
    '  instance_type = "t3.large"\n'
    "  subnet_id     = aws_subnet.web.id\n"
    "\n"
    "  iam_instance_profile = aws_iam_instance_profile.box_profile.name\n"
    "\n"
    "  depends_on = [aws_iam_role_policy.box_grants]\n"
    "\n"
    '  tags = {\n    "odin:node" = "box"\n  }\n}\n'
)
_ROLE = (
    'resource "aws_iam_role" "box_role" {\n'
    '  name = "box-role"\n'
    "\n"
    "  assume_role_policy = jsonencode({\n"
    '    Version = "2012-10-17"\n'
    "  })\n}\n"
)
_POLICY = (
    'resource "aws_iam_role_policy" "box_grants" {\n'
    '  name   = "box-grants"\n'
    "  role   = aws_iam_role.box_role.name\n"
    '  policy = "{\\"Version\\": \\"2012-10-17\\", \\"Statement\\": [{\\"Effect\\": \\"Allow\\", '
    '\\"Action\\": [\\"s3:PutObject\\"], \\"Resource\\": [\\"arn:aws:s3:::uploads\\"]}]}"\n}\n'
)
_BUCKET = (
    'resource "aws_s3_bucket" "uploads" {\n'
    '  bucket        = "uploads"\n'
    "  force_destroy = true\n"
    "\n"
    '  tags = {\n    "odin:node" = "uploads"\n  }\n}\n'
)


def _attachment(name: str = "front_box_attach", **extra: str) -> str:
    args = {
        "target_group_arn": "aws_lb_target_group.front_tg.arn",
        "target_id": "aws_instance.box.id",
        "port": "9000",
        **extra,
    }
    body = "".join(f"  {key} = {value}\n" for key, value in args.items())
    return f'resource "aws_lb_target_group_attachment" "{name}" {{\n{body}}}\n'


# `rname` is the HCL RESOURCE name and `name` is the profile's own argument --
# two different strings that both want to be called "name". Spelled apart here
# because collapsing them once already produced a fixture whose resource was
# literally called `""legacy-profile""` and a test that passed for the wrong
# reason.
def _profile(rname: str = "box_profile", **extra: str) -> str:
    args = {"name": '"box-profile"', "role": "aws_iam_role.box_role.name", **extra}
    body = "".join(f"  {key} = {value}\n" for key, value in args.items())
    return f'resource "aws_iam_instance_profile" "{rname}" {{\n{body}}}\n'


def _service(load_balancer: str = "aws_lb_target_group.front_tg.arn") -> str:
    block = "" if not load_balancer else (
        "\n  load_balancer {\n"
        f"    target_group_arn = {load_balancer}\n"
        '    container_name   = "svc"\n'
        "    container_port   = 9000\n"
        "  }\n"
    )
    return (
        'resource "aws_ecs_service" "svc" {\n'
        '  name            = "svc"\n'
        "  cluster         = aws_ecs_cluster.odin.id\n"
        "  task_definition = aws_ecs_task_definition.svc_taskdef.arn\n"
        "  desired_count   = 2\n"
        f"{block}"
        "\n"
        '  tags = {\n    "odin:node" = "svc"\n  }\n}\n'
        "\n"
        'resource "aws_ecs_cluster" "odin" {\n  name = "odin"\n}\n'
        "\n"
        'resource "aws_ecs_task_definition" "svc_taskdef" {\n'
        '  family                = "svc"\n'
        # The portMappings are load-bearing, not decoration: the node's `port`
        # comes from here, and it is what the `load_balancer` block's
        # `container_port` is checked against. Without them the node has no port,
        # the comparison is correctly skipped, and the CHANGED test can never fire.
        '  container_definitions = "[{\\"name\\": \\"svc\\", \\"image\\": \\"nginx:1.27-alpine\\", '
        '\\"portMappings\\": [{\\"containerPort\\": 9000}]}]"\n'
        "}\n"
    )


def _project(*blocks: str) -> str:
    return "\n".join(blocks)


def _changed(result) -> list[str]:
    return [w for w in result.warnings if "CHANGED" in w]


def _lost(result) -> list[str]:
    return [w for w in result.warnings if "imported without" in w]


# --------------------------------------------------------------------------
# aws_lb_target_group_attachment -> an ALB TARGET edge
# --------------------------------------------------------------------------

def test_an_attachment_becomes_an_alb_target_edge_between_the_two_nodes():
    result = parse_hcl_text(_project(_NETWORK, _ALB, _INSTANCE, _attachment()))
    assert result.unsupported == [], [(u.type, u.reason) for u in result.unsupported]
    (edge,) = result.edges
    assert edge == {"source": "front", "target": "box", "data": {"edgeType": "target"}}
    # ...and it stays an edge: the attachment never becomes a node of its own,
    # and neither do the load balancer's other two companions.
    assert {n["type"] for n in result.nodes} == {"vpc", "subnet", "alb", "ec2"}


def test_odins_own_attachment_imports_without_a_single_warning():
    """Warning noise is not harmless in a module whose whole value is that its
    warnings are worth reading -- and odin generates this exact file."""
    assert parse_hcl_text(_project(_NETWORK, _ALB, _INSTANCE, _attachment())).warnings == []


def test_an_attachment_naming_something_unimportable_is_reported_never_dropped():
    """A silently dropped attachment is a live instance the next apply takes out
    of the load balancer's rotation."""
    result = parse_hcl_text(_project(_NETWORK, _ALB, _attachment()))  # no aws_instance
    assert result.edges == []
    (entry,) = result.unsupported
    assert entry.type == "aws_lb_target_group_attachment" and entry.name == "front_box_attach"
    assert "target_id" in entry.reason
    assert "would NOT register this instance" in entry.reason


def test_an_attachment_on_a_target_group_no_listener_claims_is_reported():
    """The target group is what ties an attachment to a load balancer, and only a
    LISTENER says which load balancer a group belongs to. Without one there is no
    alb end for the edge, so the registration is reported rather than guessed at
    -- the same rule the unclaimed target group itself is held to."""
    no_listener = _ALB.replace("aws_lb_listener", "aws_lb_listener_DISABLED")
    result = parse_hcl_text(_project(_NETWORK, no_listener, _INSTANCE, _attachment()))
    reported = {u.type for u in result.unsupported}
    assert "aws_lb_target_group_attachment" in reported
    (entry,) = [u for u in result.unsupported if u.type == "aws_lb_target_group_attachment"]
    assert "target_group_arn" in entry.reason
    assert result.edges == []


def test_a_registration_port_odin_will_not_reproduce_is_reported_as_CHANGED():
    """odin registers every target on the TARGET GROUP's own port, so a source
    that registered this instance on 9999 comes back dialled at 9000 -- a
    reachability change, not a detail."""
    tf = _project(_NETWORK, _ALB, _INSTANCE, _attachment(port="9999"))
    (changed,) = _changed(parse_hcl_text(tf))
    assert changed.startswith("front -> box (alb target): imported with CHANGED")
    assert "port=9999 (odin always emits 9000)" in changed


def test_an_argument_the_attachment_does_not_model_is_reported():
    """`availability_zone` is the only other argument the type takes and odin
    emits none, so it has to be named rather than implied by a clean import."""
    tf = _project(_NETWORK, _ALB, _INSTANCE, _attachment(availability_zone='"all"'))
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("front -> box (alb target): imported without unmodeled")
    assert "availability_zone" in lost


def test_two_attachments_for_one_pair_are_one_drawn_line():
    """Two edges between the same two nodes is a canvas the UI cannot draw and a
    round trip that is not stable, so the pair is de-duplicated."""
    result = parse_hcl_text(_project(
        _NETWORK, _ALB, _INSTANCE, _attachment(), _attachment("front_box_again"),
    ))
    assert result.edges == [
        {"source": "front", "target": "box", "data": {"edgeType": "target"}},
    ]


# --------------------------------------------------------------------------
# The SIBLING: an ecs service's `load_balancer {}` block, which was worse
# --------------------------------------------------------------------------

def test_an_ecs_service_behind_a_load_balancer_comes_back_as_the_same_edge():
    """The sibling defect, found while fixing the reported one and worse than it:
    `alb -> ec2` at least came back `unsupported`, while `alb -> ecs` imported
    GREEN with the wiring gone -- `unsupported == []`, no warning, no edge, and a
    regenerated `main.tf` missing the whole `load_balancer` block.

    It hid because `load_balancer` is listed in `_CARRIED_ATTRS["ecs"]`, which
    suppressed the one warning that would have named it. A carried-set entry is a
    PROMISE that a round trip reproduces the argument; this is what keeps it."""
    result = parse_hcl_text(_project(_NETWORK, _ALB, _service()))
    assert result.unsupported == [], [(u.type, u.reason) for u in result.unsupported]
    assert result.edges == [
        {"source": "front", "target": "svc", "data": {"edgeType": "target"}},
    ]


def test_an_ecs_load_balancer_block_naming_no_imported_alb_is_reported():
    """The dropped-edge rule, on the consumer-side shape: silence would take the
    service out of the load balancer's rotation on the next apply."""
    result = parse_hcl_text(_project(
        _NETWORK, _service("aws_lb_target_group.somewhere_else.arn"),
    ))
    assert result.edges == []
    (warning,) = [w for w in result.warnings if "load_balancer" in w]
    assert warning.startswith("svc (ecs): a `load_balancer` block names a target group")
    assert "would NOT put this service behind a load balancer" in warning


def test_a_container_port_odin_will_not_reproduce_is_reported_as_CHANGED():
    """The block's `container_port` is re-derived from the TASK DEFINITION's own
    port, so a service registered on a different port comes back registered
    somewhere else. Compared against a second producer -- the task definition --
    rather than against the block itself, which could never fail."""
    tf = _project(_NETWORK, _ALB, _service().replace("container_port   = 9000",
                                                     "container_port   = 7777"))
    (changed,) = _changed(parse_hcl_text(tf))
    assert changed.startswith("front -> svc (alb target): imported with CHANGED load_balancer ")
    assert "container_port=7777" in changed


def test_an_argument_the_load_balancer_block_does_not_model_is_reported():
    """`elb_name` is the classic-ELB spelling the same block accepts, and odin
    emits nothing for it, so it has to be named rather than implied."""
    tf = _project(_NETWORK, _ALB, _service().replace(
        "    container_name   = \"svc\"\n", "    container_name   = \"svc\"\n    elb_name = \"old\"\n"))
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("front -> svc (alb target): imported without unmodeled load_balancer ")
    assert "elb_name" in lost


def test_one_load_balancer_can_front_both_kinds_at_once():
    """The two target kinds need OPPOSITE machinery in the generator (an ECS
    service registers its own tasks; an EC2 instance is registered by tofu), and
    they must still come back as two lines from the same node."""
    result = parse_hcl_text(_project(_NETWORK, _ALB, _INSTANCE, _attachment(), _service()))
    assert result.unsupported == [] and result.warnings == []
    assert {(e["source"], e["target"]) for e in result.edges} == {("front", "box"), ("front", "svc")}


# --------------------------------------------------------------------------
# aws_iam_instance_profile -> folds away
# --------------------------------------------------------------------------

def test_a_granted_instances_profile_is_neither_a_node_nor_unsupported():
    """The reported defect, at its narrowest. Before this, odin's own output came
    back with `aws_iam_instance_profile -- not supported by odin's import (yet)`
    and the instance carried two phantom dropped arguments."""
    result = parse_hcl_text(_project(
        _NETWORK, _GRANTED_INSTANCE, _profile(), _ROLE, _POLICY, _BUCKET,
    ))
    assert result.unsupported == [], [(u.type, u.reason) for u in result.unsupported]
    assert result.warnings == [], result.warnings
    assert {n["type"] for n in result.nodes} == {"vpc", "subnet", "ec2", "s3"}
    # The grant it exists for comes back as the edge, once.
    assert result.edges == [{
        "source": "box", "target": "uploads",
        "data": {"edgeType": "iam", "permissions": ["s3:PutObject"]},
    }]


def test_a_profile_odin_would_name_differently_is_reported_as_CHANGED():
    """odin names it `<ec2 label>-profile`, so a source profile called anything
    else comes back as a differently-named AWS resource."""
    tf = _project(_NETWORK, _GRANTED_INSTANCE, _profile(name='"legacy-profile"'),
                  _ROLE, _POLICY, _BUCKET)
    (changed,) = _changed(parse_hcl_text(tf))
    assert changed.startswith("box (ec2): imported with CHANGED aws_iam_instance_profile ")
    assert "name=legacy-profile (odin always emits box-profile)" in changed


def test_an_argument_the_profile_does_not_model_is_reported():
    """`path` is an IAM namespace odin has no field for, and a profile that folds
    away in silence takes it with it."""
    tf = _project(_NETWORK, _GRANTED_INSTANCE, _profile(path='"/team/"'),
                  _ROLE, _POLICY, _BUCKET)
    (lost,) = _lost(parse_hcl_text(tf))
    assert lost.startswith("box (ec2): imported without unmodeled aws_iam_instance_profile ")
    assert "path" in lost


def test_a_profile_no_instance_references_is_reported_never_dropped():
    """The unclaimed-target-group rule: it folds onto nothing, so a regenerated
    project would not contain it. odin emits one only for an instance it
    attaches, so its own output never lands here."""
    result = parse_hcl_text(_project(_NETWORK, _INSTANCE, _profile("orphan"), _ROLE))
    (entry,) = result.unsupported
    assert entry.type == "aws_iam_instance_profile" and entry.name == "orphan"
    assert "referenced by no imported aws_instance" in entry.reason


def test_an_instance_naming_a_profile_the_project_does_not_contain_is_reported():
    """The instance comes back with no role at all, which is a credential loss
    and not a missing detail."""
    result = parse_hcl_text(_project(_NETWORK, _GRANTED_INSTANCE, _ROLE, _POLICY, _BUCKET))
    (warning,) = [w for w in result.warnings if "iam_instance_profile" in w]
    assert warning.startswith("box (ec2): its `iam_instance_profile` names no imported")
    assert "NO role" in warning


def test_a_profile_whose_grant_could_not_be_imported_says_the_permissions_are_lost():
    """odin emits a profile for a granted instance AND NO OTHER, so if no `iam`
    edge survived the import there is no profile and no role in the regenerated
    project. Asked of the recovered EDGES rather than of the role's name: the
    question is not "does a role exist" but "did the grant survive"."""
    # The policy is there, but it grants a bucket this project does not contain,
    # so no edge can be built for it.
    result = parse_hcl_text(_project(_NETWORK, _GRANTED_INSTANCE, _profile(), _ROLE, _POLICY))
    (warning,) = [w for w in result.warnings if "no grant could be imported" in w]
    assert warning.startswith("box (ec2): odin emits an instance profile only for an instance")
    assert "loses the permissions the source gave it" in warning


# --------------------------------------------------------------------------
# The end-to-end claim
# --------------------------------------------------------------------------

def test_the_generate_import_generate_loop_is_byte_identical_over_both_companions():
    """The bar for this fix: odin's OWN project, containing an `alb -> ec2`
    target AND a granted `ec2`, regenerates byte-for-byte from what import gives
    back.

    ## EVERY VALUE HERE THAT HAS A DEFAULT IS DELIBERATELY NOT THE DEFAULT --
    ## do not "simplify" them back

    A round trip over defaults proves the DEFAULTS agree, not that the data
    survives: with `listenerPort`/`port`/`healthCheckPath` left alone, this file
    regenerates byte-identically even with all three dropped on import, because
    `hcl.py` refills 80/80/"/". So:

      listenerPort   8080     (`_DEFAULT_ALB_LISTENER_PORT` is "80")
      port           9000     (`_DEFAULT_ALB_TARGET_PORT` is "80")
      healthCheckPath /healthz (`_DEFAULT_ALB_HEALTH_CHECK_PATH` is "/")
      ami            ami-00abcdef0123456789 (`_DEFAULT_AMI` is ami-0c101f26f147fa7fd)
      instanceType   t3.large (`_DEFAULT_INSTANCE_TYPE` is "t3.micro")

    The listener port and the target port are also different from EACH OTHER,
    which is what makes an import that confuses the two visible: the attachment's
    port is the target group's (9000), never the listener's (8080).

    ## What this assertion CANNOT prove, and why the test above exists

    The instance profile. `hcl.py` re-derives it from the grant edge, so this
    equality held in the BROKEN state too -- measured, with the profile sitting
    in `unsupported`. A generator that puts a resource back cannot be used to
    check that the importer read it. `test_a_granted_instances_profile_is_neither_
    a_node_nor_unsupported` is the one that can fail for the profile.
    """
    canvas = {
        "nodes": [
            {"id": "v", "type": "vpc", "data": {"label": "net", "cidr": "10.0.0.0/16"}},
            {"id": "s", "type": "subnet",
             "data": {"label": "web", "cidr": "10.0.1.0/24", "vpc": "net"}},
            {"id": "l", "type": "alb", "data": {
                "label": "front", "vpc": "net", "subnet": "web",
                "listenerPort": "8080", "port": "9000", "healthCheckPath": "/healthz"}},
            {"id": "i", "type": "ec2", "data": {
                "label": "box", "vpc": "net", "subnet": "web",
                "ami": "ami-00abcdef0123456789", "instanceType": "t3.large"}},
            {"id": "c", "type": "ecs", "data": {
                "label": "web-svc", "image": "nginx:1.27-alpine", "count": "2", "port": "9000"}},
            {"id": "b", "type": "s3", "data": {"label": "uploads"}},
        ],
        "edges": [
            {"id": "t1", "source": "l", "target": "i", "data": {"edgeType": "target"}},
            {"id": "t2", "source": "l", "target": "c", "data": {"edgeType": "target"}},
            {"id": "g1", "source": "i", "target": "b",
             "data": {"edgeType": "iam", "permissions": ["s3:PutObject"]}},
        ],
    }
    main_tf = generate_tf(canvas_to_stack(canvas)).files["main.tf"]
    # Both companions really are in the file the round trip is measured over --
    # without this the assertion below could pass vacuously on a project that
    # contains neither, which is exactly how the original guard missed them.
    assert 'resource "aws_lb_target_group_attachment"' in main_tf
    assert 'resource "aws_iam_instance_profile"' in main_tf
    assert "load_balancer {" in main_tf

    imported = parse_hcl_text(main_tf)
    assert imported.unsupported == [], [(u.type, u.reason) for u in imported.unsupported]
    assert imported.warnings == [], imported.warnings

    again = generate_tf(canvas_to_stack({"nodes": imported.nodes, "edges": imported.edges}))
    assert again.files["main.tf"] == main_tf, "generate -> import -> generate must be stable"
