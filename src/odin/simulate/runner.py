"""The tofu runner (S2): `tofu apply`/`plan`/`destroy` through odin's gateway
as a server capability, under a per-env OPERATOR principal (full allow within the
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

One tofu run at a time per env: a non-blocking `asyncio.Lock`
per env -- a call arriving while the lock is held raises `SimulateBusy`
immediately (never queues; the caller gets a clean 409, not a long wait).
Every invocation streams stdout/stderr line-by-line onto the events pipeline
as `{type: "tf", env, phase, line}` (phase: "init" | "apply" | "plan" | "destroy"),
then a terminal `{type: "tf", env, phase, status: "ok"|"failed", exit_code,
[tail]}` -- `tail` (the last `_TAIL_LINES` lines) is attached only on
failure, enough to show what broke without duplicating the whole log.

Timeouts: `init`/`apply` each get their own `ODIN_TOFU_TIMEOUT` budget;
`destroy` gets a smaller WHOLE-CALL deadline (`ODIN_TOFU_DESTROY_TIMEOUT`,
default 300s, init included) and, on blowing it, a tail line naming the one
cause a wedged destroy almost always has plus the documented recovery -- see
`_default_destroy_timeout` and `_WEDGED_DESTROY_HINT` (field test 2, B6).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from odin.agent.hcl import TfProject
from odin.simulate import workspace as workspace_mod
from odin.spec.models import scrub

_TOFU_INIT_ARGS = ("init", "-input=false")
_TOFU_APPLY_ARGS = ("apply", "-auto-approve", "-input=false", "-no-color")
_TOFU_DESTROY_ARGS = ("destroy", "-auto-approve", "-input=false", "-no-color")
# Field test 3: `-detailed-exitcode` is what makes a plan a CI-usable drift
# gate -- 0 no changes, 2 changes present, 1 a real error. No `-out`: writing
# a plan file would be a mutation, and a drift check must leave the workspace
# exactly as it found it.
_TOFU_PLAN_ARGS = ("plan", "-detailed-exitcode", "-input=false", "-no-color")
# ...which also means "exit 2" is a SUCCESSFUL run of tofu, not a failure --
# the phase's own success set, so the event stream never calls drift a crash.
_PLAN_OK_CODES = (0, 2)
_TAIL_LINES = 20

# A shared provider-plugin cache so `tofu init` only downloads
# hashicorp/aws once across every env's workspace (and across runs) --
# the brief's "tofu init -input=false (cached provider)".
PLUGIN_CACHE_DIR = Path.home() / ".cache" / "odin" / "tofu-plugin-cache"


def _default_tofu_timeout() -> float:
    """Release finding #3: a wedged apply has been observed running for
    hours with nothing to stop it. `ODIN_TOFU_TIMEOUT` (seconds) overrides
    the default for `init` and `apply` (each gets its own budget, not one
    shared across the whole call). `destroy` has its own, smaller budget --
    see `_default_destroy_timeout`."""
    return float(os.environ.get("ODIN_TOFU_TIMEOUT", "600"))


def _default_destroy_timeout() -> float:
    """Field test 2, finding B6: `odin destroy` on a RESTORED env was killed by
    hand at 8m26s of `tofu destroy` with no progress and no timeout.

    Nothing was broken about the existing bound -- 8m26s is 506s, comfortably
    under the 600s `ODIN_TOFU_TIMEOUT`, so the timeout simply had not fired yet;
    and because `_init_then` gives `init` its OWN full budget, the worst case
    for one `/destroy` was 20 minutes. Neither is a bound anyone waits out.

    A destroy against local substrates is fast when it works at all (real
    measurements: a 12-resource env in 63s, three EC2 VMs in 62s, the slowest
    single operation an `aws_db_instance` destroy at 1m1s), so 300s is generous
    for a working teardown and a fifth of the old worst case for a wedged one.
    It is a DEADLINE ACROSS THE WHOLE CALL (init included), not per phase.
    `ODIN_TOFU_DESTROY_TIMEOUT` overrides."""
    return float(os.environ.get("ODIN_TOFU_DESTROY_TIMEOUT", "300"))


# What a bounded-out destroy almost always means, and the documented recovery.
# The gateway answers every AWS call with a real 503/`ServiceUnavailable` when
# the env has no running backing container (`gateway/app.py`'s
# `backing-unavailable` branch), and aws-sdk-go-v2 treats that as retryable:
# ~25 attempts with exponential backoff PER CALL, none of which prints anything
# on tofu's stdout -- so the run looks like a silent hang. A restored env boots
# no containers (documented), and `/destroy` does not start them, which is
# exactly how the field test reproduced it.
_WEDGED_DESTROY_HINT = (
    "the usual cause is that this env's AWS backing containers are not running, so every "
    "AWS call the destroy makes gets a real ServiceUnavailable and the provider retries it "
    "~25 times with backoff (silently -- retries never reach tofu's output). A restored env "
    "boots no containers: run `odin apply --env <env>` first to start them, then destroy."
)


def _default_parallelism() -> int:
    """Owner directive B3: tofu's own default (`-parallelism=10`) means a
    big canvas fans out up to 10 heavy resource operations at once --
    EC2 boots, ECS convergence waits -- on top of whatever else Apply is
    already doing. `ODIN_TOFU_PARALLELISM` overrides; read fresh per
    `TfRunner` construction, same convention as `_default_tofu_timeout`."""
    return int(os.environ.get("ODIN_TOFU_PARALLELISM", "4"))


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

    def __init__(
        self, root: Path, ws=None, timeout: float | None = None, parallelism: int | None = None,
        destroy_timeout: float | None = None,
    ) -> None:
        self._root = root
        self._ws = ws
        self._timeout = timeout if timeout is not None else _default_tofu_timeout()
        # Finding B6: destroy gets its own, smaller, WHOLE-CALL deadline.
        self._destroy_timeout = (
            destroy_timeout if destroy_timeout is not None else _default_destroy_timeout()
        )
        # Owner directive B3: threaded onto every apply/destroy's args below
        # (never `init` -- `-parallelism` only governs a resource-graph walk).
        self._parallelism = parallelism if parallelism is not None else _default_parallelism()
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
            return self._record(env, await self._init_then(
                tofu, workspace, gateway_port, access_key, secret_key, env, "apply", self._apply_args(), secrets,
            ))

    async def plan(
        self, env: str, project: TfProject, gateway_port: int, access_key: str, secret_key: str,
        secrets: frozenset[str] = frozenset(),
    ) -> TfResult:
        """Field test 3: `tofu plan -detailed-exitcode` through EXACTLY the
        machinery `apply` uses -- same workspace, same injected
        `AWS_ENDPOINT_URL`, same per-env OPERATOR credentials, same per-phase
        timeout, same per-env lock, same secret scrubbing.

        That sameness is the whole point. `main.tf` is deliberately portable
        (real AWS Terraform: no `endpoints` block, no `127.0.0.1`), so a
        hand-run `tofu plan` in `.odin/<env>/tf` with the endpoint forgotten
        talks to REAL AWS -- a field engineer did exactly that and got a
        genuine `UnrecognizedClientException` back from Amazon. There is no
        way to get the endpoint wrong through here.

        Read-only: no `-out` plan file, no `wiring.stage`, no Stack commit,
        and NOT recorded on `status()`'s last-run cache (`_record` is
        deliberately not called) -- a drift check must never make the last
        real apply look like it went differently. tofu's own in-memory
        refresh is the only thing that touches state, and it is not persisted.
        """
        lock = self._lock(env)
        if lock.locked():
            raise SimulateBusy(env)
        tofu = _require_tofu()
        async with lock:
            workspace = workspace_mod.materialize(self._root, env, project)
            return await self._init_then(
                tofu, workspace, gateway_port, access_key, secret_key, env, "plan", self._plan_args(), secrets,
                ok_codes=_PLAN_OK_CODES,
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
                return self._record(env, TfResult(ok=True, exit_code=0))  # nothing was ever applied
            return self._record(env, await self._init_then(
                tofu, workspace, gateway_port, access_key, secret_key, env, "destroy", self._destroy_args(), secrets,
                budget=self._destroy_timeout, hint=_WEDGED_DESTROY_HINT,
            ))

    def _record(self, env: str, result: TfResult) -> TfResult:
        """The last-run cache `status()` reads. Only the two MUTATING phases
        record: a `plan` that reports drift must not overwrite what the last
        apply/destroy actually did."""
        self._last[env] = result
        return result

    def _apply_args(self) -> tuple[str, ...]:
        return (*_TOFU_APPLY_ARGS, f"-parallelism={self._parallelism}")

    def _destroy_args(self) -> tuple[str, ...]:
        return (*_TOFU_DESTROY_ARGS, f"-parallelism={self._parallelism}")

    def _plan_args(self) -> tuple[str, ...]:
        return (*_TOFU_PLAN_ARGS, f"-parallelism={self._parallelism}")

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
        budget: float | None = None, hint: str = "", ok_codes: tuple[int, ...] = (0,),
    ) -> TfResult:
        """`budget`, when given, is a deadline across BOTH phases (init + the
        real one) rather than a per-phase allowance -- finding B6: `init`
        getting its own full allowance doubled the worst case for one call.
        `None` keeps the per-phase `self._timeout` behavior apply relies on.

        `ok_codes` is the phase's success set -- `(0,)` everywhere except
        `plan`, whose `-detailed-exitcode` 2 means "changes present" on a run
        that worked perfectly. `init` always keeps the plain `(0,)`."""
        env_vars = _tf_env(gateway_port, access_key, secret_key)
        deadline = None if budget is None else time.monotonic() + budget
        init_result = await self._run(
            tofu, _TOFU_INIT_ARGS, workspace, env_vars, env, "init", secrets, self._remaining(deadline), hint,
        )
        if not init_result.ok:
            return init_result
        return await self._run(
            tofu, args, workspace, env_vars, env, phase, secrets, self._remaining(deadline), hint, ok_codes,
        )

    def _remaining(self, deadline: float | None) -> float:
        """Never zero or negative: a deadline already blown still gets a token
        slice, so the phase runs, is killed, and reports honestly -- rather than
        `wait_for` raising before the subprocess even starts."""
        if deadline is None:
            return self._timeout
        return max(1.0, deadline - time.monotonic())

    async def _run(
        self, tofu: str, args: tuple[str, ...], cwd: Path, env_vars: dict[str, str], env: str, phase: str,
        secrets: frozenset[str] = frozenset(), timeout: float | None = None, hint: str = "",
        ok_codes: tuple[int, ...] = (0,),
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

        budget = self._timeout if timeout is None else timeout
        try:
            code = await asyncio.wait_for(_drain(), timeout=budget)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            code = await proc.wait()
            tail.append(f"tofu {phase} timed out after {budget:.0f}s -- process killed")
            # Finding B6: a bound with no explanation is still an opaque
            # failure. `hint` names the one cause this almost always is, and the
            # documented recovery, on the same tail the CLI/UI already print.
            if hint:
                tail.append(hint)

        ok = code in ok_codes
        payload = {"type": "tf", "env": env, "phase": phase, "status": "ok" if ok else "failed", "exit_code": code}
        if not ok:
            payload["tail"] = list(tail)
        await self._emit(payload)
        return TfResult(ok=ok, exit_code=code, tail=tuple(tail))

    async def _emit(self, message: dict) -> None:
        if self._ws is not None:
            await self._ws.broadcast(message)
