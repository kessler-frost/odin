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

from odin.runtime.colima import ColimaRuntime, ContainerSpec, _Proc


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


def test_a_failed_log_read_is_empty_not_the_clis_own_error_text():
    # `docker logs` on a vanished container writes "No such container" to
    # stderr and exits nonzero -- merging that in would present a CLI
    # diagnostic as container output.
    runner = LogRunner(stderr="Error response from daemon: No such container: gone", returncode=1)
    assert ColimaRuntime(runner=runner).logs("gone") == ""
