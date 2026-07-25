"""ColimaRuntime builds the right `docker run` argv -- unit-level (injected
subprocess), the same shape tests/runtime/test_lima.py uses for the nerdctl
side. tests/runtime/test_colima.py is the integration half that runs real
containers.

W2.6: the namespace-sharing flags a nebula mesh sidecar needs
(`fabric/sidecar.py`).
"""
from __future__ import annotations

from odin.runtime.colima import ColimaRuntime, ContainerSpec, _Proc


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None):
        self.calls.append(args)
        return _Proc(0, "cid")


def _run_call(runner: FakeRunner) -> list[str]:
    return next(c for c in runner.calls if "run" in c)


def test_ordinary_container_gets_the_host_gateway_alias():
    runner = FakeRunner()
    ColimaRuntime(runner=runner).run_container(ContainerSpec(name="pg", image="postgres:16-alpine", ports={5432: 0}))
    call = _run_call(runner)
    assert "--add-host" in call and "host.docker.internal:host-gateway" in call
    assert "--network" not in call


def test_namespace_sharing_sidecar_gets_tun_capabilities_and_no_add_host():
    """`--add-host` together with `--network container:` is a hard docker
    error ("conflicting options"), and a sidecar needs none -- it inherits the
    target's networking wholesale. NET_ADMIN + /dev/net/tun are what let
    nebula create the overlay device INSIDE the backing's namespace (a
    container capability, never host root)."""
    runner = FakeRunner()
    ColimaRuntime(runner=runner).run_container(ContainerSpec(
        name="pg-mesh", image="odin-nebula:1.10.3", network="container:pg",
        cap_add=("NET_ADMIN",), devices=("/dev/net/tun",),
        volumes={"/Users/x/.odin/prod/nebula/members/pg": "/etc/nebula"},
    ))
    call = _run_call(runner)
    assert "--add-host" not in call
    assert call[call.index("--network") + 1] == "container:pg"
    assert call[call.index("--cap-add") + 1] == "NET_ADMIN"
    assert call[call.index("--device") + 1] == "/dev/net/tun"
    assert "-v" in call and "/Users/x/.odin/prod/nebula/members/pg:/etc/nebula" in call
    assert call.index("--network") < call.index("odin-nebula:1.10.3")  # flags before the image
