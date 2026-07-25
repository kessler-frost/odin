"""`tests/containers.py::own_containers` -- the scoped teardown filter every
integration file now uses instead of `runtime.list_odin()`.

Unit-tested (not integration) precisely because the thing it must never do is
too expensive to discover for real: a filter that is too WIDE stops a
bystander's containers, and you only find out afterwards. The naming rules it
encodes are odin's own (`aws/backings.py`, `aws/rds.py`, `aws/cache.py`,
`compute/tasks.py`, `compute/functions.py`, `compute/proxy.py`,
`fabric/sidecar.py`), so the cases below are built from those builders.
"""
from __future__ import annotations

from tests.containers import own_containers


class FakeRuntime:
    def __init__(self, *names: str) -> None:
        self._names = list(names)

    def container_names(self) -> list[str]:
        return list(self._names)


_MACHINE = FakeRuntime(
    # env "bak" -- the caller's own
    "odin-aws-rustfs-bak", "odin-aws-goaws-bak", "odin-rds-bak-appdb", "odin-aws-rustfs-bak-mesh",
    # env "bak2" -- a longer env sharing the prefix, NOT the caller's
    "odin-aws-rustfs-bak2", "odin-rds-bak2-appdb",
    # somebody else's envs entirely
    "odin-aws-dynalite-prod", "odin-ecs-prod-1a2b3c4d-web", "odin-lambda-other-notify",
)


def test_it_finds_every_naming_form_for_its_own_env():
    assert own_containers(_MACHINE, "bak") == sorted([
        "odin-aws-rustfs-bak",       # suffix form: odin-aws-{backing}-{env}
        "odin-aws-goaws-bak",
        "odin-rds-bak-appdb",        # infix form: odin-rds-{env}-{id}
        "odin-aws-rustfs-bak-mesh",  # a nebula sidecar, named off its target
    ])


def test_a_longer_env_sharing_the_prefix_is_never_matched():
    """The `-` anchor is the whole point: `bak2` is a different env, and a
    teardown that swept it would be the same bug at a smaller scale."""
    mine = own_containers(_MACHINE, "bak")
    assert not [name for name in mine if "bak2" in name]


def test_another_envs_containers_are_left_alone():
    mine = own_containers(_MACHINE, "bak")
    assert "odin-aws-dynalite-prod" not in mine
    assert "odin-ecs-prod-1a2b3c4d-web" not in mine
    assert "odin-lambda-other-notify" not in mine


def test_several_envs_can_be_claimed_at_once():
    """A test that applies to `a` and `b` owns both -- and still nothing else."""
    runtime = FakeRuntime("odin-aws-rustfs-a", "odin-aws-rustfs-b", "odin-aws-rustfs-apply-full-e2e")
    assert own_containers(runtime, "a", "b") == ["odin-aws-rustfs-a", "odin-aws-rustfs-b"]


def test_a_single_letter_env_does_not_match_a_longer_one_starting_with_it():
    """`apply-full-e2e` contains `-a`, but never `-a-` nor a trailing `-a`."""
    runtime = FakeRuntime("odin-aws-rustfs-apply-full-e2e", "odin-rds-apply-full-e2e-db")
    assert own_containers(runtime, "a") == []


def test_an_empty_machine_is_empty():
    assert own_containers(FakeRuntime(), "bak") == []
