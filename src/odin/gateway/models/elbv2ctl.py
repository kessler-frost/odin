"""The gateway's Elastic Load Balancing v2 model (task W2.5): `aws_lb` +
`aws_lb_target_group` + `aws_lb_listener`, backed by a **real reverse proxy
container per load balancer** (`compute/proxy.py`, nginx:alpine -- see that
module for why nginx and not Caddy).

Like ec2net/iamctl/ecr/lambdactl/ecsctl/logsctl, elbv2 has NO backing container
to forward to: this module is the whole answer for every
`elasticloadbalancing:*` action. Unlike those, a load balancer's substrate is
REAL and asynchronous like ec2compute's instances: `CreateLoadBalancer` returns
`State.Code = "provisioning"` immediately and a daemon thread brings the proxy
container up, after which `DescribeLoadBalancers` reports `"active"` -- the
transition terraform-provider-aws's own `waitLoadBalancerActive` polls for, and
an honest one (the state IS the real container's state, never a stored
fiction).

WIRE SHAPE: elbv2 is botocore's **query** protocol (verified against
`botocore.session.get_session().get_service_model("elbv2")`: `protocol:
query`, `apiVersion: 2015-12-01`, `endpointPrefix: elasticloadbalancing`,
`xmlNamespace: http://elasticloadbalancing.amazonaws.com/doc/2015-12-01/`) --
so requests are form-encoded `Action=...` bodies with AWS's standard
`Prefix.member.N[.Field]` list serialization (NOT EC2's `Prefix.N`), and
responses are `<{Op}Response><{Op}Result>...</{Op}Result><ResponseMetadata>`
documents whose lists wrap each entry in `<member>`. Every response below
round-trips through botocore's own `QueryParser` against the real elbv2 model
in tests/gateway/test_elbv2ctl.py -- not guessed at. The SigV4 credential scope
(and hence `classify.py`'s `service`) is `elasticloadbalancing`, elbv2's
signing name.

WHAT THE PROVIDER ACTUALLY CALLS (derived EMPIRICALLY by running real tofu
against the real gateway before this module was finished -- the W2.4 audit's
lesson: an unmodeled READ breaks every apply, and elbv2 has an unusual number
of them). Per resource:
 - `aws_lb`: CreateLoadBalancer, DescribeLoadBalancers,
   DescribeLoadBalancerAttributes, ModifyLoadBalancerAttributes, DescribeTags,
   AddTags/RemoveTags, DeleteLoadBalancer.
 - `aws_lb_target_group`: CreateTargetGroup, DescribeTargetGroups,
   DescribeTargetGroupAttributes, ModifyTargetGroupAttributes, ModifyTargetGroup,
   DescribeTags, AddTags/RemoveTags, DeleteTargetGroup.
 - `aws_lb_listener`: CreateListener, DescribeListeners, ModifyListener,
   **DescribeListenerAttributes** / ModifyListenerAttributes, DescribeTags,
   AddTags/RemoveTags, DeleteListener. That bolded one is exactly the audit's
   point: it appears in no obvious place in the resource's documentation, it is
   called on EVERY listener read, and leaving it unmodeled failed the very first
   real apply outright (see `_describe_listener_attributes`).
 - the data plane odin itself drives: RegisterTargets / DeregisterTargets /
   DescribeTargetHealth.

ZERO-DRIFT (the ecsctl/logsctl approach, copied): the two places drift hides
here are TAGS and ATTRIBUTES, and both are answered from real stores.
 - Tags live in the shared `stores.tags` store keyed
   `"elasticloadbalancing:{arn}"` (ecr/ecsctl/logsctl's convention), so
   `DescribeTags` echoes exactly what `CreateX(Tags=...)`/`AddTags` set.
 - `Describe*Attributes` answers a per-resource attribute MAP seeded at create
   time with real AWS's own documented defaults (`_LB_ATTRIBUTE_DEFAULTS` /
   `_TG_ATTRIBUTE_DEFAULTS`) and merged by `Modify*Attributes`. A MISSING
   attribute key is the classic drift: the provider reads its schema field as
   unset, plans a write, and every subsequent plan is dirty -- so the defaults
   are seeded rather than left absent.
 - A listener's `DefaultActions` is stored and echoed from exactly the members
   the client sent (`Type`, `TargetGroupArn`, `Order`), never default-filled --
   ecsctl.py's byte-for-byte `containerDefinitions` rule, same reasoning.

TARGET ADDRESSES, AND WHO REGISTERS THEM. Real ECS's *service scheduler* (not
terraform) registers each task with the target group named in the service's
`loadBalancers` block, and odin does the same: `gateway/models/ecsctl.py`
calls this module's `register_target`/`deregister_target` as it launches and
stops real task containers. An odin ECS task is a bridge-mode container whose
port is published on the HOST, so its target is
`(Id="host.docker.internal", Port=<the real published host port>)` -- the
honest local analogue of an `ip` target, and exactly the address the proxy
container dials (Colima wires `host.docker.internal` via `--add-host`). An
`i-...` target Id (a canvas `ec2` node, registered by a real
`aws_lb_target_group_attachment`) resolves through `stores.ec2compute` to the
VM's real address.

TARGET HEALTH IS PROBED, NOT INVENTED. `DescribeTargetHealth` performs a REAL
HTTP GET against each registered target's real address, on the target group's
own `HealthCheckPath`, and compares the status to its `Matcher.HttpCode`; a
refused connection or a non-matching status answers `unhealthy` with the real
reason. Open-source nginx can only check upstreams PASSIVELY (`max_fails`/
`fail_timeout` -- active checks are NGINX Plus), so the path/interval are NOT
polled by the proxy; odin does this probe itself instead of reporting a health
state nothing measured. The probe is bounded (`_PROBE_TIMEOUT_SECONDS` per
target) and only runs on this one action.

Persistence: one `JsonStore` at `.odin/{env}/gateway/elbv2ctl.json`
(`stores.elbv2ctl`), flat keys `"lb:{name}"` / `"tg:{name}"` /
`"listener:{id}"` / `"targets:{tg_name}"` -- four disjoint prefixes in one flat
namespace, the convention every other multi-kind model here already uses.
"""
from __future__ import annotations

import logging
import secrets
import threading
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

import httpx
from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.compute.proxy import IDLE_LISTEN_PORT, LoadBalancerProxy, ProxyListener, _sanitize_upstream, target_address
from odin.gateway import errors
from odin.gateway.errors import exc_text
from odin.gateway.stores import NO_CHANGE, SynthStores
from odin.runtime.colima import CONTAINER_HOST

log = logging.getLogger("odin.gateway.elbv2ctl")

SERVICE = "elasticloadbalancing"
_NS = "http://elasticloadbalancing.amazonaws.com/doc/2015-12-01/"
_REQUEST_ID = "00000000-0000-0000-0000-000000000000"

# Real us-east-1 ALB hosted-zone id (a constant in real AWS too) -- the
# provider stores it as a computed attribute, so it must not vary between reads.
_CANONICAL_HOSTED_ZONE_ID = "Z35SXDOTRQ7X7K"

# odin's honest DNSName: a load balancer's proxy is published on a DYNAMIC host
# port (never a fixed 80 -- two load balancers, or two envs, would collide),
# and a DNS NAME has nowhere to put a port. So the name resolves to the right
# HOST and the real port lives where a port belongs: on the record, in
# `DescribeLoadBalancerAttributes`' `odin.endpoint.url`, and in the World facts
# `reconcile/tf_status.py` projects for the canvas node. Recorded as a v1 limit
# in ROADMAP.md rather than papered over.
_DNS_NAME = "127.0.0.1"
# The host to PROBE a `host.docker.internal` target on: that alias is wired
# into containers by Colima's `--add-host` and does NOT resolve on the macOS
# host itself, where this probe runs.
_PROBE_HOST = "127.0.0.1"
_PROBE_TIMEOUT_SECONDS = 0.3

_DEFAULT_TYPE = "application"
_DEFAULT_SCHEME = "internet-facing"
_DEFAULT_IP_ADDRESS_TYPE = "ipv4"
_DEFAULT_TARGET_TYPE = "instance"
_DEFAULT_HEALTH_CHECK_PATH = "/"
_SUPPORTED_ACTION_TYPE = "forward"

# Real AWS's documented default attribute set for an application load balancer.
# EVERY key terraform-provider-aws v5 reads for `aws_lb` is present: absent
# keys are THE zero-drift trap (the provider reads its schema field as unset,
# plans a write, and the next plan is dirty again).
_LB_ATTRIBUTE_DEFAULTS = {
    "access_logs.s3.enabled": "false",
    "access_logs.s3.bucket": "",
    "access_logs.s3.prefix": "",
    "client_keep_alive.seconds": "3600",
    "connection_logs.s3.enabled": "false",
    "connection_logs.s3.bucket": "",
    "connection_logs.s3.prefix": "",
    "deletion_protection.enabled": "false",
    "idle_timeout.timeout_seconds": "60",
    "load_balancing.cross_zone.enabled": "true",
    "routing.http.desync_mitigation_mode": "defensive",
    "routing.http.drop_invalid_header_fields.enabled": "false",
    "routing.http.preserve_host_header.enabled": "false",
    "routing.http.x_amzn_tls_version_and_cipher_suite.enabled": "false",
    "routing.http.xff_client_port.enabled": "false",
    "routing.http.xff_header_processing.mode": "append",
    "routing.http2.enabled": "true",
    "waf.fail_open.enabled": "false",
    "zonal_shift.config.enabled": "false",
}

# Same reasoning for `aws_lb_target_group` (HTTP/instance defaults).
_TG_ATTRIBUTE_DEFAULTS = {
    "deregistration_delay.timeout_seconds": "300",
    "load_balancing.algorithm.type": "round_robin",
    "load_balancing.algorithm.anomaly_mitigation": "off",
    "load_balancing.cross_zone.enabled": "use_load_balancer_configuration",
    "slow_start.duration_seconds": "0",
    "stickiness.app_cookie.cookie_name": "",
    "stickiness.app_cookie.duration_seconds": "86400",
    "stickiness.enabled": "false",
    "stickiness.lb_cookie.duration_seconds": "86400",
    "stickiness.type": "lb_cookie",
    "target_group_health.dns_failover.minimum_healthy_targets.count": "off",
    "target_group_health.dns_failover.minimum_healthy_targets.percentage": "off",
    "target_group_health.unhealthy_state_routing.minimum_healthy_targets.count": "1",
    "target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage": "off",
}

# The health-check members CreateTargetGroup seeds and ModifyTargetGroup edits,
# with real AWS's own defaults for an HTTP/instance target group. Stored as one
# dict so both are a plain merge (never a per-field if-ladder).
_HEALTH_CHECK_DEFAULTS: dict[str, object] = {
    "HealthCheckProtocol": "HTTP",
    "HealthCheckPort": "traffic-port",
    "HealthCheckEnabled": True,
    "HealthCheckPath": _DEFAULT_HEALTH_CHECK_PATH,
    "HealthCheckIntervalSeconds": 30,
    "HealthCheckTimeoutSeconds": 5,
    "HealthyThresholdCount": 5,
    "UnhealthyThresholdCount": 2,
}
_HEALTH_CHECK_INTS = (
    "HealthCheckIntervalSeconds", "HealthCheckTimeoutSeconds",
    "HealthyThresholdCount", "UnhealthyThresholdCount",
)


# --- request parsing: AWS query-protocol serialization ----------------------
# `Prefix.member.N` scalars, `Prefix.member.N.Field` structures (verified
# against botocore's own query serializer for every elbv2 input shape).


def _params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _scalars(params: dict[str, str], prefix: str) -> list[str]:
    """`prefix.member.N` -> an N-ordered list of scalars (`Subnets`,
    `SecurityGroups`, `ResourceArns`, `TagKeys`, `Names`, ...)."""
    marker = f"{prefix}.member."
    indexed = {
        int(key[len(marker):]): value
        for key, value in params.items()
        if key.startswith(marker) and key[len(marker):].isdigit()
    }
    return [indexed[i] for i in sorted(indexed)]


def _structs(params: dict[str, str], prefix: str) -> list[dict[str, str]]:
    """`prefix.member.N.Field` -> an N-ordered list of `{Field: value}` dicts
    (`Tags`, `Attributes`, `Targets`, `DefaultActions`, `SubnetMappings`).
    `Field` keeps its own dots, so a struct-inside-a-struct member arrives as a
    dotted key rather than being silently lost."""
    marker = f"{prefix}.member."
    indexed: dict[int, dict[str, str]] = {}
    for key, value in params.items():
        if not key.startswith(marker):
            continue
        index, _sep, field = key[len(marker):].partition(".")
        if index.isdigit() and field:
            indexed.setdefault(int(index), {})[field] = value
    return [indexed[i] for i in sorted(indexed)]


def _tags(params: dict[str, str]) -> dict[str, str]:
    return {item["Key"]: item.get("Value", "") for item in _structs(params, "Tags") if "Key" in item}


def _attribute_pairs(params: dict[str, str]) -> dict[str, str]:
    return {item["Key"]: item.get("Value", "") for item in _structs(params, "Attributes") if "Key" in item}


def _targets(params: dict[str, str]) -> list[dict]:
    """`Targets.member.N.{Id,Port}` -> `[{"id":..., "port": int|None}]`."""
    return [
        {"id": item["Id"], "port": int(item["Port"]) if item.get("Port", "").isdigit() else None}
        for item in _structs(params, "Targets") if item.get("Id")
    ]


# --- ids / arns ------------------------------------------------------------


def _mint_id() -> str:
    return secrets.token_hex(8)


def lb_arn(name: str, lb_id: str) -> str:
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/{name}/{lb_id}"


def tg_arn(name: str, tg_id: str) -> str:
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{name}/{tg_id}"


def listener_arn(lb_name: str, lb_id: str, listener_id: str) -> str:
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/{lb_name}/{lb_id}/{listener_id}"


def name_from_arn(arn: str) -> str:
    """The bare NAME an elbv2 ARN carries -- the resource `classify.py` reports
    and the key this module's store uses. `loadbalancer/app/{name}/{id}` and
    `listener/app/{lb}/{lbid}/{id}` both yield the LOAD BALANCER's name;
    `targetgroup/{name}/{id}` yields the target group's. Anything that isn't an
    elbv2 ARN comes back unchanged, so a caller may pass a plain name. Kept in
    lock-step with `classify.py::_elbv2_name`."""
    tail = arn.rsplit(":", 1)[-1]
    parts = tail.split("/")
    if parts[0] in ("loadbalancer", "listener") and len(parts) >= 3:
        return parts[2]
    if parts[0] == "targetgroup" and len(parts) >= 2:
        return parts[1]
    return arn


# --- store access ----------------------------------------------------------


def _lb_key(name: str) -> str:
    return f"lb:{name}"


def _tg_key(name: str) -> str:
    return f"tg:{name}"


def _listener_key(listener_id: str) -> str:
    return f"listener:{listener_id}"


def _targets_key(tg_name: str) -> str:
    return f"targets:{tg_name}"


def _lb(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.elbv2ctl.get(env, _lb_key(name))


def _all_lbs(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.elbv2ctl.items(env).items() if k.startswith("lb:")]


def _tg(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.elbv2ctl.get(env, _tg_key(name))


def _all_tgs(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.elbv2ctl.items(env).items() if k.startswith("tg:")]


def _all_listeners(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.elbv2ctl.items(env).items() if k.startswith("listener:")]


def _listeners_for_lb(stores: SynthStores, env: str, lb_name: str) -> list[dict]:
    return sorted(
        (r for r in _all_listeners(stores, env) if r["lb_name"] == lb_name),
        key=lambda r: r["port"],
    )


def _targets_for(stores: SynthStores, env: str, tg_name: str) -> list[dict]:
    return stores.elbv2ctl.get(env, _targets_key(tg_name), [])


def _tg_by_arn(stores: SynthStores, env: str, arn: str) -> dict | None:
    return _tg(stores, env, name_from_arn(arn))


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"{SERVICE}:{arn}", {})


def _set_tags(stores: SynthStores, env: str, arn: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"{SERVICE}:{arn}", tags)


def _resource_by_arn(stores: SynthStores, env: str, arn: str) -> dict | None:
    """The lb / tg / listener record an arbitrary elbv2 ARN names -- the
    tag-API's only lookup (AddTags/RemoveTags/DescribeTags all take
    `ResourceArns`, never a typed id)."""
    tail = arn.rsplit(":", 1)[-1]
    kind = tail.split("/")[0]
    if kind == "loadbalancer":
        return _lb(stores, env, name_from_arn(arn))
    if kind == "targetgroup":
        return _tg(stores, env, name_from_arn(arn))
    return next((r for r in _all_listeners(stores, env) if r["arn"] == arn), None)


# --- wire building: query-protocol XML -------------------------------------


def _response(op: str, result_xml: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{op}Response xmlns="{_NS}"><{op}Result>{result_xml}</{op}Result>'
        f"<ResponseMetadata><RequestId>{_REQUEST_ID}</RequestId></ResponseMetadata>"
        f"</{op}Response>"
    )
    return Response(xml, media_type="text/xml")


def _elem(tag: str, value: object) -> str:
    """One XML element, or "" when the value is unset -- real AWS OMITS an
    unset optional member rather than sending an empty one, and the provider
    reads an empty element as a real value (the `_drop_none` rule logsctl
    keeps for JSON)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return f"<{tag}>{'true' if value else 'false'}</{tag}>"
    return f"<{tag}>{escape(str(value))}</{tag}>"


def _members(tag: str, items: list[str]) -> str:
    return f"<{tag}>" + "".join(f"<member>{item}</member>" for item in items) + f"</{tag}>"


def _not_found(code: str, message: str) -> Response:
    return errors.synth_error(SERVICE, code, message, 400)


def _validation_error(message: str) -> Response:
    return errors.synth_error(SERVICE, "ValidationError", message, 400)


def _lb_xml(record: dict) -> str:
    zones = [
        _elem("ZoneName", zone["ZoneName"]) + _elem("SubnetId", zone["SubnetId"]) + "<LoadBalancerAddresses/>"
        for zone in record["availability_zones"]
    ]
    return (
        _elem("LoadBalancerArn", record["arn"])
        + _elem("DNSName", _DNS_NAME)
        + _elem("CanonicalHostedZoneId", _CANONICAL_HOSTED_ZONE_ID)
        + _elem("CreatedTime", record["created_time"])
        + _elem("LoadBalancerName", record["name"])
        + _elem("Scheme", record["scheme"])
        + _elem("VpcId", record["vpc_id"])
        + f"<State>{_elem('Code', record['state'])}{_elem('Reason', record.get('state_reason'))}</State>"
        + _elem("Type", record["type"])
        + _members("AvailabilityZones", zones)
        + _members("SecurityGroups", [escape(sg) for sg in record["security_groups"]])
        + _elem("IpAddressType", record["ip_address_type"])
    )


def _tg_xml(record: dict, lb_arns: list[str]) -> str:
    matcher = record.get("matcher") or {}
    return (
        _elem("TargetGroupArn", record["arn"])
        + _elem("TargetGroupName", record["name"])
        + _elem("Protocol", record.get("protocol"))
        + _elem("Port", record.get("port"))
        + _elem("VpcId", record.get("vpc_id"))
        + "".join(_elem(key, record["health_check"][key]) for key in _HEALTH_CHECK_DEFAULTS)
        + (f"<Matcher>{_elem('HttpCode', matcher.get('HttpCode'))}{_elem('GrpcCode', matcher.get('GrpcCode'))}</Matcher>" if matcher else "")
        + _members("LoadBalancerArns", [escape(arn) for arn in lb_arns])
        + _elem("TargetType", record["target_type"])
        + _elem("ProtocolVersion", record.get("protocol_version"))
        + _elem("IpAddressType", record.get("ip_address_type"))
    )


def _action_xml(action: dict) -> str:
    return (
        _elem("Type", action.get("Type"))
        + _elem("TargetGroupArn", action.get("TargetGroupArn"))
        + _elem("Order", action.get("Order"))
    )


def _listener_xml(record: dict) -> str:
    return (
        _elem("ListenerArn", record["arn"])
        + _elem("LoadBalancerArn", record["lb_arn"])
        + _elem("Port", record["port"])
        + _elem("Protocol", record["protocol"])
        + "<Certificates/>"
        + _members("DefaultActions", [_action_xml(a) for a in record["default_actions"]])
    )


def _attributes_xml(attributes: dict[str, str]) -> str:
    return _members("Attributes", [
        _elem("Key", key) + _elem("Value", value) for key, value in sorted(attributes.items())
    ])


def _tag_members_xml(tags: dict[str, str]) -> str:
    return _members("Tags", [_elem("Key", k) + _elem("Value", v) for k, v in tags.items()])


# --- the real substrate: converge one load balancer's proxy container -------


def _fail_timeout(tg: dict | None) -> int:
    """The passive fail window (`fail_timeout`) for a target group's upstreams:
    its own `HealthCheckIntervalSeconds`. The honest mapping open-source nginx
    allows -- see compute/proxy.py's HEALTH CHECKS ARE PASSIVE note."""
    interval = (tg or {}).get("health_check", {}).get("HealthCheckIntervalSeconds")
    return int(interval) if interval else int(_HEALTH_CHECK_DEFAULTS["HealthCheckIntervalSeconds"])


def _proxy_listeners(stores: SynthStores, env: str, lb_name: str) -> tuple[ProxyListener, ...]:
    """The current desired proxy shape for `lb_name`: one `ProxyListener` per
    modeled listener, its upstream being that listener's forward target group's
    actually-registered targets."""
    out: list[ProxyListener] = []
    for listener in _listeners_for_lb(stores, env, lb_name):
        forward = next((a for a in listener["default_actions"] if a.get("TargetGroupArn")), {})
        tg = _tg_by_arn(stores, env, forward.get("TargetGroupArn", "")) if forward else None
        targets = _targets_for(stores, env, tg["name"]) if tg else []
        out.append(ProxyListener(
            port=int(listener["port"]),
            upstream=_sanitize_upstream(tg["name"] if tg else "idle"),
            targets=tuple(_upstream_address(stores, env, t) for t in targets),
            fail_timeout_seconds=_fail_timeout(tg),
        ))
    return tuple(out)


def _instance_address(stores: SynthStores, env: str, instance_id: str) -> str | None:
    record = stores.ec2compute.get(env, f"instance:{instance_id}")
    return (record or {}).get("private_ip_address") or (record or {}).get("public_ip_address")


def _target_host(stores: SynthStores, env: str, target: dict) -> str:
    """A registered target's Id -> the real HOST the proxy should dial. An
    `i-...` id is a canvas `ec2` node (a real Lima VM), resolved through the
    ec2-compute store; anything else is already a host/IP (odin's own ECS task
    registration uses `host.docker.internal` -- see the module docstring's
    TARGET ADDRESSES note). An unresolvable instance id falls back to its own
    id, which simply never connects -- an honest dead upstream rather than a
    silently-dropped target."""
    target_id = target["id"]
    if target_id.startswith("i-"):
        return _instance_address(stores, env, target_id) or target_id
    return target_id


def _upstream_address(stores: SynthStores, env: str, target: dict) -> str:
    port = target.get("port") or IDLE_LISTEN_PORT
    return target_address(port, host=_target_host(stores, env, target))


# Per-(SynthStores, env, lb) lock, serializing proxy convergence against
# itself -- the exact instance-scoped WeakKeyDictionary shape
# `ecsctl._lock_for_service` uses (and for the same reason: without it two
# concurrent converges can both decide to recreate the SAME container, and the
# loser force-removes the winner's fresh one). Keyed on the SynthStores
# instance so independent stores (every test reuses env="default") never share
# a lock.
_proxy_locks: "weakref.WeakKeyDictionary[SynthStores, dict[tuple[str, str], threading.Lock]]" = weakref.WeakKeyDictionary()
_proxy_locks_guard = threading.Lock()


def _lock_for_lb(stores: SynthStores, env: str, lb_name: str) -> threading.Lock:
    with _proxy_locks_guard:
        return _proxy_locks.setdefault(stores, {}).setdefault((env, lb_name), threading.Lock())


def converge_proxy(stores: SynthStores, env: str, lb_name: str, proxy: LoadBalancerProxy) -> None:
    """Bring the load balancer's REAL nginx container in line with the current
    listener/target state and record the published endpoint + `active` state on
    the lb record. Synchronous and idempotent; every caller either already runs
    off the event loop (ecsctl's task threads) or goes through `_spawn`."""
    with _lock_for_lb(stores, env, lb_name):
        record = _lb(stores, env, lb_name)
        if record is None:  # deleted while this converge was queued
            return
        listeners = _proxy_listeners(stores, env, lb_name)
        published = proxy.ensure(stores.root, env, lb_name, listeners)

        def mutate(current: dict | None) -> dict | object:
            if current is None:
                return NO_CHANGE
            return {
                **current, "state": "active", "state_reason": None,
                "endpoints": {str(port): host_port for port, host_port in published.items()},
            }

        stores.elbv2ctl.update(env, _lb_key(lb_name), mutate)


def _converge_safely(stores: SynthStores, env: str, lb_name: str, proxy: LoadBalancerProxy) -> None:
    """`converge_proxy` on a daemon thread with no caller to raise to -- the
    same "a silent hang is forbidden" contract ec2compute's `_finish_boot` and
    ecsctl's `_launch_task` keep: a real `docker run` failure becomes the load
    balancer's honest `failed` state plus a log line, never an exception nobody
    ever sees."""
    try:
        converge_proxy(stores, env, lb_name, proxy)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        log.warning("load-balancer proxy failed for %s (env %s): %s", lb_name, env, exc)
        reason = exc_text(exc)
        stores.elbv2ctl.update(env, _lb_key(lb_name), lambda current: (
            NO_CHANGE if current is None else {**current, "state": "failed", "state_reason": reason}
        ))


def _spawn(stores: SynthStores, env: str, lb_name: str, proxy: LoadBalancerProxy) -> None:
    threading.Thread(target=_converge_safely, args=(stores, env, lb_name, proxy), daemon=True).start()


def _converge_target_group(stores: SynthStores, env: str, tg_name: str, proxy: LoadBalancerProxy, spawn: bool) -> None:
    """Re-converge every load balancer whose listener forwards to `tg_name` --
    what a target change actually has to touch.

    `spawn=False` (the ECS task-launch path, already on its own thread) still
    goes through `_converge_safely`, NOT bare `converge_proxy`: a real `docker
    cp`/`docker run` failure there would otherwise propagate into
    `ecsctl._launch_task`'s caller and kill that daemon thread outright, leaving
    a RUNNING task nothing ever registers. Same "a silent hang is forbidden"
    contract, one function."""
    tg = _tg(stores, env, tg_name)
    arn = (tg or {}).get("arn")
    lb_names = {
        listener["lb_name"] for listener in _all_listeners(stores, env)
        if any(a.get("TargetGroupArn") == arn for a in listener["default_actions"])
    }
    for lb_name in sorted(lb_names):
        (_spawn if spawn else _converge_safely)(stores, env, lb_name, proxy)


# --- the internal registration API the ECS substrate uses (never the wire) --


def register_target(
    stores: SynthStores, env: str, target_group_arn: str, target_id: str, port: int,
    proxy: LoadBalancerProxy | None = None,
) -> None:
    """Register one target and reload the fronting proxies SYNCHRONOUSLY --
    `gateway/models/ecsctl.py`'s task-launch path, the odin equivalent of real
    ECS's service scheduler registering a task with its target group. Not
    reachable over the wire, so it deliberately skips the TargetGroupNotFound
    guard `RegisterTargets` keeps: an unknown target group simply stores
    nothing (a service pointing at a deleted group has no proxy to reload
    either)."""
    _register(stores, env, target_group_arn, [{"id": target_id, "port": port}], proxy or LoadBalancerProxy(), spawn=False)


def deregister_target(
    stores: SynthStores, env: str, target_group_arn: str, target_id: str, port: int,
    proxy: LoadBalancerProxy | None = None,
) -> None:
    """The inverse of `register_target` -- called as ECS stops a task."""
    _deregister(stores, env, target_group_arn, [{"id": target_id, "port": port}], proxy or LoadBalancerProxy(), spawn=False)


def _register(stores: SynthStores, env: str, arn: str, targets: list[dict], proxy: LoadBalancerProxy, spawn: bool) -> None:
    tg = _tg_by_arn(stores, env, arn)
    if tg is None:
        return
    wanted = {(t["id"], t.get("port")) for t in targets}

    def mutate(current: list | None) -> list:
        kept = [t for t in (current or []) if (t["id"], t.get("port")) not in wanted]
        return [*kept, *targets]

    stores.elbv2ctl.update(env, _targets_key(tg["name"]), mutate)
    _converge_target_group(stores, env, tg["name"], proxy, spawn)


def _deregister(stores: SynthStores, env: str, arn: str, targets: list[dict], proxy: LoadBalancerProxy, spawn: bool) -> None:
    tg = _tg_by_arn(stores, env, arn)
    if tg is None:
        return
    doomed = {(t["id"], t.get("port")) for t in targets}
    stores.elbv2ctl.update(env, _targets_key(tg["name"]), lambda current: [
        t for t in (current or []) if (t["id"], t.get("port")) not in doomed
    ])
    _converge_target_group(stores, env, tg["name"], proxy, spawn)


def endpoint_url(record: dict) -> str | None:
    """`http://127.0.0.1:{port}` for the load balancer's FIRST listener -- the
    genuinely reachable address the DNSName can't carry (see `_DNS_NAME`).
    None until the proxy is published."""
    endpoints = record.get("endpoints") or {}
    ports = sorted(int(port) for port in endpoints)
    return f"http://{_DNS_NAME}:{endpoints[str(ports[0])]}" if ports else None


# --- Load balancer ---------------------------------------------------------


def _availability_zones(stores: SynthStores, env: str, subnet_ids: list[str]) -> list[dict]:
    zones = []
    for subnet_id in subnet_ids:
        subnet = stores.ec2net.get(env, f"subnet:{subnet_id}") or {}
        zones.append({"ZoneName": subnet.get("availability_zone") or f"{REGION}a", "SubnetId": subnet_id})
    return zones


def _create_load_balancer(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = params.get("Name", "")
    if not name:
        return _validation_error("Name is required")
    if _lb(stores, env, name) is not None:
        return errors.synth_error(
            SERVICE, "DuplicateLoadBalancerName",
            f"A load balancer with the name '{name}' already exists", 400,
        )
    subnet_ids = _scalars(params, "Subnets") or [m["SubnetId"] for m in _structs(params, "SubnetMappings") if m.get("SubnetId")]
    zones = _availability_zones(stores, env, subnet_ids)
    # Real CreateLoadBalancer DERIVES VpcId from the subnets rather than taking
    # it -- so odin reads it off the subnet records the EC2-network model
    # already holds instead of inventing one.
    vpc_id = next((
        (stores.ec2net.get(env, f"subnet:{sid}") or {}).get("vpc_id") for sid in subnet_ids
        if (stores.ec2net.get(env, f"subnet:{sid}") or {}).get("vpc_id")
    ), "")
    lb_id = _mint_id()
    record = {
        "name": name,
        "lb_id": lb_id,
        "arn": lb_arn(name, lb_id),
        "scheme": params.get("Scheme") or _DEFAULT_SCHEME,
        "type": params.get("Type") or _DEFAULT_TYPE,
        "ip_address_type": params.get("IpAddressType") or _DEFAULT_IP_ADDRESS_TYPE,
        "vpc_id": vpc_id,
        "subnets": subnet_ids,
        "security_groups": _scalars(params, "SecurityGroups"),
        "availability_zones": zones,
        "created_time": datetime.now(UTC).isoformat(),
        # Honest asynchrony (module docstring): the REAL nginx container comes
        # up on a daemon thread, and `active` is what the provider's own
        # waiter polls for.
        "state": "provisioning",
        "state_reason": None,
        "attributes": dict(_LB_ATTRIBUTE_DEFAULTS),
        "endpoints": {},
    }
    stores.elbv2ctl.set(env, _lb_key(name), record)
    _set_tags(stores, env, record["arn"], _tags(params))
    response = _response("CreateLoadBalancer", _members("LoadBalancers", [_lb_xml(record)]))
    _spawn(stores, env, name, proxy)
    return response


def _select_lbs(params: dict[str, str], env: str, stores: SynthStores) -> tuple[list[dict], str | None]:
    arns = _scalars(params, "LoadBalancerArns")
    names = _scalars(params, "Names") or [name_from_arn(arn) for arn in arns]
    if not names:
        return sorted(_all_lbs(stores, env), key=lambda r: r["name"]), None
    selected = []
    for name in names:
        record = _lb(stores, env, name)
        if record is None:
            return [], name
        selected.append(record)
    return selected, None


def _describe_load_balancers(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    selected, missing = _select_lbs(params, env, stores)
    if missing is not None:
        return _not_found("LoadBalancerNotFound", f"Load balancer '{missing}' not found")
    return _response("DescribeLoadBalancers", _members("LoadBalancers", [_lb_xml(r) for r in selected]))


def _delete_load_balancer(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("LoadBalancerArn", ""))
    record = _lb(stores, env, name)
    if record is None:
        return _not_found("LoadBalancerNotFound", f"Load balancer '{name}' not found")
    # Real DeleteLoadBalancer deletes the LB's listeners with it.
    for listener in _listeners_for_lb(stores, env, name):
        stores.elbv2ctl.delete(env, _listener_key(listener["listener_id"]))
        _set_tags(stores, env, listener["arn"], {})
    stores.elbv2ctl.delete(env, _lb_key(name))
    _set_tags(stores, env, record["arn"], {})
    # HARD delete + a REAL container removal: ids are random and never reused,
    # so the provider's post-delete read lands in the ordinary unknown-name
    # path (LoadBalancerNotFound), which is what its delete waiter treats as
    # "gone" -- ec2net.py's verified no-grace-window precedent.
    proxy.destroy(env, name)
    return _response("DeleteLoadBalancer", "")


def _describe_load_balancer_attributes(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("LoadBalancerArn", ""))
    record = _lb(stores, env, name)
    if record is None:
        return _not_found("LoadBalancerNotFound", f"Load balancer '{name}' not found")
    # `odin.endpoint.url` is an ODIN-ONLY attribute (never a real AWS key):
    # the reachable `http://127.0.0.1:{port}` a DNSName has nowhere to put.
    # Additive, so it can't drift the provider's own attribute reads.
    extra = {"odin.endpoint.url": endpoint_url(record) or ""}
    return _response("DescribeLoadBalancerAttributes", _attributes_xml({**record["attributes"], **extra}))


def _modify_load_balancer_attributes(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("LoadBalancerArn", ""))
    record = _lb(stores, env, name)
    if record is None:
        return _not_found("LoadBalancerNotFound", f"Load balancer '{name}' not found")
    attributes = {**record["attributes"], **_attribute_pairs(params)}
    stores.elbv2ctl.set(env, _lb_key(name), {**record, "attributes": attributes})
    return _response("ModifyLoadBalancerAttributes", _attributes_xml(attributes))


# --- Target group ----------------------------------------------------------


def _health_check(params: dict[str, str], current: dict | None = None) -> dict:
    """The health-check member set: defaults, then whatever the request
    actually carried (a plain merge -- CreateTargetGroup starts from the
    defaults, ModifyTargetGroup from the stored set)."""
    merged = dict(current or _HEALTH_CHECK_DEFAULTS)
    merged.update({
        key: params[key] for key in ("HealthCheckProtocol", "HealthCheckPort", "HealthCheckPath")
        if params.get(key)
    })
    merged.update({key: int(params[key]) for key in _HEALTH_CHECK_INTS if params.get(key, "").isdigit()})
    if params.get("HealthCheckEnabled"):
        merged["HealthCheckEnabled"] = params["HealthCheckEnabled"] == "true"
    return merged


def _create_target_group(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = params.get("Name", "")
    if not name:
        return _validation_error("Name is required")
    if _tg(stores, env, name) is not None:
        return errors.synth_error(
            SERVICE, "DuplicateTargetGroupName",
            f"A target group with the name '{name}' already exists", 400,
        )
    tg_id = _mint_id()
    record = {
        "name": name,
        "tg_id": tg_id,
        "arn": tg_arn(name, tg_id),
        "protocol": params.get("Protocol") or "HTTP",
        "protocol_version": params.get("ProtocolVersion") or "HTTP1",
        "port": int(params["Port"]) if params.get("Port", "").isdigit() else None,
        "vpc_id": params.get("VpcId") or "",
        "target_type": params.get("TargetType") or _DEFAULT_TARGET_TYPE,
        "ip_address_type": params.get("IpAddressType") or _DEFAULT_IP_ADDRESS_TYPE,
        "health_check": _health_check(params),
        "matcher": {"HttpCode": (params.get("Matcher.HttpCode") or "200"), "GrpcCode": params.get("Matcher.GrpcCode")},
        "attributes": dict(_TG_ATTRIBUTE_DEFAULTS),
    }
    stores.elbv2ctl.set(env, _tg_key(name), record)
    stores.elbv2ctl.set(env, _targets_key(name), [])
    _set_tags(stores, env, record["arn"], _tags(params))
    return _response("CreateTargetGroup", _members("TargetGroups", [_tg_xml(record, [])]))


def _lb_arns_for_tg(stores: SynthStores, env: str, tg_record: dict) -> list[str]:
    """The load balancers whose listeners forward to this target group -- the
    `LoadBalancerArns` member the provider reads to know the group is in use."""
    lb_names = {
        listener["lb_name"] for listener in _all_listeners(stores, env)
        if any(a.get("TargetGroupArn") == tg_record["arn"] for a in listener["default_actions"])
    }
    return sorted(
        record["arn"] for record in _all_lbs(stores, env) if record["name"] in lb_names
    )


def _describe_target_groups(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arns = _scalars(params, "TargetGroupArns")
    names = _scalars(params, "Names") or [name_from_arn(arn) for arn in arns]
    lb_filter = params.get("LoadBalancerArn")
    if names:
        selected = []
        for name in names:
            record = _tg(stores, env, name)
            if record is None:
                return _not_found("TargetGroupNotFound", f"Target group '{name}' not found")
            selected.append(record)
    else:
        selected = sorted(_all_tgs(stores, env), key=lambda r: r["name"])
    groups = [(r, _lb_arns_for_tg(stores, env, r)) for r in selected]
    if lb_filter:
        groups = [(r, arns_for) for r, arns_for in groups if lb_filter in arns_for]
    return _response("DescribeTargetGroups", _members("TargetGroups", [_tg_xml(r, a) for r, a in groups]))


def _delete_target_group(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("TargetGroupArn", ""))
    record = _tg(stores, env, name)
    if record is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name}' not found")
    stores.elbv2ctl.delete(env, _tg_key(name))
    stores.elbv2ctl.delete(env, _targets_key(name))
    _set_tags(stores, env, record["arn"], {})
    return _response("DeleteTargetGroup", "")


def _modify_target_group(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("TargetGroupArn", ""))
    record = _tg(stores, env, name)
    if record is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name}' not found")
    matcher = dict(record.get("matcher") or {})
    matcher.update({key: params[f"Matcher.{key}"] for key in ("HttpCode", "GrpcCode") if params.get(f"Matcher.{key}")})
    updated = {**record, "health_check": _health_check(params, record["health_check"]), "matcher": matcher}
    stores.elbv2ctl.set(env, _tg_key(name), updated)
    # A changed health-check interval changes the proxy's passive `fail_timeout`.
    _converge_target_group(stores, env, name, proxy, spawn=True)
    return _response("ModifyTargetGroup", _members("TargetGroups", [_tg_xml(updated, _lb_arns_for_tg(stores, env, updated))]))


def _describe_target_group_attributes(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("TargetGroupArn", ""))
    record = _tg(stores, env, name)
    if record is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name}' not found")
    return _response("DescribeTargetGroupAttributes", _attributes_xml(record["attributes"]))


def _modify_target_group_attributes(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    name = name_from_arn(params.get("TargetGroupArn", ""))
    record = _tg(stores, env, name)
    if record is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name}' not found")
    attributes = {**record["attributes"], **_attribute_pairs(params)}
    stores.elbv2ctl.set(env, _tg_key(name), {**record, "attributes": attributes})
    return _response("ModifyTargetGroupAttributes", _attributes_xml(attributes))


# --- Listener --------------------------------------------------------------


def _default_actions(params: dict[str, str]) -> list[dict]:
    """`DefaultActions.member.N.*` -> the modeled members ONLY (`Type`,
    `TargetGroupArn`, `Order`), echoed back exactly as submitted. v1 models the
    `forward` action type; `redirect` / `fixed-response` / `authenticate-*`
    have no substrate behind them here, so `_create_listener` REJECTS them with
    a real ValidationError rather than accepting a config it wouldn't serve."""
    actions = []
    for item in _structs(params, "DefaultActions"):
        action = {"Type": item.get("Type"), "TargetGroupArn": item.get("TargetGroupArn")}
        if item.get("Order", "").isdigit():
            action["Order"] = int(item["Order"])
        actions.append({k: v for k, v in action.items() if v is not None})
    return actions


def _create_listener(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    lb_name = name_from_arn(params.get("LoadBalancerArn", ""))
    lb = _lb(stores, env, lb_name)
    if lb is None:
        return _not_found("LoadBalancerNotFound", f"Load balancer '{lb_name}' not found")
    actions = _default_actions(params)
    unsupported = [a.get("Type") for a in actions if a.get("Type") != _SUPPORTED_ACTION_TYPE]
    if unsupported:
        return _validation_error(
            f"default action type '{unsupported[0]}' is not supported by odin v1 (only '{_SUPPORTED_ACTION_TYPE}')"
        )
    listener_id = _mint_id()
    record = {
        "listener_id": listener_id,
        "arn": listener_arn(lb_name, lb["lb_id"], listener_id),
        "lb_name": lb_name,
        "lb_arn": lb["arn"],
        "port": int(params["Port"]) if params.get("Port", "").isdigit() else IDLE_LISTEN_PORT,
        "protocol": params.get("Protocol") or "HTTP",
        "default_actions": actions,
        # Empty by design -- see `_describe_listener_attributes`.
        "attributes": {},
    }
    stores.elbv2ctl.set(env, _listener_key(listener_id), record)
    _set_tags(stores, env, record["arn"], _tags(params))
    response = _response("CreateListener", _members("Listeners", [_listener_xml(record)]))
    # A new listener changes the proxy's PUBLISHED PORT SET, which Docker can't
    # apply to a live container -- `LoadBalancerProxy.ensure` recreates it.
    _spawn(stores, env, lb_name, proxy)
    return response


def _describe_listeners(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arns = _scalars(params, "ListenerArns")
    lb_arn_filter = params.get("LoadBalancerArn")
    if arns:
        by_arn = {r["arn"]: r for r in _all_listeners(stores, env)}
        selected = []
        for arn in arns:
            record = by_arn.get(arn)
            if record is None:
                return _not_found("ListenerNotFound", f"Listener '{arn}' not found")
            selected.append(record)
    elif lb_arn_filter:
        selected = _listeners_for_lb(stores, env, name_from_arn(lb_arn_filter))
    else:
        selected = sorted(_all_listeners(stores, env), key=lambda r: r["arn"])
    return _response("DescribeListeners", _members("Listeners", [_listener_xml(r) for r in selected]))


def _modify_listener(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arn = params.get("ListenerArn", "")
    record = next((r for r in _all_listeners(stores, env) if r["arn"] == arn), None)
    if record is None:
        return _not_found("ListenerNotFound", f"Listener '{arn}' not found")
    actions = _default_actions(params) or record["default_actions"]
    updated = {
        **record,
        "port": int(params["Port"]) if params.get("Port", "").isdigit() else record["port"],
        "protocol": params.get("Protocol") or record["protocol"],
        "default_actions": actions,
    }
    stores.elbv2ctl.set(env, _listener_key(record["listener_id"]), updated)
    response = _response("ModifyListener", _members("Listeners", [_listener_xml(updated)]))
    _spawn(stores, env, record["lb_name"], proxy)
    return response


def _describe_listener_attributes(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    """FOUND EMPIRICALLY, not from the resource's documented surface: the first
    real `tofu apply` through the real gateway failed at
    `aws_lb_listener` create with "reading ELBv2 Listener attributes:
    InvalidAction: DescribeListenerAttributes" -- terraform-provider-aws 5.100
    calls this on EVERY listener read regardless of load-balancer type. Exactly
    the class of unmodeled READ the W2.4 audit warned breaks every apply, which
    is why the harness ran before this module was called finished.

    The attribute set is EMPTY for an application HTTP listener, matching real
    AWS: the only listener attribute AWS defines today
    (`tcp.idle_timeout.seconds`) belongs to NETWORK load balancers, which v1
    doesn't model at all -- so inventing a value here would be fiction, and the
    provider only reads that key for an NLB listener anyway."""
    arn = params.get("ListenerArn", "")
    record = next((r for r in _all_listeners(stores, env) if r["arn"] == arn), None)
    if record is None:
        return _not_found("ListenerNotFound", f"Listener '{arn}' not found")
    return _response("DescribeListenerAttributes", _attributes_xml(record.get("attributes") or {}))


def _modify_listener_attributes(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arn = params.get("ListenerArn", "")
    record = next((r for r in _all_listeners(stores, env) if r["arn"] == arn), None)
    if record is None:
        return _not_found("ListenerNotFound", f"Listener '{arn}' not found")
    attributes = {**(record.get("attributes") or {}), **_attribute_pairs(params)}
    stores.elbv2ctl.set(env, _listener_key(record["listener_id"]), {**record, "attributes": attributes})
    return _response("ModifyListenerAttributes", _attributes_xml(attributes))


def _delete_listener(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arn = params.get("ListenerArn", "")
    record = next((r for r in _all_listeners(stores, env) if r["arn"] == arn), None)
    if record is None:
        return _not_found("ListenerNotFound", f"Listener '{arn}' not found")
    stores.elbv2ctl.delete(env, _listener_key(record["listener_id"]))
    _set_tags(stores, env, arn, {})
    response = _response("DeleteListener", "")
    _spawn(stores, env, record["lb_name"], proxy)
    return response


# --- Targets ---------------------------------------------------------------


def _register_targets(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arn = params.get("TargetGroupArn", "")
    if _tg_by_arn(stores, env, arn) is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name_from_arn(arn)}' not found")
    _register(stores, env, arn, _targets(params), proxy, spawn=True)
    return _response("RegisterTargets", "")


def _deregister_targets(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arn = params.get("TargetGroupArn", "")
    if _tg_by_arn(stores, env, arn) is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name_from_arn(arn)}' not found")
    _deregister(stores, env, arn, _targets(params), proxy, spawn=True)
    return _response("DeregisterTargets", "")


def _probe_target(stores: SynthStores, env: str, tg: dict, target: dict) -> tuple[str, str, str]:
    """(State, Reason, Description) from a REAL HTTP GET against the target's
    real address on the target group's own health-check path, compared to its
    `Matcher.HttpCode`. Never invents `healthy`: a refused connection or a
    non-matching status is `unhealthy` with the actual reason (the same
    "report what the substrate really did" rule ec2/lambda/ecs verdicts
    follow). One bounded request per target, only on this action."""
    health = tg["health_check"]
    if not health.get("HealthCheckEnabled", True):
        return "unused", "Target.HealthCheckDisabled", "Health checks are disabled for this target group"
    host = _target_host(stores, env, target)
    probe_host = _PROBE_HOST if host == CONTAINER_HOST else host
    port = target.get("port") or tg.get("port") or IDLE_LISTEN_PORT
    url = f"http://{probe_host}:{port}{health['HealthCheckPath']}"
    expected = (tg.get("matcher") or {}).get("HttpCode") or "200"
    try:
        status = httpx.get(url, timeout=_PROBE_TIMEOUT_SECONDS).status_code
    except httpx.HTTPError as exc:
        return "unhealthy", "Target.Timeout", f"{type(exc).__name__} connecting to {url}"
    if str(status) in {code.strip() for code in expected.split(",")}:
        return "healthy", "", ""
    return "unhealthy", "Target.ResponseCodeMismatch", f"Health checks failed with these codes: [{status}]"


def _describe_target_health(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    arn = params.get("TargetGroupArn", "")
    tg = _tg_by_arn(stores, env, arn)
    if tg is None:
        return _not_found("TargetGroupNotFound", f"Target group '{name_from_arn(arn)}' not found")
    wanted = _targets(params)
    registered = _targets_for(stores, env, tg["name"])
    selected = [
        t for t in registered
        if not wanted or any(w["id"] == t["id"] and (w["port"] in (None, t.get("port"))) for w in wanted)
    ]
    descriptions = []
    for target in selected:
        state, reason, description = _probe_target(stores, env, tg, target)
        descriptions.append(
            f"<Target>{_elem('Id', target['id'])}{_elem('Port', target.get('port'))}</Target>"
            + _elem("HealthCheckPort", str(target.get("port") or tg.get("port") or IDLE_LISTEN_PORT))
            + f"<TargetHealth>{_elem('State', state)}{_elem('Reason', reason or None)}{_elem('Description', description or None)}</TargetHealth>"
        )
    return _response("DescribeTargetHealth", _members("TargetHealthDescriptions", descriptions))


# --- Tags (AddTags/RemoveTags/DescribeTags -- ResourceArns, never a typed id)


def _add_tags(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    new_tags = _tags(params)
    for arn in _scalars(params, "ResourceArns"):
        if _resource_by_arn(stores, env, arn) is None:
            return _not_found("ResourceNotFound", f"Resource '{arn}' not found")
        _set_tags(stores, env, arn, {**_tags_for(stores, env, arn), **new_tags})
    return _response("AddTags", "")


def _remove_tags(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    removed = set(_scalars(params, "TagKeys"))
    for arn in _scalars(params, "ResourceArns"):
        if _resource_by_arn(stores, env, arn) is None:
            return _not_found("ResourceNotFound", f"Resource '{arn}' not found")
        _set_tags(stores, env, arn, {k: v for k, v in _tags_for(stores, env, arn).items() if k not in removed})
    return _response("RemoveTags", "")


def _describe_tags(params: dict[str, str], env: str, stores: SynthStores, proxy: LoadBalancerProxy) -> Response:
    descriptions = []
    for arn in _scalars(params, "ResourceArns"):
        if _resource_by_arn(stores, env, arn) is None:
            return _not_found("ResourceNotFound", f"Resource '{arn}' not found")
        descriptions.append(_elem("ResourceArn", arn) + _tag_members_xml(_tags_for(stores, env, arn)))
    return _response("DescribeTags", _members("TagDescriptions", descriptions))


# --- dispatch --------------------------------------------------------------

_Handler = Callable[[dict[str, str], str, SynthStores, LoadBalancerProxy], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateLoadBalancer": _create_load_balancer,
    "DescribeLoadBalancers": _describe_load_balancers,
    "DeleteLoadBalancer": _delete_load_balancer,
    "DescribeLoadBalancerAttributes": _describe_load_balancer_attributes,
    "ModifyLoadBalancerAttributes": _modify_load_balancer_attributes,
    "CreateTargetGroup": _create_target_group,
    "DescribeTargetGroups": _describe_target_groups,
    "DeleteTargetGroup": _delete_target_group,
    "ModifyTargetGroup": _modify_target_group,
    "DescribeTargetGroupAttributes": _describe_target_group_attributes,
    "ModifyTargetGroupAttributes": _modify_target_group_attributes,
    "CreateListener": _create_listener,
    "DescribeListeners": _describe_listeners,
    "ModifyListener": _modify_listener,
    "DescribeListenerAttributes": _describe_listener_attributes,
    "ModifyListenerAttributes": _modify_listener_attributes,
    "DeleteListener": _delete_listener,
    "RegisterTargets": _register_targets,
    "DeregisterTargets": _deregister_targets,
    "DescribeTargetHealth": _describe_target_health,
    "AddTags": _add_tags,
    "RemoveTags": _remove_tags,
    "DescribeTags": _describe_tags,
}


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    proxy: LoadBalancerProxy | None = None,
) -> Response:
    """The whole elbv2 answer -- same no-backing contract as
    ec2net/iamctl/ecr/logsctl: an unmodeled action gets a protocol-correct
    error, never a 503 and never a silent forward. `proxy` is the injectable
    real substrate (a test passes a fake with the same `ensure`/`status`/
    `destroy` shape); production callers leave it default, mirroring
    ec2compute's `vm or InstanceVm()` precedent."""
    op = action.removeprefix(f"{SERVICE}:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error(SERVICE, "InvalidAction", f"The action {op} is not valid.", 400)
    return handler(_params(body), env, stores, proxy or LoadBalancerProxy())
