"""Container runtimes: a shared base + the default Colima (`docker`) driver.

For the walking skeleton the Runtime driver's "host" is the local Colima
container engine (run containers directly), fast and real. `LimaRuntime`
(runtime/lima.py) runs the same containers inside a Lima VM for isolation,
reusing the `_ContainerRuntime` base here — they differ only in the CLI seam
(`docker` vs `nerdctl`-in-VM) and Colima's host-gateway run flag.

Every container odin runs is labelled ``odin=1``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from odin.spec.models import Phase
from odin.util import run_command_async

LABEL = "odin"
# The label that says WHICH ENVIRONMENT a named volume belongs to, and the only
# thing any reclaim is allowed to key on.
#
# A volume's NAME (`odin-rds-{env}-{db_id}-data`) cannot answer that question:
# both halves may contain `-`, so `odin-rds-conn2-app-db-data` is env `conn2`
# database `app-db` AND env `conn2-app` database `db`, with nothing in the string
# to choose between them. Odin already carries one documented residual of exactly
# that shape (`docs/limits.md`: a `-`-suffix env collision makes `odin env rm`
# refuse), and there the wrong answer merely refuses. Here the wrong answer
# DELETES A DATABASE, so a name is not good enough. The label is set by whoever
# creates the volume, is matched by `--filter label=...` (the same mechanism
# `volume_names` already uses for `odin=1`), and is exact.
ENV_LABEL = f"{LABEL}.env"


class PortUnreadable(RuntimeError):
    """`host_port` could not READ the container's published-port map at all --
    no daemon, no CLI on PATH, no such container. Distinct from reading it and
    finding nothing published on that inside-port, which is a real answer and
    still returns 0.

    Field test 5's facts audit: `host_port` used to end
    `return int(...) if out else 0`, so ANY CLI failure produced 0 -- a value
    shaped exactly like a real port. `BackingAws.facts` interpolates it, so one
    transient `docker` hiccup wrote `http://host.docker.internal:0` into the
    `endpoint` fact of an s3/sqs/sns/dynamodb node, PERMANENTLY: that fact is
    published only on the starting->healthy transition and is never refreshed.
    A port of 0 is not a port, it is "I failed to ask" -- so failing to ask
    now raises instead of masquerading (honesty rule 1)."""

# The host as seen from inside containers: Colima's host-gateway alias (wired
# by `_run_flags`). Producers publish this instead of localhost so a consumer
# container can dial host-published ports verbatim.
CONTAINER_HOST = "host.docker.internal"


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    ports: dict[int, int] = field(default_factory=dict)  # container_port -> host_port
    labels: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    # source -> container_path, where a source is EITHER a host path (the
    # nebula config dir, a Lambda's code dir) or the NAME of a named volume
    # (`aws/rds.py`'s per-database data volume). `docker`/`nerdctl` decide
    # between the two by the same rule: a source containing a `/` is a bind
    # mount, anything else is a volume. That distinction is what makes an rds
    # container replaceable without losing its data -- see `stop` below.
    volumes: dict[str, str] = field(default_factory=dict)
    # Owner directive B4: a runaway container (a bad ECS image, a Lambda
    # handler gone wild) can't eat the host -- None emits no docker flag at
    # all (unbounded, today's behavior); the binding layer that owns each
    # workload kind's policy (compute/tasks.py, compute/functions.py) decides
    # what to pass, including its own default when the source (a taskdef, a
    # function's MemorySize) sets none.
    memory_mib: float | None = None
    cpus: float | None = None
    # W2.6 (fabric/sidecar.py): a nebula companion container joins its
    # BACKING's network namespace (`network="container:<name>"`), which is
    # what puts the overlay tun device inside the backing's namespace so an
    # unmodified upstream image answers on the mesh. `cap_add`/`devices` are
    # what nebula needs to create that tun -- container capabilities from the
    # container runtime, never host root (see sidecar.py's docstring).
    network: str | None = None
    cap_add: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    # route53: extra name -> IPv4 entries for this container's /etc/hosts,
    # emitted as `--add-host name:ip`. This is how a DRAWN DNS record becomes
    # real resolution for a container consumer -- `getent hosts api.internal`
    # answers, because docker really wrote the line.
    #
    # A dict keyed by name, so one name cannot appear twice with two addresses
    # (the argv would carry both and the resolver would silently pick one).
    # Emitted in SORTED order for a deterministic argv: the set comes from a
    # store whose iteration order is not meaningful, and an argv that reorders
    # between two identical converges is one no caller can compare.
    #
    # UNVERIFIED ON nerdctl, and named here rather than assumed. `run_container`
    # is shared with `LimaRuntime`, which drives `nerdctl` inside a Lima VM, so
    # a PLACED ECS task (`ecsctl.runtime_for_service`) would carry these flags
    # to a different binary. docker's `--add-host` behaviour is measured; the
    # nerdctl half has not been probed, and this repo's own rule is that
    # inheriting a second CLI's contract on faith is how `host_port` nearly
    # shipped a bug (see that method, which WAS probed on both). Probe
    # `nerdctl run --add-host` against a real VM before relying on route53 for
    # a placed task.
    hosts: dict[str, str] = field(default_factory=dict)


def _shares_namespace(network: str | None) -> bool:
    """Does this `--network` value put the container in ANOTHER container's
    network namespace, where `--add-host` is a hard docker error?

    Keyed on the `container:` prefix specifically, and not on "any network",
    because only that form is the one this repo has actually MEASURED docker
    rejecting (`run_container`'s own note, and
    `tests/runtime/test_colima_unit.py`). odin sets nothing else today --
    `fabric/sidecar.py` is the only caller that sets `network` at all -- so a
    caller that later introduces `--network host` or a named network owes
    docker's real behaviour a probe before assuming this function covers it.
    Guessing a broader rule here would refuse a combination that may be
    perfectly legal, which is its own kind of wrong answer."""
    return (network or "").startswith("container:")


@dataclass(frozen=True)
class RunHandle:
    id: str
    name: str


@dataclass(frozen=True)
class ContainerFacts:
    phase: Phase
    host_port: int = 0
    cpu: float = 0.0
    ram: float = 0.0
    logtail: str = ""


@dataclass(frozen=True)
class HostFacts:
    total_mem_mib: float = 0.0
    cpu_count: int = 0
    # This host's Nebula overlay IP, when it's a mesh member (M7 multi-Mac).
    # None on a single host => producers publish 127.0.0.1, behavior unchanged.
    overlay_ip: str | None = None


# Docker/nerdctl container state -> coarse runtime phase ("healthy" is an assertion's call).
_STATUS_TO_PHASE: dict[str, Phase] = {
    "running": "starting",
    "restarting": "starting",
    "paused": "starting",
    "created": "starting",  # booting, not gone — distinct from "absent" (=pending)
    "exited": "crashed",
    "dead": "crashed",
    "removing": "crashed",
    "absent": "pending",
}


def _to_mib(value: str) -> float:
    # Longest suffixes first: "MiB" also ends with "B".
    units = [("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024), ("B", 1 / 1024 / 1024)]
    for unit, factor in units:
        if value.endswith(unit):
            return float(value[: -len(unit)] or 0) * factor
    return 0.0


# A line the runtime never stamped sorts FIRST rather than being dropped (see
# `_merge_log_streams`). Timezone-aware so it compares against real stamps.
_UNSTAMPED = datetime.min.replace(tzinfo=timezone.utc)


def _stamp(token: str) -> datetime | None:
    """The `--timestamps` prefix `docker`/`nerdctl` puts in front of every log
    line, as a sortable value -- None when this token isn't one.

    The `"T"` guard is load-bearing: a Postgres line carries its OWN leading
    date (`2026-07-25 14:00:01.123 UTC [1] LOG: ...`), whose first token
    `fromisoformat` parses perfectly well, and treating that as the runtime's
    stamp would silently eat the date out of every Postgres log line. A real
    RFC3339 stamp always has the date/time separator."""
    if "T" not in token:
        return None
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _log_line(line: str) -> tuple[datetime, str]:
    stamp, _, rest = line.partition(" ")
    parsed = _stamp(stamp)
    return (_UNSTAMPED, line) if parsed is None else (parsed, rest)


def _merge_log_streams(stdout: str, stderr: str, tail: int) -> str:
    """One container's two log streams, back together as one chronological
    block of `tail` lines (field test 2, HIGH-3).

    `docker logs`/`nerdctl logs` write the container's stdout to THEIR stdout
    and its stderr to THEIR stderr, and `--tail N` selects the last N lines
    across BOTH. Reading only stdout therefore discarded whatever share of
    those N lines the process wrote to stderr -- measured on one live
    container: `--tail 10` was 0 bytes of stdout and 943 bytes of stderr, so
    `odin logs` reported an EMPTY log for a Lambda failing every invocation,
    and 0 bytes for a settled Postgres and an nginx (both log only to stderr).

    THE INTERLEAVING GUARANTEE, stated precisely. `--timestamps` makes the
    runtime prefix each line with the time ITS OWN log driver recorded when it
    read that line off the container's fd, so this merge is chronological with
    respect to the runtime's observation order -- at microsecond resolution
    (`datetime.fromisoformat` truncates the nanosecond field), with equal
    stamps keeping stdout-before-stderr because the sort is stable. It is NOT
    a guarantee about the writer's own ordering: a process whose stdout is
    block-buffered while its stderr is unbuffered genuinely IS observed out of
    write order, and no reader of `docker logs` can undo that.

    A line the runtime did NOT stamp is kept (first, in its own stream's
    order), never dropped: silently dropping log lines is the bug being fixed,
    so an unstamped runtime must degrade to "ordering unknown", not to "gone".
    """
    lines = [_log_line(line) for stream in (stdout, stderr) for line in stream.splitlines()]
    lines.sort(key=lambda pair: pair[0])
    return "\n".join(text for _stamp_value, text in lines[-tail:])


def _command_label(args: tuple[str, ...]) -> str:
    """WHICH command failed, in the shortest honest form: the subcommand plus
    the container it named (`run odin-ecs-wa-…-web-svc`, `cp`).

    NEVER the full argv, which is what this message used to be. A workload
    container's argv carries its whole ENVIRONMENT -- the gateway credentials
    `gateway/keys.py::workload_env` injects as `-e AWS_SECRET_ACCESS_KEY=…`, an
    rds `POSTGRES_PASSWORD`, a resolved `DATABASE_URL` -- and this message does
    not stay local: `gateway/models/ecsctl.py` records `str(exc)` as the task's
    `stopped_reason`, `tf_status.py` projects that as the World verdict, and the
    reconciler broadcasts it on the event stream and appends it to
    `.odin/{env}/events.jsonl` (field test 2 finding #6 -- a real workload
    secret key was found in four durable log entries). The argv was also what
    pushed the real docker error past the 200-char clip in
    `agent/debugger.py`, so the diagnosis lost the one line that explained the
    failure. Now the error text leads with the reason."""
    named = args.index("--name") + 1 if "--name" in args else 0
    return " ".join(part for part in (args[0], args[named] if named else "") if part)


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


# `exc_text`'s sibling, for the other half of "something failed and here is why":
# a SUBPROCESS that failed. `exc_text` cannot serve this one -- it treats an
# EMPTY `str(exc)`, and these strings were never empty, they were VACUOUS. The
# text `f"{CLI} {label} failed: {proc.stderr.strip()}"` renders
#
#     docker run x failed:
#
# a sentence whose reason slot is a dangling colon. Probed against the REAL
# docker on this machine (28.4.0 / Colima), not reasoned about -- three real
# commands that exit non-zero having written nothing to stderr:
#
#     docker exec <c> sh -c 'exit 1'                rc=1  stderr=''  stdout=''
#     docker exec <c> sh -c 'echo on-stdout; exit 7' rc=7 stderr=''  stdout='on-stdout\n'
#     docker run --rm alpine sh -c 'exit 3'         rc=3  stderr=''  stdout=''
#
# All three rendered `docker exec failed: ` / `docker run failed: `. So the exit
# code -- 1, 7, 3, the one fact that WAS there every time -- was dropped, and
# case two shows the reason can be on STDOUT: `_cli` keeps only stdout on
# success and only stderr on failure, so a command that explains itself on the
# wrong stream explained itself to nobody.
#
# What this states instead: the exit code always (it exists by construction --
# we are here BECAUSE it was non-zero), then stderr, then stdout if that is
# where the process spoke, and otherwise the fact that it said nothing at all --
# which is a real answer, not an absence. `_command_label` still supplies the
# COMMAND, and still deliberately omits the argv (it carries workload secrets --
# see that function).
_NO_OUTPUT = "it wrote nothing to stderr or stdout, so the exit code is the whole of it"


def _failure_reason(proc: _Proc) -> str:
    """WHY a container-CLI command failed, in a form that is never empty."""
    stdout = proc.stdout.strip()
    return proc.stderr.strip() or (
        f"nothing on stderr; on stdout: {stdout}" if stdout else _NO_OUTPUT
    )


async def _default_runner(args: list[str], input: str | None = None) -> _Proc:
    # `run_command`, so a machine with no `docker` CLI on PATH (Homebrew's
    # colima formula does NOT bring one) yields rc 127 -- which `_cli` turns
    # into "docker … failed: docker: command not found" -- instead of a bare
    # FileNotFoundError traceback out of whatever happened to call first.
    proc = await run_command_async(args, input=input)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


class _ContainerRuntime:
    """Run/inspect/stop labelled containers. Subclasses supply `_argv` (the
    container-CLI seam) and optionally `_run_flags` (runtime-specific run args).
    The subprocess runner is injectable, so subclasses are unit-testable."""

    # The container CLI's name, for failure messages (`_command_label`).
    CLI = "container"

    def __init__(self, runner=None) -> None:
        self._run = runner or _default_runner

    def _argv(self, *args: str) -> list[str]:
        """This runtime's full argv for one container-CLI command (`docker …`
        vs `limactl shell <vm> sudo nerdctl …`). It's a separate seam from
        `_cli` so `logs` can run the same command while keeping BOTH streams
        (`_cli` deliberately keeps only stdout, because for every other command
        stderr really is the error channel)."""
        raise NotImplementedError

    async def _cli(self, *args: str, check: bool = True, input: str | None = None) -> str:
        proc = await self._run(self._argv(*args), input=input)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"{self.CLI} {_command_label(args)} failed "
                f"(exit {proc.returncode}): {_failure_reason(proc)}"
            )
        return proc.stdout.strip()

    def _run_flags(self) -> list[str]:
        return []

    async def image_exists(self, tag: str) -> bool:
        # Plain `image inspect` prints a truthy "[]" to stdout when the image
        # is MISSING (docker rc=1), which silently skipped the one-time build.
        # The Id template prints nothing on a missing image; "[]" is still
        # guarded because nerdctl's behavior differs across versions.
        out = await self._cli("image", "inspect", "-f", "{{.Id}}", tag, check=False)
        return out not in ("", "[]")

    async def build(self, tag: str, dockerfile: str) -> None:
        """Build `tag` from an inline Dockerfile (no build context — piped on
        stdin, `-`). Used to bake a one-time `npm install` into a local image
        (dynalite: see BackingAws) so container boot never re-fetches from a
        registry that might be slow or flaky that day."""
        await self._cli("build", "-t", tag, "-", input=dockerfile)

    async def run_container(self, spec: ContainerSpec) -> RunHandle:
        # A namespace-sharing container takes no `_run_flags`: docker rejects
        # `--add-host` together with `--network container:` outright ("conflicting
        # options"), and it needs none -- it inherits the target's /etc/hosts-less
        # networking wholesale (fabric/sidecar.py).
        #
        # `spec.hosts` hits that SAME documented conflict, and the two are handled
        # differently on purpose. `_run_flags()` is odin's own infrastructure alias
        # (`host.docker.internal`), so dropping it for a sidecar is correct and
        # silent. `spec.hosts` is a DRAWN route53 record: dropping it silently
        # would leave a name that the canvas says resolves and the container says
        # does not -- a decorative record, which is the exact failure this feature
        # exists to avoid. There is no honest way to honour it here, so the
        # combination is refused rather than quietly discarded.
        if spec.hosts and _shares_namespace(spec.network):
            raise ValueError(
                f"cannot give {spec.name} DNS entries ({', '.join(sorted(spec.hosts))}) while it "
                f"shares another container's network namespace ({spec.network}): docker rejects "
                f"--add-host with --network container: as conflicting options. It resolves names "
                f"through the namespace it joined, so the entries belong on THAT container."
            )
        args = [
            "run", "-d", "--name", spec.name, *([] if spec.network else self._run_flags()),
            "--label", f"{LABEL}=1", "--label", f"{LABEL}.name={spec.name}",
        ]
        if spec.network:
            args += ["--network", spec.network]
        for name, address in sorted(spec.hosts.items()):
            args += ["--add-host", f"{name}:{address}"]
        for capability in spec.cap_add:
            args += ["--cap-add", capability]
        for device in spec.devices:
            args += ["--device", device]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        for cport, hport in spec.ports.items():
            args += ["-p", (f"{hport}:{cport}" if hport else str(cport))]
        for host, container in spec.volumes.items():
            args += ["-v", f"{host}:{container}"]
        if spec.memory_mib:
            args += ["--memory", f"{spec.memory_mib:g}m"]
        if spec.cpus:
            args += ["--cpus", f"{spec.cpus:g}"]
        args.append(spec.image)
        args += list(spec.command)
        return RunHandle(id=await self._cli(*args), name=spec.name)

    async def status(self, name: str) -> str:
        """Container state: running / exited / created / … / absent."""
        return await self._cli("inspect", "-f", "{{.State.Status}}", name, check=False) or "absent"

    async def exit_code(self, name: str) -> int:
        out = await self._cli("inspect", "-f", "{{.State.ExitCode}}", name, check=False)
        return int(out) if out.lstrip("-").isdigit() else -1

    async def host_port(self, name: str, container_port: int) -> int:
        """The host port `container_port` is published on -- 0 when the runtime
        ANSWERED and nothing is published there, `PortUnreadable` when it could
        not be asked at all (see that class for the corruption this closes).

        Reads the whole port MAP off `inspect` rather than running
        `<cli> port`, because `<cli> port` cannot separate those two cases.
        Verified against the real docker on this machine:

            $ docker port odin-aws-rustfs-f5notofu2 9999
            no public port '9999' published for odin-aws-rustfs-f5notofu2   rc=1
            $ docker port no-such-container 80
            Error response from daemon: No such container: …              rc=1

        Same exit code, same empty stdout, for "asked and the answer is none"
        and "could not ask" -- which is precisely why the old
        `if out else 0` had to guess, and guessed wrong. The port map does
        separate them:

            running   {"9000/tcp":[{"HostIp":"0.0.0.0","HostPort":"33776"}],
                       "9001/tcp":null}                                    rc=0
            exited    {}                                                   rc=0
            absent    (empty; "no such object" on stderr)                  rc=1

        so rc is now a real signal: 0 means the runtime answered, and the
        answer is in structured JSON rather than parsed out of a text line.

        AND VERIFIED ON `nerdctl` TOO, which is the half this method inherited
        on faith. `LimaRuntime` reuses this code against `nerdctl` inside a
        Lima VM, a different binary with no guarantee of docker's output or
        exit codes -- so it was probed on a real container in a real VM
        (nerdctl v2.0.3 / containerd v2.3.2, aarch64):

            published (-p 18080:80)
                {"80/tcp":[{"HostIp":"0.0.0.0","HostPort":"18080"}]}   rc=0
            running, nothing published
                {}                                                     rc=0
            exited
                {}                                                     rc=0
            absent
                (empty) fatal "1 errors: [no such object <name>]"       rc=1

        Identical contract to docker's. The one cosmetic difference is that
        docker publishes a second `{"HostIp":"::"}` binding for the same port
        where nerdctl publishes one -- immaterial, since only `[0]["HostPort"]`
        is read. `nerdctl port` was confirmed to carry the SAME ambiguity as
        `docker port` (rc=1 both for "no public port 80/tcp published" and for
        "no such container"), so the trap this method avoids was real on both
        runtimes, not just Colima.

        ONE nerdctl-ONLY WRINKLE, and why it is harmless: inspecting an EXITED
        container makes nerdctl write `level=warning msg="failed to inspect
        NetNS"` to STDERR while still exiting 0 with a valid `{}` on stdout.
        This method keys on the exit code alone, so that is read as the real
        answer it is -- a reader that treated "stderr is non-empty" as failure
        would raise `PortUnreadable` on every stopped container on Lima.

        Pinned by `tests/runtime/test_lima_integration.py`, which runs all
        three states against a real VM rather than fabricating these strings."""
        proc = await self._run(self._argv("inspect", "-f", "{{json .NetworkSettings.Ports}}", name))
        if proc.returncode != 0:
            raise PortUnreadable(
                f"{self.CLI} cannot read {name}'s published ports "
                f"(exit {proc.returncode}): {_failure_reason(proc)}"
            )
        bindings = json.loads(proc.stdout.strip() or "{}") or {}
        published = bindings.get(f"{container_port}/tcp") or []
        return int(published[0]["HostPort"]) if published else 0

    async def logs(self, name: str, tail: int = 20) -> str:
        """The container's last `tail` lines -- BOTH streams, merged, tailed
        after merging, so `--tail N` means N real lines (see
        `_merge_log_streams` for the bug this closes and the exact interleaving
        guarantee). The one command that does NOT go through `_cli`: there,
        stderr is the error channel; here, half the log lives on it.

        A failed read (a container that vanished between `status` and here) is
        "" exactly as before, rather than the CLI's own "No such container"
        text -- that would present a diagnostic as container output."""
        proc = await self._run(self._argv("logs", "--timestamps", "--tail", str(tail), name))
        return _merge_log_streams(proc.stdout, proc.stderr, tail) if proc.returncode == 0 else ""

    async def stats(self, name: str) -> dict[str, float]:
        """One-shot cpu% + memory (MiB) for a running container."""
        out = await self._cli(
            "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", name, check=False,
        )
        if not out:
            return {"cpu": 0.0, "ram": 0.0}
        cpu_s, mem_s = out.split(" ", 1)
        return {"cpu": float(cpu_s.strip().rstrip("%") or 0), "ram": _to_mib(mem_s.split("/")[0].strip())}

    async def facts(self, name: str, container_port: int = 0) -> ContainerFacts:
        # An ABSENT container has no port map to read, and `host_port` now says
        # so by raising -- so don't ask about one. Every other state (running,
        # created, exited, …) does have a map, so a raise from here is a real
        # runtime failure and belongs loud, not swallowed into a 0.
        status = await self.status(name)
        stats = await self.stats(name) if status == "running" else {"cpu": 0.0, "ram": 0.0}
        readable = container_port and status != "absent"
        return ContainerFacts(
            phase=_STATUS_TO_PHASE.get(status, "pending"),
            host_port=await self.host_port(name, container_port) if readable else 0,
            cpu=stats["cpu"], ram=stats["ram"],
            logtail=await self.logs(name, tail=5) if status != "absent" else "",
        )

    async def copy_in(self, name: str, host_path: str, container_path: str) -> None:
        """Copy a host file INTO a running container (`docker cp`).

        W2.5 uses this instead of a bind mount to deliver a load-balancer
        proxy's rendered config, and the reason is empirical: a `-v` of a path
        under macOS's per-user temp dir (`/private/var/folders/...`) silently
        mounts an EMPTY directory under Colima's virtiofs -- the path exists in
        the VM, so nothing errors; nginx simply came up with no config and
        accepted-then-dropped every connection. `docker cp` streams through the
        daemon, so it works regardless of which host paths the runtime VM
        happens to share."""
        await self._cli("cp", host_path, f"{name}:{container_path}")

    async def container_id(self, name: str) -> str:
        """This container's full id, or "" when it doesn't exist.

        The id is what makes "is my sidecar in the CURRENT target's network
        namespace?" answerable: a container that was killed and re-created
        keeps its NAME but never its id (fabric/sidecar.py's
        `attached_to`)."""
        return await self._cli("inspect", "-f", "{{.Id}}", name, check=False)

    async def network_mode(self, name: str) -> str:
        """The container's own network mode -- for a namespace-sharing
        container (`--network container:<target>`) this is
        `container:<the target's id AS IT WAS AT CREATION>`, because the
        runtime resolves the name to an id right then. That stale id is
        exactly the signal that the target has since been replaced."""
        return await self._cli("inspect", "-f", "{{.HostConfig.NetworkMode}}", name, check=False)

    async def exec_sh(self, name: str, script: str) -> str:
        """Run `script` with `sh -c` INSIDE a running container's namespaces
        and return its stdout ("" if the container is gone, the exec fails, or
        the script printed nothing).

        The one way to observe a network namespace odin doesn't own: the mesh
        sidecar shares its target's namespace, so a probe run here sees the
        overlay exactly as a real consumer on the mesh does
        (reconcile/assertions.py::mesh_ready_sync). Callers make the script
        self-bounding (busybox `nc -w`) and print a TOKEN on success rather
        than relying on an exit code, so this stays a plain stdout read."""
        return await self._cli("exec", name, "sh", "-c", script, check=False)

    async def signal(self, name: str, sig: str) -> None:
        """Send UNIX signal `sig` to the container's main process (`docker kill
        -s`). W2.5: how a load-balancer proxy container is told to re-read its
        rewritten config (nginx reloads on SIGHUP) WITHOUT `docker exec` and
        without recreating the container -- so an upstream change never drops
        an in-flight request. `check=False`: signalling an already-gone
        container is a no-op, exactly like `stop`."""
        await self._cli("kill", "-s", sig, name, check=False)

    async def stop(self, name: str) -> None:
        # -v: drop the container's ANONYMOUS volumes with it (an image with a
        # bare `VOLUME` line creates one per boot; without this a churn loop
        # leaks gigabytes). A NAMED volume is deliberately untouched by it --
        # probed on this machine's docker rather than assumed, because odin's
        # non-destructive rds repair rests on it:
        #
        #     $ docker rm -f -v rdsvol-probe-pg          # rc 0
        #     $ docker volume ls --filter name=rdsvol-probe-data --format '{{.Name}}'
        #     rdsvol-probe-data
        #
        # -- and the 2 rows written before the removal were still there after a
        # fresh container was started on that same volume. `remove_volume` is
        # the only thing that deletes one.
        await self._cli("rm", "-f", "-v", name, check=False)

    # --- named volumes: the state that has to OUTLIVE its container ---------
    #
    # Everything else odin runs is disposable: kill the container, run a new
    # one, nothing is lost. A database is not, and that made odin's own repair
    # destructive -- `postgres:16-alpine` declares `/var/lib/postgresql/data`
    # as a VOLUME, so every rds container already had one, just an ANONYMOUS
    # one that `stop`'s `-v` deleted along with it. Naming it is the whole fix.

    async def create_volume(self, name: str, env: str) -> None:
        """Ensure a named volume exists, LABELLED as odin's and as `env`'s.

        Idempotent by the CLI's own contract (probed: a second
        `docker volume create` on the same name exits 0 and changes nothing),
        so this needs no exists-check and no branch.

        The explicit create earns its one extra call by the labels: `-v
        name:/path` auto-creates an UNLABELLED volume, which is indistinguishable
        from one the user made by hand -- and an odin volume nobody can attribute
        to odin is one nobody can ever safely reclaim on a disk-tight machine.
        `volume_names` is what that buys.

        `env` is why the reclaim exists at all. v0.8.14 made the volume named so
        it would OUTLIVE its container; nothing then reclaimed one, and four
        orphans from two long-dead environments were measured on this machine.
        The reclaim is keyed on this label and never on the name -- see
        `ENV_LABEL` for the ambiguity that rules a name out."""
        await self._cli(
            "volume", "create", "--label", f"{LABEL}=1", "--label", f"{LABEL}.name={name}",
            "--label", f"{ENV_LABEL}={env}", name,
        )

    async def remove_volume(self, name: str) -> None:
        """Delete a named volume. Absent is success; STILL IN USE is not.

        `check=True` on purpose, unlike `stop`. Both cases were probed on the
        real docker here rather than reasoned about:

            docker volume rm -f <never-existed>   rc 0  (prints the name)
            docker volume rm -f <in-use>          rc 1  "volume is in use - [<id>]"

        so `-f` already absorbs the idempotent case, and the only way this
        fails is a volume that is genuinely still attached -- i.e. the caller
        removed the container after the volume, or not at all. That is a real
        disk leak and a real teardown failure, and swallowing it would leave
        `odin destroy` reporting a success it did not achieve."""
        await self._cli("volume", "rm", "-f", name)

    async def volume_names(self, env: str | None = None) -> list[str]:
        """Every odin-labelled volume's NAME, in one call -- `container_names`'
        twin, and `check=True` for the same reason: an empty answer here means
        "the volume is really gone", so a CLI hiccup must raise rather than
        arrive as an innocent empty list. `server.py`'s recovery disclosure
        reads it to say whether a database's data really survived, instead of
        asserting that it must have.

        With `env`, the listing is narrowed by the `ENV_LABEL` filter to the
        volumes THAT ENVIRONMENT created, and that narrowing is what every
        reclaim is built on: `docker volume prune` and any name-shaped filter are
        machine-wide, and this repo has already had a machine-wide sweep destroy
        another agent's work. Two docker filters, ANDed by docker itself, so an
        unlabelled volume (odin's own, made before v0.8.15, or a bare `-v
        name:/path` auto-create) is absent from an env-scoped answer by
        construction -- it is reported by `GET /volumes` instead of guessed at."""
        scope = ["--filter", f"label={ENV_LABEL}={env}"] if env is not None else []
        out = await self._cli(
            "volume", "ls", "--format", "{{.Name}}", "--filter", f"label={LABEL}=1", *scope,
        )
        return [line for line in out.splitlines() if line]

    async def list_odin(self) -> list[str]:
        out = await self._cli("ps", "-aq", "--filter", f"label={LABEL}=1", check=False)
        return [line for line in out.splitlines() if line]

    async def container_names(self) -> list[str]:
        """Every odin-labelled container's NAME -- running or exited, ONE
        `docker ps` call regardless of how many there are (W2.2's drift sweep
        compares whole synth stores against this single listing, never one
        `inspect` per resource).

        `check=True`, deliberately: this is the one listing whose EMPTY answer
        is load-bearing (absent from it == the container was really removed),
        so a failed CLI call must raise rather than come back as an innocent
        empty list -- see `reconcile/drift.py::_listing`."""
        out = await self._cli("ps", "-a", "--format", "{{.Names}}", "--filter", f"label={LABEL}=1")
        return [line for line in out.splitlines() if line]


class ColimaRuntime(_ContainerRuntime):
    """Drives `docker` (Colima) directly on the host."""

    CLI = "docker"

    def _argv(self, *args: str) -> list[str]:
        return ["docker", *args]

    def _run_flags(self) -> list[str]:
        # Reach the host-side AWS embed + RDS from inside containers.
        return ["--add-host", "host.docker.internal:host-gateway"]

    async def ensure_host(self) -> HostFacts:
        out = await self._cli("info", "--format", "{{.MemTotal}} {{.NCPU}}", check=False)
        if not out:
            return HostFacts()
        mem_bytes, ncpu = out.split()
        return HostFacts(total_mem_mib=int(mem_bytes) / 1024 / 1024, cpu_count=int(ncpu))
