"""W2.5 -- compute/proxy.py: `render_conf` (the whole nginx config for one
load balancer) and `LoadBalancerProxy`'s converge rules.

Unit-level on an injected fake runtime driver, the same seam
test_tasks.py/test_functions.py/test_instances.py use -- no real Docker
involved. proxy.py is deliberately shaped like compute/tasks.py's
`TaskRuntime` (a MANY-per-resource binding on a `RuntimeDriver`), so these
tests are shaped like tasks' tests.

The `FakeRuntime` below records EVERY driver call in order and exposes just
the method names as `sequence()`, because the thing worth pinning about
`ensure` is not incidental state but WHICH calls it makes: reload
(copy + SIGHUP, nothing restarted) versus recreate (stop + run). A test that
only looked at `runs`/`stopped` would pass on an implementation that also
needlessly signalled, or that reloaded a container it had just recreated.

`FakeRuntime.host_port` ANSWERS THE WAY REAL DOCKER DOES, and that is the
load-bearing part of this file rather than a detail. It used to return 0 for
any container it had no port for, including one that does not exist -- and
because of that this suite passed while `ensure` was, on the real runtime,
incapable of creating a load balancer at all. Probed against real docker
28.4.0 on Colima (2026-07-27):

    absent     ColimaRuntime.host_port -> raises PortUnreadable
                 ("error: no such object: <name>")
    exited     docker inspect ports {}  -> host_port 0
    running    {"80/tcp":[{"HostPort":"34011"}]} -> host_port 34011

so the fake raises for absent and answers 0 for anything not running. Honesty
rule 1's "probe the real component and print what it returns" applies to a
test double as much as to a guard: a double that is wrong about the upstream
proves the parser and hides the integration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from odin.compute.proxy import (
    CONF_PATH_IN_CONTAINER,
    IDLE_LISTEN_PORT,
    IMAGE,
    RELOAD_SIGNAL,
    LoadBalancerProxy,
    PortsUnpublished,
    ProxyListener,
    _ENTRY_COMMAND,
    conf_path,
    container_name,
    render_conf,
    target_address,
)
from odin.runtime.colima import CONTAINER_HOST, ContainerSpec, PortUnreadable

ENV = "default"
LB = "web"
NAME = container_name(ENV, LB)
TARGETS = (f"{CONTAINER_HOST}:10080", f"{CONTAINER_HOST}:10081")


@dataclass
class FakeRuntime:
    """Exactly the driver methods proxy.py calls, and nothing else -- so an
    added driver dependency shows up as an AttributeError here rather than as
    a surprise real `docker` invocation."""

    # Every call, in order: ("host_port", NAME, 80), ("signal", NAME, "HUP"), ...
    calls: list[tuple] = field(default_factory=list)
    runs: list[ContainerSpec] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    # (name, container_port) -> published host port; 0/absent == "not published",
    # which is the real state of a container Docker hasn't given a port yet.
    ports: dict[tuple[str, int], int] = field(default_factory=dict)
    # What `run_container` publishes for each container port it is asked for,
    # standing in for Docker's own free-port pick when the spec says host 0.
    next_ports: dict[int, int] = field(default_factory=dict)
    # `docker logs` output per container -- what a failed converge quotes.
    logtails: dict[str, str] = field(default_factory=dict)
    # The status `run_container` leaves behind, so a test can model a container
    # the daemon started and whose process died before anything read its ports.
    status_after_run: str = "running"

    async def status(self, name: str) -> str:
        self.calls.append(("status", name))
        return self.statuses.get(name, "absent")

    async def host_port(self, name: str, container_port: int) -> int:
        self.calls.append(("host_port", name, container_port))
        # See the module docstring: real docker raises for a container it has
        # never heard of and answers 0 for one that is not running.
        if self.statuses.get(name, "absent") == "absent":
            raise PortUnreadable(f"docker cannot read {name}'s published ports: no such object: {name}")
        if self.statuses[name] != "running":
            return 0
        return self.ports.get((name, container_port), 0)

    async def logs(self, name: str, tail: int = 20) -> str:
        self.calls.append(("logs", name, tail))
        return self.logtails.get(name, "")

    async def stop(self, name: str) -> None:
        self.calls.append(("stop", name))
        self.statuses.pop(name, None)
        for key in [key for key in self.ports if key[0] == name]:
            del self.ports[key]

    async def run_container(self, spec: ContainerSpec) -> None:
        self.calls.append(("run_container", spec.name))
        self.runs.append(spec)
        self.statuses[spec.name] = self.status_after_run
        for container_port in spec.ports:
            self.ports[(spec.name, container_port)] = self.next_ports.get(container_port, 0)

    async def copy_in(self, name: str, host_path: str, container_path: str) -> None:
        self.calls.append(("copy_in", name, host_path, container_path))

    async def signal(self, name: str, sig: str) -> None:
        self.calls.append(("signal", name, sig))

    def sequence(self) -> list[str]:
        return [call[0] for call in self.calls]


def _listener(port: int, upstream: str = "odin_tg_web", targets: tuple[str, ...] = TARGETS) -> ProxyListener:
    return ProxyListener(port=port, upstream=upstream, targets=targets)


# --- render_conf ------------------------------------------------------------


def test_render_conf_with_no_listeners_is_one_503_server_and_no_upstream_block():
    conf = render_conf(())

    # Exactly ONE server block, on the idle port, answering 503.
    assert conf.count("server {") == 1
    assert f"listen {IDLE_LISTEN_PORT};" in conf
    assert "return 503;" in conf
    # And NO upstream block at all -- this is the load-bearing part. An nginx
    # `upstream {}` with no `server` line is a CONFIG ERROR, so emitting an
    # empty one would make the container refuse to start (nginx never comes up
    # => the load balancer is not "empty", it is dead). 503 is also exactly
    # what a real ALB answers when nothing behind it is healthy, so the honest
    # no-targets shape and the no-listener shape are the same shape.
    assert "upstream" not in conf


def test_render_conf_emits_one_upstream_server_line_per_target_and_a_proxy_pass():
    conf = render_conf((ProxyListener(
        port=8080, upstream="odin_tg_web", targets=TARGETS, fail_timeout_seconds=17,
    ),))

    assert "upstream odin_tg_web {" in conf
    # One `server` line per registered target, with the passive health-check
    # window taken from the listener (== the target group's
    # HealthCheckIntervalSeconds): open-source nginx has max_fails/fail_timeout
    # only, so "one failed real request parks the target for one interval".
    for target in TARGETS:
        assert f"    server {target} max_fails=1 fail_timeout=17s;\n" in conf
    assert conf.count("max_fails=1") == len(TARGETS)

    assert "server {\n    listen 8080;" in conf
    assert "proxy_pass http://odin_tg_web;" in conf
    # THE mechanism that keeps a load balancer serving when one target dies:
    # nginx retries the next upstream WITHIN THE SAME client request, so the
    # client still gets its 200 instead of a 502. Without this directive a
    # dead-target test would fail even with a perfectly rendered upstream.
    assert "proxy_next_upstream error timeout http_502 http_503 http_504;" in conf
    assert "return 503;" not in conf


def test_render_conf_falls_back_to_503_for_a_listener_with_an_empty_target_list():
    # Same reason as the no-listener case: an empty `upstream {}` is an nginx
    # config error, so a listener that exists but has no registered target must
    # serve 503 rather than be rendered as an upstream with zero servers.
    conf = render_conf((_listener(8080, targets=()),))

    assert "upstream" not in conf
    assert "return 503;" in conf
    assert conf.count("server {") == 1
    assert "listen 8080;" in conf  # the LISTENER's port, not the idle port


def test_render_conf_gives_each_listener_its_own_server_block_and_listen_port():
    conf = render_conf((
        _listener(80, upstream="odin_tg_a", targets=(TARGETS[0],)),
        _listener(443, upstream="odin_tg_b", targets=(TARGETS[1],)),
    ))

    assert conf.count("server {") == 2
    assert "server {\n    listen 80;" in conf
    assert "server {\n    listen 443;" in conf
    assert conf.count("upstream odin_tg_a {") == 1
    assert conf.count("upstream odin_tg_b {") == 1
    assert "proxy_pass http://odin_tg_a;" in conf
    assert "proxy_pass http://odin_tg_b;" in conf


# --- container_name / conf_path --------------------------------------------


def test_container_name_is_odin_alb_env_lb():
    assert container_name(ENV, LB) == "odin-alb-default-web"


def test_container_name_folds_characters_docker_would_reject():
    # elbv2ctl accepts a load-balancer name VERBATIM (never validates it), so
    # anything outside Docker's own name charset has to be folded here or the
    # container would be unrunnable.
    assert container_name(ENV, "my/lb") == "odin-alb-default-my-lb"
    assert container_name(ENV, "a b") == "odin-alb-default-a-b"


def test_conf_path_lands_under_env_gateway_alb_lb():
    assert conf_path(Path("/root"), ENV, LB) == Path("/root/default/gateway/alb/web/odin.conf")
    assert CONF_PATH_IN_CONTAINER == "/etc/nginx/conf.d/odin.conf"


# --- ensure: first create ---------------------------------------------------


async def test_ensure_first_create_runs_nginx_then_copies_the_config_in(tmp_path):
    runtime = FakeRuntime(next_ports={80: 32768, 443: 32769})
    listeners = (
        _listener(80, upstream="odin_tg_a", targets=(TARGETS[0],)),
        _listener(443, upstream="odin_tg_b", targets=(TARGETS[1],)),
    )

    published = await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, listeners)

    # The returned map is what elbv2ctl records as the lb's endpoints: the
    # LISTEN port -> the host port Docker actually picked.
    assert published == {80: 32768, 443: 32769}

    host_conf = conf_path(tmp_path, ENV, LB)
    assert host_conf.read_text() == render_conf(listeners)

    # Absent container => stop (idempotent pre-clean) then run, never a signal.
    # `status` comes FIRST and no port is read before the container exists:
    # `host_port` raises `PortUnreadable` on an absent container, so the old
    # order (read the ports, then look at the status) made this very call --
    # every first converge of every new load balancer -- raise before it could
    # create anything. Measured against real docker before the fix:
    # "ensure() #1 (fresh): PortUnreadable: docker cannot read
    #  odin-alb-p1a-fresh-lb1's published ports: error: no such object".
    assert runtime.sequence() == [
        "status",                  # "absent" -> nothing to read a port off
        "stop", "run_container", "copy_in",
        "host_port", "host_port",  # read AFTER the run: the real ports
    ]

    (spec,) = runtime.runs
    assert spec.name == NAME
    assert spec.image == IMAGE == "nginx:alpine"
    # Host port 0 for every listener => Docker picks a free one, which is why
    # `ensure` has to re-read `host_port` after running.
    assert spec.ports == {80: 0, 443: 0}
    assert spec.labels == {"odin-env": ENV, "odin-alb": LB}
    # The shell prologue: drop the image's default.conf, wait for our config to
    # arrive, then `exec nginx` -- so nginx never serves the welcome page in the
    # window before the copy, and ends up as PID 1 so SIGHUP reaches it.
    assert spec.command == _ENTRY_COMMAND
    # NO volumes, deliberately: config delivery is `docker cp`, not a bind
    # mount. A `-v` of a macOS per-user temp path (`/private/var/folders/...`)
    # mounts as an EMPTY directory under Colima's virtiofs -- the path exists
    # inside the VM so nothing errors, and nginx comes up with no server block
    # at all, accepting then dropping every connection. `docker cp` streams
    # through the daemon and depends on no mount configuration whatsoever.
    assert spec.volumes == {}

    copy = next(call for call in runtime.calls if call[0] == "copy_in")
    assert copy == ("copy_in", NAME, str(host_conf), CONF_PATH_IN_CONTAINER)


async def test_ensure_with_no_listeners_still_publishes_the_idle_port(tmp_path):
    # CreateLoadBalancer runs before CreateListener, so this is the state right
    # after a canvas-drawn ALB appears: one container, one 503 listener.
    runtime = FakeRuntime(next_ports={IDLE_LISTEN_PORT: 32700})

    published = await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, ())

    assert published == {IDLE_LISTEN_PORT: 32700}
    (spec,) = runtime.runs
    assert spec.ports == {IDLE_LISTEN_PORT: 0}


# --- ensure: reload vs recreate --------------------------------------------


async def test_ensure_reloads_with_a_hup_when_the_container_already_publishes_every_port(tmp_path):
    runtime = FakeRuntime(statuses={NAME: "running"}, ports={(NAME, 80): 32768})
    listeners = (_listener(80, targets=TARGETS),)

    published = await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, listeners)

    assert published == {80: 32768}
    # Zero downtime: the config is rewritten and copied in, then nginx re-reads
    # it on SIGHUP. Nothing is stopped or re-run, so no in-flight request is
    # dropped by a target change.
    assert runtime.sequence() == ["status", "host_port", "copy_in", "signal"]
    assert runtime.runs == []
    assert ("signal", NAME, RELOAD_SIGNAL) in runtime.calls
    assert RELOAD_SIGNAL == "HUP"
    assert conf_path(tmp_path, ENV, LB).read_text() == render_conf(listeners)


async def test_ensure_recreates_when_a_wanted_port_is_not_published_yet(tmp_path):
    # The LISTENER SET changed (a second listener appeared): the container is
    # running and healthy, but Docker cannot add a published port to a live
    # container, so the only honest converge is remove-and-re-run.
    runtime = FakeRuntime(
        statuses={NAME: "running"}, ports={(NAME, 80): 32768},
        next_ports={80: 32768, 443: 32769},
    )
    listeners = (
        _listener(80, upstream="odin_tg_a", targets=(TARGETS[0],)),
        _listener(443, upstream="odin_tg_b", targets=(TARGETS[1],)),
    )

    published = await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, listeners)

    assert published == {80: 32768, 443: 32769}
    assert runtime.sequence() == [
        "status",                  # "running", so the ports are readable
        "host_port", "host_port",  # 80 -> 32768, 443 -> 0 (not published)
        "stop", "run_container", "copy_in",
        "host_port", "host_port",
    ]
    assert "signal" not in runtime.sequence()  # a recreated nginx needs no reload
    assert [spec.name for spec in runtime.runs] == [NAME]


async def test_ensure_recreates_a_crashed_container_rather_than_signalling_it(tmp_path):
    # `docker kill -s HUP` on an exited container reloads nothing; the only way
    # back to serving is a re-run. No port is read off the exited container --
    # real docker answers `{}` for one, so the read could only ever say 0.
    runtime = FakeRuntime(
        statuses={NAME: "exited"}, ports={(NAME, 80): 32768}, next_ports={80: 32768},
    )

    await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, (_listener(80),))

    assert runtime.sequence() == [
        "status", "stop", "run_container", "copy_in", "host_port",
    ]


# --- ensure: a publish failure never becomes an endpoint --------------------
# `ensure`'s return value is written straight onto the load-balancer record as
# `endpoints`, which `elbv2ctl.endpoint_url` turns into `ALB_ENDPOINT` and
# `gateway/wiring.py::producer_facts` INJECTS INTO A REAL CONSUMER CONTAINER.
# A host port of 0 is "nothing is published", so returning one hands a workload
# `http://127.0.0.1:0` behind a load balancer reporting `active`/healthy.


async def test_ensure_refuses_to_return_a_zero_port_from_the_recreate_path(tmp_path):
    # The container the daemon started is gone by the time its ports are read --
    # nginx rejecting the rendered config and exiting is the real way this
    # happens, and real docker then reports the port map as `{}` (measured:
    # "t=+0.2s status=exited host_port=0").
    runtime = FakeRuntime(status_after_run="exited", logtails={
        NAME: 'nginx: [emerg] invalid parameter "bogus:8080" in /etc/nginx/conf.d/odin.conf:2',
    })

    with pytest.raises(PortsUnpublished) as raised:
        await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, (_listener(80),))

    # Honesty rule 2: the reason names the resource, its real state, and WHY --
    # not just "failed". `elbv2ctl._converge_safely` stores this as the load
    # balancer's `State.Reason`.
    message = str(raised.value)
    assert NAME in message
    assert "[80]" in message
    assert "container is exited" in message
    assert "invalid parameter" in message


async def test_ensure_refuses_a_zero_port_even_when_only_one_listener_is_dead(tmp_path):
    # All-or-nothing: a load balancer with a live :80 and a dead :443 has no
    # honest `endpoints` map to publish, because a consumer that resolves the
    # second one gets a dead address.
    runtime = FakeRuntime(next_ports={80: 32768})  # 443 publishes nothing

    with pytest.raises(PortsUnpublished) as raised:
        await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, (_listener(80), _listener(443)))

    assert "[443]" in str(raised.value)


async def test_ensure_never_returns_a_zero_port_on_the_reload_path_either(tmp_path):
    # The SHAPE, not the instance: the reload path carried this check inline
    # (`all(published.values())`) while the recreate path had none. Both now
    # return through the same gate, so a running container that has lost a
    # published port is recreated -- and if THAT still publishes nothing, the
    # answer is a refusal, never a 0.
    runtime = FakeRuntime(statuses={NAME: "running"}, ports={(NAME, 80): 0})

    with pytest.raises(PortsUnpublished):
        await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, (_listener(80),))

    assert "run_container" in runtime.sequence()  # it did try a recreate first


async def test_ensure_creates_the_container_when_it_has_never_existed(tmp_path):
    # The regression this ordering closes, stated on its own: `host_port` RAISES
    # on an absent container (that is how "could not ask" stays distinct from
    # "nothing published"), so reading ports before checking the status made
    # every CreateLoadBalancer die with PortUnreadable and create nothing.
    runtime = FakeRuntime(next_ports={IDLE_LISTEN_PORT: 32700})
    assert runtime.statuses == {}  # nothing exists

    published = await LoadBalancerProxy(runtime).ensure(tmp_path, ENV, LB, ())

    assert published == {IDLE_LISTEN_PORT: 32700}
    assert [spec.name for spec in runtime.runs] == [NAME]


# --- destroy / target_address ----------------------------------------------


async def test_destroy_force_removes_by_exact_container_name_and_is_idempotent():
    runtime = FakeRuntime()
    proxy = LoadBalancerProxy(runtime)

    await proxy.destroy(ENV, LB)
    await proxy.destroy(ENV, LB)  # absent now -- `stop`'s contract is a no-op, no raise

    assert runtime.calls == [("stop", NAME), ("stop", NAME)]


def test_target_address_dials_back_out_through_the_container_host():
    # An ECS task publishes its port on the HOST, and the proxy container
    # reaches it via Colima's host-gateway alias -- one spelling shared with
    # elbv2ctl so the upstream entry and the health probe agree.
    assert target_address(10080) == f"{CONTAINER_HOST}:10080"
    assert CONTAINER_HOST == "host.docker.internal"
