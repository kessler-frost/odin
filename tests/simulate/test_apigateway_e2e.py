"""apigateway (v0.8.19): a drawn API is a REAL HTTP endpoint that really invokes
a REAL Lambda and returns its response to a caller holding the connection open.

THIS FILE IS THE ONLY THING THAT PROVES THE ENVELOPE. Every unit test of the
shim (`tests/gateway/test_apigw_shim.py`) drives the converter with a STUB
invoke, so it proves the translation and nothing about RIE. A unit test that
fabricates the upstream signal proves the parser, not the integration -- this
repo has four guards that passed their own tests and never fired for exactly
that reason. `test_a_real_rie_returns_the_handlers_value_verbatim` below prints
what a real `public.ecr.aws/lambda/python:3.12` container really sends, and the
recorded output belongs in the release notes rather than in a claim.

The whole path runs for real, through the UI's own single button
(`POST /apply-full`): canvas -> `canvas_to_stack` -> `generate_tf`'s four-
resource apigateway expansion (api + stage + integration + 2 routes) ->
`tofu apply` -> the gateway's apigwctl model -> `docker run nginx:alpine` +
`docker run` the RIE container -> `curl` the API's REAL published port.

Three tests, cheapest first:
 1. `test_an_api_with_no_routes_...` -- api alone: the nginx container is really
    running, `/world` reports the node healthy with a real `API_ENDPOINT`, and
    that endpoint answers a real **404 `{"message":"Not Found"}`** -- which is
    what a real HTTP API with no matching route answers. Plus
    `plan -detailed-exitcode` CLEAN, the zero-drift gate.
 2. `test_a_real_rie_returns_the_handlers_value_verbatim` -- the PROBE, as a
    test. Deploys a function that echoes its event and one that raises, invokes
    each through `lambdactl.invoke` (the one door), and PRINTS the raw bytes and
    the derived `function_error`. It asserts only what it can see, so it is a
    measurement that fails loudly if RIE ever changes, not a restatement of the
    shim's own beliefs.
 3. `test_a_drawn_api_serves_a_real_lambda` -- the flagship: `GET
    <api_endpoint>/hello` returns **200** with the handler's own body, a header
    the handler set survives, a path under the prefix reaches the handler as
    `rawPath`, an unrouted path is 404, and a handler that RAISES is **502** and
    not a 200 with a stack trace in it.

Container hygiene ABSOLUTE: both container names are deterministic
(`odin-apigw-{env}-{api}`, `odin-lambda-{env}-{fn}`) so they are registered with
the cleanup fixture up front and force-removed by EXACT name on teardown,
whatever the outcome. Nothing here uses a machine-wide filter.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from odin.compute.apigw import container_name as apigw_container_name
from odin.compute.functions import FunctionRuntime
from odin.compute.functions import container_name as fn_container_name
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV_ALONE = "apigw-alone-e2e"
ENV_SERVED = "apigw-served-e2e"
ENV_PROBE = "apigw-probe-e2e"
API = "public-api"
FN = "hello"
FN_CRASH = "boom"

# A handler that returns a payload-format-2.0 PROXY response, plus one that
# returns a bare object (2.0's "anything else is the body" rule), plus one that
# raises. All three in one file so one deployment covers every case the shim
# has to tell apart.
HANDLER = '''
import json


def handler(event, context):
    path = event.get("rawPath", "")
    if path.endswith("/raise"):
        raise ValueError("the handler exploded on purpose")
    if path.endswith("/bare"):
        return {"bare": True, "seen": path}
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json", "x-handler": "odin"},
        "body": json.dumps({"rawPath": path, "method": event["requestContext"]["http"]["method"],
                            "version": event.get("version")}),
    }
'''

ECHO_HANDLER = '''
def handler(event, context):
    return {"echoed": event}
'''

CRASH_HANDLER = '''
def handler(event, context):
    raise ValueError("boom from the handler")
'''


def _canvas(*, with_lambda: bool) -> dict:
    nodes: list[dict] = [
        {"id": "n-api", "type": "apigateway", "position": {"x": 0, "y": 0},
         "data": {"label": API}},
    ]
    edges: list[dict] = []
    if with_lambda:
        nodes.append({
            "id": "n-fn", "type": "lambda", "position": {"x": 400, "y": 0},
            "data": {
                "label": FN, "runtime": "python3.12", "handler": "app.handler",
                "files": {"app.py": HANDLER},
            },
        })
        # The TARGET edge. Deliberately saved with the LEGACY `network` type,
        # not `target`: every canvas written before the edge-type registry
        # carries that, and the route pass must key on the two NODE kinds. If it
        # ever gates on the name instead, this test 404s and says so.
        edges.append({"id": "e-api-fn", "source": "n-api", "target": "n-fn",
                      "data": {"edgeType": "network"}})
    return {"nodes": nodes, "edges": edges}


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


def _tofu(args: list[str], workspace: Path, env_vars: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tofu", *args, "-input=false", "-no-color"],
        cwd=workspace, env=env_vars, capture_output=True, text=True, timeout=timeout,
    )


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _gateway_state(root: Path, env: str, name: str) -> dict:
    path = root / env / "gateway" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _api_record(root: Path, env: str) -> dict:
    state = _gateway_state(root, env, "apigwctl")
    records = [v for k, v in state.items() if k.startswith("api:")]
    return records[0] if records else {}


@pytest.fixture
def container_cleanup():
    """Force-remove by EXACT name on teardown regardless of outcome. Never a
    label or a machine-wide filter -- another agent's containers carry the same
    `odin=1` label."""
    names: set[str] = set()
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


def _apply(client: TestClient, env: str, canvas: dict, timeout: float = 420.0) -> dict:
    response = client.post(f"/apply-full?env={env}", json=canvas, timeout=timeout)
    assert response.status_code == 200, response.text
    body = response.json()
    tf = body.get("tf") or {}
    assert tf.get("status") == "ok", f"{body.get('status')}: {json.dumps(tf, indent=2)}"
    assert body["unsupported"] == [], body
    return body


def _wait_until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.5)
    raise AssertionError(f"{what} never happened (last seen: {last!r})")


def _get(url: str, tries: int = 20) -> httpx.Response:
    """GET with a short retry -- a freshly created nginx container needs a
    moment to bind. A refused connection is retried; any real HTTP answer is
    returned, so a 404 or a 502 is a RESULT and never a retry."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            return httpx.get(url, timeout=40.0)
        except httpx.HTTPError as exc:
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"never got an HTTP answer from {url}: {last!r}")


def test_an_api_with_no_routes_runs_a_real_proxy_answers_404_and_plans_clean(tmp_path, container_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    container_cleanup.add(apigw_container_name(ENV_ALONE, API))
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        gateway_port = client.get("/health").json()["gateway"]["port"]
        _apply(client, ENV_ALONE, _canvas(with_lambda=False))

        # The proxy container is REAL, not a model fiction.
        ps = _docker("ps", "--filter", f"name={apigw_container_name(ENV_ALONE, API)}", "--format", "{{.Image}}")
        assert ps.stdout.strip() == "nginx:alpine", ps.stdout

        record = _api_record(store.root, ENV_ALONE)
        assert record.get("state") == "AVAILABLE", record
        endpoint = f"http://127.0.0.1:{record['host_port']}"

        # `/world` reports the node with the SAME address the record holds --
        # the two projections of one fact, held to each other rather than to a
        # hand-written expectation.
        world = client.get(f"/world?env={ENV_ALONE}").json()
        node = world["resources"][API]
        assert node["phase"] == "healthy", node
        assert node["facts"]["API_ENDPOINT"] == endpoint, node

        # An API with no routes answers a real HTTP API's own 404.
        response = _get(f"{endpoint}/anything")
        assert response.status_code == 404, response.text
        assert response.json() == {"message": "Not Found"}

        # ZERO DRIFT. The surface where apigateway drift would hide is the
        # api's own tags and the stage, both of which odin echoes.
        workspace = store.root / ENV_ALONE / "tf"
        keys = client.post(f"/keys?env={ENV_ALONE}").json()
        plan = _tofu(
            ["plan", "-detailed-exitcode"], workspace,
            _tf_env(gateway_port, keys["access_key_id"], keys["secret_access_key"]),
        )
        assert plan.returncode == 0, f"plan is dirty:\n{plan.stdout}\n{plan.stderr}"


def test_a_real_rie_returns_the_handlers_value_verbatim(tmp_path, container_cleanup):
    """THE PROBE, AS A TEST -- run and read this before trusting
    `apigw_shim.response_from_return_value`.

    It asserts only what it can SEE, and prints the rest: the exact bytes a real
    RIE hands back for a healthy handler and for one that raises. The shim's
    whole response half rests on `FunctionRuntime.invoke` returning the
    handler's return value as JSON, and on a RAISED handler arriving as HTTP 200
    with an error document rather than as a transport failure. Both are
    upstream facts about a container odin does not own; if a future RIE changes
    either, this fails HERE with the real bytes in the message rather than
    somewhere far away as a mysterious 200."""
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    for name in (FN, FN_CRASH):
        container_cleanup.add(fn_container_name(ENV_PROBE, name))
    runtime = FunctionRuntime(root=tmp_path)

    def _zip(source: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("app.py", source)
        return buffer.getvalue()

    async def _deploy_and_invoke(name: str, source: str, payload: bytes):
        code_dir = runtime.extract_code(ENV_PROBE, name, _zip(source))
        await runtime.ensure(ENV_PROBE, name, "python3.12", "app.handler", {}, code_dir)
        return await runtime.invoke(ENV_PROBE, name, payload)

    event = json.dumps({"version": "2.0", "rawPath": "/probe", "marker": "odin"}).encode()
    healthy = asyncio.run(_deploy_and_invoke(FN, ECHO_HANDLER, event))
    crashed = asyncio.run(_deploy_and_invoke(FN_CRASH, CRASH_HANDLER, event))

    print("\n=== MEASURED: what a real RIE returns ===")
    print("healthy payload :", healthy.payload)
    print("healthy error   :", healthy.function_error)
    print("crashed payload :", crashed.payload)
    print("crashed error   :", crashed.function_error)

    # A healthy handler's RETURN VALUE comes back verbatim as JSON.
    assert healthy.function_error is None, healthy
    assert json.loads(healthy.payload)["echoed"]["marker"] == "odin", healthy.payload

    # A RAISED handler is the one the shim must not serve as a success. RIE's
    # answer carries the runtime's own error document and NO header -- which is
    # why `_function_error` reads the body.
    assert crashed.function_error == "Unhandled", crashed
    body = json.loads(crashed.payload)
    assert body["errorType"] == "ValueError", body
    assert "boom from the handler" in body["errorMessage"], body


def test_a_drawn_api_serves_a_real_lambda(tmp_path, container_cleanup):
    """THE FLAGSHIP. A caller holds a connection open and gets a status code back
    from a real function -- which is the synchrony argument that put this edge in
    the TARGET family, proven rather than asserted."""
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    container_cleanup.add(apigw_container_name(ENV_SERVED, API))
    container_cleanup.add(fn_container_name(ENV_SERVED, FN))
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        _apply(client, ENV_SERVED, _canvas(with_lambda=True))

        record = _api_record(store.root, ENV_SERVED)
        assert record.get("state") == "AVAILABLE", record
        endpoint = f"http://127.0.0.1:{record['host_port']}"

        function = _wait_until(
            lambda: _gateway_state(store.root, ENV_SERVED, "lambdactl").get(f"fn:{FN}"),
            timeout=240.0, what="the lambda function record appearing",
        )
        assert function["state"] == "Active", function

        # 1. THE POINT: a real HTTP request, through nginx, through the shim,
        #    into a real RIE, and back.
        response = _get(f"{endpoint}/{FN}")
        assert response.status_code == 200, f"{response.status_code}: {response.text}"
        body = response.json()
        assert body["rawPath"] == f"/{FN}", body
        assert body["method"] == "GET", body
        assert body["version"] == "2.0", body
        # A header the HANDLER set survives the conversion.
        assert response.headers["x-handler"] == "odin"

        # 2. The `{proxy+}` half of the pair: a path UNDER the prefix reaches the
        #    handler with the path the CALLER sent, not odin's internal shim URL.
        deep = _get(f"{endpoint}/{FN}/a/b")
        assert deep.status_code == 200, deep.text
        assert deep.json()["rawPath"] == f"/{FN}/a/b", deep.text

        # 3. 2.0's second rule: a handler that returns a bare object gets a 200
        #    JSON body, not a 500.
        bare = _get(f"{endpoint}/{FN}/bare")
        assert bare.status_code == 200, bare.text
        assert bare.json()["bare"] is True, bare.text

        # 4. A path no route matches is nginx's own honest 404.
        missing = _get(f"{endpoint}/not-a-route")
        assert missing.status_code == 404, missing.text

        # 5. A RAISED handler is a 502, NOT a 200 with a stack trace in it. RIE
        #    answers 200 for this, so a shim that passed it through would report
        #    a crashed function as a success.
        crashed = _get(f"{endpoint}/{FN}/raise")
        assert crashed.status_code == 502, f"{crashed.status_code}: {crashed.text}"
        assert crashed.headers.get("x-amzn-errortype") == "Unhandled", dict(crashed.headers)
        assert "exploded on purpose" in crashed.text, crashed.text

        # 6. The shim is not an open invoke door: the same URL without the token
        #    nginx injects is refused, and says nothing about which id was wrong.
        gateway_port = client.get("/health").json()["gateway"]["port"]
        integration_id = next(
            key.split(":")[-1]
            for key in _gateway_state(store.root, ENV_SERVED, "apigwctl")
            if key.startswith("integration:")
        )
        forged = httpx.get(
            f"http://127.0.0.1:{gateway_port}/_odin/apigw/{ENV_SERVED}/{record['api_id']}/{integration_id}",
            timeout=10.0,
        )
        assert forged.status_code == 403, f"{forged.status_code}: {forged.text}"
        assert forged.json() == {"message": "Forbidden"}

        # 7. TEARDOWN through the product's own path: an empty canvas Apply.
        _apply(client, ENV_SERVED, {"nodes": [], "edges": []})
        gone = _docker("ps", "-a", "--filter", f"name={apigw_container_name(ENV_SERVED, API)}", "--format", "{{.Names}}")
        assert gone.stdout.strip() == "", gone.stdout
