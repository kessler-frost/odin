"""apigateway (v0.8.19): the control plane and the invoke shim, without Docker.

The harness is `test_elbv2ctl.py`'s -- a `FakeProxy` with the `ApiProxy` shape,
so every converge is real code against a recorded call rather than a container.
What that CANNOT prove is stated rather than implied: nothing here shows nginx
accepts the rendered config or that a real RIE answers the envelope. Those live
in `tests/simulate/test_apigateway_e2e.py`, which is `-m integration`.

The wire shapes asserted here were RECORDED from real terraform-provider-aws
5.100.0, not read off a doc page -- see `gateway/models/apigwctl.py`'s docstring
for the trace and for the two things it got wrong first (PascalCase members, and
a `$default` that arrives percent-encoded).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from odin.compute.apigw import ApiRoute
from odin.gateway import apigw_shim
from odin.gateway.classify import classify
from odin.gateway.models import apigwctl
from odin.gateway.stores import SynthStores

ENV = "apigw-unit"
GATEWAY_PORT = 4266


class FakeProxy:
    """The `ApiProxy` shape, no Docker. Records every route table it is given so
    a test can assert on what WOULD reach nginx."""

    def __init__(self, port: int = 31000) -> None:
        self.converges: list[tuple[str, tuple[ApiRoute, ...]]] = []
        self.destroyed: list[str] = []
        self._port = port
        self.fail_with: Exception | None = None

    async def ensure(self, root: Path, env: str, api_name: str, routes: tuple[ApiRoute, ...]) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        self.converges.append((api_name, routes))
        return self._port

    async def status(self, env: str, api_name: str) -> str:
        return "running"

    async def destroy(self, env: str, api_name: str) -> None:
        self.destroyed.append(api_name)


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


@pytest.fixture
def proxy() -> FakeProxy:
    return FakeProxy()


async def _call(stores, proxy, method: str, path: str, body: dict | None = None):
    """One request, THROUGH `classify` -- never straight into a handler.

    Going through classify is what makes these tests cover the route table too:
    a path the classifier does not recognize returns None here and the assert
    fires, rather than the handler being called directly and the gap only
    showing up in a real apply."""
    classified = classify("apigateway", method, path, {}, {}, json.dumps(body or {}).encode())
    assert classified is not None, f"{method} {path} must be classifiable"
    action, resource = classified
    return await apigwctl.pure_answer(
        action, resource, ENV, json.dumps(body).encode() if body else b"",
        stores, time.time(), proxy=proxy, gateway_port=GATEWAY_PORT, path=path,
    )


async def _create_api(stores, proxy, name: str = "public-api") -> dict:
    response = await _call(stores, proxy, "POST", "/v2/apis", {"name": name, "protocolType": "HTTP"})
    assert response.status_code == 201, response.body
    return json.loads(response.body)


# --- the API ----------------------------------------------------------------


async def test_create_api_answers_with_a_real_reachable_endpoint(stores, proxy):
    """`apiEndpoint` is the ONE field a caller can actually dial, so it must be
    odin's real published port and not a plausible amazonaws.com string."""
    api = await _create_api(stores, proxy)

    assert api["apiId"].startswith("api")
    assert api["apiEndpoint"] == f"http://127.0.0.1:{proxy._port}"
    assert api["protocolType"] == "HTTP"
    assert api["name"] == "public-api"


async def test_the_container_is_converged_before_create_answers(stores, proxy):
    """SYNCHRONOUS, unlike elbv2ctl's background converge. apigatewayv2 has no
    terraform waiter, so the provider stores whatever `GetApi` says immediately
    -- a placeholder endpoint would drift forever."""
    await _create_api(stores, proxy)
    assert [name for name, _routes in proxy.converges] == ["public-api"]


async def test_a_converge_that_fails_reports_the_real_reason_and_no_endpoint(stores, proxy):
    """Honesty rule 2: name what is still standing. A create whose nginx did not
    come up must not answer 201 with an endpoint nothing listens on."""
    proxy.fail_with = RuntimeError("nginx: [emerg] invalid parameter")
    response = await _call(stores, proxy, "POST", "/v2/apis", {"name": "doomed", "protocolType": "HTTP"})

    assert response.status_code == 500
    assert "invalid parameter" in response.body.decode()
    # The record survives so DeleteApi still works and the reason is readable.
    record = apigwctl.apis(stores, ENV)[0]
    assert record["state"] == "FAILED"
    assert apigwctl.endpoint_url(record) is None


async def test_a_websocket_api_is_refused_by_name(stores, proxy):
    """nginx answers a websocket handshake with a 200 and the caller hangs. A
    green create with a dead endpoint is worse than a refusal."""
    response = await _call(
        stores, proxy, "POST", "/v2/apis", {"name": "ws", "protocolType": "WEBSOCKET"},
    )
    assert response.status_code == 400
    assert "WEBSOCKET" in response.body.decode()


async def test_get_api_reads_back_what_create_wrote(stores, proxy):
    api = await _create_api(stores, proxy)
    response = await _call(stores, proxy, "GET", f"/v2/apis/{api['apiId']}")

    assert response.status_code == 200
    assert json.loads(response.body)["apiEndpoint"] == api["apiEndpoint"]


async def test_a_missing_api_is_a_rest_json_NotFoundException(stores, proxy):
    """The provider's destroy path keys on this exact code; a body-only error
    is a coin flip for aws-sdk-go-v2, which reads the header first."""
    response = await _call(stores, proxy, "GET", "/v2/apis/apidoesnotexist")

    assert response.status_code == 404
    assert response.headers["x-amzn-errortype"] == "NotFoundException"
    assert json.loads(response.body)["message"]


async def test_update_api_is_reachable_at_all(stores, proxy):
    """`UpdateApi` is PATCH, and the gateway's route table did not accept PATCH
    until v0.8.19. This asserts the classifier half; the router half is
    `test_closed_world_is_method_independent.py`."""
    api = await _create_api(stores, proxy)
    response = await _call(stores, proxy, "PATCH", f"/v2/apis/{api['apiId']}", {"name": "renamed"})

    assert response.status_code == 200
    assert json.loads(response.body)["name"] == "renamed"


async def test_renaming_an_api_removes_the_container_named_after_the_old_one(stores, proxy):
    """The container name is derived from the API name, so a rename that did not
    destroy the old one would strand it forever, still holding a host port."""
    api = await _create_api(stores, proxy)
    await _call(stores, proxy, "PATCH", f"/v2/apis/{api['apiId']}", {"name": "renamed"})

    assert proxy.destroyed == ["public-api"]
    assert proxy.converges[-1][0] == "renamed"


async def test_delete_api_removes_every_child_and_the_container(stores, proxy):
    api = await _create_api(stores, proxy)
    await _create_integration(stores, proxy, api["apiId"])
    response = await _call(stores, proxy, "DELETE", f"/v2/apis/{api['apiId']}")

    assert response.status_code == 204
    assert stores.apigwctl.items(ENV) == {}
    assert proxy.destroyed == ["public-api"]


# --- integrations, routes, stages -------------------------------------------


async def _create_integration(stores, proxy, api_id: str, **overrides) -> dict:
    body = {
        "integrationType": "AWS_PROXY",
        "integrationUri": "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/"
                          "arn:aws:lambda:us-east-1:000000000000:function:orders/invocations",
        "payloadFormatVersion": "2.0",
        **overrides,
    }
    response = await _call(stores, proxy, "POST", f"/v2/apis/{api_id}/integrations", body)
    assert response.status_code == 201, response.body
    return json.loads(response.body)


async def test_a_payload_format_1_0_integration_is_refused_by_name(stores, proxy):
    """A 1.0 handler reads `event["httpMethod"]`, which a 2.0 event does not
    have -- serving the wrong one raises a KeyError inside the USER's function
    and blames their code for odin's choice."""
    api = await _create_api(stores, proxy)
    response = await _call(
        stores, proxy, "POST", f"/v2/apis/{api['apiId']}/integrations",
        {"integrationType": "AWS_PROXY", "integrationUri": "x", "payloadFormatVersion": "1.0"},
    )
    assert response.status_code == 400
    assert "payload format" in response.body.decode()


async def test_an_unmodelled_integration_type_is_refused_by_name(stores, proxy):
    api = await _create_api(stores, proxy)
    response = await _call(
        stores, proxy, "POST", f"/v2/apis/{api['apiId']}/integrations",
        {"integrationType": "AWS", "integrationUri": "x"},
    )
    assert response.status_code == 400
    assert "no substrate" in response.body.decode()


async def test_a_route_becomes_one_nginx_location_prefix(stores, proxy):
    api = await _create_api(stores, proxy)
    integration = await _create_integration(stores, proxy, api["apiId"])
    await _call(
        stores, proxy, "POST", f"/v2/apis/{api['apiId']}/routes",
        {"routeKey": "ANY /orders", "target": f"integrations/{integration['integrationId']}"},
    )
    _name, routes = proxy.converges[-1]

    assert [route.prefix for route in routes] == ["/orders"]
    assert routes[0].upstream == f"host.docker.internal:{GATEWAY_PORT}"
    assert routes[0].upstream_path.startswith(apigw_shim.SHIM_PREFIX)


async def test_the_two_route_keys_odin_emits_collapse_to_one_location(stores, proxy):
    """`ANY /orders` and `ANY /orders/{proxy+}` are ONE nginx prefix. Rendering
    both would be a duplicate `location`, which nginx refuses outright -- the
    container would not start and the API would go FAILED."""
    api = await _create_api(stores, proxy)
    integration = await _create_integration(stores, proxy, api["apiId"])
    for key in ("ANY /orders", "ANY /orders/{proxy+}"):
        await _call(
            stores, proxy, "POST", f"/v2/apis/{api['apiId']}/routes",
            {"routeKey": key, "target": f"integrations/{integration['integrationId']}"},
        )
    _name, routes = proxy.converges[-1]

    assert [route.prefix for route in routes] == ["/orders"]


async def test_a_route_whose_integration_is_gone_is_dropped_not_rendered(stores, proxy):
    """A route pointing at nothing must produce nginx's honest 404 ("this API has
    no such route"), never a `location` that 502s -- the second sends whoever is
    debugging to the wrong end."""
    api = await _create_api(stores, proxy)
    integration = await _create_integration(stores, proxy, api["apiId"])
    await _call(
        stores, proxy, "POST", f"/v2/apis/{api['apiId']}/routes",
        {"routeKey": "ANY /orders", "target": f"integrations/{integration['integrationId']}"},
    )
    await _call(
        stores, proxy, "DELETE",
        f"/v2/apis/{api['apiId']}/integrations/{integration['integrationId']}",
    )
    _name, routes = proxy.converges[-1]

    assert routes == ()


async def test_a_stage_named_default_survives_percent_encoding(stores, proxy):
    """THE BUG A REAL `tofu apply` FOUND. `$default` arrives as `%24default` on
    the raw path, so a create-from-body / read-from-path pair that does not
    decode fails with `reading API Gateway v2 Stage ($default): couldn't find
    resource`."""
    api = await _create_api(stores, proxy)
    created = await _call(
        stores, proxy, "POST", f"/v2/apis/{api['apiId']}/stages",
        {"stageName": "$default", "autoDeploy": True},
    )
    assert created.status_code == 201

    response = await _call(stores, proxy, "GET", f"/v2/apis/{api['apiId']}/stages/%24default")
    assert response.status_code == 200, response.body
    assert json.loads(response.body)["stageName"] == "$default"


async def test_creating_a_stage_does_not_reconverge_the_proxy(stores, proxy):
    """A stage changes nothing about routing, and a converge is a real container
    operation. Rendering on every stage write would be work that cannot change
    the outcome."""
    api = await _create_api(stores, proxy)
    before = len(proxy.converges)
    await _call(stores, proxy, "POST", f"/v2/apis/{api['apiId']}/stages", {"stageName": "$default"})
    assert len(proxy.converges) == before


# --- the shim's target resolution -------------------------------------------


async def test_the_shim_refuses_a_wrong_token_without_saying_why(stores, proxy):
    """The token is what stops a caller naming another ENV in the URL. Telling
    it which of the three ids was wrong would be a free oracle over another
    env's records."""
    api = await _create_api(stores, proxy)
    integration = await _create_integration(stores, proxy, api["apiId"])

    resolved = apigwctl.shim_target(
        stores, ENV, api["apiId"], integration["integrationId"], "not-the-token",
    )
    assert resolved == "Forbidden"


async def test_the_shim_resolves_a_lambda_target_to_its_function_name(stores, proxy):
    api = await _create_api(stores, proxy)
    integration = await _create_integration(stores, proxy, api["apiId"])
    record = stores.apigwctl.get(ENV, apigwctl.api_key(api["apiId"]))

    resolved = apigwctl.shim_target(
        stores, ENV, api["apiId"], integration["integrationId"], record["route_token"],
    )
    assert resolved.function_name == "orders"
    assert resolved.http_upstream is None
    assert resolved.unavailable is None


async def test_an_ecs_target_with_no_running_task_is_a_reason_not_a_dead_address(stores, proxy):
    """`tofu apply` creates the API and the service in one run, so a caller can
    arrive before a task does. A 503 naming the service beats a router pointed
    at an address that does not exist."""
    api = await _create_api(stores, proxy)
    integration = await _create_integration(
        stores, proxy, api["apiId"],
        integrationType="HTTP_PROXY", integrationUri="http://checkout.odin.internal",
        payloadFormatVersion=None,
    )
    record = stores.apigwctl.get(ENV, apigwctl.api_key(api["apiId"]))

    resolved = apigwctl.shim_target(
        stores, ENV, api["apiId"], integration["integrationId"], record["route_token"],
    )
    assert resolved.function_name is None
    assert resolved.http_upstream is None
    assert "checkout" in resolved.unavailable
    assert "no running task" in resolved.unavailable


async def test_an_ecs_target_resolves_to_the_running_tasks_published_port(stores, proxy):
    api = await _create_api(stores, proxy)
    integration = await _create_integration(
        stores, proxy, api["apiId"],
        integrationType="HTTP_PROXY", integrationUri="http://checkout.odin.internal",
        payloadFormatVersion=None,
    )
    # ecsctl's OWN record shape -- keys and field names copied from
    # `ecsctl._launch_task`, not invented here.
    stores.ecsctl.set(ENV, "task:odin:t1", {
        "service_name": "checkout", "last_status": "RUNNING", "host_ports": {"80": 32770},
    })
    record = stores.apigwctl.get(ENV, apigwctl.api_key(api["apiId"]))

    resolved = apigwctl.shim_target(
        stores, ENV, api["apiId"], integration["integrationId"], record["route_token"],
    )
    assert resolved.http_upstream == "host.docker.internal:32770"


async def test_a_stopped_task_is_not_an_address(stores, proxy):
    """A STOPPED task's published port belongs to a container that is gone.
    Reading it would be the stale-address 502 this design exists to avoid."""
    api = await _create_api(stores, proxy)
    integration = await _create_integration(
        stores, proxy, api["apiId"],
        integrationType="HTTP_PROXY", integrationUri="http://checkout.odin.internal",
        payloadFormatVersion=None,
    )
    stores.ecsctl.set(ENV, "task:odin:t1", {
        "service_name": "checkout", "last_status": "STOPPED", "host_ports": {"80": 32770},
    })
    record = stores.apigwctl.get(ENV, apigwctl.api_key(api["apiId"]))

    resolved = apigwctl.shim_target(
        stores, ENV, api["apiId"], integration["integrationId"], record["route_token"],
    )
    assert resolved.http_upstream is None
    assert resolved.unavailable is not None
