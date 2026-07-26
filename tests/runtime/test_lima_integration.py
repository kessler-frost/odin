"""M7 (single-host) — LimaRuntime actually runs a container inside a real Lima VM.

Marked `integration`: boots a Lima VM (slow). Cleans up the VM afterwards.

WHY THE PORT-READ TESTS ARE HERE AND NOT ONLY IN `test_lima.py`. `host_port`
was rewritten (honesty rule 1) to read `inspect {{json .NetworkSettings.Ports}}`
instead of `<cli> port`, because `<cli> port` exits 1 for BOTH "nothing is
published there" and "no such container" and so cannot be asked honestly — a
failed read silently became port 0, which `BackingAws.facts` then wrote into a
node's `endpoint` fact permanently. That rewrite was proven against real
`docker`, and `LimaRuntime` INHERITED it without ever being run: nerdctl is a
different binary whose inspect output and exit codes are not guaranteed to
match docker's. A unit test with hand-written strings would only have proved
the parser again. These run against a real container inside a real VM.
"""
from __future__ import annotations

import time

import pytest

from odin.runtime.colima import ContainerSpec, PortUnreadable
from odin.runtime.lima import LimaRuntime

pytestmark = pytest.mark.integration

NAME = "lima-odin-test"
PORT_NAME = "lima-odin-port"
NO_PORT_NAME = "lima-odin-noport"
ABSENT_NAME = "lima-odin-never-created"
INSIDE_PORT = 80
HOST_PORT = 18080


def _await_running(rt: LimaRuntime, name: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and rt.status(name) != "running":
        time.sleep(2)


@pytest.fixture(scope="module")
def lima():
    """ONE VM for the whole module (a boot is minutes), deleted by exact name
    afterwards — the repo rule is that no stray VM survives a test run."""
    rt = LimaRuntime()
    rt.ensure_host()  # boots the VM + nerdctl (idempotent if it already exists)
    yield rt
    for name in (NAME, PORT_NAME, NO_PORT_NAME):
        rt.stop(name)
    rt._lima("delete", "--force", rt.VM, check=False)  # reclaim the VM disk


def test_runs_a_container_in_a_real_vm(lima):
    handle = lima.run_container(ContainerSpec(name=NAME, image="busybox", command=("sleep", "60")))
    assert handle.id

    _await_running(lima, NAME)
    assert lima.status(NAME) == "running"  # container is live inside the VM

    lima.stop(NAME)
    assert lima.status(NAME) == "absent"


def test_host_port_reads_a_real_published_port_through_nerdctl(lima):
    """Probed on a real VM — `nerdctl inspect -f '{{json .NetworkSettings.Ports}}'`
    on a container published with `-p 18080:80`:

        STDOUT: {"80/tcp":[{"HostIp":"0.0.0.0","HostPort":"18080"}]}
        STDERR: (empty)
        RC: 0

    Same SHAPE as docker's (docker adds a second `{"HostIp":"::"}` binding for
    the same port; `host_port` reads `[0]["HostPort"]`, so both agree)."""
    lima.stop(PORT_NAME)
    lima.run_container(ContainerSpec(
        name=PORT_NAME, image="busybox", ports={INSIDE_PORT: HOST_PORT}, command=("sleep", "600"),
    ))
    _await_running(lima, PORT_NAME)

    assert lima.host_port(PORT_NAME, INSIDE_PORT) == HOST_PORT
    assert lima.facts(PORT_NAME, container_port=INSIDE_PORT).host_port == HOST_PORT


def test_host_port_is_zero_when_nerdctl_answers_and_nothing_is_published(lima):
    """The middle state, and the whole point of the rewrite: the runtime DID
    answer, and the honest answer is "nothing published there". Probed:

        $ nerdctl inspect -f '{{json .NetworkSettings.Ports}}' <no -p container>
        STDOUT: {}                                                      RC: 0
        $ nerdctl port <same container> 80
        STDOUT: (empty)
        STDERR: level=fatal msg="no public port 80/tcp published for ..."  RC: 1

    That second pair is the trap the fix exists to avoid, confirmed present on
    nerdctl too: rc=1 here is indistinguishable from rc=1 for a container that
    does not exist (next test), so `<cli> port` could not tell them apart on
    EITHER runtime. The port map can: rc 0 means answered."""
    lima.stop(NO_PORT_NAME)
    lima.run_container(ContainerSpec(name=NO_PORT_NAME, image="busybox", command=("sleep", "600")))
    _await_running(lima, NO_PORT_NAME)

    assert lima.host_port(NO_PORT_NAME, INSIDE_PORT) == 0  # answered: none


def test_host_port_raises_rather_than_returning_zero_for_a_container_nerdctl_cannot_find(lima):
    """The state that used to corrupt a fact. Probed:

        $ nerdctl inspect -f '{{json .NetworkSettings.Ports}}' no-such-container-xyz
        STDOUT: (empty)
        STDERR: level=fatal msg="1 errors: [no such object no-such-container-xyz]"
        RC: 1

    (docker: rc=1, `error: no such object: no-such-container-xyz` — same
    contract, different wording.) A 0 here would be shaped exactly like a real
    port and would be written into an `endpoint` fact forever, so this must
    raise."""
    with pytest.raises(PortUnreadable, match=ABSENT_NAME):
        lima.host_port(ABSENT_NAME, INSIDE_PORT)


def test_facts_does_not_ask_an_absent_container_for_its_port(lima):
    """`facts()` (inherited from `_ContainerRuntime`) gates the port read on
    `status != "absent"`, and that gate is what keeps the raise above from
    reaching every caller that merely observes a not-yet-created container.
    Verified against the real nerdctl, not assumed: an absent container's
    `inspect {{.State.Status}}` is rc=1 + empty stdout, which `status()` turns
    into "absent"."""
    assert lima.status(ABSENT_NAME) == "absent"
    facts = lima.facts(ABSENT_NAME, container_port=INSIDE_PORT)
    assert facts.phase == "pending" and facts.host_port == 0 and facts.logtail == ""
