"""W2.8 final integration pass -- the cross-layer seam the ElastiCache slice
must get right end to end: a canvas with ONE elasticache node -> POST
/apply-full -> tofu ok -> a REAL `redis:7-alpine` container reaches `available`
-> a REAL SET/GET round-trip from the host on the port the model advertised ->
zero-drift re-plan -> an EMPTY-canvas Apply tears it down (the NORTHSTAR "no
Destroy button" promise), container included.

The SET/GET is the whole point: it proves the endpoint odin published in
`CacheNodes[].Endpoint` and in the node's `REDIS_URL` fact is a real,
reachable Redis and not bookkeeping. It runs over `aws.cache.resp_call` (odin's
own tiny RESP client) rather than adding a `redis` dependency for one test.

Needs Colima: unlike iam_role/ec2net (pure gateway-model, no substrate), an
elasticache cluster boots a real container through `create_app`'s default
`ColimaRuntime()`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from odin.aws import cache as cache_mod
from odin.gateway.keys import OPERATOR_NODE_ID
from odin.runtime.colima import CONTAINER_HOST
from odin.server import create_app
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "w28-elasticache-e2e"
CLUSTER = "sessions"
NODE_TYPE = "cache.t3.micro"

CANVAS = {
    "nodes": [
        {"id": "n1", "type": "elasticache", "position": {"x": 40, "y": 40},
         "size": {"width": 220, "height": 60},
         "data": {"label": CLUSTER, "nodeType": NODE_TYPE}},
    ],
    "edges": [],
}
EMPTY_CANVAS = {"nodes": [], "edges": []}


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _cache_state(store: SpecStore) -> dict:
    return json.loads((store.root / ENV / "gateway" / "cachectl.json").read_text())


def test_cache_cluster_apply_real_set_get_zero_drift_teardown(tmp_path):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    container = cache_mod.container_name(ENV, CLUSTER)
    try:
        with TestClient(app) as client:
            resp = client.post("/apply-full", params={"env": ENV}, json=CANVAS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "applied", body
            assert body["tf"] is not None and body["tf"]["status"] == "ok", body
            assert body["unsupported"] == [], body

            # The control-plane record: `available` is what tofu's own create
            # waiter converged on, so reaching here already proves the state
            # machine (creating -> available) works through a real provider.
            (cluster,) = [v for k, v in _cache_state(store).items() if k.startswith("cluster:")]
            assert cluster["cache_cluster_id"] == CLUSTER
            assert cluster["status"] == "available"
            assert cluster["cache_node_type"] == NODE_TYPE
            assert cluster["engine"] == "redis"
            assert cluster["engine_version"].startswith("7."), cluster["engine_version"]  # the REAL server's version
            port = cluster["port"]
            assert port, "an available cluster must advertise its real published port"
            assert cluster["address"] == CONTAINER_HOST

            # A REAL container is behind it, labelled so cleanup/gc find it.
            ps = _docker("ps", "--filter", f"name={container}", "--format", "{{.Names}}")
            assert ps.stdout.strip() == container, ps.stdout

            # THE PROOF: a real SET/GET on the port the model advertised.
            assert cache_mod.resp_call(port, "SET", "odin:e2e", "hello-cache") == "OK"
            assert cache_mod.resp_call(port, "GET", "odin:e2e") == "hello-cache"
            assert cache_mod.resp_call(port, "PING") == "PONG"

            # The consumer-facing facts: World carries both the container- and
            # VM-reachable forms, so `${{sessions.REDIS_URL}}` resolves.
            world = client.get("/world", params={"env": ENV}).json()
            observed = next(r for r in world["resources"] if r["id"] == CLUSTER)
            assert observed["kind"] == "elasticache"
            assert observed["phase"] == "healthy"
            assert observed["facts"]["REDIS_URL"] == f"redis://{CONTAINER_HOST}:{port}"
            assert observed["facts"]["REDIS_URL_VM"] == f"redis://host.lima.internal:{port}"

            # `odin logs <node>` reaches the cluster's own redis container.
            logs = client.get("/logs", params={"env": ENV, "node": CLUSTER}).json()
            assert logs["running"] is True, logs
            assert logs["sources"] == [container]

            # Zero drift: apply -> plan changes NOTHING (the hard gate).
            gateway_port = client.get("/health").json()["gateway"]["port"]
            access_key, secret_key = app.state.gateway_keys.issue(ENV, OPERATOR_NODE_ID)
            plan = subprocess.run(
                ["tofu", "plan", "-input=false", "-no-color", "-detailed-exitcode"],
                cwd=store.root / ENV / "tf", env=_tf_env(gateway_port, access_key, secret_key),
                capture_output=True, text=True, timeout=180,
            )
            assert plan.returncode == 0, f"drift detected (exit {plan.returncode}):\n{plan.stdout}\n{plan.stderr}"

            # Empty canvas: full teardown. elasticache has NO reconciler-driven
            # teardown path (plan.py NoOps it, like every TF-owned kind) --
            # tofu's DeleteCacheCluster is the only thing that can remove it,
            # and the gateway model is what removes the real container.
            resp2 = client.post("/apply-full", params={"env": ENV}, json=EMPTY_CANVAS)
            assert resp2.status_code == 200, resp2.text
            body2 = resp2.json()
            assert body2["status"] == "applied", body2
            assert body2["tf"] is not None and body2["tf"]["status"] == "ok", body2

            assert _cache_state(store) == {}, "cache cluster orphaned after empty-canvas apply"
            gone = _docker("ps", "-a", "--filter", f"name={container}", "--format", "{{.Names}}")
            assert gone.stdout.strip() == "", f"redis container survived teardown: {gone.stdout}"

            # World no longer reports it (the projection pruned the label).
            world_after = client.get("/world", params={"env": ENV}).json()
            assert all(r["id"] != CLUSTER for r in world_after["resources"]), world_after
    finally:
        # Belt-and-braces: never leave this env's container behind, even on a
        # mid-test failure (`rm -f` is a no-op on an absent name).
        _docker("rm", "-f", "-v", container)
