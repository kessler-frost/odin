"""`tests/containers.py::own_containers` / `own_volumes` -- the scoped teardown
filters every integration file now uses instead of `await runtime.list_odin()`.

Unit-tested (not integration) precisely because the thing it must never do is
too expensive to discover for real: a filter that is too WIDE stops a
bystander's containers, and you only find out afterwards. The naming rules it
encodes are odin's own (`aws/backings.py`, `aws/rds.py`, `aws/cache.py`,
`compute/tasks.py`, `compute/functions.py`, `compute/proxy.py`,
`fabric/sidecar.py`), so the cases below are built from those builders.
"""
from __future__ import annotations

from tests.containers import own_containers, own_volumes


class FakeRuntime:
    def __init__(self, *names: str) -> None:
        self._names = list(names)

    async def container_names(self, env=None) -> list[str]:
        return list(self._names)


_MACHINE = FakeRuntime(
    # env "bak" -- the caller's own
    "odin-aws-rustfs-bak", "odin-aws-goaws-bak", "odin-rds-bak-appdb", "odin-aws-rustfs-bak-mesh",
    # env "bak2" -- a longer env sharing the prefix, NOT the caller's
    "odin-aws-rustfs-bak2", "odin-rds-bak2-appdb",
    # somebody else's envs entirely
    "odin-aws-dynalite-prod", "odin-ecs-prod-1a2b3c4d-web", "odin-lambda-other-notify",
)


async def test_it_finds_every_naming_form_for_its_own_env():
    assert await own_containers(_MACHINE, "bak") == sorted([
        "odin-aws-rustfs-bak",       # suffix form: odin-aws-{backing}-{env}
        "odin-aws-goaws-bak",
        "odin-rds-bak-appdb",        # infix form: odin-rds-{env}-{id}
        "odin-aws-rustfs-bak-mesh",  # a nebula sidecar, named off its target
    ])


async def test_a_longer_env_sharing_the_prefix_is_never_matched():
    """The `-` anchor is the whole point: `bak2` is a different env, and a
    teardown that swept it would be the same bug at a smaller scale."""
    mine = await own_containers(_MACHINE, "bak")
    assert not [name for name in mine if "bak2" in name]


async def test_another_envs_containers_are_left_alone():
    mine = await own_containers(_MACHINE, "bak")
    assert "odin-aws-dynalite-prod" not in mine
    assert "odin-ecs-prod-1a2b3c4d-web" not in mine
    assert "odin-lambda-other-notify" not in mine


async def test_several_envs_can_be_claimed_at_once():
    """A test that applies to `a` and `b` owns both -- and still nothing else."""
    runtime = FakeRuntime("odin-aws-rustfs-a", "odin-aws-rustfs-b", "odin-aws-rustfs-apply-full-e2e")
    assert await own_containers(runtime, "a", "b") == ["odin-aws-rustfs-a", "odin-aws-rustfs-b"]


async def test_a_single_letter_env_does_not_match_a_longer_one_starting_with_it():
    """`apply-full-e2e` contains `-a`, but never `-a-` nor a trailing `-a`."""
    runtime = FakeRuntime("odin-aws-rustfs-apply-full-e2e", "odin-rds-apply-full-e2e-db")
    assert await own_containers(runtime, "a") == []


async def test_an_empty_machine_is_empty():
    assert await own_containers(FakeRuntime(), "bak") == []


# --- own_volumes: the same filter, on the other listing -----------------------
#
# `RuntimeDriver.stop` is `docker rm -f -v`, which drops a container's ANONYMOUS
# volumes and deliberately leaves NAMED ones -- that is exactly what makes an rds
# repair non-destructive (`aws/rds.py`). So "every container this test made is
# gone" stopped implying "nothing this test made is on the disk", and a teardown
# that only listed containers would leave a Postgres data volume behind on every
# run. Same scoping rules, same blast radius if they are wrong.


class FakeVolumeRuntime(FakeRuntime):
    def __init__(self, *names: str) -> None:
        super().__init__()
        self._volumes = list(names)

    async def volume_names(self, env: str | None = None) -> list[str]:
        # `own_volumes` scopes by odin's volume NAMING (the same infix rule it
        # uses for containers) and asks for the full listing, so `env` is
        # accepted for signature parity and never exercised here.
        return list(self._volumes)


_VOLUMES = FakeVolumeRuntime(
    "odin-rds-bak-appdb-data",    # the caller's own
    "odin-rds-bak2-appdb-data",   # a longer env sharing the prefix
    "odin-rds-prod-appdb-data",   # somebody else's env
)


async def test_own_volumes_finds_this_envs_data_volume():
    assert await own_volumes(_VOLUMES, "bak") == ["odin-rds-bak-appdb-data"]


async def test_own_volumes_leaves_another_envs_data_alone():
    """A volume is a DATABASE. Sweeping one that belongs to another env (or to
    the user's own running stack) destroys data that nothing can restore --
    there are no snapshots. The `-` anchor matters more here than anywhere."""
    mine = await own_volumes(_VOLUMES, "bak")
    assert "odin-rds-bak2-appdb-data" not in mine
    assert "odin-rds-prod-appdb-data" not in mine


async def test_own_volumes_on_a_machine_with_none():
    assert await own_volumes(FakeVolumeRuntime(), "bak") == []
