"""M7 (single-host) — LimaRuntime conforms to the protocol + builds the right
nerdctl-in-VM commands. Unit-level (injected subprocess), so deterministic."""
from __future__ import annotations

from odin.runtime.colima import ContainerSpec, _Proc
from odin.runtime.driver import RuntimeDriver
from odin.runtime.lima import LimaRuntime


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.responses: dict[str, _Proc] = {}

    def __call__(self, args):
        self.calls.append(args)
        joined = " ".join(args)
        for key, resp in self.responses.items():
            if key in joined:
                return resp
        return _Proc(0, "")


def test_conforms_to_runtime_driver_protocol():
    rt: RuntimeDriver = LimaRuntime(runner=FakeRunner())
    for method in ("ensure_host", "run_container", "stop", "facts", "stats"):
        assert callable(getattr(rt, method))


def test_run_container_goes_through_nerdctl_in_the_vm():
    runner = FakeRunner()
    runner.responses["nerdctl run"] = _Proc(0, "abc123")
    rt = LimaRuntime(runner=runner)

    handle = rt.run_container(ContainerSpec(
        name="job", image="busybox", env={"K": "v"}, ports={8000: 18080}, command=("true",)))
    assert handle.id == "abc123" and handle.name == "job"

    run_call = next(c for c in runner.calls if "busybox" in c)
    assert run_call[:5] == ["limactl", "shell", "allfather-host", "sudo", "nerdctl"]
    assert "-e" in run_call and "K=v" in run_call
    assert "18080:8000" in run_call and run_call[-1] == "true"


def test_status_and_exit_code_inspect_in_vm():
    runner = FakeRunner()
    runner.responses["State.Status"] = _Proc(0, "exited")
    runner.responses["State.ExitCode"] = _Proc(0, "0")
    rt = LimaRuntime(runner=runner)
    assert rt.status("job") == "exited"
    assert rt.exit_code("job") == 0
    assert rt.facts("job").phase == "crashed"  # exited -> crashed phase
