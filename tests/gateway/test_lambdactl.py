"""V4a -- gateway/models/lambdactl.py: the Lambda control-plane model
(functions), built to research-coverage.md §2d's captured call surface.

Same test method as V3a's tests/gateway/test_ec2compute.py: every request is
a REAL boto3-signed capture (the `lambda_` fixture), every response
round-trips through botocore's own rest-json parser, and a FAKE
`FunctionRuntime` is injected so these are "model logic tested without
containers" unit tests -- V4d's integration test is the only one that boots
a real RIE container.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import suppress
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.compute.functions import READY_TIMEOUT, InvokeResult
from odin.gateway.classify import classify
from odin.gateway.keys import KeyStore, workload_env
from odin.gateway.models import lambdactl, logsctl
from odin.gateway.stores import SynthStores
from odin.runtime.colima import CONTAINER_HOST
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
_GATEWAY_PORT = 4266
_ROLE_ARN = "arn:aws:iam::000000000000:role/lambda-exec"
_ZIP_BYTES = b"PK\x03\x04fake-zip-bytes"


class FakeFunctionRuntime:
    """The FunctionRuntime shape (`extract_code`/`ensure`/`invoke`/`delete`/
    `code_dir`) with no container/subprocess involved -- deterministic and
    near-instant, so the background-task state transitions lambdactl.py
    spawns can be observed with a short poll instead of a real RIE boot.

    Coroutine where the REAL `compute/functions.py::FunctionRuntime` is one
    (v0.7.7): `ensure`/`invoke`/`delete`/`status`/`logs` are `async def`;
    `extract_code`/`code_dir` are plain `def` there and stay plain here. A fake
    that got this backwards would let a missing `await` in lambdactl.py pass --
    a coroutine object is truthy, so the whole point of the fake is that its
    awaitability matches the thing it stands in for."""

    def __init__(
        self, fail_ensure: bool = False, invoke_response: bytes = b'{"ok": true}', invoke_error: str | None = None,
        block: asyncio.Event | None = None, log_text: str = "",
    ) -> None:
        self.fail_ensure = fail_ensure
        self.invoke_response = invoke_response
        self.invoke_error = invoke_error
        # Stands in for the RIE container's own stdout/stderr: a test appends
        # to it to simulate the handler printing, exactly as `docker logs
        # --tail N` would then report it.
        self.log_text = log_text
        # When set, `ensure()` blocks here until the test releases it -- lets
        # a test deterministically observe the `Pending`/`InProgress` window
        # instead of racing a near-instant fake deploy (found empirically:
        # a synchronous fake finishes before the assertion below it runs).
        # An `asyncio.Event` since v0.7.7: a `threading.Event.wait()` here
        # would block the ONE loop that has to run the very deploy task the
        # test is waiting on -- a guaranteed deadlock, not a slow test.
        self.block = block
        self.extracted: list[tuple] = []
        self.ensured: list[tuple] = []
        self.invoked: list[tuple] = []
        self.deleted: list[tuple] = []
        self.log_reads: list[tuple] = []

    def extract_code(self, env: str, name: str, zip_bytes: bytes) -> Path:
        self.extracted.append((env, name, zip_bytes))
        return Path(f"/fake/{env}/{name}-code")

    def code_dir(self, env: str, name: str) -> Path:
        return Path(f"/fake/{env}/{name}-code")

    async def ensure(
        self, env: str, name: str, runtime: str, handler: str, env_vars: dict[str, str], code_dir: Path,
        memory_mib: int | None = None,
    ) -> int:
        if self.block is not None:
            # `threading.Event.wait(timeout=5.0)` returned False and carried on
            # rather than raising; `asyncio.timeout` raises, so the suppress is
            # what preserves the ORIGINAL behaviour -- a test that never
            # releases the block still gets a deploy that eventually proceeds.
            with suppress(TimeoutError):
                async with asyncio.timeout(5.0):
                    await self.block.wait()
        self.ensured.append((env, name, runtime, handler, dict(env_vars), code_dir, memory_mib))
        if self.fail_ensure:
            raise RuntimeError("RIE never became ready")
        return 12345

    async def invoke(self, env: str, name: str, payload: bytes, timeout: float = 30.0) -> InvokeResult:
        self.invoked.append((env, name, payload))
        return InvokeResult(payload=self.invoke_response, function_error=self.invoke_error)

    async def delete(self, env: str, name: str) -> None:
        self.deleted.append((env, name))

    async def status(self, env: str, name: str) -> str:
        return "running"

    async def logs(self, env: str, name: str, tail: int = 20) -> str:
        self.log_reads.append((env, name, tail))
        return self.log_text


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("lambda")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    # `response.headers` (Starlette's own Headers, not a plain dict) is
    # deliberately passed through as-is: it's case-insensitive on `.get()`
    # (matching what a real HTTP client's header structure gives botocore's
    # parser over the wire) -- `X-Amz-Function-Error` needs exactly that,
    # since Starlette itself lowercases every header name it stores.
    raw = {"status_code": response.status_code, "headers": response.headers, "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


@pytest.fixture
def keystore(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path)


async def _answer(stores, req, substrate=None, keystore=None, gateway_port=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("lambda", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a recognized Lambda REST route must never be unmappable"
    action, resource = classified
    # `lambdactl.pure_answer` is a coroutine (v0.7.7). The `sink.call(lambda:
    # lambda_.<op>(...))` captures throughout this file are NOT: `lambda_` is a
    # real boto3 client, whose `invoke`/`delete_function`/... are synchronous.
    # `invoke` in particular exists on BOTH -- awaiting boto3's is a TypeError.
    response = await lambdactl.pure_answer(
        action, resource, ENV, req.body, stores, time.monotonic(), substrate, query, keystore, gateway_port,
    )
    assert response is not None, "lambdactl never falls through to None"
    return response


async def _create(stores, sink, lambda_, substrate, *, keystore=None, gateway_port=None, **kwargs) -> dict:
    kwargs.setdefault("FunctionName", "fn1")
    kwargs.setdefault("Role", _ROLE_ARN)
    kwargs.setdefault("Runtime", "python3.12")
    kwargs.setdefault("Handler", "lambda_function.lambda_handler")
    kwargs.setdefault("Code", {"ZipFile": _ZIP_BYTES})
    req = sink.call(lambda: lambda_.create_function(**kwargs))
    return _parse("CreateFunction", await _answer(stores, req, substrate, keystore, gateway_port))


async def _wait_for(stores, sink, lambda_, name: str, field: str, want: str, substrate, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        req = sink.call(lambda: lambda_.get_function_configuration(FunctionName=name))
        parsed = _parse("GetFunctionConfiguration", await _answer(stores, req, substrate))
        last = parsed[field]
        if last == want:
            return parsed
        # `await`, never `time.sleep`: the deploy this polls for is now a TASK
        # on this same loop, so a blocking sleep would never let it run and
        # every wait here would time out. Same 0.02s cadence as before.
        await asyncio.sleep(0.02)
    raise AssertionError(f"function {name} never reached {field}={want!r} (last seen {last!r})")


async def _wait_for_state(stores, sink, lambda_, name: str, want: str, substrate, timeout: float = 2.0) -> dict:
    return await _wait_for(stores, sink, lambda_, name, "State", want, substrate, timeout)


# --- CreateFunction / GetFunction --------------------------------------------


async def test_create_function_starts_pending_and_deploys_to_active(sink, lambda_, stores):
    block = asyncio.Event()
    substrate = FakeFunctionRuntime(block=block)
    result = await _create(stores, sink, lambda_, substrate)
    assert result["State"] == "Pending"
    assert result["StateReasonCode"] == "Creating"
    assert result["FunctionName"] == "fn1"
    assert result["Runtime"] == "python3.12"
    assert result["Version"] == "$LATEST"
    assert result["CodeSize"] == len(_ZIP_BYTES)

    block.set()
    active = await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert active["LastUpdateStatus"] == "Successful"
    assert len(substrate.ensured) == 1
    assert substrate.ensured[0][1] == "fn1"
    assert substrate.ensured[0][3] == "lambda_function.lambda_handler"


async def test_create_function_writes_the_zip_to_disk_not_the_json_store(sink, lambda_, stores, tmp_path):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    zip_path = tmp_path / ENV / "gateway" / "lambda" / "fn1.zip"
    assert zip_path.read_bytes() == _ZIP_BYTES
    state = (tmp_path / ENV / "gateway" / "lambdactl.json").read_text()
    assert "fake-zip-bytes" not in state  # the raw/base64 zip content never lands in the JSON sidecar


async def test_create_function_code_sha256_matches_real_aws_formula(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    result = await _create(stores, sink, lambda_, substrate)
    assert result["CodeSha256"] == base64.b64encode(hashlib.sha256(_ZIP_BYTES).digest()).decode()


async def test_create_function_duplicate_name_is_conflict(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.create_function(
        FunctionName="fn1", Role=_ROLE_ARN, Runtime="python3.12", Handler="h.h", Code={"ZipFile": _ZIP_BYTES},
    ))
    response = await _answer(stores, req, substrate)
    assert response.status_code == 409
    parsed = _parse("CreateFunction", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceConflictException"


async def test_create_function_without_zipfile_is_invalid_parameter(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.create_function(
        FunctionName="fn2", Role=_ROLE_ARN, Runtime="python3.12", Handler="h.h",
        Code={"S3Bucket": "my-bucket", "S3Key": "k"},
    ))
    response = await _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 400
    parsed = _parse("CreateFunction", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValueException"


# --- malformed requests (OPEN-BUGS #7, the lambda half) ----------------------
#
# Called through `pure_answer` with a hand-built body rather than the `lambda_`
# fixture, ON PURPOSE: `FunctionName` carries `min: 1` in botocore's own model,
# so boto3 refuses to SEND this and the case cannot be expressed as a signed
# capture. The gateway is a real HTTP server anything can post to.


async def _raw_create(stores, payload: dict, resource: str = "") -> Response:
    response = await lambdactl.pure_answer(
        "lambda:CreateFunction", resource, ENV, json.dumps(payload).encode(),
        stores, time.monotonic(), FakeFunctionRuntime(), {},
    )
    assert response is not None
    return response


async def test_create_function_with_no_name_anywhere_is_refused_not_keyed_empty(stores):
    """OPEN-BUGS #7: with neither a body `FunctionName` nor a URL resource this
    minted the record `fn:` and the ARN `...:function:` -- a function deployed
    for real that no later call can name, and therefore no later call can get,
    update or delete."""
    response = await _raw_create(stores, {"Role": _ROLE_ARN, "Code": {"ZipFile": base64.b64encode(_ZIP_BYTES).decode()}})
    assert response.status_code == 400
    assert _parse("CreateFunction", response, error=True)["Error"]["Code"] == "InvalidParameterValueException"
    # ...and no empty-keyed record was left behind.
    assert stores.lambdactl.get(ENV, "fn:") is None
    assert stores.lambdactl.items(ENV) == {}


async def test_create_function_still_falls_back_to_the_url_resource_for_its_name(stores):
    """The guard refuses only when BOTH are absent -- the URL-resource fallback
    is a real path and must keep working."""
    response = await _raw_create(
        stores, {"Role": _ROLE_ARN, "Code": {"ZipFile": base64.b64encode(_ZIP_BYTES).decode()}}, resource="from-url",
    )
    assert response.status_code == 201
    assert stores.lambdactl.get(ENV, "fn:from-url") is not None


async def test_a_not_found_for_a_request_that_named_no_function_does_not_trail_off(stores):
    """The reader-side half: fifteen call sites pass the URL `resource`
    straight into `_not_found`, and an empty one made the sentence trail off
    INSIDE the ARN -- `Function not found: arn:aws:lambda:...:function:`."""
    response = await lambdactl.pure_answer(
        "lambda:GetFunction", "", ENV, b"", stores, time.monotonic(), FakeFunctionRuntime(), {},
    )
    assert response.status_code == 404
    message = _parse("GetFunction", response, error=True)["Error"]["Message"]
    assert message == "Function not found: this request named no function"
    assert not message.rstrip().endswith(":")


async def test_create_function_deploy_failure_lands_failed_with_reason(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(fail_ensure=True)
    await _create(stores, sink, lambda_, substrate)
    failed = await _wait_for_state(stores, sink, lambda_, "fn1", "Failed", substrate)
    assert failed["StateReasonCode"] == "InternalError"
    assert failed["LastUpdateStatus"] == "Failed"


async def test_get_function_wraps_configuration_code_and_tags(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate, Tags={"team": "platform"})
    req = sink.call(lambda: lambda_.get_function(FunctionName="fn1"))
    parsed = _parse("GetFunction", await _answer(stores, req, substrate))
    assert parsed["Configuration"]["FunctionName"] == "fn1"
    assert parsed["Code"]["RepositoryType"] == "S3"
    assert parsed["Tags"] == {"team": "platform"}


async def test_get_function_unknown_name_is_not_found(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.get_function(FunctionName="ghost"))
    response = await _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 404
    parsed = _parse("GetFunction", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceNotFoundException"


# --- DeleteFunction -----------------------------------------------------------


async def test_delete_function_tears_down_the_container_and_the_zip(sink, lambda_, stores, tmp_path):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.delete_function(FunctionName="fn1"))
    response = await _answer(stores, req, substrate)
    assert response.status_code == 204
    assert substrate.deleted == [(ENV, "fn1")]
    assert not (tmp_path / ENV / "gateway" / "lambda" / "fn1.zip").exists()

    get_req = sink.call(lambda: lambda_.get_function(FunctionName="fn1"))
    assert (await _answer(stores, get_req, substrate)).status_code == 404


async def test_delete_function_unknown_name_is_not_found(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.delete_function(FunctionName="ghost"))
    response = await _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 404


# --- UpdateFunctionCode / UpdateFunctionConfiguration --------------------------


async def test_update_function_code_redeploys_and_updates_hash(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    block = asyncio.Event()
    substrate.block = block
    new_zip = b"PK\x03\x04updated-bytes"
    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=new_zip))
    parsed = _parse("UpdateFunctionCode", await _answer(stores, req, substrate))
    assert parsed["CodeSize"] == len(new_zip)
    assert parsed["LastUpdateStatus"] == "InProgress"

    # State (Active) is untouched by a code update -- only LastUpdateStatus moves.
    block.set()
    result = await _wait_for(stores, sink, lambda_, "fn1", "LastUpdateStatus", "Successful", substrate)
    assert result["State"] == "Active"
    assert len(substrate.ensured) == 2  # once at create, once at this redeploy


async def test_concurrent_redeploys_do_not_corrupt_or_drop_each_others_fields(sink, lambda_, stores, tmp_path):
    """Release finding #3 -- lambdactl's `_finish_deploy` (background) vs a
    synchronous UpdateFunctionCode/UpdateFunctionConfiguration handler used
    to `get()` the function record, mutate it directly, then `set()` it
    back: a classic read-modify-write race. Many concurrent redeploys
    against the SAME function must all land (none silently dropped by a
    lost race), and the sidecar file must stay valid JSON throughout.

    v0.7.7: eight THREADS became eight concurrently-scheduled TASKS, because
    that is what the code under test now faces -- `_finish_deploy` is a task on
    the same loop as the request handler it races. The claim being tested is
    unchanged and still real: all 8 redeploys plus the create must each reach
    `_redeploy_response` and spawn their own `_finish_deploy`, none silently
    dropped. Exceptions are no longer collected into a list and asserted at the
    end; `asyncio.gather` re-raises them here, which fails the test directly."""
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    async def redeploy_config(memory: int) -> None:
        req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", MemorySize=memory))
        response = await _answer(stores, req, substrate)
        assert response.status_code == 200

    await asyncio.gather(*(redeploy_config(128 * i) for i in range(1, 9)))

    # None of the 8 concurrent redeploys was silently overwritten by another
    # -- each independently reached `_redeploy_response` and spawned its own
    # `_finish_deploy`. Those run as unattended background TASKS, so this waits
    # for all 9 (8 redeploys + the initial create) rather than assuming they've
    # already landed: a spawned task has not run at all until the spawner next
    # yields, and asserting a count immediately after the REQUESTS finish would
    # be a race (found empirically once `_finish_deploy` grew one more store
    # write ahead of `ensure` -- W2.1's log-shipping cursor reset).
    deadline = time.monotonic() + 2.0
    while len(substrate.ensured) < 9 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert len(substrate.ensured) == 9
    # The sidecar itself was never left mid-write/corrupted by the hammering.
    sidecar = tmp_path / ENV / "gateway" / "lambdactl.json"
    json.loads(sidecar.read_text())  # raises if truncated/invalid


async def test_update_function_configuration_changes_fields_without_touching_code(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", Timeout=30, MemorySize=256))
    parsed = _parse("UpdateFunctionConfiguration", await _answer(stores, req, substrate))
    assert parsed["Timeout"] == 30
    assert parsed["MemorySize"] == 256
    # no NEW zip was extracted -- the redeploy reuses the existing code dir.
    assert len(substrate.extracted) == 1


# --- Invoke: the data plane ----------------------------------------------------


async def test_invoke_forwards_the_payload_to_the_substrate(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(invoke_response=b'{"echo": true}')
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    # `lambda_.invoke` is boto3's SYNCHRONOUS client method (the capture), not
    # `FunctionRuntime.invoke` the coroutine -- same name, opposite answer.
    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b'{"key": "value"}'))
    response = await _answer(stores, req, substrate)
    assert response.status_code == 200
    assert response.body == b'{"echo": true}'
    assert substrate.invoked == [(ENV, "fn1", b'{"key": "value"}')]


async def test_invoke_surfaces_function_error_header(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(invoke_response=b'{"errorType": "ValueError"}', invoke_error="Unhandled")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    parsed = _parse("Invoke", await _answer(stores, req, substrate))
    assert parsed["FunctionError"] == "Unhandled"


async def test_a_failing_invocation_is_recorded_on_the_function_record(sink, lambda_, stores):
    """Field test 2 finding #4: a function failing EVERY invocation reported
    `healthy` and nothing else, because the FunctionError the RIE reported went
    into the response header and nowhere else. `reconcile/tf_status.py` turns
    this field into the node's verdict."""
    substrate = FakeFunctionRuntime(invoke_response=b'{"errorType": "Runtime.HandlerNotFound"}', invoke_error="Unhandled")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert stores.lambdactl.get(ENV, "fn:fn1")["last_invocation_error"] is None  # cold: no alarm

    await _invoke_once(stores, sink, lambda_, substrate)

    assert stores.lambdactl.get(ENV, "fn:fn1")["last_invocation_error"] == "Unhandled"


async def test_a_redeploy_clears_the_recorded_invocation_failure(sink, lambda_, stores):
    # Fixing the handler and re-Applying must not leave the old deployment's
    # failure verdict standing: this deployment hasn't been invoked yet.
    substrate = FakeFunctionRuntime(invoke_response=b"{}", invoke_error="Unhandled")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    await _invoke_once(stores, sink, lambda_, substrate)
    assert stores.lambdactl.get(ENV, "fn:fn1")["last_invocation_error"] == "Unhandled"

    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=b"PK\x03\x04fixed"))
    await _answer(stores, req, substrate)

    assert stores.lambdactl.get(ENV, "fn:fn1")["last_invocation_error"] is None


async def test_a_recovering_invocation_clears_the_recorded_failure(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(invoke_response=b"{}", invoke_error="Unhandled")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    await _invoke_once(stores, sink, lambda_, substrate)
    assert stores.lambdactl.get(ENV, "fn:fn1")["last_invocation_error"] == "Unhandled"

    substrate.invoke_error = None  # the handler was fixed and redeployed
    await _invoke_once(stores, sink, lambda_, substrate)

    assert stores.lambdactl.get(ENV, "fn:fn1")["last_invocation_error"] is None


async def test_invoke_before_active_is_not_ready(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(block=asyncio.Event())  # never released -- stays Pending
    await _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    response = await _answer(stores, req, substrate)
    assert response.status_code == 502
    parsed = _parse("Invoke", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceNotReadyException"
    assert substrate.invoked == []


async def test_invoke_unknown_function_is_not_found(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.invoke(FunctionName="ghost", Payload=b"{}"))
    response = await _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 404


# --- W2.1 piece 3: Invoke ships the container tail into /aws/lambda/{fn} --------


_FN_LOG_GROUP = "/aws/lambda/fn1"
_FN_LOG_STREAM = "odin-lambda-default-fn1"  # compute/functions.py::container_name


async def _invoke_once(stores, sink, lambda_, substrate) -> Response:
    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    return await _answer(stores, req, substrate)


def _shipped(stores) -> list[dict]:
    return logsctl.stored_events(stores, ENV, _FN_LOG_GROUP, 100)


async def test_invoke_ships_the_container_tail_into_the_function_log_group(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(log_text="START RequestId: abc\nhello from the handler\nEND\n")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    assert (await _invoke_once(stores, sink, lambda_, substrate)).status_code == 200

    events = _shipped(stores)
    assert [e["message"] for e in events] == ["START RequestId: abc", "hello from the handler", "END"]
    # One stream per real RIE container, named after the container itself
    # (lambdactl.py's `_ship_logs`: RIE reuses one container for every invoke,
    # so AWS's `{date}/[$LATEST]{requestId}` naming has nothing to key off).
    assert {e["stream"] for e in events} == {_FN_LOG_STREAM}
    # The group was auto-created by ingestion (logsctl deviation 1), so a
    # later real CreateLogGroup can still adopt it.
    assert logsctl.group_exists(stores, ENV, _FN_LOG_GROUP)


async def test_second_invoke_of_an_unchanged_tail_ships_nothing_new(sink, lambda_, stores):
    """The dedup that makes shipping-per-invoke safe: `ingest_tail`'s cursor
    counts the lines of this container's output already ingested, so re-reading
    the same tail appends nothing -- only genuinely new lines land."""
    substrate = FakeFunctionRuntime(log_text="line one\nline two\n")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    await _invoke_once(stores, sink, lambda_, substrate)
    await _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["line one", "line two"]

    # A THIRD invoke that actually printed something new ships only that line.
    substrate.log_text += "line three\n"
    await _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["line one", "line two", "line three"]


async def test_handler_error_invoke_still_ships_its_traceback(sink, lambda_, stores):
    """The error path's logs are the whole point -- a handler that raised
    returns a FunctionError header and its traceback lives only in the
    container, so shipping must happen on that path too."""
    substrate = FakeFunctionRuntime(
        invoke_response=b'{"errorType": "ValueError"}', invoke_error="Unhandled",
        log_text='Traceback (most recent call last):\nValueError: boom\n',
    )
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    parsed = _parse("Invoke", await _invoke_once(stores, sink, lambda_, substrate))
    assert parsed["FunctionError"] == "Unhandled"
    assert "ValueError: boom" in "\n".join(e["message"] for e in _shipped(stores))


async def test_a_redeploy_resets_the_cursor_so_the_new_containers_first_lines_still_ship(sink, lambda_, stores):
    """A redeploy REPLACES the RIE container, whose output starts back at line
    1 -- `_finish_deploy` forgets the stream's cursor (logsctl's
    `reset_cursor`), so the fresh container's first lines are ingested instead
    of being mistaken for already-seen ones. The events already stored stay
    put: this resets the read position, never the log."""
    substrate = FakeFunctionRuntime(log_text="old one\nold two\nold three\n")
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    await _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["old one", "old two", "old three"]

    # The new container prints FEWER lines than the old cursor counted -- the
    # exact case a stranded cursor would swallow whole.
    substrate.log_text = "new one\n"
    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=b"PK\x03\x04new"))
    _parse("UpdateFunctionCode", await _answer(stores, req, substrate))
    await _wait_for(stores, sink, lambda_, "fn1", "LastUpdateStatus", "Successful", substrate)

    await _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["old one", "old two", "old three", "new one"]


async def test_invoke_before_active_never_reads_the_container_logs(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(block=asyncio.Event(), log_text="nothing to see")
    await _create(stores, sink, lambda_, substrate)
    assert (await _invoke_once(stores, sink, lambda_, substrate)).status_code == 502
    assert substrate.log_reads == []
    assert _shipped(stores) == []


# --- ListVersionsByFunction / GetFunctionCodeSigningConfig ---------------------


async def test_list_versions_by_function_returns_latest(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.list_versions_by_function(FunctionName="fn1"))
    parsed = _parse("ListVersionsByFunction", await _answer(stores, req, substrate))
    (version,) = parsed["Versions"]
    assert version["Version"] == "$LATEST"


async def test_get_function_code_signing_config(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.get_function_code_signing_config(FunctionName="fn1"))
    parsed = _parse("GetFunctionCodeSigningConfig", await _answer(stores, req, substrate))
    assert parsed["FunctionName"] == "fn1"


# --- Tags -----------------------------------------------------------------------


async def test_tag_resource_and_list_tags(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    tag_req = sink.call(lambda: lambda_.tag_resource(
        Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1", Tags={"env": "prod"},
    ))
    assert (await _answer(stores, tag_req, substrate)).status_code == 204

    list_req = sink.call(lambda: lambda_.list_tags(Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1"))
    parsed = _parse("ListTags", await _answer(stores, list_req, substrate))
    assert parsed["Tags"] == {"env": "prod"}


async def test_untag_resource_removes_the_key(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate, Tags={"env": "prod", "team": "x"})
    req = sink.call(lambda: lambda_.untag_resource(
        Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1", TagKeys=["env"],
    ))
    assert (await _answer(stores, req, substrate)).status_code == 204

    list_req = sink.call(lambda: lambda_.list_tags(Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1"))
    parsed = _parse("ListTags", await _answer(stores, list_req, substrate))
    assert parsed["Tags"] == {"team": "x"}


# --- workload credential injection (keystore/gateway_port, fix-wave 2b) ---------


_AWS_INJECTED_KEYS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL", "AWS_DEFAULT_REGION"}


async def test_create_with_odin_node_tag_injects_gateway_creds_into_the_container(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    await _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    env_vars = substrate.ensured[0][4]
    # `KeyStore.issue` is stable per (env, node): calling workload_env again
    # in the test reproduces the EXACT credentials the deploy must have used.
    expected = workload_env(keystore, ENV, "myfn", _GATEWAY_PORT)
    assert expected["AWS_ACCESS_KEY_ID"] == keystore.issue(ENV, "myfn")[0]
    assert env_vars == {"FOO": "bar", **expected}


async def test_injected_creds_never_leak_into_the_configuration_response(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    await _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    active = await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    # The drift-safety guarantee: the response echoes EXACTLY what the user
    # configured -- the injected AWS_* vars live only in the container.
    assert active["Environment"]["Variables"] == {"FOO": "bar"}
    assert not _AWS_INJECTED_KEYS & set(active["Environment"]["Variables"])


async def test_update_configuration_redeploy_reinjects_creds_from_persisted_tags(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    await _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"},
    )
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    # Tags are NOT resent on the update call -- they persist from creation.
    req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", MemorySize=256))
    parsed = _parse(
        "UpdateFunctionConfiguration",
        await _answer(stores, req, substrate, keystore, _GATEWAY_PORT),
    )
    assert parsed["LastUpdateStatus"] == "InProgress"
    await _wait_for(stores, sink, lambda_, "fn1", "LastUpdateStatus", "Successful", substrate)

    env_vars = substrate.ensured[1][4]
    assert env_vars == workload_env(keystore, ENV, "myfn", _GATEWAY_PORT)


async def test_create_without_keystore_behaves_exactly_as_before(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(
        stores, sink, lambda_, substrate,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert substrate.ensured[0][4] == {"FOO": "bar"}  # regression: no injection without a keystore


async def test_create_without_odin_node_tag_deploys_with_no_injected_vars(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert substrate.ensured[0][4] == {}  # no odin:node tag -> nothing to inject, deploy still lands


# --- Canvas WIRING: the node's own `env` map reaches the real container --------


async def test_the_nodes_env_refs_are_resolved_into_the_container(sink, lambda_, stores, keystore, tmp_path):
    """Field test 2, "the product hole": a lambda node's `env` refs never
    reached the RIE container. They now ride the same launch-time seam the
    issued credentials do -- so a resolved DATABASE_URL (which carries the DB
    password) never enters `fn["environment"]`, and therefore never enters the
    provider's read or tofu state."""
    stores.rdsctl.set(ENV, "db:appdb", {
        "db_instance_identifier": "appdb", "master_username": "app", "master_password": "s3cret",
        "db_name": "shop", "status": "available", "endpoint_port": 33366,
    })
    SpecStore(tmp_path).apply(canvas_to_stack({
        "nodes": [
            {"id": "n1", "type": "rds", "data": {"label": "appdb"}},
            {"id": "n2", "type": "lambda", "data": {
                "label": "myfn", "env": {"DATABASE_URL": "${{appdb.DATABASE_URL}}"}}},
        ],
        "edges": [],
    }, env=ENV))
    substrate = FakeFunctionRuntime()
    await _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    active = await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    env_vars = substrate.ensured[0][4]
    assert env_vars["DATABASE_URL"] == f"postgresql://app:s3cret@{CONTAINER_HOST}:33366/shop"
    assert env_vars["FOO"] == "bar"  # the declared variable survives alongside it
    # Zero-drift + no-secrets-in-state: the response still echoes ONLY what the
    # user configured.
    assert active["Environment"]["Variables"] == {"FOO": "bar"}


async def test_an_unresolvable_ref_lands_the_function_failed_with_a_naming_reason(sink, lambda_, stores, keystore, tmp_path):
    stores.rdsctl.set(ENV, "db:appdb", {
        "db_instance_identifier": "appdb", "master_username": "app", "master_password": "s3cret",
        "db_name": "shop", "status": "creating", "endpoint_port": 0,
    })
    SpecStore(tmp_path).apply(canvas_to_stack({
        "nodes": [
            {"id": "n1", "type": "rds", "data": {"label": "appdb"}},
            {"id": "n2", "type": "lambda", "data": {
                "label": "myfn", "env": {"DATABASE_URL": "${{appdb.DATABASE_URL}}"}}},
        ],
        "edges": [],
    }, env=ENV))
    substrate = FakeFunctionRuntime()
    await _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"},
    )
    failed = await _wait_for_state(stores, sink, lambda_, "fn1", "Failed", substrate)
    assert "DATABASE_URL" in failed["StateReason"], failed
    assert "appdb" in failed["StateReason"], failed
    assert not substrate.ensured, "no container may start with a hole in its environment"


# --- W2.2's honesty fix: the reality sweep's seam + the Apply-driven
# recovery that makes "re-Apply to recreate" true for lambda ------------------


async def test_mark_function_failed_is_what_a_caller_reads_after_the_container_vanishes(sink, lambda_, stores):
    """The sweep's seam: a function whose RIE container was removed outside
    odin reads `Failed` with the drift reason (so /world says crashed and says
    why), and Invoke refuses instead of dialing a container that isn't there.
    The RECORD survives -- real AWS never deletes a function because its
    execution environment died."""
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    lambdactl.mark_function_failed(
        stores, ENV, "fn1", "container odin-lambda-default-fn1 removed outside odin — re-Apply to recreate",
    )

    req = sink.call(lambda: lambda_.get_function(FunctionName="fn1"))
    config = _parse("GetFunction", await _answer(stores, req, substrate))["Configuration"]
    assert config["State"] == "Failed"
    assert config["StateReasonCode"] == "InternalError"
    assert "removed outside odin" in config["StateReason"]
    assert config["LastUpdateStatus"] == "Successful", "the last DEPLOY did succeed -- only the sandbox died"

    invoke = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    response = await _answer(stores, invoke, substrate)
    assert _parse("Invoke", response, error=True)["Error"]["Code"] == "ResourceNotReadyException"
    assert substrate.invoked == []


async def test_converge_functions_recreates_the_container_of_a_failed_function(sink, lambda_, stores):
    """What an Apply now does (server.py's /apply-full). tofu can never fix
    this itself: the `aws_lambda_function` config is unchanged, so its plan is
    empty -- the same reason `ecsctl.converge_services` exists for tasks."""
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    lambdactl.mark_function_failed(stores, ENV, "fn1", "container removed outside odin")

    # Still a PLAIN call -- `converge_functions` stayed a `def` in v0.7.7; it
    # returns the `asyncio.Task`s it started, which is why this test has to be
    # `async` (creating a task needs a running loop), not because it awaits.
    lambdactl.converge_functions(stores, ENV, substrate)

    active = await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert active["StateReason"] == "The function is ready."
    assert active["LastUpdateStatus"] == "Successful"
    assert len(substrate.ensured) == 2, "the container was really re-created"
    assert substrate.ensured[1][:4] == (ENV, "fn1", "python3.12", "lambda_function.lambda_handler")
    assert substrate.ensured[1][5] == substrate.code_dir(ENV, "fn1"), "same code, restarted container"


async def _settle() -> None:
    """Give any task a call MIGHT have spawned time to record itself --
    `FakeFunctionRuntime.ensure` is instant, so a converge that wrongly fired
    lands well inside this window (the same short-poll technique `_wait_for`
    uses for the transitions that are SUPPOSED to happen). `asyncio.sleep`,
    not `time.sleep`: a blocking sleep would deny the loop to the very task
    this is trying to give a chance to run, so a wrongly-spawned converge
    would go UNDETECTED and the test would pass vacuously."""
    await asyncio.sleep(0.1)


async def test_converge_functions_leaves_an_active_function_completely_alone(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    lambdactl.converge_functions(stores, ENV, substrate)

    # The claim is synchronous, so an untouched `Successful` proves no converge
    # was even started -- restarting a healthy function's container on every
    # Apply would be a self-inflicted outage.
    req = sink.call(lambda: lambda_.get_function_configuration(FunctionName="fn1"))
    assert _parse("GetFunctionConfiguration", await _answer(stores, req, substrate))["LastUpdateStatus"] == "Successful"
    await _settle()
    assert len(substrate.ensured) == 1


async def test_converge_functions_skips_a_function_mid_deploy(sink, lambda_, stores):
    """The apply that triggers this converge may itself have just called
    UpdateFunctionCode on the same function: two `ensure` calls racing for one
    container is exactly the fight this skip prevents."""
    substrate = FakeFunctionRuntime(block=asyncio.Event())
    await _create(stores, sink, lambda_, substrate)
    substrate.block.set()
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    lambdactl.mark_function_failed(stores, ENV, "fn1", "container removed outside odin")
    # A real redeploy, parked mid-`ensure`: Failed AND LastUpdateStatus=InProgress.
    substrate.block.clear()
    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=b"new-zip"))
    assert _parse("UpdateFunctionCode", await _answer(stores, req, substrate))["LastUpdateStatus"] == "InProgress"

    lambdactl.converge_functions(stores, ENV, substrate)

    substrate.block.set()
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    await _settle()
    assert len(substrate.ensured) == 2, "the redeploy already in flight is the only ensure"


# --- the post-apply verification (`ecsctl.wait_for_steady_services`' twin) ---


async def test_wait_for_active_functions_reports_a_function_whose_redeploy_failed(sink, lambda_, stores):
    """THE hole this closes: `converge_functions` starts a redeploy and
    returns, so /apply-full scored `applied` the instant it was spawned even
    when the container never came back. The wait AWAITS that task and reports
    what really happened -- naming the function and the REAL reason."""
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    lambdactl.mark_function_failed(stores, ENV, "fn1", "container removed outside odin")
    substrate.fail_ensure = True  # the redeploy will fail the same way it failed before

    deploying = lambdactl.converge_functions(stores, ENV, substrate)
    faults = await lambdactl.wait_for_active_functions(stores, ENV, deploying)

    # `_exc_text`: the class rides with the message, so a redeploy that fails
    # with an exception carrying none still names something real here rather
    # than reporting a fault with a blank reason.
    assert faults == [lambdactl.FunctionFault(
        node="fn1", state="Failed", reason="RuntimeError: RIE never became ready",
    )], faults


async def test_wait_for_active_functions_is_one_store_read_when_everything_is_active(sink, lambda_, stores):
    """The happy path may not slow down: nothing is deploying, so the wait
    returns on its first pass without sleeping or polling once."""
    substrate = FakeFunctionRuntime()
    await _create(stores, sink, lambda_, substrate)
    await _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    started = time.monotonic()
    faults = await lambdactl.wait_for_active_functions(stores, ENV, [])
    elapsed = time.monotonic() - started

    assert faults == []
    assert elapsed < 0.1, f"a healthy env cost {elapsed:.3f}s -- it must cost one store read"


async def test_wait_for_active_functions_waits_for_a_function_that_is_still_deploying(sink, lambda_, stores):
    """The trap the ECS version avoids, for lambda: a function that is merely
    STILL STARTING must never fail an apply. The wait blocks on the in-flight
    deploy (`Pending`) rather than judging it at that instant."""
    block = asyncio.Event()
    substrate = FakeFunctionRuntime(block=block)
    await _create(stores, sink, lambda_, substrate)
    pending = _parse("GetFunctionConfiguration", await _answer(
        stores, sink.call(lambda: lambda_.get_function_configuration(FunctionName="fn1")), substrate,
    ))
    assert pending["State"] == "Pending", "the create is genuinely still in flight"

    # `loop.call_later`, the `threading.Timer(0.2, block.set)` this replaces:
    # same 0.2s delay, and `asyncio.Event.set` is a plain sync call the loop
    # can make directly. The release must still arrive WHILE the wait below is
    # running -- that overlap is the whole test.
    asyncio.get_running_loop().call_later(0.2, block.set)
    faults = await lambdactl.wait_for_active_functions(stores, ENV, [], timeout=5.0)

    assert faults == [], "a function that was still coming up must not fail the apply"


async def test_wait_for_active_functions_gives_up_at_its_budget(sink, lambda_, stores):
    """Bound 3: a deploy that never finishes is a bounded apply, not a hang.
    Reported as the transitional state it is really stuck in, never invented."""
    substrate = FakeFunctionRuntime(block=asyncio.Event())  # never released
    await _create(stores, sink, lambda_, substrate)

    started = time.monotonic()
    faults = await lambdactl.wait_for_active_functions(stores, ENV, [], timeout=0.6)
    elapsed = time.monotonic() - started

    assert 0.5 < elapsed < 3.0, elapsed
    assert faults == [], "a function still mid-deploy at the budget is not (yet) a failure"
    substrate.block.set()


def test_active_timeout_defaults_to_outlasting_the_deploy_it_verifies(monkeypatch):
    """The budget must be LONGER than `FunctionRuntime.ensure`'s own wait, or
    the verification would hard-stop while the thread it is verifying is still
    working and report a transitional state instead of the real reason."""
    monkeypatch.delenv("ODIN_LAMBDA_ACTIVE_TIMEOUT", raising=False)
    assert lambdactl.active_timeout() > READY_TIMEOUT

    monkeypatch.setenv("ODIN_LAMBDA_ACTIVE_TIMEOUT", "7")
    assert lambdactl.active_timeout() == 7.0
