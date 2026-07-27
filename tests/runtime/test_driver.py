"""S1.3 — ColimaRuntime conforms to the RuntimeDriver Protocol + phase mapping."""
from __future__ import annotations

import pytest

from odin.runtime.colima import ColimaRuntime, ContainerSpec, _Proc, _STATUS_TO_PHASE
from odin.runtime.driver import RuntimeDriver


def test_colima_satisfies_runtime_driver_protocol():
    rt: RuntimeDriver = ColimaRuntime()  # structural — must have all methods
    for method in ("ensure_host", "run_container", "stop", "facts", "stats", "image_exists", "build"):
        assert callable(getattr(rt, method))


async def test_stop_removes_anonymous_volumes_too():
    # Without -v every removed container leaks its anonymous volumes (postgres
    # creates one per boot) — a container-churn loop fills the disk.
    calls: list[list[str]] = []

    async def runner(args, input=None):
        calls.append(args)
        return _Proc(0, "")

    await ColimaRuntime(runner=runner).stop("job")
    assert calls == [["docker", "rm", "-f", "-v", "job"]]


def test_status_to_phase_mapping():
    assert _STATUS_TO_PHASE["running"] == "starting"   # healthy is an assertion's call
    assert _STATUS_TO_PHASE["exited"] == "crashed"
    assert _STATUS_TO_PHASE["absent"] == "pending"


async def test_image_exists_inspects_the_tag_without_raising_when_absent():
    calls: list[list[str]] = []

    async def runner(args, input=None):
        calls.append(args)
        return _Proc(1, "")  # docker image inspect exits nonzero for an unknown tag

    assert await ColimaRuntime(runner=runner).image_exists("odin-dynalite:1") is False
    # The Id template is load-bearing: plain inspect prints a truthy "[]" for
    # a MISSING image, which made image_exists lie and skip the dynalite bake.
    assert calls == [["docker", "image", "inspect", "-f", "{{.Id}}", "odin-dynalite:1"]]


async def test_image_exists_true_when_inspect_returns_json():
    async def runner(args, input=None):
        return _Proc(0, '[{"Id": "sha256:abc"}]')

    assert await ColimaRuntime(runner=runner).image_exists("odin-dynalite:1") is True


async def test_build_pipes_the_dockerfile_on_stdin_with_no_context_dir():
    calls: list[tuple[list[str], str | None]] = []

    async def runner(args, input=None):
        calls.append((args, input))
        return _Proc(0, "")

    await ColimaRuntime(runner=runner).build("odin-dynalite:1", "FROM node:20-alpine\n")

    assert calls == [(["docker", "build", "-t", "odin-dynalite:1", "-"], "FROM node:20-alpine\n")]


async def test_build_raises_on_failure_same_as_any_other_cli_call():
    async def runner(args, input=None):
        return _Proc(1, "", "some build error")

    with pytest.raises(RuntimeError, match="some build error"):
        await ColimaRuntime(runner=runner).build("odin-dynalite:1", "FROM node:20-alpine\n")


# --- owner directive B4: --memory/--cpus, only when the spec sets them ------


async def test_run_container_emits_no_memory_or_cpu_flags_when_unset():
    calls: list[list[str]] = []

    async def runner(args, input=None):
        calls.append(args)
        return _Proc(0, "container-id")

    await ColimaRuntime(runner=runner).run_container(ContainerSpec(name="job", image="alpine"))
    assert "--memory" not in calls[0]
    assert "--cpus" not in calls[0]


async def test_run_container_emits_memory_and_cpus_when_set():
    calls: list[list[str]] = []

    async def runner(args, input=None):
        calls.append(args)
        return _Proc(0, "container-id")

    await ColimaRuntime(runner=runner).run_container(
        ContainerSpec(name="job", image="alpine", memory_mib=512.0, cpus=1.5)
    )
    args = calls[0]
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--cpus") + 1] == "1.5"


# --- W2.5: signal + copy_in, the two methods compute/proxy.py added ---------


async def test_signal_delivers_the_named_signal_to_the_containers_main_process():
    calls: list[list[str]] = []

    async def runner(args, input=None):
        calls.append(args)
        return _Proc(0, "")

    await ColimaRuntime(runner=runner).signal("odin-alb-default-web", "HUP")
    # This exact argv is how a load-balancer proxy is told to re-read its
    # rewritten config: nginx reloads on SIGHUP, so an upstream change needs
    # neither a `docker exec` seam nor a container recreate -- and never drops
    # an in-flight request.
    assert calls == [["docker", "kill", "-s", "HUP", "odin-alb-default-web"]]


async def test_signal_tolerates_a_nonzero_exit_like_stop_does():
    # check=False: signalling an already-gone container is a no-op, exactly
    # like `stop`. A converge that races a delete must not raise.
    async def runner(args, input=None):
        return _Proc(1, "", "Error response from daemon: No such container: gone")

    assert await ColimaRuntime(runner=runner).signal("gone", "HUP") is None


async def test_copy_in_streams_the_host_file_into_the_container_through_the_daemon():
    calls: list[list[str]] = []

    async def runner(args, input=None):
        calls.append(args)
        return _Proc(0, "")

    await ColimaRuntime(runner=runner).copy_in(
        "odin-alb-default-web", "/host/odin.conf", "/etc/nginx/conf.d/odin.conf",
    )
    # `docker cp` instead of a `-v` bind mount, found the hard way: a mount of a
    # path under macOS's per-user temp dir silently resolves to an EMPTY
    # directory under Colima's virtiofs, so nginx came up with no config at all.
    assert calls == [[
        "docker", "cp", "/host/odin.conf", "odin-alb-default-web:/etc/nginx/conf.d/odin.conf",
    ]]


async def test_copy_in_raises_on_a_failed_copy_unlike_signal():
    # Deliberate asymmetry with `signal`/`stop` (which pass check=False):
    # `copy_in` leaves `check` at its default True, so a failed delivery is
    # LOUD. It has to be -- the proxy container's entrypoint blocks in a wait
    # loop until the config file appears, so a silently-dropped copy would be a
    # container that never serves anything and never explains why. elbv2ctl's
    # `_converge_safely` turns this raise into the lb's honest `failed` state.
    async def runner(args, input=None):
        return _Proc(1, "", "no such directory")

    with pytest.raises(RuntimeError, match="no such directory"):
        await ColimaRuntime(runner=runner).copy_in(
            "job", "/host/odin.conf", "/etc/nginx/conf.d/odin.conf",
        )
