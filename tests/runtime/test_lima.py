"""M7 (single-host) — LimaRuntime conforms to the protocol + builds the right
nerdctl-in-VM commands. Unit-level (injected subprocess), so deterministic."""
from __future__ import annotations

import pytest

from odin.runtime.colima import ContainerSpec, _Proc
from odin.runtime.driver import RuntimeDriver
from odin.runtime.lima import LimaRuntime


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.responses: dict[str, _Proc] = {}

    async def __call__(self, args, input=None):
        self.calls.append(args)
        joined = " ".join(args)
        for key, resp in self.responses.items():
            if key in joined:
                return resp
        return _Proc(0, "")


def test_conforms_to_runtime_driver_protocol():
    rt: RuntimeDriver = LimaRuntime(runner=FakeRunner())
    for method in ("ensure_host", "run_container", "stop", "facts", "stats", "image_exists", "build"):
        assert callable(getattr(rt, method))


async def test_run_container_goes_through_nerdctl_in_the_vm():
    runner = FakeRunner()
    runner.responses["nerdctl run"] = _Proc(0, "abc123")
    rt = LimaRuntime(runner=runner)

    handle = await rt.run_container(ContainerSpec(
        name="job", image="busybox", env={"K": "v"}, ports={8000: 18080},
        command=("true",), volumes={"/host/conf": "/conf"}))
    assert handle.id == "abc123" and handle.name == "job"

    run_call = next(c for c in runner.calls if "busybox" in c)
    assert run_call[:5] == ["limactl", "shell", "odin-host", "sudo", "nerdctl"]
    assert "-e" in run_call and "K=v" in run_call
    assert "-v" in run_call and "/host/conf:/conf" in run_call
    assert run_call.index("-v") < run_call.index("busybox")  # flags before the image, or nerdctl eats them
    assert "18080:8000" in run_call and run_call[-1] == "true"


async def test_status_and_exit_code_inspect_in_vm():
    runner = FakeRunner()
    runner.responses["State.Status"] = _Proc(0, "exited")
    runner.responses["State.ExitCode"] = _Proc(0, "0")
    rt = LimaRuntime(runner=runner)
    assert await rt.status("job") == "exited"
    assert await rt.exit_code("job") == 0
    assert (await rt.facts("job")).phase == "crashed"  # exited -> crashed phase


async def test_build_pipes_the_dockerfile_through_limactl_shell_into_nerdctl():
    class RecordingRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.inputs: list[str | None] = []

        async def __call__(self, args, input=None):
            self.inputs.append(input)
            return await super().__call__(args, input=input)

    runner = RecordingRunner()
    rt = LimaRuntime(runner=runner)

    await rt.build("odin-dynalite:1", "FROM node:20-alpine\n")

    build_call = next(c for c in runner.calls if "build" in c)
    assert build_call == [
        "limactl", "shell", "odin-host", "sudo", "nerdctl",
        "build", "-t", "odin-dynalite:1", "-",
    ]
    assert runner.inputs == ["FROM node:20-alpine\n"]


async def test_image_exists_inspects_through_the_vm():
    runner = FakeRunner()
    runner.responses["image inspect"] = _Proc(0, "sha256:abc")
    rt = LimaRuntime(runner=runner)
    assert await rt.image_exists("odin-dynalite:1") is True


async def test_logs_runs_nerdctl_logs_tail_in_the_vm():
    # LimaRuntime.logs is inherited from _ContainerRuntime unchanged -- this
    # locks its exact command shape now that observability code depends on it.
    # `--timestamps` is what lets the two streams be merged chronologically
    # (field test 2, HIGH-3).
    runner = FakeRunner()
    runner.responses["nerdctl logs"] = _Proc(0, "line1\nline2\n")
    rt = LimaRuntime(runner=runner)
    assert await rt.logs("job", tail=5) == "line1\nline2"
    logs_call = next(c for c in runner.calls if "logs" in c)
    assert logs_call == [
        "limactl", "shell", "odin-host", "sudo", "nerdctl", "logs", "--timestamps", "--tail", "5", "job",
    ]


async def test_logs_in_the_vm_keeps_the_containers_stderr_half_too():
    # The nerdctl side of field test 2's HIGH-3: an nginx/Postgres inside the
    # shared VM logs to stderr, and `limactl shell` hands that back on ITS
    # stderr.
    runner = FakeRunner()
    runner.responses["nerdctl logs"] = _Proc(
        0,
        "2026-07-25T14:00:01.000000000Z started\n",
        "2026-07-25T14:00:02.000000000Z crashed: bad config\n",
    )
    assert (await LimaRuntime(runner=runner).logs("job")).splitlines() == [
        "started", "crashed: bad config",
    ]


async def test_image_exists_false_on_dockers_empty_array_stdout():
    # REAL docker/nerdctl prints literal "[]" to stdout (rc=1) for a missing
    # image — a truthy string. This exact behavior skipped the dynalite image
    # build in S5's e2e (bool("[]") is True), so the inspect MUST use a
    # format template whose output is empty when the image is absent.
    runner = FakeRunner()
    runner.responses["image inspect"] = _Proc(1, "[]")
    rt = LimaRuntime(runner=runner)
    assert await rt.image_exists("odin-dynalite:1") is False


# --- the OUTER seam's failure text ------------------------------------------
#
# `_lima` (limactl itself, as opposed to `_cli`'s nerdctl-in-the-VM) raised
# `f"limactl … failed: {proc.stderr.strip()}"`, an exact twin of
# `compute/instances.py::_lima`. Measured against REAL limactl 2.1.3 and a REAL
# Lima VM, `limactl shell <vm> -- sh -c 'exit 3'` is rc=3 with BOTH streams
# empty and `... 'echo on-stdout; exit 7'` puts the reason on STDOUT -- see
# tests/test_compute/test_instances.py for the full probe. Fixed as a twin, from
# ONE `_failure_reason`, so the two cannot drift apart again.


async def test_a_limactl_failure_with_no_output_names_the_exit_code():
    runner = FakeRunner()
    runner.responses["limactl create"] = _Proc(3, "", "")
    with pytest.raises(RuntimeError) as raised:
        await LimaRuntime(runner=runner)._lima(
            "create", "--tty=false", "--name=odin-host", "/tmp/x.yaml")
    message = str(raised.value)
    assert message == (
        "limactl create --tty=false --name=odin-host /tmp/x.yaml failed (exit 3): "
        "it wrote nothing to stderr or stdout, so the exit code is the whole of it"
    )
    assert not message.rstrip().endswith(":")


async def test_a_limactl_failure_that_spoke_on_stdout_is_not_reported_as_silent():
    runner = FakeRunner()
    runner.responses["limactl start"] = _Proc(7, "on-stdout\n", "")
    with pytest.raises(RuntimeError) as raised:
        await LimaRuntime(runner=runner)._lima("start", "odin-host")
    assert str(raised.value) == (
        "limactl start odin-host failed (exit 7): nothing on stderr; on stdout: on-stdout"
    )


async def test_ensure_host_surfaces_a_real_limactl_error_with_its_exit_code():
    """The reachable path: `ensure_host` creates+starts the shared VM with
    `check=True`, so this text is what a caller actually sees."""
    runner = FakeRunner()
    runner.responses["limactl start"] = _Proc(1, "", 'level=fatal msg="no such template"\n')
    with pytest.raises(RuntimeError) as raised:
        await LimaRuntime(runner=runner).ensure_host()
    assert str(raised.value) == (
        'limactl start odin-host failed (exit 1): level=fatal msg="no such template"'
    )
