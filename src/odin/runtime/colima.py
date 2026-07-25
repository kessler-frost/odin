"""Container runtimes: a shared base + the default Colima (`docker`) driver.

For the walking skeleton the Runtime driver's "host" is the local Colima
container engine (run containers directly), fast and real. `LimaRuntime`
(runtime/lima.py) runs the same containers inside a Lima VM for isolation,
reusing the `_ContainerRuntime` base here — they differ only in the CLI seam
(`docker` vs `nerdctl`-in-VM) and Colima's host-gateway run flag.

Every container odin runs is labelled ``odin=1``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from odin.spec.models import Phase

LABEL = "odin"

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
    volumes: dict[str, str] = field(default_factory=dict)  # host_path -> container_path
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


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""


def _default_runner(args: list[str], input: str | None = None) -> _Proc:
    proc = subprocess.run(args, capture_output=True, text=True, input=input)
    return _Proc(proc.returncode, proc.stdout, proc.stderr)


class _ContainerRuntime:
    """Run/inspect/stop labelled containers. Subclasses supply `_cli` (the
    container-CLI seam) and optionally `_run_flags` (runtime-specific run args).
    The subprocess runner is injectable, so subclasses are unit-testable."""

    def __init__(self, runner=None) -> None:
        self._run = runner or _default_runner

    def _cli(self, *args: str, check: bool = True, input: str | None = None) -> str:
        raise NotImplementedError

    def _run_flags(self) -> list[str]:
        return []

    def image_exists(self, tag: str) -> bool:
        # Plain `image inspect` prints a truthy "[]" to stdout when the image
        # is MISSING (docker rc=1), which silently skipped the one-time build.
        # The Id template prints nothing on a missing image; "[]" is still
        # guarded because nerdctl's behavior differs across versions.
        out = self._cli("image", "inspect", "-f", "{{.Id}}", tag, check=False)
        return out not in ("", "[]")

    def build(self, tag: str, dockerfile: str) -> None:
        """Build `tag` from an inline Dockerfile (no build context — piped on
        stdin, `-`). Used to bake a one-time `npm install` into a local image
        (dynalite: see BackingAws) so container boot never re-fetches from a
        registry that might be slow or flaky that day."""
        self._cli("build", "-t", tag, "-", input=dockerfile)

    def run_container(self, spec: ContainerSpec) -> RunHandle:
        # A namespace-sharing container takes no `_run_flags`: docker rejects
        # `--add-host` together with `--network container:` outright ("conflicting
        # options"), and it needs none -- it inherits the target's /etc/hosts-less
        # networking wholesale (fabric/sidecar.py).
        args = [
            "run", "-d", "--name", spec.name, *([] if spec.network else self._run_flags()),
            "--label", f"{LABEL}=1", "--label", f"{LABEL}.name={spec.name}",
        ]
        if spec.network:
            args += ["--network", spec.network]
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
        return RunHandle(id=self._cli(*args), name=spec.name)

    def status(self, name: str) -> str:
        """Container state: running / exited / created / … / absent."""
        return self._cli("inspect", "-f", "{{.State.Status}}", name, check=False) or "absent"

    def exit_code(self, name: str) -> int:
        out = self._cli("inspect", "-f", "{{.State.ExitCode}}", name, check=False)
        return int(out) if out.lstrip("-").isdigit() else -1

    def host_port(self, name: str, container_port: int) -> int:
        out = self._cli("port", name, str(container_port), check=False)
        return int(out.splitlines()[0].rsplit(":", 1)[-1]) if out else 0

    def logs(self, name: str, tail: int = 20) -> str:
        return self._cli("logs", "--tail", str(tail), name, check=False)

    def stats(self, name: str) -> dict[str, float]:
        """One-shot cpu% + memory (MiB) for a running container."""
        out = self._cli(
            "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", name, check=False,
        )
        if not out:
            return {"cpu": 0.0, "ram": 0.0}
        cpu_s, mem_s = out.split(" ", 1)
        return {"cpu": float(cpu_s.strip().rstrip("%") or 0), "ram": _to_mib(mem_s.split("/")[0].strip())}

    def facts(self, name: str, container_port: int = 0) -> ContainerFacts:
        status = self.status(name)
        stats = self.stats(name) if status == "running" else {"cpu": 0.0, "ram": 0.0}
        return ContainerFacts(
            phase=_STATUS_TO_PHASE.get(status, "pending"),
            host_port=self.host_port(name, container_port) if container_port else 0,
            cpu=stats["cpu"], ram=stats["ram"],
            logtail=self.logs(name, tail=5) if status != "absent" else "",
        )

    def copy_in(self, name: str, host_path: str, container_path: str) -> None:
        """Copy a host file INTO a running container (`docker cp`).

        W2.5 uses this instead of a bind mount to deliver a load-balancer
        proxy's rendered config, and the reason is empirical: a `-v` of a path
        under macOS's per-user temp dir (`/private/var/folders/...`) silently
        mounts an EMPTY directory under Colima's virtiofs -- the path exists in
        the VM, so nothing errors; nginx simply came up with no config and
        accepted-then-dropped every connection. `docker cp` streams through the
        daemon, so it works regardless of which host paths the runtime VM
        happens to share."""
        self._cli("cp", host_path, f"{name}:{container_path}")

    def signal(self, name: str, sig: str) -> None:
        """Send UNIX signal `sig` to the container's main process (`docker kill
        -s`). W2.5: how a load-balancer proxy container is told to re-read its
        rewritten config (nginx reloads on SIGHUP) WITHOUT `docker exec` and
        without recreating the container -- so an upstream change never drops
        an in-flight request. `check=False`: signalling an already-gone
        container is a no-op, exactly like `stop`."""
        self._cli("kill", "-s", sig, name, check=False)

    def stop(self, name: str) -> None:
        # -v: drop the container's anonymous volumes with it (postgres creates
        # one per boot; without this a churn loop leaks gigabytes).
        self._cli("rm", "-f", "-v", name, check=False)

    def list_odin(self) -> list[str]:
        out = self._cli("ps", "-aq", "--filter", f"label={LABEL}=1", check=False)
        return [line for line in out.splitlines() if line]

    def container_names(self) -> list[str]:
        """Every odin-labelled container's NAME -- running or exited, ONE
        `docker ps` call regardless of how many there are (W2.2's drift sweep
        compares whole synth stores against this single listing, never one
        `inspect` per resource).

        `check=True`, deliberately: this is the one listing whose EMPTY answer
        is load-bearing (absent from it == the container was really removed),
        so a failed CLI call must raise rather than come back as an innocent
        empty list -- see `reconcile/drift.py::_listing`."""
        out = self._cli("ps", "-a", "--format", "{{.Names}}", "--filter", f"label={LABEL}=1")
        return [line for line in out.splitlines() if line]


class ColimaRuntime(_ContainerRuntime):
    """Drives `docker` (Colima) directly on the host."""

    def _cli(self, *args: str, check: bool = True, input: str | None = None) -> str:
        proc = self._run(["docker", *args], input=input)
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _run_flags(self) -> list[str]:
        # Reach the host-side AWS embed + RDS from inside containers.
        return ["--add-host", "host.docker.internal:host-gateway"]

    def ensure_host(self) -> HostFacts:
        out = self._cli("info", "--format", "{{.MemTotal}} {{.NCPU}}", check=False)
        if not out:
            return HostFacts()
        mem_bytes, ncpu = out.split()
        return HostFacts(total_mem_mib=int(mem_bytes) / 1024 / 1024, cpu_count=int(ncpu))
