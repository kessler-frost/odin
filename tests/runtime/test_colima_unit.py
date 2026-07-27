"""ColimaRuntime builds the right `docker run` argv -- unit-level (injected
subprocess), the same shape tests/runtime/test_lima.py uses for the nerdctl
side. tests/runtime/test_colima.py is the integration half that runs real
containers.

W2.6: the namespace-sharing flags a nebula mesh sidecar needs
(`fabric/sidecar.py`).

Field test 2 (HIGH-3): `logs()` reads BOTH streams and tails the COMBINED
result -- a settled Postgres, an nginx and a Lambda traceback are all on
stderr, and keeping stdout alone reported an empty log for a container that
was talking constantly.
"""
from __future__ import annotations

import pytest

from odin.runtime.colima import ColimaRuntime, ContainerSpec, PortUnreadable, _Proc


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None):
        self.calls.append(args)
        return _Proc(0, "cid")


class LogRunner:
    """A runner that answers the log read with a separate stdout and stderr,
    exactly as `docker logs` does."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.calls: list[list[str]] = []
        self._proc = _Proc(returncode, stdout, stderr)

    def __call__(self, args, input=None):
        self.calls.append(args)
        return self._proc


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


# --- field test 2, HIGH-3: logs() must not discard the stderr stream --------


def test_logs_asks_for_timestamps_so_the_two_streams_can_be_merged():
    runner = LogRunner()
    ColimaRuntime(runner=runner).logs("pg", tail=10)
    assert runner.calls == [["docker", "logs", "--timestamps", "--tail", "10", "pg"]]


def test_logs_returns_the_stderr_stream_a_settled_postgres_only_writes_to():
    # THE field-verified case: `docker logs --tail N` on a settled Postgres
    # puts every line on stderr, so reading stdout alone reported 0 bytes for
    # a database that was logging normally.
    runner = LogRunner(stderr=(
        "2026-07-25T14:00:01.000000000Z LOG:  database system is ready\n"
        "2026-07-25T14:00:02.000000000Z LOG:  checkpoint starting\n"
    ))
    assert ColimaRuntime(runner=runner).logs("pg") == (
        "LOG:  database system is ready\nLOG:  checkpoint starting"
    )


def test_logs_merges_the_two_streams_in_the_runtimes_own_timestamp_order():
    runner = LogRunner(
        stdout=(
            "2026-07-25T14:00:01.000000000Z out-first\n"
            "2026-07-25T14:00:03.000000000Z out-third\n"
        ),
        stderr=(
            "2026-07-25T14:00:02.000000000Z err-second\n"
            "2026-07-25T14:00:04.000000000Z err-fourth\n"
        ),
    )
    assert ColimaRuntime(runner=runner).logs("app").splitlines() == [
        "out-first", "err-second", "out-third", "err-fourth",
    ]


def test_the_tail_is_applied_after_combining_so_tail_n_means_n_real_lines():
    # The second half of the bug: `docker logs --tail N` selects N lines across
    # BOTH streams, so dropping one stream made `--tail 10` mean "however many
    # of those 10 happened to be stdout" -- often zero.
    runner = LogRunner(
        stdout="2026-07-25T14:00:01.000000000Z out-1\n2026-07-25T14:00:03.000000000Z out-3\n",
        stderr="2026-07-25T14:00:02.000000000Z err-2\n2026-07-25T14:00:04.000000000Z err-4\n",
    )
    lines = ColimaRuntime(runner=runner).logs("app", tail=3).splitlines()
    assert lines == ["err-2", "out-3", "err-4"]  # the last 3 of the 4 combined


def test_a_line_the_runtime_never_stamped_is_kept_not_dropped():
    # Requiring a parseable stamp to KEEP a line would re-introduce the very
    # bug being fixed for any runtime/version that doesn't stamp.
    runner = LogRunner(stdout="plain stdout line\n", stderr="plain stderr line\n")
    assert ColimaRuntime(runner=runner).logs("app").splitlines() == [
        "plain stdout line", "plain stderr line",
    ]


def test_a_postgres_lines_own_leading_date_is_never_mistaken_for_the_stamp():
    # Postgres prefixes its own `2026-07-25 14:00:01.123 UTC` -- a date-only
    # first token. Treating that as the runtime's stamp would silently eat it.
    runner = LogRunner(stderr=(
        "2026-07-25T14:00:01.000000000Z 2026-07-25 14:00:01.123 UTC [1] LOG:  ready\n"
    ))
    assert ColimaRuntime(runner=runner).logs("pg") == "2026-07-25 14:00:01.123 UTC [1] LOG:  ready"


async def test_a_failed_run_names_the_container_and_the_error_never_the_credentials():
    """Field test 2 finding #6 (security): this exception text becomes an ECS
    task's `stopped_reason`, and from there the World verdict -> the WebSocket
    -> the durable `events.jsonl`. The old message was the whole argv, which
    for any workload container carries `-e AWS_SECRET_ACCESS_KEY=…` (issued by
    `gateway/keys.py::workload_env`) -- and pushed the actual docker error past
    every downstream truncation."""
    def runner(args, input=None):
        return _Proc(1, "", "Unable to find image 'nginx:bogus-9z9z' locally")

    rt = ColimaRuntime(runner=runner)
    with pytest.raises(RuntimeError) as raised:
        await rt.run_container(ContainerSpec(
            name="odin-ecs-wa-a53adf2b-web-svc", image="nginx:bogus-9z9z",
            env={"AWS_ACCESS_KEY_ID": "AKODINFAKEFAKEFAKEFA",
                 "AWS_SECRET_ACCESS_KEY": "fake-issued-secret-000000000000000000000",
                 "DATABASE_URL": "postgresql://app:fake-db-password@host.docker.internal:33366/appdb"},
        ))
    message = str(raised.value)
    assert "AKODINFAKEFAKEFAKEFA" not in message
    assert "fake-issued-secret-000000000000000000000" not in message
    assert "fake-db-password" not in message
    # …and it still says which container died and why, up front.
    assert message == (
        "docker run odin-ecs-wa-a53adf2b-web-svc failed (exit 1): "
        "Unable to find image 'nginx:bogus-9z9z' locally"
    )


def test_a_failed_command_with_no_named_container_still_names_the_subcommand():
    def runner(args, input=None):
        return _Proc(1, "", "no such directory")

    with pytest.raises(RuntimeError, match=r"^docker cp failed \(exit 1\): no such directory$"):
        ColimaRuntime(runner=runner).copy_in("job", "/host/odin.conf", "/etc/nginx/odin.conf")


# --- a failed command that said NOTHING (OPEN-BUGS #5) ----------------------
#
# These three pin the shape probed against the REAL docker 28.4.0 on Colima,
# whose output is recorded in `_failure_reason`'s own comment: `docker exec <c>
# sh -c 'exit 1'` is rc=1 with BOTH streams empty, `... 'echo on-stdout; exit
# 7'` is rc=7 with the reason on STDOUT, and `docker run --rm alpine sh -c 'exit
# 3'` is rc=3 with both empty. The runners below replay exactly those three
# triples -- the parser is what is under test here; the integration was proved
# by running the real commands.


def test_a_failure_with_no_output_at_all_still_states_a_reason():
    """The bug: `f"{CLI} {label} failed: {stderr.strip()}"` rendered `docker run
    x failed: ` -- a sentence whose reason is a dangling colon. The exit code
    was there the whole time (it is why we are raising) and was dropped."""
    def runner(args, input=None):
        return _Proc(3, "", "")

    with pytest.raises(RuntimeError) as raised:
        ColimaRuntime(runner=runner).run_container(ContainerSpec(name="x", image="alpine:latest"))
    message = str(raised.value)
    assert message == (
        "docker run x failed (exit 3): it wrote nothing to stderr or stdout, "
        "so the exit code is the whole of it"
    )
    # The property that matters more than the exact wording: nothing trails off.
    assert not message.rstrip().endswith(":")


def test_a_failure_that_explained_itself_on_stdout_is_not_reported_as_silent():
    """Real case, measured: `docker exec <c> sh -c 'echo on-stdout; exit 7'` is
    rc=7, stderr empty, reason on stdout. `_cli` keeps only stdout on success
    and only stderr on failure, so this reason reached nobody."""
    def runner(args, input=None):
        return _Proc(7, "on-stdout\n", "")

    with pytest.raises(RuntimeError) as raised:
        ColimaRuntime(runner=runner).copy_in("job", "/host/f", "/etc/f")
    assert str(raised.value) == "docker cp failed (exit 7): nothing on stderr; on stdout: on-stdout"


def test_an_unreadable_port_map_names_the_exit_code_and_the_silence_too():
    """`host_port`'s PortUnreadable carried the same defect in a milder form --
    it had `or 'no output'`, but threw the exit code away. One wording now."""
    def runner(args, input=None):
        return _Proc(125, "", "")

    with pytest.raises(PortUnreadable) as raised:
        ColimaRuntime(runner=runner).host_port("gone", 8080)
    message = str(raised.value)
    assert message == (
        "docker cannot read gone's published ports (exit 125): it wrote nothing "
        "to stderr or stdout, so the exit code is the whole of it"
    )
    assert not message.rstrip().endswith(":")


def test_a_failed_log_read_is_empty_not_the_clis_own_error_text():
    # `docker logs` on a vanished container writes "No such container" to
    # stderr and exits nonzero -- merging that in would present a CLI
    # diagnostic as container output.
    runner = LogRunner(stderr="Error response from daemon: No such container: gone", returncode=1)
    assert ColimaRuntime(runner=runner).logs("gone") == ""


# --- field test 5 facts audit: a failed port read must never look like port 0 -
#
# Every payload below was captured from the real `docker` on this machine (see
# `host_port`'s docstring for the transcript) -- these are the runtime's own
# answers, not a fabricated upstream signal.


class PortRunner:
    """Answers the port-map read with a captured `docker inspect` result."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.calls: list[list[str]] = []
        self._proc = _Proc(returncode, stdout, stderr)

    def __call__(self, args, input=None):
        self.calls.append(args)
        return self._proc


_RUNNING = ('{"9000/tcp":[{"HostIp":"0.0.0.0","HostPort":"33776"},'
            '{"HostIp":"::","HostPort":"33776"}],"9001/tcp":null}')


def test_host_port_reads_the_structured_port_map_not_a_text_line():
    runner = PortRunner(stdout=_RUNNING)
    assert ColimaRuntime(runner=runner).host_port("odin-aws-rustfs-prod", 9000) == 33776
    assert runner.calls == [
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", "odin-aws-rustfs-prod"],
    ]


def test_a_port_the_container_publishes_nothing_on_is_an_honest_zero():
    # rc 0 means the runtime ANSWERED; "9001/tcp": null is that answer.
    assert ColimaRuntime(runner=PortRunner(stdout=_RUNNING)).host_port("rustfs", 9001) == 0
    # An exited container's whole map is `{}` -- same shape of answer.
    assert ColimaRuntime(runner=PortRunner(stdout="{}")).host_port("dead", 9000) == 0


def test_a_port_read_that_FAILED_raises_instead_of_returning_zero():
    """THE fix. `host_port` used to end `return int(...) if out else 0`, so any
    CLI failure produced 0 -- and 0 is shaped like a real port, so
    `BackingAws.facts` interpolated it into a durable `endpoint` fact that is
    written once and never refreshed. `docker port` cannot be asked honestly
    (it exits 1 both for "nothing published there" and for "no such
    container"); the port MAP can, and a nonzero rc there is a real failure."""
    runner = PortRunner(stderr="error: no such object: gone", returncode=1)
    with pytest.raises(PortUnreadable, match="cannot read gone's published ports"):
        ColimaRuntime(runner=runner).host_port("gone", 9000)


def test_facts_never_asks_an_absent_container_for_a_port():
    """An absent container has no port map, so `facts()` must not ask for one
    -- the raise above is for a runtime that failed, not for "there is nothing
    there", which `status` already reports as `pending`."""
    calls: list[list[str]] = []

    def runner(args, input=None):
        calls.append(args)
        return _Proc(1, "", "Error: No such object: gone")

    facts = ColimaRuntime(runner=runner).facts("gone", container_port=9000)
    assert (facts.phase, facts.host_port) == ("pending", 0)
    assert not [c for c in calls if "{{json .NetworkSettings.Ports}}" in c]
