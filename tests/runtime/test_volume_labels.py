"""The argv the driver really builds for the `odin.env` volume label -- the
one thing the whole reclaim rests on.

## Why this file exists at the argv level

`reclaim_env_volumes` deletes a Postgres data directory, and its entire safety
argument is "the listing I am iterating is narrowed to ONE environment". That
narrowing happens in exactly two places, both of them a string handed to the
docker CLI: the `--label odin.env=<env>` that `create_volume` writes, and the
`--filter label=odin.env=<env>` that `volume_names(env=...)` reads it back with.
Get either wrong and the failure is silent in the worst possible direction --
`volume_names(env="a")` returning every volume on the machine looks exactly like
`volume_names(env="a")` working, right up until a reclaim runs.

That is this repo's four-silently-dead-guards shape, so the argv is pinned here
rather than trusted, and `tests/volumes.py` models the SEMANTICS the argv asks
for so the same mistake cannot pass at the level above either.

## What is NOT claimed here

That docker honours these flags. `--label k=v` on `volume create` and `--filter
label=k=v` on `volume ls` are the same two mechanisms `volume_names` and
`list_odin` already use in production for `odin=1`, which is why they were chosen
over `--format '{{.Labels}}'` (unprobed on this machine, and honesty rule 1 says
do not code against a signal you have not seen arrive). The integration half is
`tests/aws/test_rds_postgres.py`'s real-docker rds volume tests.
"""
from __future__ import annotations

from odin.runtime.colima import ENV_LABEL, LABEL, ColimaRuntime, _Proc


class FakeRunner:
    def __init__(self, stdout: str = ""):
        self.calls: list[list[str]] = []
        self._stdout = stdout

    async def __call__(self, args, input=None):
        self.calls.append(args)
        return _Proc(0, self._stdout)


def _call(runner: FakeRunner, verb: str) -> list[str]:
    return next(c for c in runner.calls if verb in c)


def test_the_env_label_is_odin_dot_env():
    """Pinned as a NAME, because two files build strings from it and a rename
    that only lands in one of them silently stops every reclaim from matching."""
    assert ENV_LABEL == "odin.env"
    assert ENV_LABEL.startswith(f"{LABEL}.")


async def test_create_volume_labels_the_volume_with_its_env():
    runner = FakeRunner()
    await ColimaRuntime(runner=runner).create_volume("odin-rds-prod-app-db-data", "prod")
    call = _call(runner, "create")

    # The env label, as one argv element -- an env name may contain a space
    # (`_env_dir` allows it), and there is no shell here to re-split it.
    assert "--label" in call
    assert f"{ENV_LABEL}=prod" in call
    # ...and the two labels that were already there stay: `odin=1` is what
    # `volume_names` lists on at all, and an unlabelled odin volume is one
    # nobody can ever safely reclaim.
    assert f"{LABEL}=1" in call
    assert f"{LABEL}.name=odin-rds-prod-app-db-data" in call
    assert call[-1] == "odin-rds-prod-app-db-data"


async def test_an_env_name_with_a_space_stays_one_argv_element():
    runner = FakeRunner()
    await ColimaRuntime(runner=runner).create_volume("odin-rds-my env-db-data", "my env")
    assert f"{ENV_LABEL}=my env" in _call(runner, "create")


async def test_volume_names_without_an_env_lists_every_odin_volume():
    """The full listing two callers depend on: the recovery disclosure's
    "did this database's data survive", and `GET /volumes`."""
    runner = FakeRunner("odin-rds-a-db-data\nodin-rds-b-db-data\n")
    names = await ColimaRuntime(runner=runner).volume_names()
    call = _call(runner, "ls")

    assert names == ["odin-rds-a-db-data", "odin-rds-b-db-data"]
    assert f"label={LABEL}=1" in call
    # No env narrowing at all -- not `label=odin.env=` or `label=odin.env=None`,
    # either of which would silently return NOTHING and read as a clean machine.
    assert not any(ENV_LABEL in arg for arg in call)


async def test_volume_names_with_an_env_adds_the_label_filter():
    """THE guard. Mutation-test: drop the `scope` splat from `volume_names` and
    this fails -- and so does every safety test in
    `tests/aws/test_volume_reclaim.py`, because the fake honours this filter."""
    runner = FakeRunner("odin-rds-prod-app-db-data\n")
    names = await ColimaRuntime(runner=runner).volume_names(env="prod")
    call = _call(runner, "ls")

    assert names == ["odin-rds-prod-app-db-data"]
    # BOTH filters, ANDed by docker: still odin's own volumes, and only this
    # env's. Dropping `odin=1` would reach a user's hand-made volume.
    assert call.count("--filter") == 2
    assert f"label={LABEL}=1" in call
    assert f"label={ENV_LABEL}=prod" in call
    # ...and it is a LABEL filter, not a name filter. `--filter name=odin-rds-prod-`
    # is the thing that cannot work: `odin-rds-conn2-app-db-data` is env `conn2`
    # AND env `conn2-app`, so a name filter over-reaches by construction.
    assert not any(arg.startswith("name=") for arg in call)
