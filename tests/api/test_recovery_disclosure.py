"""An apply that repairs a container out from under the user must SAY so.

## The trade this file exists to keep honest

v0.8.2 moved the drift sweep BEFORE the converges so an apply repairs what it
discovers (measured: 302.8s of applies against a killed database that never came
back, down to 5.9s). That fixed a real lie -- `/world` said
`crashed — re-Apply to recreate` and the re-Apply converged nothing -- but it
bought the fix with a new silence: the apply now destroys and rebuilds a
container nobody asked it to touch, and for rds that means it destroys a
DATABASE. `PostgresRds.create_db` mounts no volume, so the replacement comes up
EMPTY.

`tests/simulate/test_false_green_window_e2e.py` originally asserted the opposite
contract (fail first, recover on a second apply) precisely to keep that visible;
its own words were that "no operator should learn about that from a green
apply". `server.py::_recovering_resources` is how the faster behaviour keeps
that promise instead of trading it away, and this file is the fast, substrate-
free half of its coverage: the e2e proves the recovery really happens, these
prove it is correctly NAMED.

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

from odin.gateway.models import rdsctl
from odin.gateway.stores import SynthStores
from odin.server import _recovered_line, _recovering_resources

ENV = "disclosure"


def _failed_db(stores: SynthStores, identifier: str = "app-db", reason: str | None = "container ... is not running (exit 137)") -> None:
    stores.rdsctl.set(ENV, f"db:{identifier}", {
        "db_instance_identifier": identifier, "status": rdsctl.FAILED,
        "status_reason": reason, "master_username": "app", "master_password": "apppass123",
        "db_name": "postgres", "endpoint_address": "127.0.0.1", "endpoint_port": 0,
    })


def _failed_fn(
    stores: SynthStores, name: str = "worker", reason: str | None = "its container was removed outside odin",
    last_update: str = "Successful",
) -> None:
    stores.lambdactl.set(ENV, f"fn:{name}", {
        "function_name": name, "state": "Failed", "state_reason": reason,
        "state_reason_code": "InternalError", "last_update_status": last_update,
    })


def test_a_healthy_env_discloses_nothing(tmp_path):
    """The overwhelming majority of applies, which must pay nothing for this and
    must not grow a spurious `recovered_resources` key."""
    stores = SynthStores(tmp_path)
    stores.rdsctl.set(ENV, "db:app-db", {"db_instance_identifier": "app-db", "status": rdsctl.AVAILABLE})
    stores.lambdactl.set(ENV, "fn:worker", {"function_name": "worker", "state": "Active"})
    assert _recovering_resources(stores, ENV) == []


def test_an_empty_env_discloses_nothing(tmp_path):
    assert _recovering_resources(SynthStores(tmp_path), ENV) == []


def test_a_failed_database_is_named_with_the_real_reason(tmp_path):
    """The field name is the point: `status_reason` is what `rdsctl._update`
    writes and what `_db_fault` reads. A different spelling here would report
    every recovery as "it was not running" and lose the exit code."""
    stores = SynthStores(tmp_path)
    _failed_db(stores)
    assert _recovering_resources(stores, ENV) == [
        {"kind": "rds", "node": "app-db", "reason": "container ... is not running (exit 137)"},
    ]


def test_a_failed_function_is_named_with_the_real_reason(tmp_path):
    """`state_reason`, the twin spelling on the lambda side -- a different
    module, a different field name, and the same failure mode."""
    stores = SynthStores(tmp_path)
    _failed_fn(stores)
    assert _recovering_resources(stores, ENV) == [
        {"kind": "lambda", "node": "worker", "reason": "its container was removed outside odin"},
    ]


def test_a_function_already_mid_redeploy_is_not_claimed(tmp_path):
    """`last_update_status == "InProgress"` is what `converge_functions` skips,
    so claiming it here would announce a recovery this apply is not performing
    -- the false-report shape, pointed the other way."""
    stores = SynthStores(tmp_path)
    _failed_fn(stores, last_update="InProgress")
    assert _recovering_resources(stores, ENV) == []


def test_a_reasonless_record_still_names_the_resource(tmp_path):
    """A record whose reason never got written must not produce `None` in the
    middle of an operator-facing sentence."""
    stores = SynthStores(tmp_path)
    _failed_db(stores, reason=None)
    (item,) = _recovering_resources(stores, ENV)
    assert item["reason"] == "it was not running"
    assert "None" not in _recovered_line(item)


def test_several_resources_are_all_named(tmp_path):
    stores = SynthStores(tmp_path)
    _failed_db(stores, "app-db")
    _failed_db(stores, "reports-db")
    _failed_fn(stores, "worker")
    named = {(i["kind"], i["node"]) for i in _recovering_resources(stores, ENV)}
    assert named == {("rds", "app-db"), ("rds", "reports-db"), ("lambda", "worker")}


def test_another_envs_wreckage_is_not_reported(tmp_path):
    """Every store read in odin is per-env; a shared report would name a
    database belonging to an env this apply never touched."""
    stores = SynthStores(tmp_path)
    _failed_db(stores)
    assert _recovering_resources(stores, "other-env") == []


def test_the_rds_line_states_the_data_loss(tmp_path):
    """THE disclosure. If this sentence stops saying the data is gone, the
    v0.8.2 trade is no longer paid for and the e2e's promise is broken."""
    line = _recovered_line({"kind": "rds", "node": "app-db", "reason": "it was not running"})
    assert "app-db" in line and "re-created" in line
    assert "data did not survive" in line


def test_the_lambda_line_does_not_claim_data_loss(tmp_path):
    """A rebuilt RIE container is stateless -- saying otherwise would train
    operators to ignore the sentence that matters."""
    line = _recovered_line({"kind": "lambda", "node": "worker", "reason": "it was not running"})
    assert "worker" in line and "re-created" in line
    assert "data did not survive" not in line
