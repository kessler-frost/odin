"""v0.8.15: a REAL nginx load balancer in front of a REAL Lima VM.

THIS PATH HAD NEVER ONCE RUN. `elbv2ctl._target_host` documented that an
`i-…` target Id "resolves through `stores.ec2compute` to the VM's real
address", and `_instance_address` implemented it — reading
`private_ip_address`/`public_ip_address`, two keys `gateway/models/
ec2compute.py` has never written (its record carries `private_ip`/
`public_ip`). It therefore returned `None` for every real instance and the
proxy was handed the bare `i-…` id as an upstream, which nginx cannot dial.
Its one unit test wrote the record by hand with the key the reader wanted, so
it passed throughout — honesty rule 1, living inside a test.

Nothing on the canvas could produce an `i-…` target either, because
`_ALB_TARGET_KINDS` excluded ec2, so the field never contradicted it. Both
halves are fixed and this is what proves it, in the only way that can: a real
`tofu apply` through odin's own gateway, a real VM, a real container, and a
real HTTP request.

THE ASSERTIONS ARE ORDERED BY WHAT THEY WOULD HAVE CAUGHT, cheapest first,
because the last one depends on network reachability between two different
VMs and the first three do not:
 1. the generated Terraform carries an `aws_lb_target_group_attachment`;
 2. the gateway registered the target under the INSTANCE ID (`i-…`) — the
    only form that exercises `_target_host`'s branch at all;
 3. the rendered nginx upstream is the VM's REAL ADDRESS and NOT the `i-…`
    id. This is the exact regression, and it is asserted against the file
    `docker cp`'d into the running proxy, not against a model value;
 4. the load balancer's published port answers 200 with the VM's own bytes.

Container/VM hygiene ABSOLUTE, and scoped: the proxy name is deterministic
and registered up front; the VM name is read from the persisted
`ec2compute.json` and registered BEFORE the apply's return code is asserted,
because RunInstances writes the record (and `limactl create` may already have
made a VM directory) even when the boot then fails.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from odin.compute.instances import vm_name
from odin.compute.proxy import conf_path as proxy_conf_path
from odin.compute.proxy import container_name as proxy_container_name
from odin.server import create_app
from odin.spec.store import SpecStore

pytestmark = pytest.mark.integration

ENV = "edgebld-alb-ec2"
VPC = "edgebld-net"
SUBNET = "edgebld-subnet"
LB = "edgebld-lb"
INSTANCE = "edgebld-box"
PORT = 80
BODY = "odin-alb-ec2-ok"

# Started by cloud-init, as root, before anything else needs the VM. `python3`
# is in the Ubuntu 24.04 cloud image by construction, so this adds no package
# install to the boot -- which matters, since an apt fetch would make the test
# depend on the network and on an archive mirror.
USER_DATA = f"""#!/bin/sh
mkdir -p /srv/odin
printf '%s' '{BODY}' > /srv/odin/index.html
nohup python3 -m http.server {PORT} --directory /srv/odin >/var/log/odin-http.log 2>&1 &
"""


def _canvas() -> dict:
    """Exactly what the UI posts, containment stamps included
    (`ui/src/lib/containment.ts` writes `vpc`/`subnet`; the backend never
    re-derives them from geometry).

    The edge is typed `network` ON PURPOSE. That is what every canvas saved
    before the edge-type registry carries, and `iac/hcl.py`'s target pass
    matches on the two NODE kinds rather than on `edge.kind` precisely so
    those canvases keep working. A test that typed it `target` would prove the
    happy path and miss the whole hazard.
    """
    return {
        "nodes": [
            {"id": "n-vpc", "type": "vpc", "position": {"x": 0, "y": 0},
             "data": {"label": VPC, "cidr": "10.0.0.0/16"}},
            {"id": "n-subnet", "type": "subnet", "position": {"x": 20, "y": 40},
             "data": {"label": SUBNET, "cidr": "10.0.1.0/24", "vpc": VPC}},
            {"id": "n-alb", "type": "alb", "position": {"x": 60, "y": 80},
             "data": {"label": LB, "lbType": "application", "listenerPort": "80",
                      "port": str(PORT), "healthCheckPath": "/", "vpc": VPC, "subnet": SUBNET}},
            {"id": "n-ec2", "type": "ec2", "position": {"x": 400, "y": 80},
             "data": {"label": INSTANCE, "instanceType": "t3.micro",
                      "subnet": SUBNET, "vpc": VPC, "userData": USER_DATA}},
        ],
        "edges": [{"id": "e-alb-ec2", "source": "n-alb", "target": "n-ec2",
                   "data": {"edgeType": "network"}}],
    }


def _gateway_state(root: Path, env: str, name: str) -> dict:
    path = root / env / "gateway" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _instances(root: Path) -> list[dict]:
    return [v for k, v in _gateway_state(root, ENV, "ec2compute").items() if k.startswith("instance:")]


def _registered_targets(root: Path) -> list[dict]:
    return _gateway_state(root, ENV, "elbv2ctl").get(f"targets:{LB}-tg", [])


def _lb_record(root: Path) -> dict:
    return _gateway_state(root, ENV, "elbv2ctl").get(f"lb:{LB}", {})


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture
def vm_cleanup():
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(["limactl", "stop", "--force", name], capture_output=True, timeout=60)
        subprocess.run(["limactl", "delete", "--force", name], capture_output=True, timeout=60)


@pytest.fixture
def container_cleanup():
    names: set[str] = set()
    yield names
    for name in names:
        _docker("rm", "-f", "-v", name)


def _wait_until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(1.0)
    raise AssertionError(f"{what} never happened (last seen: {last!r})")


def test_a_load_balancer_really_serves_from_a_real_vm(tmp_path, vm_cleanup, container_cleanup):
    assert shutil.which("tofu"), "OpenTofu must be on PATH for this integration test"
    assert shutil.which("limactl"), "limactl must be on PATH for this integration test"
    assert shutil.which("docker"), "docker must be on PATH for this integration test"
    container_cleanup.add(proxy_container_name(ENV, LB))
    store = SpecStore(tmp_path)
    app = create_app(store=store)
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.post(f"/apply-full?env={ENV}", json=_canvas(), timeout=900.0)

        # BEFORE any assertion on the apply: a boot that failed still leaves a
        # VM directory behind, and an assertion failure here must never skip
        # the registration (test_ec2_tf_e2e.py learned this by leaking one).
        for instance in _instances(store.root):
            vm_cleanup.append(vm_name(ENV, instance["instance_id"]))

        assert response.status_code == 200, response.text
        body = response.json()
        tf = body.get("tf") or {}
        assert tf.get("status") == "ok", f"{body.get('status')}: {json.dumps(tf, indent=2)}"
        # The whole point of the change: this edge is no longer a coverage gap.
        assert body["unsupported"] == [], body
        print(f"\n[edgebld] apply (incl. real Lima VM boot) took {time.monotonic() - started:.1f}s")

        # (1) the generated Terraform really carries the attachment.
        main_tf = (store.root / ENV / "tf" / "main.tf").read_text()
        assert 'resource "aws_lb_target_group_attachment"' in main_tf, main_tf

        # (2) the gateway registered the target BY INSTANCE ID -- the only form
        # that reaches `_target_host`'s `i-` branch at all.
        (instance,) = _instances(store.root)
        instance_id = instance["instance_id"]
        targets = _registered_targets(store.root)
        assert [t["id"] for t in targets] == [instance_id], targets
        assert targets[0]["port"] == PORT, targets

        # The address the fix is about, straight from the record ec2compute wrote.
        address = instance["private_ip"] or instance["public_ip"]
        assert address, f"the VM booted with no address at all: {instance}"
        print(f"[edgebld] instance {instance_id} -> {address}")

        # (3) THE REGRESSION ITSELF. Read from the rendered nginx config on
        # disk -- the bytes `docker cp` put inside the running proxy -- rather
        # than from any in-process value.
        conf = _wait_until(
            lambda: proxy_conf_path(store.root, ENV, LB).read_text()
            if proxy_conf_path(store.root, ENV, LB).exists() else None,
            timeout=60.0, what="the proxy config being rendered",
        )
        assert f"server {address}:{PORT}" in conf, conf
        assert instance_id not in conf, (
            f"the upstream is still the bare instance id, which nginx cannot dial:\n{conf}"
        )

        # (4) end to end: the load balancer's own published port serves the
        # VM's bytes. Retried -- cloud-init's http.server starts moments after
        # the VM reports running, and nginx passively marks a dead upstream.
        record = _wait_until(
            lambda: _lb_record(store.root) if _lb_record(store.root).get("state") == "active" else None,
            timeout=120.0, what="the load balancer reaching state=active",
        )
        published = record["endpoints"][str(PORT)]
        url = f"http://127.0.0.1:{published}/index.html"
        answer = _wait_until(
            lambda: _try_get(url), timeout=180.0, what=f"a 200 from the load balancer at {url}",
        )
        assert answer.status_code == 200, answer.text
        assert answer.text == BODY, answer.text
        print(f"[edgebld] GET {url} -> {answer.status_code} {answer.text!r}")

        # Teardown through the product's own path, not just the fixtures.
        empty = client.post(f"/apply-full?env={ENV}", json={"nodes": [], "edges": []}, timeout=600.0)
        assert empty.status_code == 200, empty.text
        assert (empty.json().get("tf") or {}).get("status") == "ok", empty.text


def _try_get(url: str) -> httpx.Response | None:
    """A real HTTP answer or None. Only a 200 counts as success for the wait:
    nginx answers 502/504 while the upstream is still coming up, and treating
    those as an answer would assert on the proxy's own error page."""
    try:
        response = httpx.get(url, timeout=5.0)
    except httpx.HTTPError:
        return None
    return response if response.status_code == 200 else None
