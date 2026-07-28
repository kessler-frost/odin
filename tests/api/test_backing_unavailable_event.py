"""The `backing_unavailable` WebSocket event's RECOVERY text, through the real
app and its real gateway listener.

The residual this closes: the recovery was one sentence -- "run Apply (or `odin
apply --env X`) to start it" -- sent to whoever the caller happened to be. But
this event fires from the gateway the moment a call finds no backing port, and
the caller making AWS calls during an apply or destroy is TOFU, which holds the
OPERATOR key. Telling that user to run Apply points them at the command they
are already inside and 503-ing from.

Nothing here fabricates the signal (honesty rule 1): the app is the real
`create_app`, the gateway is the real listener its lifespan starts on a real
port, the credentials come from the real `KeyStore`, and the request is a real
SigV4-signed boto3 call. The event is read back out of the durable event log
`ConnectionManager.broadcast` really writes.
"""
from __future__ import annotations

import json

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore
from odin.gateway.policy import compile_policies_from_iam
from odin.server import create_app
from odin.spec.store import SpecStore
from tests.api.test_apply import FakeRds, FakeRuntime

ENV = "unavail"
# One bucket, and one workload node holding an s3 IAM edge to it -- so the
# WORKLOAD principal's call passes the policy check and reaches the same
# no-backing branch the operator's does. (The operator is full-allow by
# construction and needs no edge.)
CANVAS = {
    "nodes": [
        {"id": "b", "type": "s3", "data": {"label": "uploads"}},
        {"id": "w", "type": "lambda", "data": {"label": "worker"}},
    ],
    "edges": [{"source": "w", "target": "b", "data": {"edgeType": "iam", "permissions": ["s3:GetObject"]}}],
}


class NoBackings:
    """A `BackingAws`-shaped stand-in that publishes NO backing ports -- the
    routing table a gateway 503 is read off, empty. Everything else is a no-op
    so the reconciler's own tick converges normally around it."""

    async def ensure_backing(self, kind: str) -> None: ...
    async def gc(self, kinds: set) -> None: ...
    async def backing_ports(self) -> dict:
        return {}
    async def exists(self, kind: str, rid: str) -> bool:
        return True
    async def facts(self, kind: str, rid: str) -> dict:
        return {}
    async def provision(self, kind: str, rid: str, *args) -> None: ...
    async def deprovision(self, kind: str, rid: str) -> None: ...
    async def subscriptions(self, rid: str) -> tuple:
        return ()
    async def aws_env(self) -> dict:
        return {}


@pytest.fixture
def wired(tmp_path):
    """The real app with its gateway on an ephemeral port, plus credentials
    issued BEFORE it starts (the KeyStore loads an env's file once, lazily)."""
    keystore = KeyStore(tmp_path)
    creds = {
        node: keystore.issue(ENV, node) for node in (OPERATOR_NODE_ID, "worker")
    }
    app = create_app(
        runtime=FakeRuntime(), store=SpecStore(tmp_path), rds=FakeRds(),
        aws=NoBackings(), backings=False, gateway_port=0,
    )
    with TestClient(app) as client:
        # The apply commits the Stack (so the IAM edge really is compiled into
        # the gateway's policy) while the routing table stays empty -- exactly
        # the condition the gateway answers ServiceUnavailable for.
        assert client.post("/apply", params={"env": ENV}, json=CANVAS).status_code == 200
        # v0.8.12: the gateway authorizes from the APPLIED IAM, so the fixture has
        # to contain what a real apply produces. `/apply` commits the Stack
        # without running tofu, so nothing here creates the role or its policy --
        # under the old edge-compiled enforcement the canvas alone was enough,
        # and this test went from ServiceUnavailable to AccessDenied when that
        # changed. Seeded rather than reworded, because the case under test is
        # what a GRANTED principal sees when the backing is down.
        stores = client.app.state.gateway_stores
        stores.iamctl.set(ENV, "role:worker-role", {
            "role_name": "worker-role", "role_id": "AROATEST", "path": "/",
            "assume_role_policy_document": "{}", "description": "",
            "inline_policies": {"worker-grants": json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "uploads"}],
            })},
            "attached_policy_arns": [],
        })
        stores.lambdactl.set(ENV, "fn:worker", {
            "function_name": "worker", "state": "Active",
            "role": "arn:aws:iam::000000000000:role/worker-role",
        })
        # The gateway is handed its policy map at reconcile time, and the seeding
        # above lands after that, so it has to be recompiled here or the gateway
        # is still holding the empty map it was given during `/apply`.
        gateway = client.app.state.gateway
        existing = gateway._envs.get(ENV)
        gateway.update(
            ENV,
            compile_policies_from_iam(stores, ENV),
            # Carried over rather than passed as `{}`: the empty routing table is
            # the very thing this test asserts on, so writing it here would be
            # the test manufacturing its own answer.
            existing.backing_ports if existing else {},
        )
        yield client, tmp_path, creds


async def _call_s3(endpoint: str, creds: tuple[str, str]) -> str:
    access_key, secret_key = creds
    s3 = boto3.client(  # boto3's own factory is SYNC (not BackingAws.client)
        "s3", endpoint_url=endpoint, region_name="us-east-1",
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
        ),
    )
    with pytest.raises(ClientError) as caught:
        s3.get_object(Bucket="uploads", Key="a.txt")
    return caught.value.response["Error"]["Code"]


def _recovery_for(root, node_id: str) -> str:
    """The `recovery` on the last `backing_unavailable` event this principal
    caused, read out of the durable per-env log the broadcast really wrote."""
    lines = (root / ENV / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    matching = [
        e for e in events
        if e.get("type") == "backing_unavailable" and e.get("resource_id") == node_id
    ]
    assert matching, f"no backing_unavailable event for {node_id!r} in {[e.get('type') for e in events]}"
    return matching[-1]["recovery"]


async def test_a_tofu_run_is_not_told_to_run_the_command_it_is_inside(wired):
    """The OPERATOR principal is a tofu run -- `/apply-full` and `/destroy` are
    the only issuers of that key -- so this event is being emitted from inside
    an apply or destroy that is failing right now."""
    client, root, creds = wired
    endpoint = f"http://127.0.0.1:{client.get('/health').json()['gateway']['port']}"

    assert await _call_s3(endpoint, creds[OPERATOR_NODE_ID]) == "ServiceUnavailable"
    recovery = _recovery_for(root, OPERATOR_NODE_ID)

    assert "no s3 backing container is running" in recovery   # the shared diagnosis half
    assert "IN FLIGHT" in recovery                            # ...and WHEN this reached them
    assert "Starting another Apply" in recovery and "will not help" in recovery
    assert "fix the error IT reports" in recovery
    # The old, wrong-for-this-moment sentence must be gone for this principal.
    assert "run Apply" not in recovery
    assert f"odin apply --env {ENV}" not in recovery


async def test_a_workload_principal_still_gets_the_apply_advice(wired):
    """The other half: a lambda/ecs container calling S3 is NOT inside an
    apply, so Apply really is what starts the backing for it. Same env, same
    gateway, same missing backing -- only the principal differs."""
    client, root, creds = wired
    endpoint = f"http://127.0.0.1:{client.get('/health').json()['gateway']['port']}"

    assert await _call_s3(endpoint, creds["worker"]) == "ServiceUnavailable"
    recovery = _recovery_for(root, "worker")

    assert "no s3 backing container is running" in recovery
    assert f"run Apply (or `odin apply --env {ENV}`) to start it" in recovery
    assert "IN FLIGHT" not in recovery
