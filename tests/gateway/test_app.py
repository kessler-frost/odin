"""GatewayState -- the OPERATOR principal special-case (S2 CONTRACT
ADDENDUM): `statements_for(env, "__operator__")` is full-allow, without any
canvas edge, and survives `update()` wholesale-rebuilding the compiled
policy map every reconciler tick."""
from __future__ import annotations

from odin.gateway.app import GatewayState
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.gateway.policy import evaluate


def test_operator_statements_full_allow_with_no_prior_update():
    state = GatewayState()  # update() never called for this env at all
    statements = state.statements_for("default", OPERATOR_NODE_ID)
    assert evaluate(statements, "s3:CreateBucket", "anything")
    assert evaluate(statements, "sqs:DeleteQueue", "some-queue")


def test_operator_statements_survive_update_rebuilding_the_env():
    state = GatewayState()
    state.update("default", {"worker": []}, {"s3": 9000})  # compile_policies output carries no operator entry
    statements = state.statements_for("default", OPERATOR_NODE_ID)
    assert evaluate(statements, "dynamodb:CreateTable", "items")


def test_operator_statements_scoped_per_call_not_leaked_into_workload_lookup():
    state = GatewayState()
    state.update("default", {"worker": []}, {})
    # a real workload node, absent from the compiled map, still default-denies --
    # the operator special-case must not become an accidental wildcard fallback.
    assert state.statements_for("default", "worker") == []
    assert state.statements_for("default", "stranger") == []


def test_operator_statements_work_for_any_env_without_registration():
    state = GatewayState()
    assert evaluate(state.statements_for("never-seen-env", OPERATOR_NODE_ID), "s3:CreateBucket", "x")
