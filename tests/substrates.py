"""The stand-ins that make `create_app` genuinely hermetic, plus the recorder
that PROVES it.

`tests/volumes.py`'s neighbour, and here for the same reason: these are shared
by a dozen test modules, and a fixture living in one test file that eleven
others import makes the import graph a chain of unrelated collections rather
than a fixture library.

## Why four seams and not one

`create_app` has four substrate arguments because odin has four kinds of
substrate, and they are not interchangeable:

  `runtime=`  a `RuntimeDriver` -- containers. Covers ECS tasks, Lambda RIE
              containers, the rds Postgres, the AWS backings, the drift sweep.
              `tests/api/test_apply_full.py::FakeRuntime` is the stand-in.
  `rds=`      a `PostgresRds`. Derived from `runtime` in production, injectable
              because the gateway's RDS model and `/apply-full`'s converge pass
              must be given the SAME one.
  `vm=`       an `InstanceVm` -- Lima. NOT derivable from `runtime`:
              `RuntimeDriver` is a container API (`run_container`/`facts`/
              `logs`) and a Lima VM is not a container, so there is nothing for
              a runtime fake to bind here. `NoVm` below.
  `runner=`   a `TfRunner` -- the `tofu` BINARY. Also not derivable, and for a
              sharper reason: tofu is not a substrate odin drives at all, it is
              a program on PATH that drives odin. `NoTofu` below.

Handing a test only the first of those and calling it isolated is exactly the
defect these exist to close (see `server.py::Substrates`).
"""
from __future__ import annotations

import asyncio.base_events
import subprocess
from pathlib import Path

from odin.simulate.runner import TfResult

# Every binary odin shells out to that means a REAL machine was touched.
# Spelled out in full rather than derived from any odin module (honesty rule 5):
# a list the subject can reach is a list the subject can shrink.
MACHINE_BINARIES = frozenset({
    "docker", "nerdctl", "limactl", "lima", "colima", "tofu", "terraform",
    "nebula", "nebula-cert", "ssh-keygen", "ssh",
})


class SpawnRecorder:
    """Every process born inside the `with` block, by argv.

    Hooks the two lowest points in CPython at which a process can be born, so
    nothing slips past by importing differently:

        subprocess.Popen.__init__              every sync spawn
        BaseEventLoop.subprocess_exec          every create_subprocess_exec

    That depth is deliberate. A check written against `odin.util.run_command`
    would share a source with its subject -- a new call site reaching for
    `asyncio` directly would slip past it, and the guard would report the
    silence as isolation (honesty rule 5). CPython's own spawn points are
    somewhere the subject cannot reach.

    Both hooks record BEFORE the exec, so a spawn that fails with
    `FileNotFoundError` has still been seen -- which is what lets a test prove
    the recorder fires without running the very binaries it is policing."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __enter__(self) -> SpawnRecorder:
        self._popen = subprocess.Popen.__init__
        self._exec = asyncio.base_events.BaseEventLoop.subprocess_exec
        recorder = self

        def popen_init(proc, args, *a, **kw):
            recorder._record(args)
            return recorder._popen(proc, args, *a, **kw)

        def subprocess_exec(loop, protocol_factory, program, *args, **kw):
            recorder._record([program, *args])
            return recorder._exec(loop, protocol_factory, program, *args, **kw)

        subprocess.Popen.__init__ = popen_init
        asyncio.base_events.BaseEventLoop.subprocess_exec = subprocess_exec
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._popen
        asyncio.base_events.BaseEventLoop.subprocess_exec = self._exec

    def _record(self, args) -> None:
        argv = [args] if isinstance(args, (str, bytes, Path)) else list(args)
        self.argv.append([str(a) for a in argv])

    @property
    def machine_calls(self) -> list[str]:
        """The spawns that prove a real machine was reached.

        Reads the WORDS of each argv rather than only its head, because
        `shell=True` puts the whole command line in `argv[0]`: `sh -c 'docker
        rm -f ...'` is a docker call and must not read as a clean run. Matching
        is on the BASENAME, so `/opt/homebrew/bin/tofu` and `/nonexistent/docker`
        both count."""
        return sorted({
            " ".join(argv) for argv in self.argv
            if MACHINE_BINARIES & {word.split("/")[-1] for word in " ".join(argv).split()}
        })


class NoVm:
    """An `InstanceVm` stand-in: the limactl surface `/apply-full` and
    `/destroy` reach, answering what a machine with none of these VMs answers.

    `refresh_nebula` and `push_hosts` answer `unchanged` because that is the
    real no-churn answer for a VM whose compiled rules and record set have not
    moved -- the case both passes are written to make cheap. Deliberately NOT
    `failed`: `ensure_instance_mesh` RAISES on a failed VM, so a stand-in that
    failed would make a test assert isolation by way of an exception and prove
    nothing about the passes it is supposed to run through.

    `disks()` answering `[]` is likewise the honest answer, not a shortcut: a
    machine that never created a Lima disk for this env has none to list. It is
    also what the REAL `limactl disk list` returned in every test that used to
    call it -- the difference is that this answer does not depend on what the
    developer happens to have on their machine."""

    async def refresh_nebula(self, name, join):
        return "unchanged"

    async def push_hosts(self, name, root, env, host_id, resolved):
        return "unchanged"

    async def delete(self, name):
        return None

    async def exists(self, name):
        return False

    async def disks(self, check=False):
        return []

    async def delete_disk(self, name):
        return None


class NoTofu:
    """A `TfRunner` stand-in: the whole tofu surface `/apply-full` and
    `/destroy` touch, answering exactly what a clean run answers.

    `applied` is recorded so a test can assert the tofu half really RAN -- an
    isolation claim over a route that refused early would prove nothing, and
    this is the in-band witness that it did not."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.destroyed: list[str] = []

    def status(self, env: str) -> dict:
        return {"running": False, "workspace_exists": False, "last": None}

    async def apply(self, env, project, gateway_port, access_key, secret_key, secrets=frozenset()):
        self.applied.append(env)
        return TfResult(ok=True, exit_code=0, tail=())

    async def destroy(self, env, gateway_port, access_key, secret_key):
        self.destroyed.append(env)
        return TfResult(ok=True, exit_code=0, tail=())
