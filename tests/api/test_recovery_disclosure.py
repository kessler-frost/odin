"""An apply that repairs a container out from under the user must SAY so.

## The trade this file exists to keep honest

v0.8.2 moved the drift sweep BEFORE the converges so an apply repairs what it
discovers (measured: 302.8s of applies against a killed database that never came
back, down to 5.9s). That fixed a real lie -- `/world` said
`crashed — re-Apply to recreate` and the re-Apply converged nothing -- but it
bought the fix with a new silence: the apply now destroys and rebuilds a
container nobody asked it to touch, and for rds that meant it destroyed a
DATABASE -- `PostgresRds.create_db` mounted no volume, so the replacement came
up EMPTY.

`tests/simulate/test_false_green_window_e2e.py` originally asserted the opposite
contract (fail first, recover on a second apply) precisely to keep that visible;
its own words were that "no operator should learn about that from a green
apply". `server.py::_recovering_resources` is how the faster behaviour keeps
that promise instead of trading it away, and this file is the fast, substrate-
free half of its coverage: the e2e proves the recovery really happens, these
prove it is correctly NAMED.

## v0.8.14 flipped the sentence and kept the report

`aws/rds.py::volume_name` gave each instance a named data volume that outlives
its container, so the ordinary repair is now non-destructive and the warning
became a footnote. That is a change to what the line SAYS, not to whether it is
said: an apply still replaces a container nobody asked it to, and the operator
still has to hear which one and why.

It also added the one thing this file now guards hardest. The good sentence is
not asserted, it is MEASURED -- `_recovering_resources` asks the runtime whether
the volume is really still there, in the one instant between the sweep that
marks the death and the converge that repairs it. A disclosure that assumed the
happy case would be a guard reading no signal (honesty rule 1), and it would
reassure someone about data it was in the middle of losing in exactly the case
where the volume was destroyed too.

## Why these are worth having separately from the e2e

The e2e needs ~90s of real Postgres boot, so it can only afford a couple of
shapes. The risk that actually bit here is cheaper to test and easier to get
wrong: `_recovering_resources` reads RECORD FIELDS by name across two store
modules (`status_reason` on rds, `state_reason` on lambda), and a typo in either
would silently report "no reason recorded" forever while every other assertion
in the suite still passed. Reading the wrong field name is this repo's honesty
rule 1 in miniature -- a guard wired to a signal that never arrives.
"""
from __future__ import annotations

from pathlib import Path

from odin.aws.rds import volume_name
from odin.gateway.models import lambdactl, rdsctl
from odin.gateway.stores import SynthStores
from odin.server import _recovered_line, _recovering_resources

ENV = "disclosure"


class FakeRuntime:
    """Just the volume half of a RuntimeDriver -- `_recovering_resources` asks
    it one question: which odin volumes exist right now?

    `_failed_db` seeds the matching volume by default, because that IS the
    ordinary case: a dead rds container leaves its named data volume behind,
    which is what makes the repair non-destructive. Tests that want the other
    branch pass `volume=False`."""

    def __init__(self, *names: str):
        self.volumes = set(names)

    async def volume_names(self, env: str | None = None) -> list[str]:
        # `env` is accepted and ignored ON PURPOSE, and only safely so: nothing
        # in this file removes anything. `_recovered_resources` asks for the FULL
        # listing (`env=None`) and looks one exact name up in it.
        return sorted(self.volumes)


def _failed_db(
    stores: SynthStores, runtime: FakeRuntime, identifier: str = "app-db",
    reason: str | None = "container ... is not running (exit 137)",
    volume: bool = True,
) -> None:
    """A database whose CONTAINER died. `volume=True` is what really happens:
    `create_db` made a named data volume and `docker rm -f -v` does not touch
    it, so it is still on the machine when the apply looks."""
    stores.rdsctl.set(ENV, f"db:{identifier}", {
        "db_instance_identifier": identifier, "status": rdsctl.FAILED,
        "status_reason": reason, "master_username": "app", "master_password": "apppass123",
        "db_name": "postgres", "endpoint_address": "127.0.0.1", "endpoint_port": 0,
    })
    runtime.volumes.update({volume_name(ENV, identifier)} if volume else set())


def _failed_fn(
    stores: SynthStores, name: str = "worker", reason: str | None = "its container was removed outside odin",
    last_update: str = "Successful",
) -> None:
    # The FULL record shape `converge_functions` reads, not the subset
    # `_recovering_resources` needs -- the drift ratchet below runs the real
    # converge against these, and a thin seed would only prove the getter.
    stores.lambdactl.set(ENV, f"fn:{name}", {
        "function_name": name, "state": "Failed", "state_reason": reason,
        "state_reason_code": "InternalError", "last_update_status": last_update,
        "last_update_status_reason": None, "last_update_status_reason_code": None,
        "runtime": "python3.12", "handler": "lambda_function.lambda_handler",
        "environment": {}, "memory_size": 128,
        "function_arn": f"arn:aws:lambda:us-east-1:000000000000:function:{name}",
    })


async def test_a_healthy_env_discloses_nothing(tmp_path):
    """The overwhelming majority of applies, which must pay nothing for this and
    must not grow a spurious `recovered_resources` key."""
    stores = SynthStores(tmp_path)
    stores.rdsctl.set(ENV, "db:app-db", {"db_instance_identifier": "app-db", "status": rdsctl.AVAILABLE})
    stores.lambdactl.set(ENV, "fn:worker", {"function_name": "worker", "state": "Active"})
    assert await _recovering_resources(stores, ENV, FakeRuntime()) == []


async def test_an_empty_env_discloses_nothing(tmp_path):
    assert await _recovering_resources(SynthStores(tmp_path), ENV, FakeRuntime()) == []


async def test_a_failed_database_is_named_with_the_real_reason(tmp_path):
    """The field name is the point: `status_reason` is what `rdsctl._update`
    writes and what `_db_fault` reads. A different spelling here would report
    every recovery as "it was not running" and lose the exit code."""
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime)
    assert await _recovering_resources(stores, ENV, runtime) == [
        {
            "kind": "rds", "node": "app-db",
            "reason": "container ... is not running (exit 137)", "data_kept": True,
        },
    ]


async def test_a_failed_database_whose_volume_is_also_gone_reports_the_data_loss(tmp_path):
    """The SAME record, the same reason, and the opposite verdict -- because the
    only difference is out on the machine, in the volume listing. `data_kept` is
    measured, not derived from the record, which is the whole reason this
    function had to grow a runtime argument.

    Mutation check: hard-code `data_kept` to True in `server.py` and this fails;
    hard-code it to False and the test above fails."""
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime, volume=False)
    (item,) = await _recovering_resources(stores, ENV, runtime)
    assert item["data_kept"] is False
    assert "data did not survive" in _recovered_line(item)


async def test_another_databases_volume_does_not_vouch_for_this_one(tmp_path):
    """`in` against a listing is only as good as the name it looks for. A
    substring match, or a check for "any odin volume at all", would report every
    database's data as safe the moment ONE database in the env still had one."""
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime, "reports-db")
    _failed_db(stores, runtime, "app-db", volume=False)
    kept = {i["node"]: i["data_kept"] for i in await _recovering_resources(stores, ENV, runtime)}
    assert kept == {"reports-db": True, "app-db": False}


async def test_a_failed_function_is_named_with_the_real_reason(tmp_path):
    """`state_reason`, the twin spelling on the lambda side -- a different
    module, a different field name, and the same failure mode."""
    stores = SynthStores(tmp_path)
    _failed_fn(stores)
    assert await _recovering_resources(stores, ENV, FakeRuntime()) == [
        {"kind": "lambda", "node": "worker", "reason": "its container was removed outside odin"},
    ]


async def test_a_function_already_mid_redeploy_is_not_claimed(tmp_path):
    """`last_update_status == "InProgress"` is what `converge_functions` skips,
    so claiming it here would announce a recovery this apply is not performing
    -- the false-report shape, pointed the other way."""
    stores = SynthStores(tmp_path)
    _failed_fn(stores, last_update="InProgress")
    assert await _recovering_resources(stores, ENV, FakeRuntime()) == []


async def test_a_reasonless_record_still_names_the_resource(tmp_path):
    """A record whose reason never got written must not produce `None` in the
    middle of an operator-facing sentence."""
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime, reason=None)
    (item,) = await _recovering_resources(stores, ENV, runtime)
    assert item["reason"] == "it was not running"
    assert "None" not in _recovered_line(item)


async def test_several_resources_are_all_named(tmp_path):
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime, "app-db")
    _failed_db(stores, runtime, "reports-db")
    _failed_fn(stores, "worker")
    named = {(i["kind"], i["node"]) for i in await _recovering_resources(stores, ENV, runtime)}
    assert named == {("rds", "app-db"), ("rds", "reports-db"), ("lambda", "worker")}


async def test_another_envs_wreckage_is_not_reported(tmp_path):
    """Every store read in odin is per-env; a shared report would name a
    database belonging to an env this apply never touched."""
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime)
    assert await _recovering_resources(stores, "other-env", runtime) == []


def test_the_rds_line_states_that_the_data_survived(tmp_path):
    """THE disclosure, and the v0.8.14 flip. Until the named volume landed this
    sentence had to say the data was GONE -- that warning was the only thing
    paying for the v0.8.2 trade (an apply that silently rebuilt a database and
    reported green). Now the repair really is non-destructive and the sentence
    is a footnote; what must not happen is it going quiet, because the operator
    still needs to know a container they did not touch was replaced."""
    line = _recovered_line({
        "kind": "rds", "node": "app-db", "reason": "it was not running", "data_kept": True,
    })
    assert "app-db" in line and "re-created" in line
    assert "its data survived" in line
    assert "data did not survive" not in line


def test_the_rds_line_still_states_a_real_data_loss(tmp_path):
    """...and the warning is not gone, only conditional. A recovery whose volume
    was destroyed too says exactly what it used to say."""
    line = _recovered_line({
        "kind": "rds", "node": "app-db", "reason": "it was not running", "data_kept": False,
    })
    assert "data did not survive" in line


def test_an_unknown_kind_claims_neither_survival_nor_loss(tmp_path):
    """`_RECOVERY_COST` is keyed by (kind, data_kept) and an unmapped pair falls
    through to a neutral truth. A new kind added to `_recovering_resources` and
    forgotten here must not inherit either claim -- honesty rule 2's "derive the
    status from a map; an unmapped outcome falls through" shape."""
    line = _recovered_line({"kind": "ecs", "node": "web", "reason": "it was not running"})
    assert "(it was rebuilt)" in line
    assert "survive" not in line


def test_the_lambda_line_does_not_claim_data_loss(tmp_path):
    """A rebuilt RIE container is stateless -- saying otherwise would train
    operators to ignore the sentence that matters."""
    line = _recovered_line({"kind": "lambda", "node": "worker", "reason": "it was not running"})
    assert "worker" in line and "re-created" in line
    assert "execution environment was rebuilt" in line
    assert "survive" not in line


# --- the drift ratchet ---------------------------------------------------------
#
# `_recovering_resources` PREDICTS what the converges are about to do, by reading
# the same records against the same criteria. Two independent copies of one rule
# is how a report starts lying quietly: loosen `converge_functions`' skip and the
# apply would announce recoveries it never performed; tighten it and a real one
# would go unannounced -- and in BOTH directions every existing test still passes,
# because each half is individually correct.
#
# So these tie the two together BEHAVIOURALLY: the same store, the real converge,
# and the claim checked against what it actually spawned. Same shape as
# `capacity.py`'s `DEFAULT_TASK_MEMORY_MIB` mirror.


class _NeverRuns:
    """A substrate whose containers are never actually built -- these assert WHICH
    records a converge claims, not that a container comes up."""

    def __init__(self, *args, **kwargs):
        pass

    def code_dir(self, env, function_name):
        return Path("/nonexistent") / env / function_name

    async def ensure(self, *args, **kwargs):
        raise RuntimeError("not a real substrate")


async def test_the_lambda_claim_matches_what_converge_functions_really_spawns(tmp_path):
    stores = SynthStores(tmp_path)
    _failed_fn(stores, "worker")
    _failed_fn(stores, "mid-redeploy", last_update="InProgress")  # converge skips this one
    stores.lambdactl.set(ENV, "fn:healthy", {"function_name": "healthy", "state": "Active"})

    claimed = {
        i["node"] for i in await _recovering_resources(stores, ENV, FakeRuntime())
        if i["kind"] == "lambda"
    }
    spawned = lambdactl.converge_functions(stores, ENV, substrate=_NeverRuns())
    for task in spawned:  # they raise by design; don't leave them unretrieved
        task.cancel()

    assert claimed == {"worker"}
    assert len(spawned) == len(claimed), (
        f"the apply claimed {claimed} but converge_functions spawned {len(spawned)} redeploys — "
        "the report and the repair have drifted apart"
    )


async def test_the_rds_claim_matches_what_converge_db_instances_really_spawns(tmp_path):
    stores, runtime = SynthStores(tmp_path), FakeRuntime()
    _failed_db(stores, runtime, "app-db")
    stores.rdsctl.set(ENV, "db:healthy-db", {
        "db_instance_identifier": "healthy-db", "status": rdsctl.AVAILABLE,
    })

    claimed = {
        i["node"] for i in await _recovering_resources(stores, ENV, runtime) if i["kind"] == "rds"
    }
    spawned = rdsctl.converge_db_instances(stores, ENV, substrate=_NeverRuns())
    for task in spawned:
        task.cancel()

    assert claimed == {"app-db"}
    assert len(spawned) == len(claimed), (
        f"the apply claimed {claimed} but converge_db_instances spawned {len(spawned)} re-creates"
    )


def test_the_recovery_line_drops_the_sweeps_call_to_action(tmp_path):
    """Field test 7. The drift sweep ends a verdict with "— re-Apply to
    recreate", which is right on a crashed node in `/world` and WRONG quoted into
    a recovery line: the apply has already done it. Measured in the field:

        rds app-db was re-created because container odin-rds-ft-app-db is not
        running (exit 137) — re-Apply to recreate (its data did not survive ...)

    telling someone to repeat the action they just watched happen.
    """
    line = _recovered_line({
        "kind": "rds", "node": "app-db", "data_kept": True,
        "reason": "container odin-rds-ft-app-db is not running (exit 137) — re-Apply to recreate",
    })
    assert "is not running (exit 137)" in line, "the CAUSE must survive"
    assert "re-Apply" not in line, "the advice must not"
    assert "its data survived" in line


def test_a_reason_without_the_suffix_is_untouched(tmp_path):
    line = _recovered_line({"kind": "lambda", "node": "worker", "reason": "its container was removed"})
    assert "because its container was removed (" in line
