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
import shutil
import socket
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from odin.runtime.colima import ColimaRuntime, ContainerSpec

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
    function_error: str | None  # the X-Amz-Function-Error header value, or None


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
        code_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            archive.extractall(code_dir)
        return code_dir

    def ensure(
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
        self._rt.stop(name)  # clear any exited remnant (UpdateFunctionCode redeploy, or a stale prior run)
        image = RUNTIME_IMAGES.get(runtime, RUNTIME_IMAGES[DEFAULT_RUNTIME])
        self._rt.run_container(ContainerSpec(
            name=name, image=image, env=dict(env_vars),
            ports={_RIE_PORT: 0},
            labels={"odin-env": env, "odin-lambda-fn": function_name},
            command=(handler,) if handler else (),
            volumes={str(code_dir): "/var/task"},
            memory_mib=float(memory_mib) if memory_mib else None,
        ))
        return self._await_ready(name)

    def _await_ready(self, name: str) -> int:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            port = self._rt.host_port(name, _RIE_PORT)
            if port and _tcp_open(port):
                return port
            time.sleep(self._poll_interval)
        raise RuntimeError(f"{name} RIE never became ready:\n{self._rt.logs(name)}")

    def invoke(self, env: str, function_name: str, payload: bytes, timeout: float = 30.0) -> InvokeResult:
        """The data plane: forward `payload` bytes straight to the
        function's own RIE container and hand back its response bytes
        verbatim, plus the FunctionError header if the handler raised --
        the gateway's Invoke handler is a pure pass-through of both."""
        port = self._rt.host_port(container_name(env, function_name), _RIE_PORT)
        if not port:
            raise RuntimeError(f"{container_name(env, function_name)} is not running")
        response = httpx.post(f"http://127.0.0.1:{port}{_INVOKE_PATH}", content=payload, timeout=timeout)
        return InvokeResult(payload=response.content, function_error=response.headers.get("X-Amz-Function-Error"))

    def delete(self, env: str, function_name: str) -> None:
        self._rt.stop(container_name(env, function_name))
        shutil.rmtree(self.code_dir(env, function_name), ignore_errors=True)

    def status(self, env: str, function_name: str) -> str:
        return self._rt.status(container_name(env, function_name))


def _tcp_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0
