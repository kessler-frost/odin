"""`reclaim_env_volumes` — giving back the disk a dead environment left behind,
and refusing to touch anything else.

## The bug

v0.8.14 made an rds instance's PGDATA a NAMED volume so odin's own repair stopped
handing back an empty database. That fix relies on `docker rm -f -v` NOT removing
a named volume — and nothing then removed one either. Measured on the development
machine: four orphans from two environments that no longer existed in any form
(no containers, no `.odin/<env>/`, absent from `GET /envs`), found only by a
hand-run `docker volume ls`. A Postgres data directory is not small.

## WHERE THE SAFETY LINE IS, and why it is there and not elsewhere

Getting a volume GC wrong destroys a user's database, so this one is drawn twice,
conservatively, and the tests below are organised around making each line FIRE.

1. **A live environment's volumes are never swept — at all.** Not "swept unless
   the node is in the Stack": a database whose node was dragged off the canvas
   mid-edit still holds the user's rows, and `plan()` has no way to tell that from
   a deletion. So the only thing that removes a live env's volume stays
   `delete_db`, i.e. a real DELETE through a real apply. This sweep is only ever
   entered with an env NAME somebody supplied (`odin env rm <env>`), and never on
   a reconciler tick — `reconcile/reconciler.py`'s `gc` is per-env, docker volumes
   are per-MACHINE, and "no environment claims this" is a question no per-env loop
   can answer without deleting a second odin's databases.
2. **Scoping is by LABEL, never by name.** `odin-rds-conn2-app-db-data` is env
   `conn2` database `app-db` and env `conn2-app` database `db` — both halves may
   contain `-` and the string does not say which. A name-shaped filter therefore
   over-reaches by construction, which for containers is a documented refusal
   (`docs/limits.md`) and here would be a deletion. `create_volume` writes
   `odin.env`; the sweep reads it back with a docker label filter and nothing else.

And behind both, docker's own refusal to remove an attached volume — which this
reports rather than swallows, because a reclaim that silently skips everything
looks identical to one that had nothing to do.
"""
from __future__ import annotations

import pytest

from odin.aws.rds import (
    VOLUME_PREFIX,
    VOLUME_SUFFIX,
    reclaim_env_volumes,
    volume_env_candidates,
    volume_name,
)
from tests.volumes import IN_USE, BlindVolumes, FakeVolumes


class Machine(FakeVolumes):
    """A bare fake with only the volume surface — all the reclaim ever touches."""


# --- the name constants, pinned against the builder that makes the names ---


def test_the_prefix_and_suffix_match_what_volume_name_really_builds():
    """Two module constants describe the ends of `volume_name`'s output, and
    everything that parses a volume name reads them. A change to `volume_name`
    that left them behind would make `volume_env_candidates` return `()` for
    every real volume — i.e. silently stop attributing anything — so they are
    derived from the builder here rather than eyeballed."""
    built = volume_name("someenv", "somedb")
    assert built.startswith(VOLUME_PREFIX)
    assert built.endswith(VOLUME_SUFFIX)
    assert built == f"{VOLUME_PREFIX}someenv-somedb{VOLUME_SUFFIX}"


# --- the ambiguity, made explicit instead of guessed at --------------------


def test_a_volume_name_names_more_than_one_possible_env():
    """The measured orphan, and the reason nothing that DELETES may parse a
    name. `odin-rds-conn2-app-db-data` has two readings and the string does not
    choose between them."""
    assert volume_env_candidates("odin-rds-conn2-app-db-data") == ("conn2", "conn2-app")
    assert volume_name("conn2", "app-db") == "odin-rds-conn2-app-db-data"
    assert volume_name("conn2-app", "db") == "odin-rds-conn2-app-db-data"


def test_candidates_come_shortest_first():
    """`GET /volumes` offers a candidate to a user, so the order is contract:
    the shortest reading is tried first, and a longer one could only be right if
    it were a live env — which would have made the volume non-orphan."""
    assert volume_env_candidates("odin-rds-a-b-c-d-data") == ("a", "a-b", "a-b-c")


def test_a_single_segment_name_yields_no_candidate_rather_than_a_wrong_one():
    """`odin-rds-db-data` leaves no segment for the db_id, so it is not a name
    `volume_name` could have produced. Answering `("db",)` would invite
    `odin env rm db` against an env that never existed."""
    assert volume_env_candidates("odin-rds-db-data") == ()


@pytest.mark.parametrize("name", [
    "odin-aws-rustfs-prod",        # a container, not a volume
    "odin-rds-prod-db",            # no -data suffix
    "rds-prod-db-data",            # no odin-rds- prefix
    "some-users-own-volume",       # not odin's at all
    "",
])
def test_a_name_that_is_not_an_rds_data_volume_yields_nothing(name):
    assert volume_env_candidates(name) == ()


# --- SAFETY LINE 2: the sweep reaches exactly one env's labelled volumes ---


async def test_it_reclaims_this_envs_volumes_and_reports_them_by_name():
    machine = Machine().seed_volume("odin-rds-gone-app-db-data", "gone")
    machine.seed_volume("odin-rds-gone-other-db-data", "gone")

    result = await reclaim_env_volumes(machine, "gone")

    assert result.reclaimed == ("odin-rds-gone-app-db-data", "odin-rds-gone-other-db-data")
    assert result.failed is False
    assert machine.volumes == set()


async def test_another_envs_volume_is_untouched():
    """The blast-radius test. `keep` is not being removed and its database must
    still be there afterwards."""
    machine = Machine().seed_volume("odin-rds-gone-db-data", "gone")
    machine.seed_volume("odin-rds-keep-db-data", "keep")

    result = await reclaim_env_volumes(machine, "gone")

    assert result.reclaimed == ("odin-rds-gone-db-data",)
    assert machine.volumes == {"odin-rds-keep-db-data"}


async def test_an_env_whose_name_prefixes_this_one_keeps_its_data():
    """THE case a name filter gets wrong, and the reason the label exists.

    `odin env rm conn2` must not take `conn2-other`'s database. Both volumes
    start with `odin-rds-conn2-`, so `--filter name=odin-rds-conn2-` — or any
    `startswith` in Python — deletes both. Mutation-test: make
    `reclaim_env_volumes` list with `env=None` and filter by
    `name.startswith(f"odin-rds-{env}-")` and this test fails while every other
    test in this file still passes."""
    machine = Machine().seed_volume("odin-rds-conn2-app-db-data", "conn2")
    machine.seed_volume("odin-rds-conn2-other-app-db-data", "conn2-other")

    result = await reclaim_env_volumes(machine, "conn2")

    assert result.reclaimed == ("odin-rds-conn2-app-db-data",)
    assert machine.volumes == {"odin-rds-conn2-other-app-db-data"}, (
        "a prefix-sharing environment's database must survive its neighbour's removal"
    )


async def test_a_volume_with_no_env_label_is_left_alone_rather_than_guessed_at():
    """A volume created before v0.8.15 carries no `odin.env`, so no label filter
    can attribute it. Left standing on purpose: the alternative is parsing its
    name, which cannot tell `conn2` from `conn2-app`. `GET /volumes` names it
    instead, with `docker volume rm` as the manual step."""
    machine = Machine().seed_volume("odin-rds-legacy-db-data", None)

    result = await reclaim_env_volumes(machine, "legacy")

    assert result.reclaimed == ()
    assert result.failed is False, "nothing to reclaim is not a failure"
    assert machine.volumes == {"odin-rds-legacy-db-data"}


async def test_an_env_with_nothing_to_reclaim_succeeds_quietly():
    result = await reclaim_env_volumes(Machine(), "never-had-a-database")
    assert result == type(result)()  # the empty result, and NOT a failure
    assert result.failed is False


# --- docker's refusal is reported, never swallowed -------------------------


async def test_a_volume_still_attached_to_a_container_is_named_with_dockers_reason():
    """Docker refuses to remove a volume a container references (probed:
    `rc 1: remove <vol>: volume is in use - [<id>]`). That refusal is the guard
    doing its job, and the point is that odin says so — a reclaim that logged
    nothing and reclaimed nothing would look identical to one with nothing to do.

    Mutation-test: swallow the `except` in `reclaim_env_volumes` (drop the
    `standing.append`) and this fails."""
    machine = Machine().seed_volume("odin-rds-busy-db-data", "busy")
    machine.refuse_volume("odin-rds-busy-db-data")

    result = await reclaim_env_volumes(machine, "busy")

    assert result.reclaimed == ()
    assert result.failed is True
    assert result.standing == ({
        "volume": "odin-rds-busy-db-data",
        "reason": IN_USE.format(name="odin-rds-busy-db-data"),
    },)
    assert "volume is in use" in result.standing[0]["reason"]
    assert machine.volumes == {"odin-rds-busy-db-data"}, "the data is still there"


async def test_one_refusal_does_not_hide_the_others():
    """Per-volume, not per-sweep: a single attached volume must not make odin
    stop asking about the rest, or a user fixes one container and finds the next
    refusal only on the next attempt."""
    machine = Machine()
    for node in ("a", "b", "c"):
        machine.seed_volume(f"odin-rds-mix-{node}-data", "mix")
    machine.refuse_volume("odin-rds-mix-b-data")

    result = await reclaim_env_volumes(machine, "mix")

    assert result.reclaimed == ("odin-rds-mix-a-data", "odin-rds-mix-c-data")
    assert [s["volume"] for s in result.standing] == ["odin-rds-mix-b-data"]
    assert machine.volumes == {"odin-rds-mix-b-data"}


# --- "could not ask" must not wear the words of "there are none" ----------


async def test_a_machine_odin_cannot_ask_reports_unknown_and_removes_nothing():
    """Field test 6's F4, one level down. An empty list from a docker that would
    not answer is indistinguishable from a clean machine, and the caller turns
    the second into a completed removal. `unknown` carries the real reason and
    `failed` is True."""
    result = await reclaim_env_volumes(BlindVolumes(), "gone")

    assert result.unknown == "Cannot connect to the Docker daemon"
    assert result.failed is True
    assert result.reclaimed == ()


async def test_unknown_is_distinguishable_from_nothing_to_do():
    """The two answers a single boolean would collapse. Mutation-test: make
    `failed` return `bool(self.standing)` only, and this fails."""
    nothing = await reclaim_env_volumes(Machine(), "clean")
    unknown = await reclaim_env_volumes(BlindVolumes(), "unreadable")

    assert nothing.reclaimed == unknown.reclaimed == ()
    assert nothing.failed is False
    assert unknown.failed is True
    assert not nothing.unknown and unknown.unknown
