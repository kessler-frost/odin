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

CONFIG DELIVERY: the rendered config lives at
`.odin/{env}/gateway/alb/{lb_name}/default.conf` and the **directory** is
bind-mounted at `/etc/nginx/conf.d` (nginx:alpine's stock nginx.conf already
`include`s `/etc/nginx/conf.d/*.conf` from inside its `http {}` block, and our
mount shadows the image's own `default.conf`). Mounting the DIRECTORY rather
than the file is load-bearing: `atomic_write_text` replaces the file by
rename, which gives it a new inode -- a single-file bind mount would keep
showing the container the OLD inode forever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from odin.runtime.colima import CONTAINER_HOST, ColimaRuntime, ContainerSpec
from odin.util import atomic_write_text

IMAGE = "nginx:alpine"
CONF_FILENAME = "default.conf"
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


_UNSAFE_IN_CONTAINER_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def container_name(env: str, lb_name: str) -> str:
    """`odin-alb-{env}-{lb_name}` -- the ONLY name this module passes to the
    runtime driver. Real elbv2 restricts load-balancer names to `[A-Za-z0-9-]`
    (a strict subset of what Docker accepts), but odin's own model accepts a
    name VERBATIM rather than validating it (gateway/models/elbv2ctl.py's
    "accepted, never validated" rule), so anything Docker would reject is
    folded to `-` here instead of producing an unrunnable container."""
    return f"odin-alb-{env}-{_UNSAFE_IN_CONTAINER_NAME.sub('-', lb_name)}"


def conf_dir(root: Path, env: str, lb_name: str) -> Path:
    """The bind-mounted directory holding this proxy's rendered config (module
    docstring: a DIRECTORY, never the file itself)."""
    return root / env / "gateway" / "alb" / lb_name


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
        `{listen_port: published host port}`.

        ONE deterministic rule, no hidden state: write the config, then
        - the container is running AND every wanted port is already published
          => rewrite + SIGHUP (a target change; zero downtime);
        - otherwise => remove and re-run it (first create, or the LISTENER SET
          itself changed, which is a published-port change Docker can't apply
          to a live container).
        Idempotent: called after every elbv2 mutation that can change what the
        proxy should serve, and a no-change call is one config write plus one
        signal."""
        name = container_name(env, lb_name)
        ports = tuple(listener.port for listener in listeners) or (IDLE_LISTEN_PORT,)
        atomic_write_text(conf_dir(root, env, lb_name) / CONF_FILENAME, render_conf(listeners))
        published = {port: self._rt.host_port(name, port) for port in ports}
        if self._rt.status(name) == "running" and all(published.values()):
            self._rt.signal(name, RELOAD_SIGNAL)
            return published
        self._rt.stop(name)
        self._rt.run_container(ContainerSpec(
            name=name, image=IMAGE,
            ports={port: 0 for port in ports},  # 0 => Docker picks a free host port
            volumes={str(conf_dir(root, env, lb_name)): "/etc/nginx/conf.d"},
            labels={"odin-env": env, "odin-alb": lb_name},
            memory_mib=_MEMORY_MIB, cpus=_CPUS,
        ))
        return {port: self._rt.host_port(name, port) for port in ports}

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
