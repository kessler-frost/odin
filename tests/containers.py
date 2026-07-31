"""Which odin containers does THIS test own?

Every integration test in this suite used to tear down with
`await runtime.list_odin()` -- i.e. EVERY container carrying the `odin=1` label on
the machine, regardless of which env, which test, or which PROCESS created
it. That is not a coordination nicety:

  - run the suite while your own `odin start` stack is up and the suite stops
    your stack's backings out from under you;
  - during the v0.7.0 field test it made these fixtures capable of
    force-removing a concurrently-running agent's containers, and re-running
    a file safely meant first checking by hand that the machine was clear.

Nothing in these tests needs that reach. Each one creates containers in envs
it names, and odin's own container naming carries the env, so "mine" is
decidable:

  - `odin-aws-{backing}-{env}`                 -- env is a SUFFIX
  - `odin-rds-{env}-{id}` / `odin-ecs-{env}-…` /
    `odin-lambda-{env}-…` / `odin-cache-{env}-…` /
    `odin-alb-{env}-…`                         -- env is an INFIX
  - `odin-rds-{env}-{id}-data` (a rds data VOLUME, `own_volumes`) -- the same
    INFIX form, so it needs no rule of its own either
  - `…-mesh` (a backing's nebula sidecar, named off its target) -- inherits
    whichever of the two forms its target had, so it needs no rule of its own

Both forms are anchored on `-`, which is what keeps a longer env sharing this
one's prefix out: `bak2`'s containers never match `bak`, and `apply-full-e2e`
never matches `a`.

RESIDUAL LIMIT, stated rather than implied away: a test whose env IS
`default` (or `a`/`b`) is scoped to a name a user's own stack can also be
using. Scoping still stops that test from touching every OTHER env on the
machine, which is the blast radius that actually bit; a test sharing an env
name with a live stack was already colliding with it on the container names
themselves, and the fix for that is to give the test its own env, not a
smarter teardown filter.
"""
from __future__ import annotations

import subprocess


def _mine(names, envs: tuple[str, ...]) -> list[str]:
    return sorted(
        name for name in names
        if any(name.endswith(f"-{env}") or f"-{env}-" in name for env in envs)
    )


async def own_containers(runtime, *envs: str) -> list[str]:
    """Every odin container belonging to `envs`, by odin's own naming -- the
    scoped replacement for `await runtime.list_odin()` in an integration teardown.

    Pass every env the test applies to; the result is what that test is
    allowed to stop, and (once it has cleaned up) what must be empty."""
    return _mine(await runtime.container_names(), envs)


async def own_volumes(runtime, *envs: str) -> list[str]:
    """The same question for VOLUMES, and it needs asking separately because
    stopping a container no longer implies removing its storage.

    `RuntimeDriver.stop` is `docker rm -f -v`, which drops a container's
    ANONYMOUS volumes and deliberately leaves NAMED ones -- that is exactly what
    makes an rds repair non-destructive (`aws/rds.py`). So a teardown that only
    listed containers would report "everything this test made is gone" while a
    Postgres data volume sat on the disk, and nothing else in odin would ever
    reclaim it.

    Named `odin-rds-{env}-{node}-data`, so the env INFIX rule `own_containers`
    already uses matches it unchanged."""
    return _mine(await runtime.volume_names(), envs)


def reap_volumes(*envs: str) -> None:
    """`own_volumes` for a SYNC teardown fixture: remove `envs`' data volumes,
    then ASSERT none survived.

    Why this exists at all is the named-volume asymmetry. Every teardown
    fixture in this suite reclaims containers with `docker rm -f -v`, and that
    `-v` removes a container's ANONYMOUS volumes while deliberately leaving
    NAMED ones -- which is the whole point of `aws/rds.py`'s named PGDATA
    volume (it is what makes odin's own rds repair non-destructive). So a
    fixture that removed only containers reported a clean teardown while
    `odin-rds-{env}-{db}-data` sat on the disk, once per run, and nothing else
    ever reclaimed it: `reclaim_env_volumes` is driven by `odin env rm`, which
    an integration test never calls.

    Scoped to these envs' own names, never a label and never machine-wide --
    another agent's volumes must not be reachable from here. The assertion is
    the load-bearing half: a teardown that quietly does nothing looks exactly
    like a teardown that had nothing to do, which is how this leaked.

    Sync (and shelling out) rather than `own_volumes`, because the fixtures
    that need it are sync and hold no `RuntimeDriver`."""
    for env in envs:
        subprocess.run(
            f"docker volume ls -q --filter name=-{env}- | xargs -r docker volume rm",
            shell=True, capture_output=True, check=False, timeout=60,
        )
    left = [
        name for env in envs
        for name in subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", f"name=-{env}-"],
            capture_output=True, text=True, check=False, timeout=60,
        ).stdout.split()
    ]
    assert left == [], f"{list(envs)} left {len(left)} volumes standing: {left}"
