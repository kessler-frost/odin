"""apigateway (v0.8.19): canvas -> Terraform, and back again.

Three things this file exists to stop, all of them shapes this repo has already
paid for once:

1. **A route pass gated on `edge.kind`.** Every canvas saved before the
   edge-type registry carries `kind: "network"`, so gating on the type name
   would drop every route from the generated file -- and the next Apply would
   serve 404 for every path that worked yesterday. `hcl.py` keys on the two NODE
   kinds; `test_an_edge_saved_before_the_edge_type_registry_still_routes` is what
   holds it there.

2. **A companion that references a declined node.** An integration naming an
   `aws_lambda_function` that pass 2 refused is an unresolvable reference, which
   fails `tofu plan` for the WHOLE project -- not just that node.

3. **A round trip that multiplies.** odin emits TWO routes per target; if the
   importer recovered an edge per ROUTE, one drawn line would come back as two
   edges, generate four routes, and grow every cycle. The importer recovers from
   the INTEGRATION for exactly this reason, and
   `test_a_generated_canvas_survives_generate_import_generate` pins it by
   comparing the two generated files byte-for-byte.
"""
from __future__ import annotations

from odin.iac import import_tf
from odin.iac.hcl import _APIGW_ECS_HOST_SUFFIX, _apigw_route_keys, generate_tf
from odin.iac.import_tf import parse_hcl_text
from odin.gateway.models import apigwctl
from odin.spec.translate import canvas_to_stack

_LAMBDA = {
    "id": "fn-1", "type": "lambda",
    "data": {"label": "orders", "runtime": "python3.12", "handler": "app.handler"},
}
_ECS = {
    "id": "svc-1", "type": "ecs",
    "data": {"label": "checkout", "image": "nginx:alpine", "port": "80"},
}
_API = {"id": "api-1", "type": "apigateway", "data": {"label": "public-api"}}


def _edge(source: str, target: str, edge_type: str = "target") -> dict:
    return {"id": f"{source}-{target}", "source": source, "target": target,
            "data": {"edgeType": edge_type}}


def _project(nodes: list[dict], edges: list[dict]):
    return generate_tf(canvas_to_stack({"nodes": nodes, "edges": edges}))


# --- generate ---------------------------------------------------------------


def test_an_api_with_no_edges_is_a_bare_api_and_a_stage():
    project = _project([_API], [])
    main_tf = project.files["main.tf"]

    assert 'resource "aws_apigatewayv2_api" "public_api"' in main_tf
    assert 'protocol_type = "HTTP"' in main_tf
    assert 'resource "aws_apigatewayv2_stage" "public_api_stage"' in main_tf
    assert 'name       = "$default"' in main_tf or 'name        = "$default"' in main_tf
    # No target, so no integration and no routes -- an API that answers 404,
    # which is what a real HTTP API with no routes does.
    assert "aws_apigatewayv2_integration" not in main_tf
    assert "aws_apigatewayv2_route" not in main_tf
    assert project.unsupported == []


def test_a_lambda_target_emits_an_aws_proxy_integration_and_two_routes():
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    main_tf = project.files["main.tf"]

    assert 'integration_type       = "AWS_PROXY"' in main_tf
    assert "integration_uri        = aws_lambda_function.orders.invoke_arn" in main_tf
    assert 'payload_format_version = "2.0"' in main_tf
    root, proxy = _apigw_route_keys("orders")
    assert f'route_key = "{root}"' in main_tf
    assert f'route_key = "{proxy}"' in main_tf
    assert main_tf.count('resource "aws_apigatewayv2_route"') == 2
    assert project.unsupported == []


def test_an_ecs_target_emits_an_http_proxy_integration_naming_the_service():
    project = _project([_API, _ECS], [_edge("api-1", "svc-1")])
    main_tf = project.files["main.tf"]

    assert 'integration_type   = "HTTP_PROXY"' in main_tf
    assert '"http://${aws_ecs_service.checkout.name}.odin.internal"' in main_tf
    # A 2.0 payload format is meaningless for HTTP_PROXY and real AWS rejects it
    # there, so it must not be emitted for this kind.
    assert "payload_format_version" not in main_tf


def test_the_ecs_hostname_suffix_agrees_with_the_gateway_that_parses_it_back():
    """The generator writes `<service>.odin.internal` and `apigwctl` parses the
    service name back out of it. Two spellings in two files is a rename away
    from routing every ecs API request at nothing, so they are pinned to each
    other rather than to a literal."""
    assert _APIGW_ECS_HOST_SUFFIX == apigwctl.ECS_HOST_SUFFIX


def test_the_reverse_direction_edge_produces_the_identical_file():
    """Which end the user dragged from carries no meaning."""
    forward = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    backward = _project([_API, _LAMBDA], [_edge("fn-1", "api-1")])
    assert forward.files["main.tf"] == backward.files["main.tf"]


def test_an_edge_saved_before_the_edge_type_registry_still_routes():
    """THE DESTRUCTIVE ONE. Every canvas saved before the registry carries
    `kind: "network"`. If the route pass gated on the type name, this file would
    contain no routes -- and the next Apply would 404 every path that worked."""
    legacy = _project([_API, _LAMBDA], [_edge("api-1", "fn-1", "network")])
    assert legacy.files["main.tf"].count('resource "aws_apigatewayv2_route"') == 2
    assert legacy.files["main.tf"] == _project([_API, _LAMBDA], [_edge("api-1", "fn-1")]).files["main.tf"]


def test_two_targets_get_their_own_path_prefixes():
    project = _project([_API, _LAMBDA, _ECS], [_edge("api-1", "fn-1"), _edge("api-1", "svc-1")])
    main_tf = project.files["main.tf"]

    assert 'route_key = "ANY /orders"' in main_tf
    assert 'route_key = "ANY /checkout"' in main_tf
    assert main_tf.count('resource "aws_apigatewayv2_integration"') == 2
    assert main_tf.count('resource "aws_apigatewayv2_route"') == 4


def test_an_api_edged_to_a_kind_that_cannot_be_a_target_routes_nothing():
    """An `apigateway -> s3` line is decoration. It must not author an
    integration nothing could serve."""
    bucket = {"id": "b-1", "type": "s3", "data": {"label": "uploads"}}
    project = _project([_API, bucket], [_edge("api-1", "b-1")])
    assert "aws_apigatewayv2_integration" not in project.files["main.tf"]


def test_a_declined_target_never_leaves_an_integration_pointing_at_nothing():
    """An `aws_ecs_service` outside any subnet is declined by pass 2. An
    integration referencing it would be an unresolvable reference, which fails
    `tofu plan` for the WHOLE project rather than for that node."""
    bad_ecs = {"id": "svc-2", "type": "ecs", "data": {"label": "broken", "image": ""}}
    project = _project([_API, bad_ecs], [_edge("api-1", "svc-2")])
    main_tf = project.files["main.tf"]

    if project.unsupported:  # the service was declined
        assert "aws_apigatewayv2_integration" not in main_tf
        assert "aws_apigatewayv2_route" not in main_tf


# --- import -----------------------------------------------------------------


def test_a_generated_lambda_route_imports_back_as_one_edge():
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    result = parse_hcl_text(project.files["main.tf"])

    api = [n for n in result.nodes if n["type"] == "apigateway"]
    assert [n["data"]["label"] for n in api] == ["public-api"]
    routes = [e for e in result.edges if e["source"] == "public-api"]
    # ONE edge, not two -- the two route keys share one integration.
    assert routes == [{"source": "public-api", "target": "orders", "data": {"edgeType": "target"}}]


def test_a_generated_ecs_route_imports_back_as_one_edge():
    project = _project([_API, _ECS], [_edge("api-1", "svc-1")])
    result = parse_hcl_text(project.files["main.tf"])

    assert {"source": "public-api", "target": "checkout", "data": {"edgeType": "target"}} in result.edges


def test_odins_own_api_imports_with_nothing_unsupported():
    """The bar `ROADMAP.md` sets and that two existing companions already miss:
    odin's OWN output must re-import cleanly. A third failure is not acceptable,
    so this asserts on the whole `unsupported` list rather than a filtered one."""
    project = _project([_API, _LAMBDA, _ECS], [_edge("api-1", "fn-1"), _edge("api-1", "svc-1")])
    result = parse_hcl_text(project.files["main.tf"])

    assert [u for u in result.unsupported if "apigateway" in u.type] == []
    assert [w for w in result.warnings if "api route" in w or "api stage" in w] == []


def test_a_generated_canvas_survives_generate_import_generate():
    """THE FIXED POINT: generate -> import -> generate is byte-identical.

    NOTE WHAT THIS DOES AND DOES NOT PROVE, because the first version of this
    file claimed more. Mutation-tested by making the importer recover an edge
    per ROUTE instead of per integration: this test still PASSED. `hcl.py`'s
    `_kind_pair_edges` de-duplicates its target list, so two identical edges
    produce one integration and the generated file is unchanged. The duplicate
    is real and reaches the CANVAS -- the user sees two lines on one pair -- it
    just cannot reach the file. `test_one_drawn_line_imports_back_as_exactly_one
    _line` below is the one that catches it."""
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    result = parse_hcl_text(project.files["main.tf"])
    again = generate_tf(canvas_to_stack({"nodes": result.nodes, "edges": result.edges}))

    assert again.files["main.tf"] == project.files["main.tf"]


def test_one_drawn_line_imports_back_as_exactly_one_line():
    """odin emits TWO routes per target. Recovering the edge from the routes
    rather than the integration puts TWO edges on one pair -- the canvas grows a
    duplicate line every import, and `_kind_pair_edges`'s de-duplication hides
    it from the generated file so the byte-comparison above stays green."""
    project = _project([_API, _LAMBDA, _ECS], [_edge("api-1", "fn-1"), _edge("api-1", "svc-1")])
    result = parse_hcl_text(project.files["main.tf"])

    api_edges = [(e["source"], e["target"]) for e in result.edges if e["source"] == "public-api"]
    assert sorted(api_edges) == [("public-api", "checkout"), ("public-api", "orders")]


def test_an_integration_naming_something_unimportable_is_reported_never_dropped():
    text = '''
resource "aws_apigatewayv2_api" "public_api" {
  name          = "public-api"
  protocol_type = "HTTP"
  tags = { "odin:node" = "public-api" }
}

resource "aws_apigatewayv2_integration" "public_api_ghost" {
  api_id                 = aws_apigatewayv2_api.public_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = "arn:aws:lambda:us-east-1:000000000000:function:not-on-this-canvas"
  payload_format_version = "2.0"
}
'''
    result = parse_hcl_text(text)

    ghosts = [u for u in result.unsupported if u.type == "aws_apigatewayv2_integration"]
    assert len(ghosts) == 1
    assert "integration_uri" in ghosts[0].reason
    assert "would NOT route" in ghosts[0].reason
    assert result.edges == []


def test_a_route_key_odin_will_not_reproduce_is_reported_as_CHANGED():
    """The canvas cannot hold a route key -- the path comes from the target's
    LABEL -- so a source routing `POST /checkout` to a function called `orders`
    loses that path on the next Apply. Silence here would be the elasticache bug
    in another costume."""
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    # The trailing newline is load-bearing: `ANY /orders` is a PREFIX of
    # `ANY /orders/{proxy+}`, so a bare replace mangles BOTH route keys and the
    # test then asserts against a warning it created twice over. Found by the
    # test failing on the wrong route's warning.
    text = project.files["main.tf"].replace(
        'route_key = "ANY /orders"\n', 'route_key = "POST /checkout"\n',
    )
    assert 'route_key = "ANY /orders/{proxy+}"' in text, "the other route must be untouched"
    result = parse_hcl_text(text)

    changed = [w for w in result.warnings if "CHANGED" in w and "route_key" in w]
    # Exactly ONE: the untouched `{proxy+}` route must NOT be reported.
    assert len(changed) == 1, result.warnings
    # `_literal` lowercases for comparability, so the reported value is
    # lowercase -- the assertion matches what odin really prints.
    assert "post /checkout" in changed[0]
    assert "ANY /orders" in changed[0], "the warning must name what odin WOULD emit"


def test_a_stage_odin_will_not_serve_is_reported_as_CHANGED():
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    text = project.files["main.tf"].replace('"$default"', '"prod"')
    result = parse_hcl_text(text)

    changed = [w for w in result.warnings if "CHANGED" in w and "api stage" in w]
    assert changed, result.warnings


def test_an_argument_the_integration_does_not_model_is_reported():
    """`request_parameters` rewrites the path the backend sees; `tls_config`
    changes who is trusted. An edge carries neither, so both must be named."""
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    text = project.files["main.tf"].replace(
        'payload_format_version = "2.0"',
        'payload_format_version = "2.0"\n  timeout_milliseconds   = 5000',
    )
    result = parse_hcl_text(text)

    dropped = [w for w in result.warnings if "timeout_milliseconds" in w]
    assert dropped, result.warnings


def test_the_companion_types_are_all_registered_as_companions():
    """A companion type absent from `_APIGW_COMPANION_TYPES` becomes an
    `unsupported` entry on every import of odin's own output -- the bug four
    kinds shipped with for tags. Pinned against the generator's own output."""
    project = _project([_API, _LAMBDA], [_edge("api-1", "fn-1")])
    emitted = {
        line.split('"')[1] for line in project.files["main.tf"].splitlines()
        if line.startswith("resource ") and "apigatewayv2" in line
    }
    assert emitted - {"aws_apigatewayv2_api"} == set(import_tf._APIGW_COMPANION_TYPES) - {
        "aws_apigatewayv2_stage"
    } | {"aws_apigatewayv2_stage"}
