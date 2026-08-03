"""ApiProxy -- the REAL substrate behind `gateway/models/apigwctl.py`'s
`aws_apigatewayv2_api`: one **nginx:alpine** container per HTTP API, whose
`location` blocks are that API's routes.

SAME MECHANISM AS `compute/proxy.py`, NOT A SECOND ONE. Every moving part is
imported from there -- the image, the entry command, the config path inside the
container, the reload signal, the name sanitizer -- so there is exactly one
"odin runs an nginx and reloads it with SIGHUP" implementation in the repo and
this module only decides what goes in the config. Read `compute/proxy.py`'s
docstring first; the four reasons nginx was chosen (10MB image, plain-text
config, SIGHUP reload needing one driver method, request-level failover) hold
here unchanged, as does CONFIG DELIVERY IS `docker cp`, NOT A BIND MOUNT.

WHAT IS DIFFERENT FROM THE LOAD-BALANCER SHAPE, and why:

1. **One listener, always port 80.** A load balancer's listener ports are
   canvas-authored, so `LoadBalancerProxy.ensure` has to recreate the container
   whenever the listener SET changes (a published-port change Docker cannot
   apply to a live container). An HTTP API has no listener concept at all -- it
   has ONE endpoint -- so this proxy always listens on 80 and the published host
   port is therefore STABLE for the API's whole life. That is what lets
   `apigwctl` answer `CreateApi` with a real `apiEndpoint` and never have it
   drift: every later route/integration change is a config copy plus a SIGHUP,
   never a new port.

2. **Routes are `location` blocks, not upstreams.** An API routes by PATH; a
   load balancer balances one path across many targets. So the rendered file is
   one `server {}` with one `location` pair per route, plus a catch-all `location
   /` answering 404 -- which is what a real HTTP API answers for a path no route
   matches (`{"message":"Not Found"}`).

3. **A route pair, not a single location.** Route key `ANY /orders` matches only
   `/orders`; `ANY /orders/{proxy+}` matches `/orders/a/b` and NOT `/orders`.
   odin emits both for every target, so a whole path prefix is served, and nginx
   needs the matching pair: `location = /orders` (exact) and `location /orders/`
   (prefix). Both proxy to the same place.

READINESS IS PROBED, WHICH `compute/proxy.py` DELIBERATELY DOES NOT DO. Its
module docstring names the open residual: `ensure` returns the port DOCKER
PUBLISHED, which is not the claim "nginx is serving on it", and a config nginx
rejects makes it exit ~180ms later -- so a load balancer goes `active` on a
host port nothing is listening on. That residual is fatal here rather than
merely wrong, because an API's endpoint is handed straight back to `tofu` as
`apiEndpoint` and then to a human who curls it. So `ensure` finishes with a real
TCP-connect probe of the published port (`compute/functions.py::_tcp_open`'s
technique, on the shape `FunctionRuntime._await_ready` uses for RIE) and raises
`ProxyNotServing` -- carrying the container's real status and log tail -- when
nothing answers. It is NOT retrofitted onto `LoadBalancerProxy` here: that is a
change to a shipped converge contract with its own tests, and doing it as a
side effect of adding a service is how two things break at once.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from odin.compute.functions import _tcp_open
from odin.compute.proxy import (
    CONF_PATH_IN_CONTAINER,
    IMAGE,
    RELOAD_SIGNAL,
    _ENTRY_COMMAND,
    safe_name,
)
from odin.runtime.colima import CONTAINER_HOST, ColimaRuntime, ContainerSpec
from odin.util import atomic_write_text

# The one port nginx listens on inside every API's container (see note 1).
LISTEN_PORT = 80
# Same caps as the load-balancer proxy: a router is tiny and must never be the
# thing that eats the host.
_MEMORY_MIB = 64.0
_CPUS = 0.5
# How long `ensure` waits for nginx to accept a connection on the published
# port. A `docker run` of an already-pulled 10MB image plus an nginx start is
# sub-second; the budget is generous so a loaded machine does not fail an apply.
READY_TIMEOUT = 30.0
_POLL_INTERVAL = 0.1

# What a real HTTP API answers for a path no route matches. Copied from the
# wire rather than invented: real API Gateway v2 sends 404 with exactly
# `{"message":"Not Found"}`.
_NOT_FOUND_BODY = '{"message":"Not Found"}'


def container_name(env: str, api_name: str) -> str:
    """`odin-apigw-{env}-{api_name}` -- the ONLY name this module passes to the
    runtime driver. `safe_name` is `compute/proxy.py`'s, for the reason its
    docstring gives: the same reduction has to be used for the container name
    AND the config path, or two API names can share a container while writing
    two different config files."""
    return f"odin-apigw-{env}-{safe_name(api_name)}"


def conf_path(root: Path, env: str, api_name: str) -> Path:
    return root / env / "gateway" / "apigw" / safe_name(api_name) / "odin.conf"


@dataclass(frozen=True)
class ApiRoute:
    """One route's rendered `location` pair.

    `prefix` is the path the route owns (`/orders`, or `/` for a `$default`
    route that owns everything). `upstream` is a real `host:port`. `upstream_path`
    is what REPLACES `prefix` in the proxied request: `""` forwards the path
    unchanged, `"/"` strips the prefix (the HTTP_PROXY case, so an ecs task sees
    `/a/b` rather than `/orders/a/b`), and a real path is the shim route an
    AWS_PROXY integration is rewritten onto.

    `headers` are `proxy_set_header` lines -- how the shim learns which
    integration it is serving, and the ONLY thing that authorizes the call (see
    `gateway/apigw_shim.py`)."""

    prefix: str
    upstream: str
    upstream_path: str = ""
    headers: tuple[tuple[str, str], ...] = ()


class ProxyNotServing(RuntimeError):
    """The proxy container published a host port and NOTHING ANSWERED ON IT.

    Distinct from `compute/proxy.py`'s `PortsUnpublished` ("docker published
    nothing"), and one step further: docker did publish, and nginx is not there.
    The overwhelmingly likely cause is a config nginx refused, which is why the
    message carries the log tail -- `nginx: [emerg] ...` is the whole answer and
    it is only ever in the container's own output."""


def _proxy_body(route: ApiRoute) -> str:
    headers = "".join(
        f"        proxy_set_header {name} {value};\n" for name, value in route.headers
    )
    return (
        f"        proxy_pass http://{route.upstream}{route.upstream_path};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        f"{headers}"
        "        proxy_connect_timeout 2s;\n"
        # A lambda handler may legitimately run to its own 30s timeout, and the
        # shim waits for it -- an nginx read timeout shorter than that would
        # turn a slow-but-correct function into a 504 the caller cannot explain.
        "        proxy_read_timeout 35s;\n"
    )


def render_conf(routes: tuple[ApiRoute, ...]) -> str:
    """The whole `/etc/nginx/conf.d/odin.conf` for one API.

    An API with NO routes renders just the 404 catch-all, which is what a real
    HTTP API with no routes does -- and, unlike an empty `upstream {}` block,
    is a config nginx starts on. That matters because `CreateApi` runs before
    any `CreateRoute`, so the very first converge of every API is this case."""
    blocks = []
    for route in sorted(routes, key=lambda r: r.prefix):
        body = _proxy_body(route)
        if route.prefix == "/":
            blocks.append(f"    location / {{\n{body}    }}\n")
            continue
        blocks.append(f"    location = {route.prefix} {{\n{body}    }}\n")
        blocks.append(f"    location {route.prefix}/ {{\n{body}    }}\n")
    if not any(route.prefix == "/" for route in routes):
        blocks.append(
            "    location / {\n"
            "        default_type application/json;\n"
            f"        return 404 '{_NOT_FOUND_BODY}';\n"
            "    }\n"
        )
    return f"server {{\n    listen {LISTEN_PORT};\n\n" + "\n".join(blocks) + "}\n"


class ApiProxy:
    """Per-API nginx container lifecycle, on an injectable `RuntimeDriver` --
    the same seam `LoadBalancerProxy`/`TaskRuntime`/`FunctionRuntime` use, so a
    test drives it with no real Docker involved."""

    def __init__(self, runtime=None, ready_timeout: float = READY_TIMEOUT) -> None:
        self._rt = runtime or ColimaRuntime()
        self._ready_timeout = ready_timeout

    async def ensure(self, root: Path, env: str, api_name: str, routes: tuple[ApiRoute, ...]) -> int:
        """Converge the real proxy container onto `routes` and return the REAL
        published host port.

        Same one deterministic rule as `LoadBalancerProxy.ensure`, simplified by
        there being exactly one port: a RUNNING container that already publishes
        80 is reloaded (copy the config in, SIGHUP -- zero downtime, no in-flight
        request dropped); anything else is removed and re-run. Idempotent, and a
        no-change call is one copy plus one signal.

        There is exactly ONE return, through `_serving_port`, for the reason
        `LoadBalancerProxy.ensure` documents: a check on one of two branches is
        a check that is half there."""
        name = container_name(env, api_name)
        host_conf = conf_path(root, env, api_name)
        atomic_write_text(host_conf, render_conf(routes))
        published = await self._live_port(name)
        if published:
            await self._rt.copy_in(name, str(host_conf), CONF_PATH_IN_CONTAINER)
            await self._rt.signal(name, RELOAD_SIGNAL)
        else:
            await self._rt.stop(name)
            await self._rt.run_container(ContainerSpec(
                name=name, image=IMAGE,
                ports={LISTEN_PORT: 0},  # 0 => Docker picks a free host port
                labels={"odin-env": env, "odin-apigw": api_name},
                command=_ENTRY_COMMAND,
                memory_mib=_MEMORY_MIB, cpus=_CPUS,
            ))
            # The container is up running the wait loop; THIS copy is what lets
            # nginx actually start (compute/proxy.py's docstring).
            await self._rt.copy_in(name, str(host_conf), CONF_PATH_IN_CONTAINER)
            published = await self._rt.host_port(name, LISTEN_PORT)
        return await self._serving_port(name, published)

    async def _live_port(self, name: str) -> int:
        """The host port a RUNNING container publishes for 80, or 0 -- including
        for a container that does not exist.

        The status check before the port read is what makes the read LEGAL, not
        merely tidy: `host_port` RAISES `PortUnreadable` on an absent container,
        so reading first would make the very FIRST `ensure` for a new API raise
        before it could create anything (measured on `LoadBalancerProxy`, whose
        `_live_ports` docstring quotes the real failure)."""
        if await self._rt.status(name) != "running":
            return 0
        return await self._rt.host_port(name, LISTEN_PORT)

    async def _serving_port(self, name: str, published: int) -> int:
        """`published`, once something really answers on it -- else
        `ProxyNotServing` naming the container's status and its log tail.

        This is the check `compute/proxy.py` names as its open residual. The
        wire-reachable trigger is the same one it found: a route whose upstream
        is not a valid nginx server address makes nginx answer `[emerg] invalid
        parameter` and exit, having published a port first."""
        if not published:
            raise ProxyNotServing(
                f"{name} published no host port for {LISTEN_PORT} (container is "
                f"{await self._rt.status(name)}); last log lines: "
                f"{(await self._rt.logs(name, 5)).strip() or 'none'}"
            )
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if await _tcp_open(published):
                return published
            await asyncio.sleep(_POLL_INTERVAL)
        raise ProxyNotServing(
            f"{name} published host port {published} but nothing accepted a TCP connection "
            f"there within {self._ready_timeout:g}s (container is {await self._rt.status(name)}); "
            f"last log lines: {(await self._rt.logs(name, 5)).strip() or 'none'}"
        )

    async def status(self, env: str, api_name: str) -> str:
        return await self._rt.status(container_name(env, api_name))

    async def destroy(self, env: str, api_name: str) -> None:
        """Force-remove the proxy container (idempotent on an absent name --
        `_ContainerRuntime.stop`'s contract)."""
        await self._rt.stop(container_name(env, api_name))


def target_address(host_port: int, host: str = CONTAINER_HOST) -> str:
    """The `host:port` a route proxies to for a target odin published on the
    HOST (an ECS task's bridge-mode container, or odin's own gateway carrying
    the invoke shim). `CONTAINER_HOST` (`host.docker.internal`, wired by
    Colima's `--add-host`) is how a container dials back out to the host."""
    return f"{host}:{host_port}"
