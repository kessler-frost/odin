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


async def own_containers(runtime, *envs: str) -> list[str]:
    """Every odin container belonging to `envs`, by odin's own naming -- the
    scoped replacement for `await runtime.list_odin()` in an integration teardown.

    Pass every env the test applies to; the result is what that test is
    allowed to stop, and (once it has cleaned up) what must be empty."""
    names = await runtime.container_names()
    return sorted(
        name for name in names
        if any(name.endswith(f"-{env}") or f"-{env}-" in name for env in envs)
    )
