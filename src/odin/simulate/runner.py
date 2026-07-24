"""The tofu runner (S2): `tofu apply`/`destroy` through odin's gateway as a
server capability, under a per-env OPERATOR principal (full allow within the
env, no canvas edge required -- see `gateway/app.py`'s
`GatewayState.statements_for` special-case and `gateway/keys.OPERATOR_NODE_ID`).

Boundary with the reconciler (S-plan task S2, Global Constraints): canvas
Apply still provisions backings via the Reconciler (today's path); Simulate
provisions the SAME backings via tofu -> gateway. Both converge on the
identical per-env RustFS/goaws/dynalite containers -- there is no separate
teardown path to keep in sync; odin's existing `/destroy?env=` (which tears
the reconciler's backing containers down wholesale) removes whatever a tofu
apply created too. `/tf/destroy` is tofu's OWN destroy against its
last-applied state, independent of the canvas.

One `tofu apply`/`destroy` at a time per env: a non-blocking `asyncio.Lock`
per env -- a call arriving while the lock is held raises `SimulateBusy`
immediately (never queues; the caller gets a clean 409, not a long wait).
Every invocation streams stdout/stderr line-by-line onto the events pipeline
as `{type: "tf", env, phase, line}` (phase: "init" | "apply" | "destroy"),
then a terminal `{type: "tf", env, phase, status: "ok"|"failed", exit_code,
[tail]}` -- `tail` (the last `_TAIL_LINES` lines) is attached only on
failure, enough to show what broke without duplicating the whole log.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from odin.agent.hcl import TfProject
from odin.simulate import workspace as workspace_mod
from odin.spec.models import scrub

_TOFU_INIT_ARGS = ("init", "-input=false")
_TOFU_APPLY_ARGS = ("apply", "-auto-approve", "-input=false", "-no-color")
_TOFU_DESTROY_ARGS = ("destroy", "-auto-approve", "-input=false", "-no-color")
_TAIL_LINES = 20

# A shared provider-plugin cache so `tofu init` only downloads
# hashicorp/aws once across every env's workspace (and across runs) --
# the brief's "tofu init -input=false (cached provider)".
PLUGIN_CACHE_DIR = Path.home() / ".cache" / "odin" / "tofu-plugin-cache"


def _default_tofu_timeout() -> float:
    """Release finding #3: a wedged apply has been observed running for
    hours with nothing to stop it. `ODIN_TOFU_TIMEOUT` (seconds) overrides
    the default for every phase (init/apply/destroy each get their own
    budget, not one shared across the whole call)."""
    return float(os.environ.get("ODIN_TOFU_TIMEOUT", "600"))


class TofuNotInstalled(Exception):
    """`tofu` isn't on PATH -- the runner's own preflight (there's no `odin
    doctor` yet; the runner checks itself and the route turns this into a
    clean 409)."""


class SimulateBusy(Exception):
    """A tofu run is already in flight for this env (the per-env lock)."""

    def __init__(self, env: str) -> None:
        super().__init__(f"a tofu run is already in progress for env {env!r}")
        self.env = env


@dataclass(frozen=True)
class TfResult:
    ok: bool
    exit_code: int
    tail: tuple[str, ...] = ()


def _require_tofu() -> str:
    tofu = shutil.which("tofu")
    if tofu is None:
        raise TofuNotInstalled()
    return tofu


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


class TfRunner:
    """Materializes + drives tofu for one odin instance; owns the per-env
    concurrency lock and the last-result cache `status()` reads. `ws` is a
    `ConnectionManager` (or anything with an async `broadcast(dict)`) --
    optional so unit tests can construct a runner with no event sink."""

    def __init__(self, root: Path, ws=None, timeout: float | None = None) -> None:
        self._root = root
        self._ws = ws
        self._timeout = timeout if timeout is not None else _default_tofu_timeout()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, TfResult] = {}

    def _lock(self, env: str) -> asyncio.Lock:
        return self._locks.setdefault(env, asyncio.Lock())

    async def apply(
        self, env: str, project: TfProject, gateway_port: int, access_key: str, secret_key: str,
        secrets: frozenset[str] = frozenset(),
    ) -> TfResult:
        lock = self._lock(env)
        if lock.locked():
            raise SimulateBusy(env)
        tofu = _require_tofu()
        async with lock:
            workspace = workspace_mod.materialize(self._root, env, project)
            return await self._init_then(
                tofu, workspace, gateway_port, access_key, secret_key, env, "apply", _TOFU_APPLY_ARGS, secrets,
            )

    async def destroy(
        self, env: str, gateway_port: int, access_key: str, secret_key: str,
        secrets: frozenset[str] = frozenset(),
    ) -> TfResult:
        lock = self._lock(env)
        if lock.locked():
            raise SimulateBusy(env)
        tofu = _require_tofu()
        async with lock:
            workspace = workspace_mod.tf_dir(self._root, env)
            if not workspace.exists():
                result = TfResult(ok=True, exit_code=0)  # nothing was ever applied
                self._last[env] = result
                return result
            return await self._init_then(
                tofu, workspace, gateway_port, access_key, secret_key, env, "destroy", _TOFU_DESTROY_ARGS, secrets,
            )

    def status(self, env: str) -> dict:
        last = self._last.get(env)
        return {
            "env": env,
            "running": self._lock(env).locked(),
            "workspace_exists": workspace_mod.tf_dir(self._root, env).exists(),
            "last": None if last is None else {"ok": last.ok, "exit_code": last.exit_code, "tail": list(last.tail)},
        }

    async def _init_then(
        self, tofu: str, workspace: Path, gateway_port: int, access_key: str, secret_key: str,
        env: str, phase: str, args: tuple[str, ...], secrets: frozenset[str] = frozenset(),
    ) -> TfResult:
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        init_result = await self._run(tofu, _TOFU_INIT_ARGS, workspace, env_vars, env, "init", secrets)
        if not init_result.ok:
            self._last[env] = init_result
            return init_result
        result = await self._run(tofu, args, workspace, env_vars, env, phase, secrets)
        self._last[env] = result
        return result

    async def _run(
        self, tofu: str, args: tuple[str, ...], cwd: Path, env_vars: dict[str, str], env: str, phase: str,
        secrets: frozenset[str] = frozenset(),
    ) -> TfResult:
        # `start_new_session=True` (release finding #3): tofu spawns its own
        # provider-plugin child process, so a plain kill of tofu's own pid on
        # timeout would still leave that child running. Killing the whole
        # process GROUP (tofu is its leader) reaps both.
        proc = await asyncio.create_subprocess_exec(
            tofu, *args, cwd=cwd, env=env_vars,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        tail: deque[str] = deque(maxlen=_TAIL_LINES)

        async def _drain() -> int:
            async for raw_line in proc.stdout:
                # Security finding #3: tofu's own plan/apply diff can print a
                # resource argument's ACTUAL value (it has no concept of odin's
                # `sensitive` flag) -- scrub known secrets out of every line
                # before it reaches the tail, the WS event stream, or events.jsonl.
                line = scrub(raw_line.decode(errors="replace").rstrip("\n"), secrets)
                tail.append(line)
                await self._emit({"type": "tf", "env": env, "phase": phase, "line": line})
            return await proc.wait()

        try:
            code = await asyncio.wait_for(_drain(), timeout=self._timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            code = await proc.wait()
            tail.append(f"tofu {phase} timed out after {self._timeout:.0f}s -- process killed")

        ok = code == 0
        payload = {"type": "tf", "env": env, "phase": phase, "status": "ok" if ok else "failed", "exit_code": code}
        if not ok:
            payload["tail"] = list(tail)
        await self._emit(payload)
        return TfResult(ok=ok, exit_code=code, tail=tuple(tail))

    async def _emit(self, message: dict) -> None:
        if self._ws is not None:
            await self._ws.broadcast(message)
