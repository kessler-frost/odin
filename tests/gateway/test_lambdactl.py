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

import base64
import hashlib
import json
import threading
import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.compute.functions import InvokeResult
from odin.gateway.classify import classify
from odin.gateway.keys import KeyStore, workload_env
from odin.gateway.models import lambdactl, logsctl
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "default"
_GATEWAY_PORT = 4266
_ROLE_ARN = "arn:aws:iam::000000000000:role/lambda-exec"
_ZIP_BYTES = b"PK\x03\x04fake-zip-bytes"


class FakeFunctionRuntime:
    """The FunctionRuntime shape (`extract_code`/`ensure`/`invoke`/`delete`/
    `code_dir`) with no container/subprocess involved -- deterministic and
    near-instant, so the background-thread state transitions lambdactl.py
    spawns can be observed with a short poll instead of a real RIE boot."""

    def __init__(
        self, fail_ensure: bool = False, invoke_response: bytes = b'{"ok": true}', invoke_error: str | None = None,
        block: threading.Event | None = None, log_text: str = "",
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

    def ensure(
        self, env: str, name: str, runtime: str, handler: str, env_vars: dict[str, str], code_dir: Path,
        memory_mib: int | None = None,
    ) -> int:
        if self.block is not None:
            self.block.wait(timeout=5.0)
        self.ensured.append((env, name, runtime, handler, dict(env_vars), code_dir, memory_mib))
        if self.fail_ensure:
            raise RuntimeError("RIE never became ready")
        return 12345

    def invoke(self, env: str, name: str, payload: bytes, timeout: float = 30.0) -> InvokeResult:
        self.invoked.append((env, name, payload))
        return InvokeResult(payload=self.invoke_response, function_error=self.invoke_error)

    def delete(self, env: str, name: str) -> None:
        self.deleted.append((env, name))

    def status(self, env: str, name: str) -> str:
        return "running"

    def logs(self, env: str, name: str, tail: int = 20) -> str:
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


def _answer(stores, req, substrate=None, keystore=None, gateway_port=None) -> Response:
    path, query = split_url(req.url)
    classified = classify("lambda", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a recognized Lambda REST route must never be unmappable"
    action, resource = classified
    response = lambdactl.pure_answer(
        action, resource, ENV, req.body, stores, time.monotonic(), substrate, query, keystore, gateway_port,
    )
    assert response is not None, "lambdactl never falls through to None"
    return response


def _create(stores, sink, lambda_, substrate, *, keystore=None, gateway_port=None, **kwargs) -> dict:
    kwargs.setdefault("FunctionName", "fn1")
    kwargs.setdefault("Role", _ROLE_ARN)
    kwargs.setdefault("Runtime", "python3.12")
    kwargs.setdefault("Handler", "lambda_function.lambda_handler")
    kwargs.setdefault("Code", {"ZipFile": _ZIP_BYTES})
    req = sink.call(lambda: lambda_.create_function(**kwargs))
    return _parse("CreateFunction", _answer(stores, req, substrate, keystore, gateway_port))


def _wait_for(stores, sink, lambda_, name: str, field: str, want: str, substrate, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        req = sink.call(lambda: lambda_.get_function_configuration(FunctionName=name))
        parsed = _parse("GetFunctionConfiguration", _answer(stores, req, substrate))
        last = parsed[field]
        if last == want:
            return parsed
        time.sleep(0.02)
    raise AssertionError(f"function {name} never reached {field}={want!r} (last seen {last!r})")


def _wait_for_state(stores, sink, lambda_, name: str, want: str, substrate, timeout: float = 2.0) -> dict:
    return _wait_for(stores, sink, lambda_, name, "State", want, substrate, timeout)


# --- CreateFunction / GetFunction --------------------------------------------


def test_create_function_starts_pending_and_deploys_to_active(sink, lambda_, stores):
    block = threading.Event()
    substrate = FakeFunctionRuntime(block=block)
    result = _create(stores, sink, lambda_, substrate)
    assert result["State"] == "Pending"
    assert result["StateReasonCode"] == "Creating"
    assert result["FunctionName"] == "fn1"
    assert result["Runtime"] == "python3.12"
    assert result["Version"] == "$LATEST"
    assert result["CodeSize"] == len(_ZIP_BYTES)

    block.set()
    active = _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert active["LastUpdateStatus"] == "Successful"
    assert len(substrate.ensured) == 1
    assert substrate.ensured[0][1] == "fn1"
    assert substrate.ensured[0][3] == "lambda_function.lambda_handler"


def test_create_function_writes_the_zip_to_disk_not_the_json_store(sink, lambda_, stores, tmp_path):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    zip_path = tmp_path / ENV / "gateway" / "lambda" / "fn1.zip"
    assert zip_path.read_bytes() == _ZIP_BYTES
    state = (tmp_path / ENV / "gateway" / "lambdactl.json").read_text()
    assert "fake-zip-bytes" not in state  # the raw/base64 zip content never lands in the JSON sidecar


def test_create_function_code_sha256_matches_real_aws_formula(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    result = _create(stores, sink, lambda_, substrate)
    assert result["CodeSha256"] == base64.b64encode(hashlib.sha256(_ZIP_BYTES).digest()).decode()


def test_create_function_duplicate_name_is_conflict(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.create_function(
        FunctionName="fn1", Role=_ROLE_ARN, Runtime="python3.12", Handler="h.h", Code={"ZipFile": _ZIP_BYTES},
    ))
    response = _answer(stores, req, substrate)
    assert response.status_code == 409
    parsed = _parse("CreateFunction", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceConflictException"


def test_create_function_without_zipfile_is_invalid_parameter(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.create_function(
        FunctionName="fn2", Role=_ROLE_ARN, Runtime="python3.12", Handler="h.h",
        Code={"S3Bucket": "my-bucket", "S3Key": "k"},
    ))
    response = _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 400
    parsed = _parse("CreateFunction", response, error=True)
    assert parsed["Error"]["Code"] == "InvalidParameterValueException"


def test_create_function_deploy_failure_lands_failed_with_reason(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(fail_ensure=True)
    _create(stores, sink, lambda_, substrate)
    failed = _wait_for_state(stores, sink, lambda_, "fn1", "Failed", substrate)
    assert failed["StateReasonCode"] == "InternalError"
    assert failed["LastUpdateStatus"] == "Failed"


def test_get_function_wraps_configuration_code_and_tags(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate, Tags={"team": "platform"})
    req = sink.call(lambda: lambda_.get_function(FunctionName="fn1"))
    parsed = _parse("GetFunction", _answer(stores, req, substrate))
    assert parsed["Configuration"]["FunctionName"] == "fn1"
    assert parsed["Code"]["RepositoryType"] == "S3"
    assert parsed["Tags"] == {"team": "platform"}


def test_get_function_unknown_name_is_not_found(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.get_function(FunctionName="ghost"))
    response = _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 404
    parsed = _parse("GetFunction", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceNotFoundException"


# --- DeleteFunction -----------------------------------------------------------


def test_delete_function_tears_down_the_container_and_the_zip(sink, lambda_, stores, tmp_path):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.delete_function(FunctionName="fn1"))
    response = _answer(stores, req, substrate)
    assert response.status_code == 204
    assert substrate.deleted == [(ENV, "fn1")]
    assert not (tmp_path / ENV / "gateway" / "lambda" / "fn1.zip").exists()

    get_req = sink.call(lambda: lambda_.get_function(FunctionName="fn1"))
    assert _answer(stores, get_req, substrate).status_code == 404


def test_delete_function_unknown_name_is_not_found(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.delete_function(FunctionName="ghost"))
    response = _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 404


# --- UpdateFunctionCode / UpdateFunctionConfiguration --------------------------


def test_update_function_code_redeploys_and_updates_hash(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    block = threading.Event()
    substrate.block = block
    new_zip = b"PK\x03\x04updated-bytes"
    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=new_zip))
    parsed = _parse("UpdateFunctionCode", _answer(stores, req, substrate))
    assert parsed["CodeSize"] == len(new_zip)
    assert parsed["LastUpdateStatus"] == "InProgress"

    # State (Active) is untouched by a code update -- only LastUpdateStatus moves.
    block.set()
    result = _wait_for(stores, sink, lambda_, "fn1", "LastUpdateStatus", "Successful", substrate)
    assert result["State"] == "Active"
    assert len(substrate.ensured) == 2  # once at create, once at this redeploy


def test_concurrent_redeploys_do_not_corrupt_or_drop_each_others_fields(sink, lambda_, stores, tmp_path):
    """Release finding #3 -- lambdactl's `_finish_deploy` (background) vs a
    synchronous UpdateFunctionCode/UpdateFunctionConfiguration handler used
    to `get()` the function record, mutate it directly, then `set()` it
    back: a classic read-modify-write race. Many concurrent redeploys
    against the SAME function must all land (none silently dropped by a
    lost race), and the sidecar file must stay valid JSON throughout."""
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    errors: list[Exception] = []

    def redeploy_config(memory: int) -> None:
        try:
            req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", MemorySize=memory))
            response = _answer(stores, req, substrate)
            assert response.status_code == 200
        except Exception as exc:  # pragma: no cover - fails the test via errors list
            errors.append(exc)

    threads = [threading.Thread(target=redeploy_config, args=(128 * i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # None of the 8 concurrent redeploys was silently overwritten by another
    # -- each independently reached `_redeploy_response` and spawned its own
    # `_finish_deploy`. Those run on DAEMON threads, so this waits for all 9
    # (8 redeploys + the initial create) rather than assuming they've already
    # landed: the fake substrate's `ensure` is fast, not instantaneous, and
    # asserting a thread-count immediately after `join()`ing the REQUEST
    # threads is a race (found empirically once `_finish_deploy` grew one more
    # store write ahead of `ensure` -- W2.1's log-shipping cursor reset).
    deadline = time.monotonic() + 2.0
    while len(substrate.ensured) < 9 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(substrate.ensured) == 9
    # The sidecar itself was never left mid-write/corrupted by the hammering.
    sidecar = tmp_path / ENV / "gateway" / "lambdactl.json"
    json.loads(sidecar.read_text())  # raises if truncated/invalid


def test_update_function_configuration_changes_fields_without_touching_code(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", Timeout=30, MemorySize=256))
    parsed = _parse("UpdateFunctionConfiguration", _answer(stores, req, substrate))
    assert parsed["Timeout"] == 30
    assert parsed["MemorySize"] == 256
    # no NEW zip was extracted -- the redeploy reuses the existing code dir.
    assert len(substrate.extracted) == 1


# --- Invoke: the data plane ----------------------------------------------------


def test_invoke_forwards_the_payload_to_the_substrate(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(invoke_response=b'{"echo": true}')
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b'{"key": "value"}'))
    response = _answer(stores, req, substrate)
    assert response.status_code == 200
    assert response.body == b'{"echo": true}'
    assert substrate.invoked == [(ENV, "fn1", b'{"key": "value"}')]


def test_invoke_surfaces_function_error_header(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(invoke_response=b'{"errorType": "ValueError"}', invoke_error="Unhandled")
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    parsed = _parse("Invoke", _answer(stores, req, substrate))
    assert parsed["FunctionError"] == "Unhandled"


def test_invoke_before_active_is_not_ready(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(block=threading.Event())  # never released -- stays Pending
    _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    response = _answer(stores, req, substrate)
    assert response.status_code == 502
    parsed = _parse("Invoke", response, error=True)
    assert parsed["Error"]["Code"] == "ResourceNotReadyException"
    assert substrate.invoked == []


def test_invoke_unknown_function_is_not_found(sink, lambda_, stores):
    req = sink.call(lambda: lambda_.invoke(FunctionName="ghost", Payload=b"{}"))
    response = _answer(stores, req, FakeFunctionRuntime())
    assert response.status_code == 404


# --- W2.1 piece 3: Invoke ships the container tail into /aws/lambda/{fn} --------


_FN_LOG_GROUP = "/aws/lambda/fn1"
_FN_LOG_STREAM = "odin-lambda-default-fn1"  # compute/functions.py::container_name


def _invoke_once(stores, sink, lambda_, substrate) -> Response:
    req = sink.call(lambda: lambda_.invoke(FunctionName="fn1", Payload=b"{}"))
    return _answer(stores, req, substrate)


def _shipped(stores) -> list[dict]:
    return logsctl.stored_events(stores, ENV, _FN_LOG_GROUP, 100)


def test_invoke_ships_the_container_tail_into_the_function_log_group(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(log_text="START RequestId: abc\nhello from the handler\nEND\n")
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    assert _invoke_once(stores, sink, lambda_, substrate).status_code == 200

    events = _shipped(stores)
    assert [e["message"] for e in events] == ["START RequestId: abc", "hello from the handler", "END"]
    # One stream per real RIE container, named after the container itself
    # (lambdactl.py's `_ship_logs`: RIE reuses one container for every invoke,
    # so AWS's `{date}/[$LATEST]{requestId}` naming has nothing to key off).
    assert {e["stream"] for e in events} == {_FN_LOG_STREAM}
    # The group was auto-created by ingestion (logsctl deviation 1), so a
    # later real CreateLogGroup can still adopt it.
    assert logsctl.group_exists(stores, ENV, _FN_LOG_GROUP)


def test_second_invoke_of_an_unchanged_tail_ships_nothing_new(sink, lambda_, stores):
    """The dedup that makes shipping-per-invoke safe: `ingest_tail`'s cursor
    counts the lines of this container's output already ingested, so re-reading
    the same tail appends nothing -- only genuinely new lines land."""
    substrate = FakeFunctionRuntime(log_text="line one\nline two\n")
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    _invoke_once(stores, sink, lambda_, substrate)
    _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["line one", "line two"]

    # A THIRD invoke that actually printed something new ships only that line.
    substrate.log_text += "line three\n"
    _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["line one", "line two", "line three"]


def test_handler_error_invoke_still_ships_its_traceback(sink, lambda_, stores):
    """The error path's logs are the whole point -- a handler that raised
    returns a FunctionError header and its traceback lives only in the
    container, so shipping must happen on that path too."""
    substrate = FakeFunctionRuntime(
        invoke_response=b'{"errorType": "ValueError"}', invoke_error="Unhandled",
        log_text='Traceback (most recent call last):\nValueError: boom\n',
    )
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    parsed = _parse("Invoke", _invoke_once(stores, sink, lambda_, substrate))
    assert parsed["FunctionError"] == "Unhandled"
    assert "ValueError: boom" in "\n".join(e["message"] for e in _shipped(stores))


def test_a_redeploy_resets_the_cursor_so_the_new_containers_first_lines_still_ship(sink, lambda_, stores):
    """A redeploy REPLACES the RIE container, whose output starts back at line
    1 -- `_finish_deploy` forgets the stream's cursor (logsctl's
    `reset_cursor`), so the fresh container's first lines are ingested instead
    of being mistaken for already-seen ones. The events already stored stay
    put: this resets the read position, never the log."""
    substrate = FakeFunctionRuntime(log_text="old one\nold two\nold three\n")
    _create(stores, sink, lambda_, substrate)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["old one", "old two", "old three"]

    # The new container prints FEWER lines than the old cursor counted -- the
    # exact case a stranded cursor would swallow whole.
    substrate.log_text = "new one\n"
    req = sink.call(lambda: lambda_.update_function_code(FunctionName="fn1", ZipFile=b"PK\x03\x04new"))
    _parse("UpdateFunctionCode", _answer(stores, req, substrate))
    _wait_for(stores, sink, lambda_, "fn1", "LastUpdateStatus", "Successful", substrate)

    _invoke_once(stores, sink, lambda_, substrate)
    assert [e["message"] for e in _shipped(stores)] == ["old one", "old two", "old three", "new one"]


def test_invoke_before_active_never_reads_the_container_logs(sink, lambda_, stores):
    substrate = FakeFunctionRuntime(block=threading.Event(), log_text="nothing to see")
    _create(stores, sink, lambda_, substrate)
    assert _invoke_once(stores, sink, lambda_, substrate).status_code == 502
    assert substrate.log_reads == []
    assert _shipped(stores) == []


# --- ListVersionsByFunction / GetFunctionCodeSigningConfig ---------------------


def test_list_versions_by_function_returns_latest(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.list_versions_by_function(FunctionName="fn1"))
    parsed = _parse("ListVersionsByFunction", _answer(stores, req, substrate))
    (version,) = parsed["Versions"]
    assert version["Version"] == "$LATEST"


def test_get_function_code_signing_config(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    req = sink.call(lambda: lambda_.get_function_code_signing_config(FunctionName="fn1"))
    parsed = _parse("GetFunctionCodeSigningConfig", _answer(stores, req, substrate))
    assert parsed["FunctionName"] == "fn1"


# --- Tags -----------------------------------------------------------------------


def test_tag_resource_and_list_tags(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate)
    tag_req = sink.call(lambda: lambda_.tag_resource(
        Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1", Tags={"env": "prod"},
    ))
    assert _answer(stores, tag_req, substrate).status_code == 204

    list_req = sink.call(lambda: lambda_.list_tags(Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1"))
    parsed = _parse("ListTags", _answer(stores, list_req, substrate))
    assert parsed["Tags"] == {"env": "prod"}


def test_untag_resource_removes_the_key(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate, Tags={"env": "prod", "team": "x"})
    req = sink.call(lambda: lambda_.untag_resource(
        Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1", TagKeys=["env"],
    ))
    assert _answer(stores, req, substrate).status_code == 204

    list_req = sink.call(lambda: lambda_.list_tags(Resource="arn:aws:lambda:us-east-1:000000000000:function:fn1"))
    parsed = _parse("ListTags", _answer(stores, list_req, substrate))
    assert parsed["Tags"] == {"team": "x"}


# --- workload credential injection (keystore/gateway_port, fix-wave 2b) ---------


_AWS_INJECTED_KEYS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL", "AWS_DEFAULT_REGION"}


def test_create_with_odin_node_tag_injects_gateway_creds_into_the_container(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    env_vars = substrate.ensured[0][4]
    # `KeyStore.issue` is stable per (env, node): calling workload_env again
    # in the test reproduces the EXACT credentials the deploy must have used.
    expected = workload_env(keystore, ENV, "myfn", _GATEWAY_PORT)
    assert expected["AWS_ACCESS_KEY_ID"] == keystore.issue(ENV, "myfn")[0]
    assert env_vars == {"FOO": "bar", **expected}


def test_injected_creds_never_leak_into_the_configuration_response(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    active = _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    # The drift-safety guarantee: the response echoes EXACTLY what the user
    # configured -- the injected AWS_* vars live only in the container.
    assert active["Environment"]["Variables"] == {"FOO": "bar"}
    assert not _AWS_INJECTED_KEYS & set(active["Environment"]["Variables"])


def test_update_configuration_redeploy_reinjects_creds_from_persisted_tags(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    _create(
        stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT,
        Tags={"odin:node": "myfn"},
    )
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)

    # Tags are NOT resent on the update call -- they persist from creation.
    req = sink.call(lambda: lambda_.update_function_configuration(FunctionName="fn1", MemorySize=256))
    parsed = _parse(
        "UpdateFunctionConfiguration",
        _answer(stores, req, substrate, keystore, _GATEWAY_PORT),
    )
    assert parsed["LastUpdateStatus"] == "InProgress"
    _wait_for(stores, sink, lambda_, "fn1", "LastUpdateStatus", "Successful", substrate)

    env_vars = substrate.ensured[1][4]
    assert env_vars == workload_env(keystore, ENV, "myfn", _GATEWAY_PORT)


def test_create_without_keystore_behaves_exactly_as_before(sink, lambda_, stores):
    substrate = FakeFunctionRuntime()
    _create(
        stores, sink, lambda_, substrate,
        Tags={"odin:node": "myfn"}, Environment={"Variables": {"FOO": "bar"}},
    )
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert substrate.ensured[0][4] == {"FOO": "bar"}  # regression: no injection without a keystore


def test_create_without_odin_node_tag_deploys_with_no_injected_vars(sink, lambda_, stores, keystore):
    substrate = FakeFunctionRuntime()
    _create(stores, sink, lambda_, substrate, keystore=keystore, gateway_port=_GATEWAY_PORT)
    _wait_for_state(stores, sink, lambda_, "fn1", "Active", substrate)
    assert substrate.ensured[0][4] == {}  # no odin:node tag -> nothing to inject, deploy still lands
