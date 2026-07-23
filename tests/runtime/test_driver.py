"""S1.3 — ColimaRuntime conforms to the RuntimeDriver Protocol + phase mapping."""
from __future__ import annotations

import pytest

from odin.runtime.colima import ColimaRuntime, _Proc, _STATUS_TO_PHASE
from odin.runtime.driver import RuntimeDriver


def test_colima_satisfies_runtime_driver_protocol():
    rt: RuntimeDriver = ColimaRuntime()  # structural — must have all methods
    for method in ("ensure_host", "run_container", "stop", "facts", "stats", "image_exists", "build"):
        assert callable(getattr(rt, method))


def test_stop_removes_anonymous_volumes_too():
    # Without -v every removed container leaks its anonymous volumes (postgres
    # creates one per boot) — a container-churn loop fills the disk.
    calls: list[list[str]] = []

    def runner(args, input=None):
        calls.append(args)
        return _Proc(0, "")

    ColimaRuntime(runner=runner).stop("job")
    assert calls == [["docker", "rm", "-f", "-v", "job"]]


def test_status_to_phase_mapping():
    assert _STATUS_TO_PHASE["running"] == "starting"   # healthy is an assertion's call
    assert _STATUS_TO_PHASE["exited"] == "crashed"
    assert _STATUS_TO_PHASE["absent"] == "pending"


def test_image_exists_inspects_the_tag_without_raising_when_absent():
    calls: list[list[str]] = []

    def runner(args, input=None):
        calls.append(args)
        return _Proc(1, "")  # docker image inspect exits nonzero for an unknown tag

    assert ColimaRuntime(runner=runner).image_exists("odin-dynalite:1") is False
    # The Id template is load-bearing: plain inspect prints a truthy "[]" for
    # a MISSING image, which made image_exists lie and skip the dynalite bake.
    assert calls == [["docker", "image", "inspect", "-f", "{{.Id}}", "odin-dynalite:1"]]


def test_image_exists_true_when_inspect_returns_json():
    def runner(args, input=None):
        return _Proc(0, '[{"Id": "sha256:abc"}]')

    assert ColimaRuntime(runner=runner).image_exists("odin-dynalite:1") is True


def test_build_pipes_the_dockerfile_on_stdin_with_no_context_dir():
    calls: list[tuple[list[str], str | None]] = []

    def runner(args, input=None):
        calls.append((args, input))
        return _Proc(0, "")

    ColimaRuntime(runner=runner).build("odin-dynalite:1", "FROM node:20-alpine\n")

    assert calls == [(["docker", "build", "-t", "odin-dynalite:1", "-"], "FROM node:20-alpine\n")]


def test_build_raises_on_failure_same_as_any_other_cli_call():
    def runner(args, input=None):
        return _Proc(1, "", "some build error")

    with pytest.raises(RuntimeError, match="some build error"):
        ColimaRuntime(runner=runner).build("odin-dynalite:1", "FROM node:20-alpine\n")
