"""FunctionRuntime -- the substrate binding for gateway/models/lambdactl.py's
Lambda functions: a REAL RIE (Runtime Interface Emulator) container per
function, on the SAME `RuntimeDriver` protocol every other odin workload
uses (NORTHSTAR directive 5: Lambda -> RIE, Apache-2.0, built into AWS's own
`public.ecr.aws/lambda/*` base images -- verified by research-coverage.md's
Q1 license/availability check).

Shape mirrors `compute/instances.py::InstanceVm` (V3b), not `aws/backings.py`:
this is a MANY-per-resource binding (one container per function, like one VM
per EC2 instance), not aws/backings.py's one-shared-container-per-env-per-
-kind shape. Container naming: `odin-lambda-{env}-{function_name}` --
the ONLY name this module ever passes to the runtime driver.

Readiness (the brief's "REAL readiness, not a timer"): after `run_container`,
`ensure()` polls a raw TCP connect to the RIE's published port -- NOT a
synthetic warm-up POST to the invoke path. RIE's invoke route dispatches
straight into the function's own handler code on ANY request that reaches
it (there is no separate lightweight health endpoint), so a warm-up "ping"
would actually RUN the user's function once as a side effect of state
polling -- for arbitrary pasted code that's a real behavior change (e.g. a
handler that sends something), not a health check. A TCP-connect probe (the
exact technique `gateway/app.py::_wait_until_serving` already uses for
uvicorn) proves the RIE process is up and accepting connections without
executing a single line of the function -- for RIE's tiny static-binary
HTTP server, socket-accept and route-readiness are effectively the same
moment, so this is a real signal, not a looser approximation.

Code layout: CreateFunction's zip is extracted to a host directory under
`.odin/{env}/gateway/lambda/{name}-code/` (the Colima "$HOME only" mount
rule `aws/backings.py` documents -- `.odin` already lives under the repo
checkout, itself under $HOME) and bind-mounted at `/var/task`, matching AWS's
own local-testing convention for these base images
(`docker run -v $(pwd):/var/task ... public.ecr.aws/lambda/python:3.12
<handler>`) -- the handler string becomes the container's CMD override, no
`_HANDLER` env var needed (the base image's own bootstrap reads argv[0]).
"""
from __future__ import annotations

import io
import json
import shutil
import socket
import asyncio
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from odin.runtime.colima import ColimaRuntime, ContainerSpec
from odin.util import private_mkdir

# AWS's own base images (Apache-2.0 RIE built in, research-verified). Only
# the runtimes odin's HCL layer offers on the canvas (agent/hcl.py's
# `_LAMBDA_RUNTIME_ENTRY`) are mapped; an unrecognized runtime string falls
# back to the default rather than failing closed -- ImageId's own "accepted
# verbatim, never validated" precedent (gateway/models/ec2compute.py).
RUNTIME_IMAGES: dict[str, str] = {
    "python3.12": "public.ecr.aws/lambda/python:3.12",
    "python3.13": "public.ecr.aws/lambda/python:3.13",
    "nodejs20.x": "public.ecr.aws/lambda/nodejs:20",
    "nodejs22.x": "public.ecr.aws/lambda/nodejs:22",
}
DEFAULT_RUNTIME = "python3.12"

_RIE_PORT = 8080
READY_TIMEOUT = 180.0  # a cold `public.ecr.aws/lambda/*` pull is a real multi-hundred-MB fetch
_INVOKE_PATH = "/2015-03-31/functions/function/invocations"


def container_name(env: str, function_name: str) -> str:
    return f"odin-lambda-{env}-{function_name}"


@dataclass(frozen=True)
class InvokeResult:
    payload: bytes
    function_error: str | None  # the X-Amz-Function-Error value, or None


# Field test 3: `aws lambda invoke` on a function whose handler RAISES came
# back `StatusCode: 200` with no `FunctionError` -- the documented AWS way to
# detect a failed invoke -- so a CI job scored a crashing function as a
# success. Root cause, verified against a real
# `public.ecr.aws/lambda/python:3.12` container: RIE does NOT send
# `X-Amz-Function-Error`. A raised handler answers `200 OK` with body
# `{"errorMessage", "errorType", "requestId", "stackTrace"}` and no such
# header; an import failure or a runtime exit answers `502` with the same
# shape. Reading only the header therefore reported EVERY invocation as
# clean -- including for `last_invocation_error`, the field v0.7.1 added for
# the World verdict, which is fed from this same value and so was also always
# None. One signal, read off the response RIE actually sends.
_ERROR_PAYLOAD_KEYS = ("errorType", "errorMessage")

# Real Lambda's two values are `Handled` and `Unhandled` (who noticed the
# error: the function's runtime, or Lambda itself). RIE collapses that
# distinction -- it reports every failure the same way, in the body, with no
# out-of-band marker -- so odin reports the one an uncaught handler exception
# gets on real AWS rather than inventing a difference it cannot observe.
_UNHANDLED = "Unhandled"


def _payload_object(payload: bytes) -> dict:
    """The response body as a JSON object, or `{}` for anything else -- a
    handler may legitimately return a list, a bare scalar, or raw bytes."""
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _function_error(response: httpx.Response) -> str | None:
    """`Unhandled` when this invocation failed, else None.

    Failed means either RIE refused the invoke outright (any non-200: an
    init failure, a runtime exit) or it answered 200 with the runtime's own
    error document -- BOTH of `errorType`/`errorMessage`, so a handler that
    merely returns something error-shaped isn't accused of crashing. The
    header is still honored first, in case a future RIE starts sending it.
    """
    failed = response.status_code != 200 or all(
        key in _payload_object(response.content) for key in _ERROR_PAYLOAD_KEYS
    )
    return response.headers.get("X-Amz-Function-Error") or (_UNHANDLED if failed else None)


class FunctionRuntime:
    """Per-function RIE container lifecycle, on an injectable `RuntimeDriver`
    (the same seam `aws/backings.py`/`InstanceVm` use, so a test can inject a
    fake runtime with no real Docker involved)."""

    def __init__(
        self, runtime=None, root: Path = Path(".odin"),
        ready_timeout: float = READY_TIMEOUT, poll_interval: float = 0.5,
    ) -> None:
        # `runtime` defaults lazily (like ec2compute.py's `vm or InstanceVm()`)
        # so gateway/models/lambdactl.py's `pure_answer` can construct a real
        # one per-call with no shared state -- `root` still must be threaded
        # explicitly (it's `stores.root`, known only at call time).
        # `ready_timeout`/`poll_interval` are constructor knobs purely for
        # testability (InstanceVm's own `poll_interval` precedent) -- real
        # callers keep the module defaults.
        self._rt = runtime or ColimaRuntime()
        self._root = root
        self._ready_timeout = ready_timeout
        self._poll_interval = poll_interval

    def code_dir(self, env: str, function_name: str) -> Path:
        return self._root / env / "gateway" / "lambda" / f"{function_name}-code"

    def extract_code(self, env: str, function_name: str, zip_bytes: bytes) -> Path:
        """Wipe + re-populate the function's code directory from `zip_bytes`
        -- called on both CreateFunction and UpdateFunctionCode, so a stale
        file from a PREVIOUS deployment can never survive alongside a new
        one (the zip is the whole desired contents of /var/task, not a
        patch)."""
        code_dir = self.code_dir(env, function_name)
        shutil.rmtree(code_dir, ignore_errors=True)
        private_mkdir(code_dir)  # under .odin/<env>/gateway — 0700 like the rest
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            archive.extractall(code_dir)
        return code_dir

    async def ensure(
        self, env: str, function_name: str, runtime: str, handler: str,
        env_vars: dict[str, str], code_dir: Path, memory_mib: int | None = None,
    ) -> int:
        """(Re)create the function's container from `code_dir` and block
        until its RIE answers -- the caller (lambdactl.py's background
        thread) is the one already off the request path. Returns the host
        port RIE published on. Raises on a boot/readiness failure; the
        caller turns that into the function's terminal `Failed` state,
        never a silent hang (same contract as `InstanceVm.boot`).

        `memory_mib` (owner directive B4): the function's own real
        `MemorySize` (lambdactl.py always sets one -- `_DEFAULT_MEMORY`, 128,
        when CreateFunction didn't) -- capped onto the REAL container so a
        runaway handler can't eat the host; `None` (a caller that predates
        this) leaves the container unbounded, same as before."""
        name = container_name(env, function_name)
        await self._rt.stop(name)  # clear any exited remnant (UpdateFunctionCode redeploy, or a stale prior run)
        image = RUNTIME_IMAGES.get(runtime, RUNTIME_IMAGES[DEFAULT_RUNTIME])
        await self._rt.run_container(ContainerSpec(
            name=name, image=image, env=dict(env_vars),
            ports={_RIE_PORT: 0},
            labels={"odin-env": env, "odin-lambda-fn": function_name},
            command=(handler,) if handler else (),
            volumes={str(code_dir): "/var/task"},
            memory_mib=float(memory_mib) if memory_mib else None,
        ))
        return await self._await_ready(name)

    async def _await_ready(self, name: str) -> int:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            port = await self._rt.host_port(name, _RIE_PORT)
            if port and _tcp_open(port):
                return port
            await asyncio.sleep(self._poll_interval)
        raise RuntimeError(f"{name} RIE never became ready: {await self._not_ready_reason(name)}")

    async def _not_ready_reason(self, name: str) -> str:
        """WHY the wait ended, in a form that is never empty.

        This message used to be `f"{name} RIE never became ready:\\n{logs}"`,
        and `_ContainerRuntime.logs` answers `""` both for a container that
        wrote nothing and for one the runtime could not read -- so the whole
        explanation was a dangling colon and a blank line. Measured against a
        REAL container (`docker run -d --name <fn> alpine sleep 300`, nothing
        published on 8080, nothing logged), driving this real method to a real
        timeout, it rendered exactly:

            'odin-lambda-p1bprobe3-quiet RIE never became ready:\\n'

        ...while `status` said `running` and `host_port` said `0` at that same
        instant. Both were free to read and both were thrown away, which is the
        actual defect: the logs were never the only witness, just the only one
        anybody asked. So the reason now leads with the two readings that are
        ALWAYS available -- how long odin waited, and what the container was
        doing when it gave up -- and treats the log tail as the bonus it is.

        The exit code is reported only for a container that is NOT running: a
        live container's `{{.State.ExitCode}}` is `0` (verified on the same
        probe), and "exit code 0" printed under a failure is the kind of true-
        looking detail that sends a reader down the wrong path."""
        status = await self._rt.status(name)
        state = status if status == "running" else f"{status}, exit code {await self._rt.exit_code(name)}"
        logs = await self._rt.logs(name)
        tail = f"Its logs:\n{logs}" if logs else (
            "It has logged nothing, so the container state above is the whole of it."
        )
        return (
            f"nothing accepted a TCP connection on its published port {_RIE_PORT} "
            f"within {self._ready_timeout:g}s. Container: {state}. {tail}"
        )

    async def invoke(self, env: str, function_name: str, payload: bytes, timeout: float = 30.0) -> InvokeResult:
        """The data plane: forward `payload` bytes straight to the
        function's own RIE container and hand back its response bytes
        verbatim, plus the FunctionError if the handler raised (see
        `_function_error` -- RIE sends no header, so it is read off the
        response) -- the gateway's Invoke handler is a pure pass-through of
        both."""
        port = await self._rt.host_port(container_name(env, function_name), _RIE_PORT)
        if not port:
            raise RuntimeError(f"{container_name(env, function_name)} is not running")
        response = httpx.post(f"http://127.0.0.1:{port}{_INVOKE_PATH}", content=payload, timeout=timeout)
        return InvokeResult(payload=response.content, function_error=_function_error(response))

    async def delete(self, env: str, function_name: str) -> None:
        await self._rt.stop(container_name(env, function_name))
        shutil.rmtree(self.code_dir(env, function_name), ignore_errors=True)

    async def status(self, env: str, function_name: str) -> str:
        return await self._rt.status(container_name(env, function_name))

    async def logs(self, env: str, function_name: str, tail: int = 20) -> str:
        """The RIE container's own log tail -- the function's stdout/stderr,
        which is what `gateway/models/lambdactl.py` ships into
        `/aws/lambda/{name}` after every Invoke. Never raises: the driver's
        `logs` is a `check=False` CLI call, so a container that's gone
        answers with "" (`_ContainerRuntime.logs`'s contract)."""
        return await self._rt.logs(container_name(env, function_name), tail)


def _tcp_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0
