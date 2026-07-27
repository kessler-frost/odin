"""LoadBalancerProxy -- the REAL substrate behind gateway/models/elbv2ctl.py's
`aws_lb`: one **nginx:alpine** container per load balancer, whose upstream
servers are the target group's actually-registered targets.

Shape mirrors compute/tasks.py's `TaskRuntime` (a MANY-per-resource binding on
an injectable `RuntimeDriver`), not aws/backings.py's one-shared-container-
per-env-per-kind shape: one proxy container per LOAD BALANCER, named
`odin-alb-{env}-{lb_name}` and carrying the usual `odin=1` label (via
`_ContainerRuntime.run_container`) so every cleanup/gc sweep in the repo finds
it.

WHY NGINX (and not Caddy -- both are permissive, so licence wasn't the
tiebreaker):
1. `nginx:alpine` is ~10MB and pulls in seconds. Disk headroom is a standing
   repo constraint (CLAUDE.md's "clean up after EVERY heavy step").
2. Its config for this job is a plain text template -- one `server` line per
   target inside one `upstream` block. Nothing to compile, nothing to encode.
3. **Reload is a signal.** nginx re-reads its configuration on SIGHUP, which
   `docker kill -s HUP` delivers, so a target change needs exactly ONE new
   driver method (`RuntimeDriver.signal`) -- no `docker exec` seam and no
   admin-API HTTP dance (Caddy v2 dropped v1's SIGUSR1 reload; it needs
   `caddy reload` over exec or a POST to :2019). Fewer moving parts, and an
   upstream change never drops an in-flight request.
4. `proxy_next_upstream` gives REQUEST-LEVEL failover: when one target's
   process is gone, nginx retries the next target within the SAME client
   request, so the client still gets its 200. That is the behaviour that makes
   a load balancer a load balancer, and it is what W2.5's integration test
   proves by `docker rm -f`-ing one of two ECS tasks.

HEALTH CHECKS ARE PASSIVE, SAID PLAINLY. Open-source nginx has only passive
upstream health checking (`max_fails` / `fail_timeout`); ACTIVE checks
(`health_check` with a URI + interval) are an NGINX Plus feature. So a target
group's `HealthCheckPath` is **not polled by the proxy**: the honest mapping
is `fail_timeout` <- `HealthCheckIntervalSeconds` and `max_fails=1`, i.e. "one
failed real request takes a target out of rotation for one health-check
interval". The path/matcher are still stored and echoed (that is what keeps
`tofu plan` zero-drift), and `DescribeTargetHealth` answers from a REAL
odin-performed probe of the target's real address (elbv2ctl.py's
`_probe_target`), never from an invented "healthy".

CONFIG DELIVERY IS `docker cp`, NOT A BIND MOUNT -- found the hard way. The
rendered config lives at `.odin/{env}/gateway/alb/{lb_name}/odin.conf` and is
copied into the container at `/etc/nginx/conf.d/odin.conf` (nginx:alpine's
stock nginx.conf already `include`s `/etc/nginx/conf.d/*.conf` from inside its
`http {}` block). A `-v` of that host directory LOOKED right and failed
silently: under Colima's virtiofs a path beneath macOS's per-user temp dir
(`/private/var/folders/...`) mounts as an EMPTY directory -- the path exists
inside the VM, so nothing errors -- and nginx came up with no server block at
all, accepting then dropping every connection. `docker cp` streams through the
daemon, so it depends on no mount configuration whatsoever.

The container's command is a two-line shell prologue -- delete the image's own
`default.conf`, wait for `odin.conf` to arrive, then `exec nginx` -- so nginx
NEVER serves the nginx welcome page in the window between `docker run` and the
copy. `exec` also keeps nginx as PID 1, which is what makes `docker kill -s
HUP` reach it.

KNOWN LIMIT, MEASURED, NOT CLOSED: `ensure` reports the port DOCKER PUBLISHED,
which is not the same claim as "nginx is serving on it". A recreate reads the
port immediately after `docker cp`, and an nginx that then REJECTS the config
exits a moment later -- so the load balancer goes `active` on a real host port
that nothing is listening on. Timed against the real container on 2026-07-27:

    +0.000s  ensure's own read -> host_port=34029
    +0.179s  container stopped publishing (status=exited)

i.e. the read wins the race by ~180ms, every time (6 of 6 converges in a
separate run). `_require_published` does NOT catch this: the port it is handed
is genuine at the instant it is read. Closing it needs a real readiness probe
of the published port (the shape `compute/functions.py::FunctionRuntime.ensure`
uses for RIE), which is a bigger change than this module's converge contract.
The wire-reachable trigger found for it is a target id that is not a valid
nginx server address -- `elbv2ctl._target_host` returns a non-`i-` id VERBATIM
(its own docstring), so `aws_lb_target_group_attachment.target_id = "10.0.0.1
bogus"` renders `server 10.0.0.1 bogus:8080;` and nginx answers
`[emerg] invalid parameter`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from odin.runtime.colima import CONTAINER_HOST, ColimaRuntime, ContainerSpec
from odin.util import atomic_write_text

IMAGE = "nginx:alpine"
CONF_FILENAME = "odin.conf"
CONF_DIR_IN_CONTAINER = "/etc/nginx/conf.d"
CONF_PATH_IN_CONTAINER = f"{CONF_DIR_IN_CONTAINER}/{CONF_FILENAME}"
# Drop the image's own server block, wait for `docker cp` to deliver ours, then
# become nginx (module docstring: no welcome-page window, and nginx ends up as
# PID 1 so SIGHUP reaches it).
_ENTRY_COMMAND = (
    "sh", "-c",
    f"rm -f {CONF_DIR_IN_CONTAINER}/default.conf; "
    f"while [ ! -f {CONF_PATH_IN_CONTAINER} ]; do sleep 0.1; done; "
    "exec nginx -g 'daemon off;'",
)
# The signal nginx re-reads its configuration on.
RELOAD_SIGNAL = "HUP"
# The port the proxy listens on when the load balancer has NO listener yet
# (CreateLoadBalancer runs before CreateListener). Real AWS refuses the
# connection in that state; odin answers 503, which is also what a real ALB
# answers once a listener exists but has no healthy target -- one honest
# "nothing to serve" shape instead of two.
IDLE_LISTEN_PORT = 80
# Owner directive B4's spirit (compute/tasks.py's own caps): a proxy is tiny
# and must never be the thing that eats the host.
_MEMORY_MIB = 64.0
_CPUS = 0.5


_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def safe_name(lb_name: str) -> str:
    """A load-balancer name reduced to characters that are safe as BOTH a Docker
    container name and a single path segment.

    Real elbv2 restricts names to `[A-Za-z0-9-]` (a strict subset of what either
    needs), but odin's own model accepts a name VERBATIM rather than validating
    it (`gateway/models/elbv2ctl.py`'s "accepted, never validated" rule), so
    every name that reaches the substrate goes through here. Used by BOTH
    `container_name` and `conf_path` deliberately: sanitizing only one of them
    let `my/lb` and `my-lb` share a container while writing two DIFFERENT config
    files (they would fight over it), and let a `..` segment walk the config out
    of its own directory."""
    return _UNSAFE_IN_NAME.sub("-", lb_name)


def container_name(env: str, lb_name: str) -> str:
    """`odin-alb-{env}-{lb_name}` -- the ONLY name this module passes to the
    runtime driver."""
    return f"odin-alb-{env}-{safe_name(lb_name)}"


def conf_path(root: Path, env: str, lb_name: str) -> Path:
    """Where this proxy's rendered config lives ON THE HOST, before being
    `docker cp`'d into the container (module docstring). Same `safe_name`
    reduction as the container -- see its docstring for why that matters."""
    return root / env / "gateway" / "alb" / safe_name(lb_name) / CONF_FILENAME


@dataclass(frozen=True)
class ProxyListener:
    """One rendered `server {}`: the port nginx listens on inside the
    container, the real `host:port` addresses to balance across, and the
    passive fail window derived from the target group's health-check interval.
    `targets` empty => that listener answers 503 (see IDLE_LISTEN_PORT)."""

    port: int
    upstream: str  # an nginx identifier (sanitized target-group name)
    targets: tuple[str, ...]
    fail_timeout_seconds: int = 30


class PortsUnpublished(RuntimeError):
    """The proxy container ANSWERED about its published ports, and the answer is
    that a listener port has none -- so this load balancer has no address to
    hand anyone. Distinct from `runtime/colima.py`'s `PortUnreadable` ("could
    not ask at all"), and the same idea one layer up.

    Raised instead of returned because the ONLY use of `ensure`'s return value
    is `elbv2ctl.converge_proxy` writing it onto the load-balancer record as
    `endpoints`, which `endpoint_url` turns into `ALB_ENDPOINT` and
    `gateway/wiring.py::producer_facts` INJECTS INTO A REAL CONSUMER CONTAINER.
    A host port of 0 is not a port, it is "nothing is published": returning it
    made the load balancer go `active` with `endpoints {"80": 0}` and handed a
    workload `http://127.0.0.1:0`. Raising routes it to
    `elbv2ctl._converge_safely`, which already does the honest thing: state
    `failed` with this text as the load balancer's `State.Reason`. It leaves
    the PREVIOUS converge's `endpoints` on the record (they are that converge's
    result, and are worth keeping to look at), so what actually withholds the
    fact is `elbv2ctl.endpoint_url` refusing to call a recorded port an address
    unless the state is `active` -- see its docstring."""


def _sanitize_upstream(name: str) -> str:
    """An nginx upstream identifier. Target-group names are `[A-Za-z0-9-]`, and
    `-` is legal in an nginx upstream name, so this only has to namespace it
    away from any other directive."""
    return f"odin_{name.replace('-', '_')}"


def render_conf(listeners: tuple[ProxyListener, ...]) -> str:
    """The whole `/etc/nginx/conf.d/default.conf` for one load balancer.

    A listener WITH targets gets an `upstream` block plus a `proxy_pass` to it;
    `proxy_next_upstream` is what turns a dead target into a retry against the
    next one rather than a 502 for the client. A listener with NO targets gets
    `return 503` -- an `upstream {}` with no `server` line is a nginx CONFIG
    ERROR (the container would refuse to start at all), and 503 is what a real
    ALB answers when no target is healthy.
    """
    blocks: list[str] = []
    for listener in listeners or (ProxyListener(port=IDLE_LISTEN_PORT, upstream="idle", targets=()),):
        body = (
            f"        proxy_pass http://{listener.upstream};\n"
            "        proxy_next_upstream error timeout http_502 http_503 http_504;\n"
            "        proxy_connect_timeout 2s;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        ) if listener.targets else "        return 503;\n"
        if listener.targets:
            servers = "".join(
                f"    server {target} max_fails=1 fail_timeout={listener.fail_timeout_seconds}s;\n"
                for target in listener.targets
            )
            blocks.append(f"upstream {listener.upstream} {{\n{servers}}}\n")
        blocks.append(
            f"server {{\n    listen {listener.port};\n\n"
            f"    location / {{\n{body}    }}\n}}\n"
        )
    return "\n".join(blocks)


class LoadBalancerProxy:
    """Per-load-balancer nginx container lifecycle, on an injectable
    `RuntimeDriver` (the same seam TaskRuntime/FunctionRuntime/InstanceVm use,
    so a test drives it with no real Docker involved)."""

    def __init__(self, runtime=None) -> None:
        self._rt = runtime or ColimaRuntime()

    def ensure(self, root: Path, env: str, lb_name: str, listeners: tuple[ProxyListener, ...]) -> dict[int, int]:
        """Converge the real proxy container onto `listeners` and return
        `{listen_port: published host port}`, every one of them REAL.

        ONE deterministic rule, no hidden state: render the config, then
        - the container is running AND every wanted port is already published
          => copy the config in + SIGHUP (a target change; zero downtime, no
          in-flight request dropped);
        - otherwise => remove and re-run it (first create, or the LISTENER SET
          itself changed, which is a published-port change Docker cannot apply
          to a live container).
        Idempotent: called after every elbv2 mutation that can change what the
        proxy should serve, and a no-change call is one copy plus one signal.

        There is exactly ONE return, and it goes through `_require_published`.
        That is the point rather than an implementation detail: the reload
        branch used to carry its `all(published.values())` check inline while
        the recreate branch had none, so a publish failure reached the record on
        exactly one of the two paths. A single exit cannot be half-fixed."""
        name = container_name(env, lb_name)
        ports = tuple(listener.port for listener in listeners) or (IDLE_LISTEN_PORT,)
        host_conf = conf_path(root, env, lb_name)
        atomic_write_text(host_conf, render_conf(listeners))
        published = self._live_ports(name, ports)
        # `all({})` is True, hence the `published and`: an empty map means
        # there was no running container to read ports off at all.
        if published and all(published.values()):
            self._rt.copy_in(name, str(host_conf), CONF_PATH_IN_CONTAINER)
            self._rt.signal(name, RELOAD_SIGNAL)
        else:
            self._rt.stop(name)
            self._rt.run_container(ContainerSpec(
                name=name, image=IMAGE,
                ports={port: 0 for port in ports},  # 0 => Docker picks a free host port
                labels={"odin-env": env, "odin-alb": lb_name},
                command=_ENTRY_COMMAND,
                memory_mib=_MEMORY_MIB, cpus=_CPUS,
            ))
            # The container is up running `_ENTRY_COMMAND`'s wait loop; THIS copy
            # is what lets nginx actually start (module docstring).
            self._rt.copy_in(name, str(host_conf), CONF_PATH_IN_CONTAINER)
            published = {port: self._rt.host_port(name, port) for port in ports}
        return self._require_published(name, published)

    def _live_ports(self, name: str, ports: tuple[int, ...]) -> dict[int, int]:
        """`{listen_port: host port}` as the RUNNING container really publishes
        them -- a 0 for any it does not -- and `{}` when there is no running
        container to ask.

        The status check is what makes the port read LEGAL, not merely tidy.
        `host_port` RAISES `PortUnreadable` on a container that does not exist
        (that is how "I could not ask" stays distinguishable from "nothing is
        published"), so reading the ports FIRST -- which is what this method
        replaced -- made the very first `ensure` for a NEW load balancer raise
        before it could ever create the container. Measured against real docker
        on 2026-07-27, twice in a row on the same fresh name:

            status before      : absent
            ensure() #1 (fresh): PortUnreadable: docker cannot read
                                 odin-alb-p1a-fresh-lb1's published ports:
                                 error: no such object: odin-alb-p1a-fresh-lb1
            status after #1    : absent

        i.e. every `CreateLoadBalancer` ended in `state: failed` and no proxy
        container was ever created. `PortUnreadable` (`runtime/colima.py`,
        commit a67b218) landed AFTER this module (7005988) and nothing here
        was adjusted for it."""
        if self._rt.status(name) != "running":
            return {}
        return {port: self._rt.host_port(name, port) for port in ports}

    def _require_published(self, name: str, published: dict[int, int]) -> dict[int, int]:
        """`published` unchanged, or `PortsUnpublished` naming the ports with no
        host port, the container's real status, and its last log lines.

        The log tail is there because it is the only thing that says WHY. The
        real failure this closes is nginx refusing the rendered config and
        exiting, and `docker logs` on the real exited container says exactly
        that -- measured, not assumed:

            nginx: [emerg] invalid parameter "bogus:8080" in
            /etc/nginx/conf.d/odin.conf:2

        which `_converge_safely` then stores as the load balancer's
        `State.Reason` (honesty rule 2: name the real reason, not "failed")."""
        dead = sorted(port for port, host_port in published.items() if not host_port)
        if not dead:
            return published
        raise PortsUnpublished(
            f"{name} published no host port for {dead} (container is "
            f"{self._rt.status(name)}); last log lines: {self._rt.logs(name, 5).strip() or 'none'}"
        )

    def status(self, env: str, lb_name: str) -> str:
        return self._rt.status(container_name(env, lb_name))

    def destroy(self, env: str, lb_name: str) -> None:
        """Force-remove the proxy container (idempotent on an absent name --
        `_ContainerRuntime.stop`'s contract)."""
        self._rt.stop(container_name(env, lb_name))


def target_address(host_port: int, host: str = CONTAINER_HOST) -> str:
    """The `host:port` an upstream entry uses for a target odin registered
    itself (an ECS task): the task's container publishes its port on the HOST,
    and `CONTAINER_HOST` (`host.docker.internal`, wired by Colima's
    `--add-host`) is how the proxy container dials back out to it. Kept here
    so elbv2ctl and its tests agree on one spelling."""
    return f"{host}:{host_port}"
