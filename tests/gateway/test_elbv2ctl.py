"""W2.5 -- gateway/models/elbv2ctl.py: the Elastic Load Balancing v2 model
(load balancers, target groups, listeners, targets, tags, attributes).

Same test method as V3a/V4a/V5a: every request is a REAL boto3-signed capture
(the `elbv2` fixture), every response round-trips through botocore's OWN query
parser against the real elbv2 service model -- so a member name or a list
wrapper that botocore wouldn't accept fails here rather than three layers away
in a real `tofu apply` -- and a FAKE `LoadBalancerProxy` is injected so these
are "model logic tested without containers" unit tests. The real nginx container
only shows up in tests/simulate/test_alb_tf_e2e.py.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.compute.proxy import PortsUnpublished, ProxyListener
from odin.gateway import wiring
from odin.gateway.classify import classify
from odin.gateway.models import elbv2ctl
from odin.gateway.stores import SynthStores
from odin.runtime.colima import CONTAINER_HOST

from .conftest import split_url
# The ec2-compute harness, reused rather than re-faked: `_instance_address`
# reads a record `ec2compute` writes, so the only honest way to test it is to
# let `ec2compute` write one (see `test_an_ec2_target_resolves_to_the_vms_real_address`).
from .test_ec2compute import FakeInstanceVm, _run_instance, _subnet, _wait_for_state

_SESSION = botocore.session.get_session()
ENV = "default"
LB = "web-lb"
TG = "web-lb-tg"
SUBNET = "subnet-1234567890abcdef0"


class FakeProxy:
    """The `LoadBalancerProxy` shape (`ensure`/`status`/`destroy`) with no Docker
    involved. `ensure` records the exact `ProxyListener` tuple it was handed, so
    a test can assert WHAT the real nginx would have been told to serve.

    All three are `async def` because all three are on the real class (v0.7.7):
    a sync stand-in would make `await proxy.ensure(...)` a TypeError, and a fake
    whose shape has drifted from the real one proves nothing about the real one.
    """

    def __init__(self) -> None:
        self.ensured: list[tuple[str, tuple[ProxyListener, ...]]] = []
        self.destroyed: list[str] = []

    async def ensure(self, root: Path, env: str, lb_name: str, listeners: tuple[ProxyListener, ...]) -> dict[int, int]:
        self.ensured.append((lb_name, listeners))
        ports = tuple(listener.port for listener in listeners) or (80,)
        return {port: 40_000 + port for port in ports}

    async def status(self, env: str, lb_name: str) -> str:
        return "running"

    async def destroy(self, env: str, lb_name: str) -> None:
        self.destroyed.append(lb_name)

    @property
    def last_listeners(self) -> tuple[ProxyListener, ...]:
        return self.ensured[-1][1]


def _parse(operation: str, response: Response, *, error: bool = False):
    model = _SESSION.get_service_model("elbv2")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": response.body}
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300, parsed
    else:
        assert response.status_code < 300, parsed
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    stores = SynthStores(tmp_path)
    # CreateLoadBalancer derives VpcId + the AZ from the SUBNET records the
    # EC2-network model already holds (never invents them), so the fixture seeds
    # one exactly as gateway/models/ec2net.py would have.
    stores.ec2net.set(ENV, f"subnet:{SUBNET}", {
        "subnet_id": SUBNET, "vpc_id": "vpc-1", "availability_zone": "us-east-1a",
    })
    return stores


@pytest.fixture
def proxy() -> FakeProxy:
    return FakeProxy()


async def _settled(timeout: float = 2.0) -> None:
    """Run every `background()` task the calls above started to completion.

    v0.7.7 turned the daemon threads these tests used to poll for into asyncio
    tasks, and awaiting them is both the honest wait and a STRICTER one than
    the poll it replaces. Two reasons, in the order they bit:

    1. A poll built out of `time.sleep` blocks the very loop the task needs in
       order to run, and a poll built out of `await asyncio.sleep` still reads
       `proxy.ensured` BEFORE the freshly created task has run at all -- a
       daemon thread was already running by then, a task is not (measured: four
       of these tests raised `IndexError` off `last_listeners` on the first
       loop iteration).
    2. A poll can be satisfied by the WRONG converge. `elbv2ctl` spawns one
       `_converge_safely` per affected load balancer, and CreateLoadBalancer's
       own first converge records an EMPTY listener tuple (no listener exists
       yet) -- exactly the state `test_delete_listener_takes_it_out_of_the_proxy`
       polls for."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if not pending:
            return
        await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))


async def _answer(stores, req, proxy) -> Response:
    path, query = split_url(req.url)
    classified = classify("elasticloadbalancing", req.method, path, query, req.headers, req.body)
    assert classified is not None, "a recognized elbv2 action must never be unmappable"
    action, resource = classified
    return await elbv2ctl.pure_answer(action, resource, ENV, req.body, stores, time.monotonic(), proxy=proxy)


async def _create_lb(stores, sink, elbv2, proxy, **kwargs) -> dict:
    kwargs.setdefault("Name", LB)
    kwargs.setdefault("Subnets", [SUBNET])
    # `sink.call` stays SYNCHRONOUS: `elbv2` is a boto3 client, whose operations
    # are ordinary blocking calls against the capture sink -- never awaitable.
    req = sink.call(lambda: elbv2.create_load_balancer(**kwargs))
    (lb,) = _parse("CreateLoadBalancer", await _answer(stores, req, proxy))["LoadBalancers"]
    return lb


async def _create_tg(stores, sink, elbv2, proxy, **kwargs) -> dict:
    kwargs.setdefault("Name", TG)
    kwargs.setdefault("Protocol", "HTTP")
    kwargs.setdefault("Port", 80)
    kwargs.setdefault("VpcId", "vpc-1")
    req = sink.call(lambda: elbv2.create_target_group(**kwargs))
    (tg,) = _parse("CreateTargetGroup", await _answer(stores, req, proxy))["TargetGroups"]
    return tg


async def _create_listener(stores, sink, elbv2, proxy, lb_arn: str, tg_arn: str, port: int = 80) -> dict:
    req = sink.call(lambda: elbv2.create_listener(
        LoadBalancerArn=lb_arn, Protocol="HTTP", Port=port,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    ))
    (listener,) = _parse("CreateListener", await _answer(stores, req, proxy))["Listeners"]
    return listener


async def _trio(stores, sink, elbv2, proxy) -> tuple[dict, dict, dict]:
    """The three resources ONE `alb` canvas node expands to."""
    lb = await _create_lb(stores, sink, elbv2, proxy)
    tg = await _create_tg(stores, sink, elbv2, proxy)
    listener = await _create_listener(stores, sink, elbv2, proxy, lb["LoadBalancerArn"], tg["TargetGroupArn"])
    return lb, tg, listener


# --- load balancer ---------------------------------------------------------


async def test_create_load_balancer_answers_provisioning_and_brings_up_the_real_proxy(stores, sink, elbv2, proxy):
    lb = await _create_lb(stores, sink, elbv2, proxy, Tags=[{"Key": "odin:node", "Value": LB}])
    assert lb["LoadBalancerName"] == LB
    assert lb["Type"] == "application"
    # Honest asynchrony: the real nginx container comes up on a background task,
    # and `active` is what terraform-provider-aws's own waiter polls for.
    assert lb["State"] == {"Code": "provisioning"}
    # VpcId + the AZ come off the SUBNET record, exactly as real
    # CreateLoadBalancer derives them.
    assert lb["VpcId"] == "vpc-1"
    assert lb["AvailabilityZones"] == [{"ZoneName": "us-east-1a", "SubnetId": SUBNET, "LoadBalancerAddresses": []}]
    assert lb["LoadBalancerArn"].endswith(f"loadbalancer/app/{LB}/{lb['LoadBalancerArn'].rsplit('/', 1)[-1]}")
    # `background()` runs the converge as an asyncio task, so wait for it.
    await _settled()
    assert proxy.ensured, "CreateLoadBalancer must bring up the real proxy"


async def test_describe_load_balancers_reports_active_once_the_proxy_is_up(stores, sink, elbv2, proxy):
    await _create_lb(stores, sink, elbv2, proxy)
    await elbv2ctl.converge_proxy(stores, ENV, LB, proxy)
    req = sink.call(lambda: elbv2.describe_load_balancers(Names=[LB]))
    (lb,) = _parse("DescribeLoadBalancers", await _answer(stores, req, proxy))["LoadBalancers"]
    assert lb["State"] == {"Code": "active"}
    # DNSName is a HOST with no room for a port; the reachable endpoint (which
    # odin publishes on a dynamic host port) rides in an odin-only attribute.
    assert lb["DNSName"] == "127.0.0.1"
    assert elbv2ctl.endpoint_url(stores.elbv2ctl.get(ENV, f"lb:{LB}")) == "http://127.0.0.1:40080"


async def test_a_second_load_balancer_with_the_same_name_is_rejected(stores, sink, elbv2, proxy):
    await _create_lb(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.create_load_balancer(Name=LB, Subnets=[SUBNET]))
    parsed = _parse("CreateLoadBalancer", await _answer(stores, req, proxy), error=True)
    assert parsed["Error"]["Code"] == "DuplicateLoadBalancerName"


async def test_describing_an_unknown_load_balancer_is_a_real_not_found(stores, sink, elbv2, proxy):
    req = sink.call(lambda: elbv2.describe_load_balancers(Names=["nope"]))
    parsed = _parse("DescribeLoadBalancers", await _answer(stores, req, proxy), error=True)
    assert parsed["Error"]["Code"] == "LoadBalancerNotFound"


async def test_delete_load_balancer_removes_its_listeners_and_the_real_container(stores, sink, elbv2, proxy):
    lb, _tg, _listener = await _trio(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"]))
    _parse("DeleteLoadBalancer", await _answer(stores, req, proxy))
    remaining = stores.elbv2ctl.items(ENV)
    assert not [k for k in remaining if k.startswith(("lb:", "listener:"))], remaining
    assert proxy.destroyed == [LB]
    # HARD delete, no grace window: ids are never reused, so the provider's own
    # post-delete read lands in the ordinary not-found path (ec2net's verified
    # precedent).
    follow_up = sink.call(lambda: elbv2.describe_load_balancers(Names=[LB]))
    assert _parse("DescribeLoadBalancers", await _answer(stores, follow_up, proxy), error=True)["Error"]["Code"] == "LoadBalancerNotFound"


# --- attributes: where drift hides -----------------------------------------


async def test_load_balancer_attributes_are_seeded_with_real_aws_defaults(stores, sink, elbv2, proxy):
    """A MISSING attribute key IS the drift: the provider reads its schema field
    as unset, plans a write, and the next plan is dirty again. So every key it
    reads has to be there from creation."""
    lb = await _create_lb(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.describe_load_balancer_attributes(LoadBalancerArn=lb["LoadBalancerArn"]))
    attributes = {a["Key"]: a["Value"] for a in _parse("DescribeLoadBalancerAttributes", await _answer(stores, req, proxy))["Attributes"]}
    assert attributes["idle_timeout.timeout_seconds"] == "60"
    assert attributes["deletion_protection.enabled"] == "false"
    assert attributes["routing.http2.enabled"] == "true"
    assert attributes["routing.http.desync_mitigation_mode"] == "defensive"
    assert set(elbv2ctl._LB_ATTRIBUTE_DEFAULTS) <= set(attributes)


async def test_modifying_a_load_balancer_attribute_survives_the_next_read(stores, sink, elbv2, proxy):
    lb = await _create_lb(stores, sink, elbv2, proxy)
    modify = sink.call(lambda: elbv2.modify_load_balancer_attributes(
        LoadBalancerArn=lb["LoadBalancerArn"],
        Attributes=[{"Key": "idle_timeout.timeout_seconds", "Value": "120"}],
    ))
    _parse("ModifyLoadBalancerAttributes", await _answer(stores, modify, proxy))
    read = sink.call(lambda: elbv2.describe_load_balancer_attributes(LoadBalancerArn=lb["LoadBalancerArn"]))
    attributes = {a["Key"]: a["Value"] for a in _parse("DescribeLoadBalancerAttributes", await _answer(stores, read, proxy))["Attributes"]}
    assert attributes["idle_timeout.timeout_seconds"] == "120"
    # The merge is a merge: the other defaults are untouched.
    assert attributes["routing.http2.enabled"] == "true"


async def test_target_group_attributes_are_seeded_and_mergeable(stores, sink, elbv2, proxy):
    tg = await _create_tg(stores, sink, elbv2, proxy)
    read = sink.call(lambda: elbv2.describe_target_group_attributes(TargetGroupArn=tg["TargetGroupArn"]))
    attributes = {a["Key"]: a["Value"] for a in _parse("DescribeTargetGroupAttributes", await _answer(stores, read, proxy))["Attributes"]}
    assert attributes["deregistration_delay.timeout_seconds"] == "300"
    assert attributes["stickiness.enabled"] == "false"
    assert set(elbv2ctl._TG_ATTRIBUTE_DEFAULTS) <= set(attributes)

    modify = sink.call(lambda: elbv2.modify_target_group_attributes(
        TargetGroupArn=tg["TargetGroupArn"],
        Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "5"}],
    ))
    _parse("ModifyTargetGroupAttributes", await _answer(stores, modify, proxy))
    again = sink.call(lambda: elbv2.describe_target_group_attributes(TargetGroupArn=tg["TargetGroupArn"]))
    merged = {a["Key"]: a["Value"] for a in _parse("DescribeTargetGroupAttributes", await _answer(stores, again, proxy))["Attributes"]}
    assert merged["deregistration_delay.timeout_seconds"] == "5"
    assert merged["stickiness.enabled"] == "false"


async def test_listener_attributes_are_empty_for_an_application_listener(stores, sink, elbv2, proxy):
    """The read that FAILED the very first real apply until it was modeled --
    terraform-provider-aws 5.100 calls DescribeListenerAttributes on every
    listener read. Empty is the honest answer: the only listener attribute AWS
    defines (`tcp.idle_timeout.seconds`) belongs to network load balancers."""
    _lb, _tg, listener = await _trio(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.describe_listener_attributes(ListenerArn=listener["ListenerArn"]))
    assert _parse("DescribeListenerAttributes", await _answer(stores, req, proxy))["Attributes"] == []


async def test_modifying_a_listener_attribute_survives_the_next_read(stores, sink, elbv2, proxy):
    _lb, _tg, listener = await _trio(stores, sink, elbv2, proxy)
    modify = sink.call(lambda: elbv2.modify_listener_attributes(
        ListenerArn=listener["ListenerArn"],
        Attributes=[{"Key": "tcp.idle_timeout.seconds", "Value": "60"}],
    ))
    _parse("ModifyListenerAttributes", await _answer(stores, modify, proxy))
    read = sink.call(lambda: elbv2.describe_listener_attributes(ListenerArn=listener["ListenerArn"]))
    assert _parse("DescribeListenerAttributes", await _answer(stores, read, proxy))["Attributes"] == [
        {"Key": "tcp.idle_timeout.seconds", "Value": "60"},
    ]


# --- tags: the other drift surface (ARN-only API) --------------------------


async def test_create_time_tags_round_trip_through_describe_tags(stores, sink, elbv2, proxy):
    lb = await _create_lb(stores, sink, elbv2, proxy, Tags=[{"Key": "odin:node", "Value": LB}])
    req = sink.call(lambda: elbv2.describe_tags(ResourceArns=[lb["LoadBalancerArn"]]))
    (description,) = _parse("DescribeTags", await _answer(stores, req, proxy))["TagDescriptions"]
    assert description["ResourceArn"] == lb["LoadBalancerArn"]
    assert description["Tags"] == [{"Key": "odin:node", "Value": LB}]


async def test_add_and_remove_tags_work_on_all_three_resource_kinds(stores, sink, elbv2, proxy):
    lb, tg, listener = await _trio(stores, sink, elbv2, proxy)
    for arn in (lb["LoadBalancerArn"], tg["TargetGroupArn"], listener["ListenerArn"]):
        add = sink.call(lambda arn=arn: elbv2.add_tags(ResourceArns=[arn], Tags=[{"Key": "team", "Value": "core"}]))
        _parse("AddTags", await _answer(stores, add, proxy))
        read = sink.call(lambda arn=arn: elbv2.describe_tags(ResourceArns=[arn]))
        (description,) = _parse("DescribeTags", await _answer(stores, read, proxy))["TagDescriptions"]
        assert {t["Key"]: t["Value"] for t in description["Tags"]}["team"] == "core"

        remove = sink.call(lambda arn=arn: elbv2.remove_tags(ResourceArns=[arn], TagKeys=["team"]))
        _parse("RemoveTags", await _answer(stores, remove, proxy))
        after = sink.call(lambda arn=arn: elbv2.describe_tags(ResourceArns=[arn]))
        (description,) = _parse("DescribeTags", await _answer(stores, after, proxy))["TagDescriptions"]
        assert "team" not in {t["Key"] for t in description["Tags"]}


async def test_tagging_an_unknown_arn_is_a_real_not_found(stores, sink, elbv2, proxy):
    bogus = "arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/ghost/deadbeefdeadbeef"
    req = sink.call(lambda: elbv2.describe_tags(ResourceArns=[bogus]))
    assert _parse("DescribeTags", await _answer(stores, req, proxy), error=True)["Error"]["Code"] == "ResourceNotFound"


# --- target group + listener ------------------------------------------------


async def test_create_target_group_echoes_the_health_check_it_was_given(stores, sink, elbv2, proxy):
    tg = await _create_tg(stores, sink, elbv2, proxy, HealthCheckPath="/healthz", HealthCheckIntervalSeconds=10)
    assert tg["HealthCheckPath"] == "/healthz"
    assert tg["HealthCheckIntervalSeconds"] == 10
    # Unset members fall back to real AWS's own HTTP/instance defaults rather
    # than being omitted (an omitted member reads as unset and drifts).
    assert tg["HealthCheckProtocol"] == "HTTP"
    assert tg["HealthCheckPort"] == "traffic-port"
    assert tg["HealthCheckEnabled"] is True
    assert tg["HealthCheckTimeoutSeconds"] == 5
    assert tg["Matcher"] == {"HttpCode": "200"}
    assert tg["TargetType"] == "instance"


async def test_a_target_group_reports_the_load_balancers_whose_listeners_forward_to_it(stores, sink, elbv2, proxy):
    lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.describe_target_groups(TargetGroupArns=[tg["TargetGroupArn"]]))
    (described,) = _parse("DescribeTargetGroups", await _answer(stores, req, proxy))["TargetGroups"]
    assert described["LoadBalancerArns"] == [lb["LoadBalancerArn"]]


async def test_modify_target_group_updates_the_health_check_and_the_proxy_fail_window(stores, sink, elbv2, proxy):
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.modify_target_group(
        TargetGroupArn=tg["TargetGroupArn"], HealthCheckPath="/ready", HealthCheckIntervalSeconds=7,
    ))
    (modified,) = _parse("ModifyTargetGroup", await _answer(stores, req, proxy))["TargetGroups"]
    assert modified["HealthCheckPath"] == "/ready"
    # The health-check interval IS the proxy's passive `fail_timeout` -- the only
    # honest mapping open-source nginx allows.
    await _settled()
    assert proxy.last_listeners[0].fail_timeout_seconds == 7


async def test_create_listener_echoes_its_default_action_verbatim(stores, sink, elbv2, proxy):
    _lb, tg, listener = await _trio(stores, sink, elbv2, proxy)
    # ecsctl's byte-for-byte rule: exactly the members the client sent, never
    # default-filled -- that echo is what keeps apply -> plan clean.
    assert listener["DefaultActions"] == [{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}]
    assert listener["Port"] == 80
    assert listener["Protocol"] == "HTTP"


async def test_an_unmodeled_default_action_type_is_refused_not_silently_accepted(stores, sink, elbv2, proxy):
    lb = await _create_lb(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.create_listener(
        LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
        DefaultActions=[{"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "404"}}],
    ))
    parsed = _parse("CreateListener", await _answer(stores, req, proxy), error=True)
    assert parsed["Error"]["Code"] == "ValidationError"
    assert "fixed-response" in parsed["Error"]["Message"]


async def test_the_listener_set_is_what_the_proxy_listens_on(stores, sink, elbv2, proxy):
    lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    await _create_listener(stores, sink, elbv2, proxy, lb["LoadBalancerArn"], tg["TargetGroupArn"], port=8080)
    await _settled()
    assert sorted(listener.port for listener in proxy.last_listeners) == [80, 8080]


async def test_delete_listener_takes_it_out_of_the_proxy(stores, sink, elbv2, proxy):
    _lb, _tg, listener = await _trio(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.delete_listener(ListenerArn=listener["ListenerArn"]))
    _parse("DeleteListener", await _answer(stores, req, proxy))
    await _settled()
    assert proxy.last_listeners == ()


# --- targets + health -------------------------------------------------------


async def test_register_targets_becomes_the_proxys_upstream_list(stores, sink, elbv2, proxy):
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    req = sink.call(lambda: elbv2.register_targets(
        TargetGroupArn=tg["TargetGroupArn"],
        Targets=[{"Id": CONTAINER_HOST, "Port": 32768}, {"Id": CONTAINER_HOST, "Port": 32769}],
    ))
    _parse("RegisterTargets", await _answer(stores, req, proxy))
    await _settled()
    assert proxy.last_listeners[0].targets == (f"{CONTAINER_HOST}:32768", f"{CONTAINER_HOST}:32769")


async def test_deregister_targets_takes_one_out_again(stores, sink, elbv2, proxy):
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], CONTAINER_HOST, 32768, proxy=proxy)
    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], CONTAINER_HOST, 32769, proxy=proxy)
    req = sink.call(lambda: elbv2.deregister_targets(
        TargetGroupArn=tg["TargetGroupArn"], Targets=[{"Id": CONTAINER_HOST, "Port": 32768}],
    ))
    _parse("DeregisterTargets", await _answer(stores, req, proxy))
    await _settled()
    assert proxy.last_listeners[0].targets == (f"{CONTAINER_HOST}:32769",)


async def test_registering_the_same_target_twice_is_idempotent(stores, sink, elbv2, proxy):
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], CONTAINER_HOST, 32768, proxy=proxy)
    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], CONTAINER_HOST, 32768, proxy=proxy)
    assert stores.elbv2ctl.get(ENV, f"targets:{TG}") == [{"id": CONTAINER_HOST, "port": 32768}]


async def test_an_ec2_target_resolves_to_the_vms_real_address(stores, sink, elbv2, ec2, proxy):
    """THE INSTANCE RECORD IS BUILT BY ec2compute ITSELF, through a real
    RunInstances and a real boot, and that is the whole point of the test.

    Its predecessor wrote the record by hand as
    `{"instance_id": ..., "private_ip_address": "192.168.5.7"}` -- a key
    `ec2compute` has never written; its record carries `private_ip`/`public_ip`.
    So `_instance_address` returned None for every REAL instance and the proxy
    was handed the bare `i-...` id as an upstream, which nginx can never dial,
    while this test passed for as long as it existed. Rule 1, exactly: a test
    that fabricates the upstream signal proves the parser, not the integration.
    Nothing on the canvas could produce an `i-...` target until v0.8.15 added
    the `aws_lb_target_group_attachment`, which is why it was never caught in
    the field either.
    """
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    subnet_id = await _subnet(stores, sink, ec2)
    vm = FakeInstanceVm(ip="192.168.64.42")
    run = await _run_instance(stores, sink, ec2, vm, ImageId="ami-1", InstanceType="t3.micro", SubnetId=subnet_id)
    instance_id = run["Instances"][0]["InstanceId"]
    await _wait_for_state(stores, sink, ec2, instance_id, "running", vm)

    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], instance_id, 8080, proxy=proxy)
    assert proxy.last_listeners[0].targets == ("192.168.64.42:8080",)


async def test_describe_target_health_probes_for_real_and_never_invents_healthy(stores, sink, elbv2, proxy):
    """The honesty rule: nothing polls the health-check path on odin's behalf
    (open-source nginx checks upstreams only PASSIVELY), so this action does a
    REAL HTTP GET. A target that isn't listening therefore reports `unhealthy`
    with the actual connection failure, not a comfortable default."""
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    # Port 1 on loopback: nothing is listening, and nothing can be.
    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], "127.0.0.1", 1, proxy=proxy)
    req = sink.call(lambda: elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"]))
    (description,) = _parse("DescribeTargetHealth", await _answer(stores, req, proxy))["TargetHealthDescriptions"]
    assert description["Target"] == {"Id": "127.0.0.1", "Port": 1}
    assert description["TargetHealth"]["State"] == "unhealthy"
    assert description["TargetHealth"]["Reason"] == "Target.Timeout"
    assert "127.0.0.1:1/" in description["TargetHealth"]["Description"]


async def test_describe_target_health_reports_unused_when_health_checks_are_disabled(stores, sink, elbv2, proxy):
    _lb, tg, _listener = await _trio(stores, sink, elbv2, proxy)
    await elbv2ctl.register_target(stores, ENV, tg["TargetGroupArn"], "127.0.0.1", 1, proxy=proxy)
    modify = sink.call(lambda: elbv2.modify_target_group(
        TargetGroupArn=tg["TargetGroupArn"], HealthCheckEnabled=False,
    ))
    _parse("ModifyTargetGroup", await _answer(stores, modify, proxy))
    req = sink.call(lambda: elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"]))
    (description,) = _parse("DescribeTargetHealth", await _answer(stores, req, proxy))["TargetHealthDescriptions"]
    assert description["TargetHealth"]["State"] == "unused"


async def test_registering_into_an_unknown_target_group_is_a_real_not_found(stores, sink, elbv2, proxy):
    bogus = "arn:aws:elasticloadbalancing:us-east-1:000000000000:targetgroup/ghost/deadbeefdeadbeef"
    req = sink.call(lambda: elbv2.register_targets(TargetGroupArn=bogus, Targets=[{"Id": "i-1"}]))
    parsed = _parse("RegisterTargets", await _answer(stores, req, proxy), error=True)
    assert parsed["Error"]["Code"] == "TargetGroupNotFound"


# --- upgrade safety: an OLDER record must not brick a read ------------------
# `records.py::TargetGroup` types `health_check` as a bare mapping on purpose,
# so a target group written before a member existed still LOADS. That makes the
# reader the half that has to tolerate it -- and `_tg_xml` used to hard-index
# all eight `_HEALTH_CHECK_DEFAULTS` keys, so such a record raised `KeyError` on
# every DescribeTargetGroups, which is the read terraform does BEFORE it could
# send anything that would repair the record.


def _older_tg_record(stores, *, without: str) -> dict:
    """A target group exactly as today's writer stores one, minus one
    health-check member -- i.e. one written by an odin from before that member
    was added."""
    record = {
        "name": TG, "tg_id": "def456", "arn": elbv2ctl.tg_arn(TG, "def456"),
        "protocol": "HTTP", "protocol_version": "HTTP1", "port": 80, "vpc_id": "vpc-1",
        "target_type": "ip", "ip_address_type": "ipv4",
        "health_check": {k: v for k, v in elbv2ctl._HEALTH_CHECK_DEFAULTS.items() if k != without},
        "matcher": {"HttpCode": "200", "GrpcCode": None},
        "attributes": dict(elbv2ctl._TG_ATTRIBUTE_DEFAULTS),
    }
    stores.elbv2ctl.set(ENV, f"tg:{TG}", record)
    return record


async def test_describe_target_groups_answers_for_a_record_missing_a_health_check_member(stores, sink, elbv2, proxy):
    _older_tg_record(stores, without="HealthCheckEnabled")

    req = sink.call(lambda: elbv2.describe_target_groups(Names=[TG]))
    (described,) = _parse("DescribeTargetGroups", await _answer(stores, req, proxy))["TargetGroups"]

    # Real AWS's own default is filled in, NOT omitted: an omitted member reads
    # as unset in the provider's schema and makes the next plan dirty (the same
    # zero-drift rule `_LB_ATTRIBUTE_DEFAULTS` documents).
    assert described["HealthCheckEnabled"] is True
    assert described["HealthCheckPath"] == "/"


async def test_describe_target_health_answers_for_a_record_missing_the_health_check_path(stores, sink, elbv2, proxy):
    # The sibling read: `_probe_target` hard-indexed `HealthCheckPath` the same
    # way, so DescribeTargetHealth was the second permanent 500.
    record = _older_tg_record(stores, without="HealthCheckPath")
    stores.elbv2ctl.set(ENV, f"targets:{TG}", [{"id": "10.0.0.9", "port": 1}])

    req = sink.call(lambda: elbv2.describe_target_health(TargetGroupArn=record["arn"]))
    parsed = _parse("DescribeTargetHealth", await _answer(stores, req, proxy))

    (description,) = parsed["TargetHealthDescriptions"]
    # Nothing is listening on that address, so the honest answer is unhealthy --
    # the point is that it ANSWERS instead of raising.
    assert description["TargetHealth"]["State"] == "unhealthy"


async def test_modify_target_group_heals_a_record_that_predates_a_member(stores, sink, elbv2, proxy):
    _older_tg_record(stores, without="HealthCheckEnabled")

    req = sink.call(lambda: elbv2.modify_target_group(
        TargetGroupArn=elbv2ctl.tg_arn(TG, "def456"), HealthCheckPath="/ready",
    ))
    _parse("ModifyTargetGroup", await _answer(stores, req, proxy))

    # The next WRITE carries the full member set forward, so the gap does not
    # travel with the record forever.
    assert "HealthCheckEnabled" in stores.elbv2ctl.get(ENV, f"tg:{TG}")["health_check"]


# --- a host port of 0 is not an endpoint ------------------------------------


async def test_endpoint_url_is_none_for_a_stored_host_port_of_zero(stores, sink, elbv2, proxy):
    """A record written by odin <= v0.7.5, whose `host_port` ended
    `return int(...) if out else 0` and turned any transient `docker` failure
    into a port-shaped 0. Load-balancer records are only rewritten by a
    converge, so the 0 persists -- and `gateway/wiring.py::producer_facts`
    INJECTS the resulting `ALB_ENDPOINT` into a real consumer container.
    Measured before this clause: `producer_facts()` answered
    `{'web': {'ALB_ENDPOINT': 'http://127.0.0.1:0'}}`."""
    await _create_lb(stores, sink, elbv2, proxy)
    record = stores.elbv2ctl.get(ENV, f"lb:{LB}")
    stores.elbv2ctl.set(ENV, f"lb:{LB}", {**record, "state": "active", "endpoints": {"80": 0}})

    stored = stores.elbv2ctl.get(ENV, f"lb:{LB}")
    assert elbv2ctl.endpoint_url(stored) is None
    assert await wiring.producer_facts(stores, ENV) == {}


async def test_a_proxy_that_cannot_publish_leaves_the_load_balancer_failed_and_factless(stores, sink, elbv2, proxy):
    """`compute/proxy.py::ensure` raises `PortsUnpublished` rather than
    returning a 0, so `_converge_safely` records the honest outcome: state
    `failed`, the real reason, and NO `ALB_ENDPOINT` for anything to consume.

    The load balancer converged successfully first, which is the case worth
    pinning: `_converge_safely` leaves the previous converge's `endpoints` on
    the record, so before the state gate this failed load balancer went on
    advertising the host port its now-dead container used to answer on."""
    await _create_lb(stores, sink, elbv2, proxy)
    await elbv2ctl.converge_proxy(stores, ENV, LB, proxy)
    assert await wiring.producer_facts(stores, ENV) != {}  # it really was published

    class UnpublishableProxy(FakeProxy):
        async def ensure(self, root, env, lb_name, listeners):
            raise PortsUnpublished(
                'odin-alb-default-web published no host port for [80] (container is exited); '
                'last log lines: nginx: [emerg] invalid parameter "bogus:8080"'
            )

    await elbv2ctl._converge_safely(stores, ENV, LB, UnpublishableProxy())

    record = stores.elbv2ctl.get(ENV, f"lb:{LB}")
    assert record["state"] == "failed"
    assert "invalid parameter" in record["state_reason"]
    assert record["endpoints"] == {"80": 40080}  # the last converge's result, kept
    assert elbv2ctl.endpoint_url(record) is None  # but no longer an address
    assert await wiring.producer_facts(stores, ENV) == {}


# --- the no-backing contract ------------------------------------------------


async def test_an_unmodeled_action_gets_a_protocol_correct_error_never_a_503(stores, proxy):
    """Same contract as ec2net/iamctl/ecr/logsctl: elbv2 has no backing to
    forward to, so falling through would 503 every call."""
    response = await elbv2ctl.pure_answer(
        "elasticloadbalancing:CreateRule", "*", ENV, b"Action=CreateRule", stores, 0.0, proxy=proxy,
    )
    assert response.status_code == 400
    assert b"InvalidAction" in response.body
    assert b"CreateRule" in response.body


def test_name_from_arn_matches_classifys_own_reduction():
    """These two must never drift apart: `classify.py::_elbv2_name` decides the
    IAM resource and this decides the store key."""
    from odin.gateway.classify import _elbv2_name

    arns = (
        f"arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/{LB}/abc123",
        f"arn:aws:elasticloadbalancing:us-east-1:000000000000:targetgroup/{TG}/abc123",
        f"arn:aws:elasticloadbalancing:us-east-1:000000000000:listener/app/{LB}/abc123/def456",
        "not-an-arn-at-all",
    )
    for arn in arns:
        assert elbv2ctl.name_from_arn(arn) == _elbv2_name(arn)
