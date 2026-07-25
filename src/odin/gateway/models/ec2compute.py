"""The gateway's EC2-compute model (task V3): instances + key pairs, built to
the captured `aws_instance`/`aws_key_pair` call surface in
docs/superpowers/research/research-coverage.md §2b and MiniStack's own EC2
instance shape (§2.6: "pure in-memory, no VM ... instant `running`") --
adopted as a design, explicitly NOT as behavior: NORTHSTAR's own resolved
item 4 is "EC2 instances = real Lima VMs", so unlike MiniStack this module's
states are REAL (`compute/instances.py::InstanceVm` boots an actual VM per
instance) and `RunInstances` returns `pending`, never an instant `running`.

EXTENDS V1a's `ec2net.py` branch (module docstring there): this module owns
RunInstances/DescribeInstances/Terminate/Stop/Start + the four key-pair
actions + the read-only instance-hydration describes; `pure_answer` below
falls through to `ec2net.pure_answer` for everything it doesn't recognize
(VPC/Subnet/SG, CreateTags/DeleteTags/DescribeTags -- resource-id-agnostic,
so instance/key-pair/volume tags work there unmodified once `_RESOURCE_TYPES`
knows their id prefixes -- and the catch-all InvalidAction envelope for a
genuinely unknown ec2 action). `gateway/synth.py` calls this module's
`pure_answer` for every `ec2:*` action now, not `ec2net`'s directly.

Wire-parsing helpers (`_params`/`_indexed`/`_scalars`/`_filters`/`_matches`/
`_spec_tags`/`_mint`/`_response`/`_tag_set_xml`) are a deliberate DUPLICATE of
ec2net.py's private copies, not an import -- the same self-containment
`iamctl.py`'s module docstring argues for (no coupling between sibling model
modules; each stays independently readable/testable). All shapes verified
the same way V1a's were: every response round-trips through botocore's own
`EC2QueryParser` in tests/gateway/test_ec2compute.py.

Model decisions, each traced to the research:
- RunInstances is MinCount=MaxCount=1 only (v1) -- ec2net's SubnetId/GroupId
  id-shaped resource extraction in classify.py already treats every ec2:*
  call as OPERATOR-only, so a single instance per call is the whole surface
  the TF provider ever needs (`aws_instance` is a singular resource).
- ImageId is accepted VERBATIM, never validated (research §2b: "a stub
  catalog of 2-3 AMIs" is documentation, not enforcement -- `compute/
  lima_yaml.py` always boots the same Ubuntu 24.04 image regardless of which
  string is stored, so validating would only reject configs for no behavioral
  reason).
- InstanceType maps to `compute.models.get_instance_type` (the SAME table
  `LimaRuntime`'s own host VM uses, extended with t3.* rows) -- default
  `t3.micro` when the field is absent.
- The state machine is real and asynchronous: RunInstances mints the record
  as `pending` and returns immediately, spawning a background thread that
  calls `InstanceVm.boot` -- the provider's own DescribeInstances waiter
  (research: "sleeps ~10s, then polls until running") is built for exactly
  this Lima-boot latency, no timing hack needed. A boot failure lands the
  instance in `terminated` with a `StateReason`, never a silent hang
  (`_finish_boot`'s `except` is the one deliberately broad catch in this
  module -- required so an uncaught exception on a daemon thread can't strand
  an instance `pending` forever). Stop/Start/Terminate follow the same
  immediate-transitional-state + background-completion shape.
- Terminate keeps the record ~60s after `terminated` (MiniStack's lazy-sweep
  pattern, matching SQS's delete-grace shape in gateway/synth.py) so a
  `tofu destroy` polling DescribeInstances still briefly sees the instance
  before it's gone; `_sweep_terminated` runs on every DescribeInstances call.
- ImportKeyPair (not CreateKeyPair) is what `aws_key_pair`'s `public_key`
  argument drives (research §2b) -- both are modeled, but only Import is on
  the integration test's critical path. CreateKeyPair still generates a REAL
  RSA keypair via `ssh-keygen` (no mock-only modes), the same shelling-out
  style `fabric/nebula.py` uses for `nebula-cert`.
- The instance's auto root EBS volume (DescribeVolumes parity, research
  §2b) is minted alongside the instance and nested on its record rather than
  given its own store namespace -- v1 never models more than one volume per
  instance, so a separate key family would be pure overhead.
- Nebula join info (the instance's containing VPC, if any) is resolved HERE
  (from `stores.ec2net`, already available on the shared `stores` object) and
  passed down as `compute.instances.NebulaJoin` -- Layer 2 (`InstanceVm`)
  owns signing the cert and writing the config, this module only decides
  WHETHER to ask for it.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.compute.instances import InstanceVm, NebulaJoin, vm_name
from odin.compute.models import INSTANCE_TYPES, get_instance_type
from odin.fabric.models import FirewallRules
from odin.fabric.nebula import LighthouseManager, union_firewalls
from odin.gateway import errors
from odin.gateway.keys import KeyStore, workload_env
from odin.gateway.models import ec2net
from odin.gateway.stores import NO_CHANGE, SynthStores

log = logging.getLogger("odin.gateway.ec2compute")

_EC2_NS = "http://ec2.amazonaws.com/doc/2016-11-15/"
_REQUEST_ID = "00000000-0000-0000-0000-000000000000"

# research §2b: real EC2 instance-state codes, echoed verbatim on the wire.
_STATE_CODES = {
    "pending": 0, "running": 16, "shutting-down": 32,
    "terminated": 48, "stopping": 64, "stopped": 80,
}

# Documentation only -- see the module docstring's "accepted verbatim" note.
_DEFAULT_AMI = "ami-0c101f26f147fa7fd"

_DEFAULT_INSTANCE_TYPE = "t3.micro"

# research §2b: the DescribeInstanceAttribute names the provider's hydration
# probes -- each attribute name doubles as the response element tag (same
# convention ec2net.py's DescribeVpcAttribute uses, verified against
# botocore's own EC2 model).
_INSTANCE_ATTRIBUTE_DEFAULTS = {
    "disableApiTermination": "false",
    "instanceInitiatedShutdownBehavior": "stop",
    "sourceDestCheck": "true",
}

# The lazy-sweep window (MiniStack's pattern; matches synth.py's SQS
# QUEUE_DELETE_GRACE_SECONDS in spirit, just longer -- a `tofu destroy`
# poll cadence is coarser than SQS's).
_TERMINATED_SWEEP_SECONDS = 60.0

# Release finding #3 -- the resurrection race: RunInstances spawns
# `_finish_boot` on a daemon thread that can still be mid-flight when a
# TerminateInstances call for the SAME instance wins the race and completes
# first. Once an instance has entered one of these states, NO later
# completion (a slow boot finishing as "running", a stale Stop/Start
# finishing as "stopped"/"running") may pull it back out -- `_update_instance`
# below enforces this as an explicit allowed-transitions guard, not just
# "last write wins".
_TERMINAL_STATES = frozenset({"shutting-down", "terminated"})

# `compute.instances.vm_name`'s own prefix -- the startup reaper
# (`reap_orphaned_vms` below) only ever considers a VM shaped like this,
# never anything else `limactl list` happens to report.
_VM_NAME_PREFIX = "odin-ec2-"


def _mint(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(9)[:17]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _gib(size: str) -> int:
    """`"<N>GiB"` -> `N` -- both `VmConfig.disk` and `.memory` use this exact
    string shape in `compute/models.py`'s table, so one parser covers both a
    volume's size and an instance type's memory (in GiB)."""
    return int(size.removesuffix("GiB"))


# --- request parsing: EC2 query-protocol serialization (duplicated from
# ec2net.py -- see module docstring) -------------------------------------


def _params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _indexed(params: dict[str, str], prefix: str) -> list[dict[str, str]]:
    grouped: dict[int, dict[str, str]] = {}
    for key, value in params.items():
        if not key.startswith(f"{prefix}."):
            continue
        index, _, rest = key[len(prefix) + 1:].partition(".")
        if index.isdigit():
            grouped.setdefault(int(index), {})[rest] = value
    return [grouped[i] for i in sorted(grouped)]


def _scalars(params: dict[str, str], prefix: str) -> list[str]:
    return [item[""] for item in _indexed(params, prefix) if "" in item]


def _filters(params: dict[str, str]) -> dict[str, list[str]]:
    return {f["Name"]: _scalars(f, "Value") for f in _indexed(params, "Filter") if "Name" in f}


def _matches(filters: dict[str, list[str]], attrs: dict[str, str]) -> bool:
    return all(attrs.get(name) in values for name, values in filters.items())


def _spec_tags(params: dict[str, str]) -> dict[str, str]:
    return {
        tag["Key"]: tag.get("Value", "")
        for spec in _indexed(params, "TagSpecification")
        for tag in _indexed(spec, "Tag")
    }


# --- store access -------------------------------------------------------


def _key(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


def _instance(stores: SynthStores, env: str, instance_id: str) -> dict | None:
    return stores.ec2compute.get(env, _key("instance", instance_id))


def _records(stores: SynthStores, env: str, kind: str) -> list[dict]:
    return [v for k, v in stores.ec2compute.items(env).items() if k.startswith(f"{kind}:")]


def _res_tags(stores: SynthStores, env: str, resource_id: str) -> dict[str, str]:
    return stores.tags.get(env, f"ec2:{resource_id}", {})


def _keypair(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.ec2compute.get(env, _key("keypair", name))


# --- wire building: EC2-protocol XML (duplicated from ec2net.py) --------


def _response(action_name: str, inner: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{action_name}Response xmlns="{_EC2_NS}">'
        f"<requestId>{_REQUEST_ID}</requestId>{inner}</{action_name}Response>"
    )
    return Response(xml, media_type="text/xml")


def _tag_set_xml(tags: dict[str, str]) -> str:
    items = "".join(f"<item><key>{escape(k)}</key><value>{escape(v)}</value></item>" for k, v in tags.items())
    return f"<tagSet>{items}</tagSet>"


def _not_found_instance(instance_id: str) -> Response:
    return errors.synth_error("ec2", "InvalidInstanceID.NotFound", f"The instance ID '{instance_id}' does not exist", 400)


# --- security groups on the instance (field-test finding #2) ----------------


def _eni_id(instance_id: str) -> str:
    """A deterministic primary-ENI id per instance -- not separately stored
    (same technique as `_reservation_id`), and reversible so
    ModifyNetworkInterfaceAttribute can map the ENI back to its instance."""
    return f"eni-{instance_id.removeprefix('i-')}"


def _instance_from_eni(eni_id: str) -> str:
    return f"i-{eni_id.removeprefix('eni-')}"


def _instance_groups(stores: SynthStores, env: str, instance: dict) -> list[tuple[str, str]]:
    """(group_id, group_name) for the security groups EXPLICITLY attached at
    RunInstances (an ec2 node's `vpc_security_group_ids`). Empty for an
    instance launched with none -- it inherits the VPC default SG, which the
    provider reads as a computed value (no drift), so we emit no group/ENI
    membership at all and keep the pre-SG DescribeInstances shape byte-for-byte
    (the existing no-SG zero-drift path is unchanged)."""
    groups = []
    for gid in instance.get("security_group_ids") or []:
        sg = stores.ec2net.get(env, f"sg:{gid}")
        groups.append((gid, sg["group_name"] if sg else ""))
    return groups


def _group_set_xml(groups: list[tuple[str, str]]) -> str:
    items = "".join(
        f"<item><groupId>{escape(gid)}</groupId><groupName>{escape(gname)}</groupName></item>"
        for gid, gname in groups
    )
    return f"<groupSet>{items}</groupSet>"


def _network_interface_set_xml(instance: dict, groups: list[tuple[str, str]]) -> str:
    """The instance's PRIMARY network interface (deviceIndex 0) carrying its
    security groups. terraform-provider-aws reads `vpc_security_group_ids` for
    a VPC instance from THIS (the primary NI's Groups), not the top-level
    groupSet -- and its update path errors 'Failed to update
    vpc_security_group_ids ... does not contain a primary network interface'
    when the describe has no primary NI (field-test finding #2). Emitting it
    (with the launched groups) is what makes a re-apply see zero drift and never
    reach that update path. Only emitted when groups are present."""
    instance_id = instance["instance_id"]
    return (
        "<networkInterfaceSet><item>"
        f"<networkInterfaceId>{_eni_id(instance_id)}</networkInterfaceId>"
        f"<subnetId>{instance.get('subnet_id') or ''}</subnetId>"
        f"<vpcId>{instance.get('vpc_id') or ''}</vpcId>"
        "<status>in-use</status>"
        f"<sourceDestCheck>{_INSTANCE_ATTRIBUTE_DEFAULTS['sourceDestCheck']}</sourceDestCheck>"
        f"{_group_set_xml(groups)}"
        "<attachment>"
        f"<attachmentId>eni-attach-{instance_id.removeprefix('i-')}</attachmentId>"
        "<deviceIndex>0</deviceIndex><status>attached</status><deleteOnTermination>true</deleteOnTermination>"
        "</attachment>"
        "</item></networkInterfaceSet>"
    )


def _instance_xml(instance: dict, tags: dict[str, str], groups: list[tuple[str, str]]) -> str:
    parts = [
        f"<instanceId>{instance['instance_id']}</instanceId>",
        f"<imageId>{escape(instance['image_id'])}</imageId>",
        f"<instanceState><code>{_STATE_CODES[instance['state_name']]}</code><name>{instance['state_name']}</name></instanceState>",
        "<privateDnsName></privateDnsName><dnsName></dnsName><reason></reason>",
    ]
    if instance.get("key_name"):
        parts.append(f"<keyName>{escape(instance['key_name'])}</keyName>")
    parts.append(f"<instanceType>{instance['instance_type']}</instanceType>")
    parts.append(f"<launchTime>{instance['launch_time']}</launchTime>")
    parts.append(f"<placement><availabilityZone>{escape(instance['availability_zone'])}</availabilityZone></placement>")
    if instance.get("subnet_id"):
        parts.append(f"<subnetId>{instance['subnet_id']}</subnetId>")
    if instance.get("vpc_id"):
        parts.append(f"<vpcId>{instance['vpc_id']}</vpcId>")
    if instance.get("private_ip"):
        parts.append(f"<privateIpAddress>{instance['private_ip']}</privateIpAddress>")
    if instance.get("public_ip"):
        parts.append(f"<ipAddress>{instance['public_ip']}</ipAddress>")
    if groups:
        # The instance-level groupSet AND the primary-ENI membership below --
        # the provider reads the ENI's for a VPC instance, but real AWS carries
        # both, and emitting neither is exactly what stranded re-apply (finding #2).
        parts.append(_group_set_xml(groups))
    # sourceDestCheck sits on the Instance shape ITSELF too (not just the
    # separate DescribeInstanceAttribute call) -- found empirically (V3d):
    # omitting it here let the Go SDK default it to `false` on create-time
    # hydration while DescribeInstanceAttribute's own default answered
    # `true`, a real drift on every plan. Keeping both in lockstep with
    # `_INSTANCE_ATTRIBUTE_DEFAULTS["sourceDestCheck"]`.
    parts.append(f"<sourceDestCheck>{_INSTANCE_ATTRIBUTE_DEFAULTS['sourceDestCheck']}</sourceDestCheck>")
    parts.append("<rootDeviceType>ebs</rootDeviceType><rootDeviceName>/dev/sda1</rootDeviceName>")
    volume = instance["root_volume"]
    parts.append(
        "<blockDeviceMapping><item><deviceName>/dev/sda1</deviceName>"
        f"<ebs><volumeId>{volume['volume_id']}</volumeId><status>attached</status>"
        "<deleteOnTermination>true</deleteOnTermination></ebs></item></blockDeviceMapping>"
    )
    if groups:
        parts.append(_network_interface_set_xml(instance, groups))
    if instance.get("state_reason"):
        reason = instance["state_reason"]
        parts.append(f"<stateReason><code>{escape(reason['code'])}</code><message>{escape(reason['message'])}</message></stateReason>")
    parts.append(_tag_set_xml(tags))
    return "".join(parts)


def _reservation_id(instance_id: str) -> str:
    # Deterministic, not separately stored: v1 never groups multiple
    # instances under one reservation (MinCount=MaxCount=1), so deriving it
    # from the instance id is equivalent to minting+persisting one.
    return f"r-{instance_id.removeprefix('i-')}"


def _reservation_inner_xml(stores: SynthStores, env: str, instance: dict) -> str:
    """The `Reservation` shape's members -- used TWO different ways on the
    wire (verified against botocore's own operation models): RunInstances'
    response IS a bare Reservation (reservationId/ownerId/groupSet/
    instancesSet sit directly under `<RunInstancesResponse>`), while
    DescribeInstances wraps N of these in `<reservationSet><item>...`. v1
    never groups multiple instances under one reservation (MinCount=MaxCount
    =1), so this always covers exactly one instance."""
    # The RESERVATION-level groupSet stays empty (real AWS's shape for a VPC
    # instance -- group membership rides the instance-level groupSet + the
    # primary ENI, both built inside `_instance_xml` from these `groups`).
    groups = _instance_groups(stores, env, instance)
    return (
        f"<reservationId>{_reservation_id(instance['instance_id'])}</reservationId>"
        f"<ownerId>{ACCOUNT}</ownerId><groupSet/>"
        f"<instancesSet><item>{_instance_xml(instance, _res_tags(stores, env, instance['instance_id']), groups)}</item></instancesSet>"
    )


def _reservation_set_xml(stores: SynthStores, env: str, instances: list[dict]) -> str:
    items = "".join(f"<item>{_reservation_inner_xml(stores, env, i)}</item>" for i in instances)
    return f"<reservationSet>{items}</reservationSet>"


def _state_change_xml(instance_id: str, previous: str, current: str) -> str:
    return (
        f"<item><instanceId>{instance_id}</instanceId>"
        f"<currentState><code>{_STATE_CODES[current]}</code><name>{current}</name></currentState>"
        f"<previousState><code>{_STATE_CODES[previous]}</code><name>{previous}</name></previousState>"
        "</item>"
    )


def _volume_xml(volume: dict) -> str:
    return (
        f"<volumeId>{volume['volume_id']}</volumeId><size>{volume['size']}</size>"
        f"<availabilityZone>{escape(volume['availability_zone'])}</availabilityZone>"
        f"<status>in-use</status><createTime>{volume['create_time']}</createTime>"
        "<attachmentSet><item>"
        f"<volumeId>{volume['volume_id']}</volumeId><instanceId>{volume['instance_id']}</instanceId>"
        f"<device>{volume['device']}</device><status>attached</status>"
        f"<attachTime>{volume['create_time']}</attachTime><deleteOnTermination>true</deleteOnTermination>"
        "</item></attachmentSet>"
        '<volumeType>gp3</volumeType><iops>3000</iops><encrypted>false</encrypted>'
    )


# --- background completion: the async state machine (the "never block"
# requirement -- every handler below returns a transitional state
# immediately, a daemon thread finishes the real work) -------------------


def _update_instance(stores: SynthStores, env: str, instance_id: str, **fields: object) -> None:
    incoming_state = fields.get("state_name")

    def mutate(instance: dict | None) -> dict | object:
        if instance is None:  # already terminated + swept -- nothing to update
            return NO_CHANGE
        if instance["state_name"] in _TERMINAL_STATES and incoming_state not in (None, *_TERMINAL_STATES):
            # A late boot/stop/start completion racing a terminate that
            # already won -- see the module's "resurrection race" note above.
            return NO_CHANGE
        instance = dict(instance)
        instance.update(fields)
        return instance

    stores.ec2compute.update(env, _key("instance", instance_id), mutate)


def _finish_boot(
    stores: SynthStores, env: str, instance_id: str, name: str, vm_config, ssh_pubkey, user_data, nebula, vm: InstanceVm,
    env_vars: dict[str, str] | None = None,
) -> None:
    # Deliberately broad: this runs on a daemon thread with no caller to
    # propagate an exception to. Without this catch, a boot failure would
    # kill the thread silently and strand the instance `pending` forever --
    # exactly the "silent hang" the brief forbids. Any failure instead
    # becomes a real, provider-visible terminal state.
    try:
        ip = vm.boot(name, vm_config, hostname=instance_id, ssh_pubkey=ssh_pubkey, user_data=user_data, nebula=nebula, env_vars=env_vars)
    except Exception as exc:
        log.warning("boot failed for instance %s (%s): %s", instance_id, name, exc)
        _update_instance(
            stores, env, instance_id, state_name="terminated",
            state_reason={"code": "Server.InternalError", "message": str(exc)},
            terminated_at=time.monotonic(),
        )
        return
    _update_instance(stores, env, instance_id, state_name="running", private_ip=ip, public_ip=ip)


def _finish_terminate(stores: SynthStores, env: str, instance_id: str, name: str, vm: InstanceVm) -> None:
    try:
        vm.delete(name)
    except Exception as exc:
        # VM delete honesty (release finding #4): a failed delete must NOT
        # be reported as a clean `terminated` -- a caller (tofu's own
        # destroy waiter, a human) polling DescribeInstances deserves the
        # truth, not a record claiming the VM is gone when it might not be.
        # The record stays `shutting-down` with the failure recorded and
        # `delete_failed` set, so `_retry_failed_deletes` (every
        # Describe-driven pass, below) tries again -- and the startup
        # reaper (`reap_orphaned_vms`) is the backstop for a VM that
        # outlives every record of it entirely (e.g. this env's store gets
        # destroyed before a retry ever lands).
        log.error("VM delete failed for instance %s (%s), will retry on next describe: %s", instance_id, name, exc)
        _update_instance(
            stores, env, instance_id,
            state_reason={"code": "Server.InternalError", "message": f"VM delete failed, retrying: {exc}"},
            delete_failed=True,
        )
        return
    _update_instance(
        stores, env, instance_id, state_name="terminated", terminated_at=time.monotonic(),
        state_reason=None, delete_failed=False,
    )
    _maybe_stop_lighthouse(stores, env)


def _maybe_stop_lighthouse(stores: SynthStores, env: str) -> None:
    """R3's "last VM leaves" half of `LighthouseManager`'s lifecycle (the
    "first VM joins" half is `InstanceVm._activate_nebula`, co-located with
    the VM-side activation it exists to make truthful). A true no-op
    whenever this env has no lighthouse pidfile -- every unit test's fresh
    `tmp_path`, so this never touches a real process outside the real
    integration test."""
    still_meshed = any(
        i.get("vpc_id") and i["state_name"] not in _TERMINAL_STATES
        for i in _records(stores, env, "instance")
    )
    if not still_meshed:
        LighthouseManager().ensure_stopped(stores.root, env)


def _claim_delete_retry(instance: dict | None) -> dict | object:
    """The mutator `_retry_failed_deletes` uses to atomically claim ONE
    retry attempt for an instance stuck in `shutting-down` with a prior
    delete failure -- flips `delete_failed` off BEFORE spawning the retry,
    so a second concurrent Describe-driven pass sees it already claimed and
    skips it (no two threads calling `vm.delete` on the same VM at once).
    `_finish_terminate` re-sets `delete_failed` if THIS attempt also fails,
    making it eligible again for the next pass."""
    if instance is None or instance["state_name"] != "shutting-down" or not instance.get("delete_failed"):
        return NO_CHANGE
    instance = dict(instance)
    instance["delete_failed"] = False
    return instance


def _retry_failed_deletes(stores: SynthStores, env: str, vm: InstanceVm) -> None:
    for instance in _records(stores, env, "instance"):
        instance_id = instance["instance_id"]
        claimed = stores.ec2compute.update(env, _key("instance", instance_id), _claim_delete_retry)
        if claimed is NO_CHANGE:
            continue
        _spawn(_finish_terminate, stores, env, instance_id, vm_name(env, instance_id), vm)


def _finish_stop(stores: SynthStores, env: str, instance_id: str, name: str, vm: InstanceVm) -> None:
    try:
        vm.stop(name)
    except Exception as exc:
        log.warning("VM stop failed for instance %s (%s): %s", instance_id, name, exc)
    _update_instance(stores, env, instance_id, state_name="stopped", private_ip=None, public_ip=None)


def _finish_start(stores: SynthStores, env: str, instance_id: str, name: str, vm: InstanceVm) -> None:
    try:
        ip = vm.start(name)
    except Exception as exc:
        log.warning("VM start failed for instance %s (%s): %s", instance_id, name, exc)
        _update_instance(
            stores, env, instance_id, state_name="stopped",
            state_reason={"code": "Server.InternalError", "message": str(exc)},
        )
        return
    _update_instance(stores, env, instance_id, state_name="running", private_ip=ip, public_ip=ip)


def _spawn(target: Callable[..., None], *args: object) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


# --- Instances ------------------------------------------------------------


def _sweep_terminated(stores: SynthStores, env: str, now: float) -> None:
    for instance in _records(stores, env, "instance"):
        terminated_at = instance.get("terminated_at")
        if terminated_at is not None and now - terminated_at > _TERMINATED_SWEEP_SECONDS:
            stores.ec2compute.delete(env, _key("instance", instance["instance_id"]))


def mark_instance_terminated(stores: SynthStores, env: str, instance_id: str, reason: str) -> None:
    """Public seam for the reality sweep (`reconcile/drift.py`): this
    instance's Lima VM is GONE (deleted outside odin), so the record says
    `terminated` with `reason` -- the SAME terminal shape `_finish_boot`'s
    failure path and `_finish_terminate` already write, through the same
    `_update_instance` guard (a terminate already winning the race is never
    pulled back out).

    THIS is what makes "re-Apply to recreate" true rather than a comforting
    lie (NORTHSTAR directive 5): terraform-provider-aws's own Read treats a
    `terminated` instance as gone and drops it from state, so the next
    `tofu apply` plans a create and the VM genuinely comes back. A record
    still claiming `running` answers DescribeInstances with a VM that doesn't
    exist, tofu plans nothing, and the resource never returns.

    `drifted` is the flag that keeps World honest at the same time: a plain
    `terminated` record is EXCLUDED from the projection (reconcile/
    tf_status.py -- the v0.5.2 phantom-EC2 fix), while a drifted one projects
    `crashed` + this reason instead of silently vanishing off the canvas.
    `terminated_at` is set so the normal lazy sweep (`_sweep_terminated`)
    still reclaims the record on the recovery apply's own describes.

    `Client.UserInitiatedShutdown` is a REAL EC2 state-reason code, and the
    accurate one: something outside odin (a human, another tool) did delete
    the VM -- never an invented code."""
    _update_instance(
        stores, env, instance_id, state_name="terminated",
        state_reason={"code": "Client.UserInitiatedShutdown", "message": reason},
        terminated_at=time.monotonic(), drifted=True,
    )


def _instance_firewall(stores: SynthStores, env: str, vpc: dict, security_group_ids: list[str]) -> FirewallRules | None:
    """W2.6 piece 1: the firewall an instance's VM actually gets -- the UNION
    of its ASSIGNED security groups (AWS's own semantics: SG rules are
    permissive-only, so a resource's effective rule set is every attached
    group's rules combined -- `fabric.nebula.union_firewalls`).

    Falls back to the containing VPC's DEFAULT security group only when the
    instance was launched with none, which is exactly what real AWS does in
    that case. Before this, EVERY instance got the VPC default SG's firewall
    even when the canvas explicitly assigned it others (v0.5.4 made
    `SecurityGroupIds` reflect in DescribeInstances for zero-drift, but the
    firewall was still compiled from the default group) -- so a drawn
    per-instance SG was decorative on the wire. An assigned group whose
    record is missing/uncompiled contributes nothing rather than silently
    widening the instance to the VPC default."""
    assigned = [
        f for gid in security_group_ids
        if (f := ec2net.compiled_firewall(stores, env, gid)) is not None
    ]
    if assigned:
        return union_firewalls(assigned)
    return ec2net.compiled_firewall(stores, env, vpc["default_sg_id"])


def _run_instances(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    subnet_id = params.get("SubnetId", "")
    subnet = stores.ec2net.get(env, f"subnet:{subnet_id}") if subnet_id else None
    if subnet_id and subnet is None:
        return errors.synth_error("ec2", "InvalidSubnetID.NotFound", f"The subnet ID '{subnet_id}' does not exist", 400)
    vpc = stores.ec2net.get(env, f"vpc:{subnet['vpc_id']}") if subnet else None

    key_name = params.get("KeyName") or None
    keypair = _keypair(stores, env, key_name) if key_name else None
    if key_name and keypair is None:
        return errors.synth_error("ec2", "InvalidKeyPair.NotFound", f"The key pair '{key_name}' does not exist", 400)

    instance_type = params.get("InstanceType") or _DEFAULT_INSTANCE_TYPE
    vm_config = get_instance_type(instance_type)
    instance_id = _mint("i")
    az = subnet["availability_zone"] if subnet else f"{REGION}a"
    launch_time = _now_iso()
    user_data_b64 = params.get("UserData", "")
    # field-test finding #2: the security groups the canvas' `vpc_security_group_ids`
    # drives (RunInstances `SecurityGroupId.N`). Stored so DescribeInstances can
    # reflect them on the instance's primary ENI -> zero drift on re-apply. An
    # instance launched with none keeps `[]` and inherits the VPC default SG for
    # its Nebula firewall exactly as before.
    security_group_ids = _scalars(params, "SecurityGroupId")

    instance = {
        "instance_id": instance_id,
        "image_id": params.get("ImageId") or _DEFAULT_AMI,
        "instance_type": instance_type,
        "key_name": key_name,
        "subnet_id": subnet_id or None,
        "vpc_id": vpc["vpc_id"] if vpc else None,
        "security_group_ids": security_group_ids,
        "state_name": "pending",
        "state_reason": None,
        "private_ip": None,
        "public_ip": None,
        "availability_zone": az,
        "launch_time": launch_time,
        "root_volume": {
            "volume_id": _mint("vol"), "instance_id": instance_id,
            "size": _gib(vm_config.disk), "device": "/dev/sda1",
            "availability_zone": az, "create_time": launch_time,
        },
        "terminated_at": None,
        "user_data_b64": user_data_b64,
    }
    stores.ec2compute.set(env, _key("instance", instance_id), instance)
    tags = _spec_tags(params)
    stores.tags.set(env, f"ec2:{instance_id}", tags)

    # Render the `pending` response BEFORE spawning the boot thread: the
    # store hands back the SAME dict object it was given (JsonStore keeps
    # references, not copies), so `instance` here and the record `_finish_boot`
    # later mutates via `_update_instance` are literally the same object --
    # rendering after `_spawn` risked reading an already-`running` instance
    # back on a fast (fake-VM) boot, a real race, not a test artifact.
    response = _response("RunInstances", _reservation_inner_xml(stores, env, instance))

    nebula = NebulaJoin(
        root=stores.root, env=env, host_id=instance_id,
        firewall=_instance_firewall(stores, env, vpc, security_group_ids),
        # W2.6: the instance's own SG ids become its nebula cert GROUPS, which
        # is what makes another node's "allow 5432 from sg-web" rule (an AWS
        # UserIdGroupPairs rule -- `sg_rules_to_firewall` compiles it to
        # `group: sg-...`) actually match this instance on the wire.
        groups=tuple(security_group_ids),
    ) if vpc else None
    ssh_pubkey = keypair.get("public_key") if keypair else None
    user_data = base64.b64decode(user_data_b64).decode("utf-8", "replace") if user_data_b64 else None
    name = vm_name(env, instance_id)
    # Workload identity (fix-wave 2b finding #2): an instance carrying the
    # `odin:node` tag (agent/hcl.py stamps it on every canvas-node-backed
    # resource) boots with its keystore credentials + the gateway endpoint
    # baked into cloud-init -- the VM can call the gateway AS ITSELF. These
    # env vars go ONLY into the VM, never into any AWS API response body.
    label = tags.get("odin:node")
    injectable = keystore is not None and gateway_port is not None and label
    env_vars = workload_env(keystore, env, label, gateway_port) if injectable else None
    _spawn(_finish_boot, stores, env, instance_id, name, vm_config, ssh_pubkey, user_data, nebula, vm, env_vars)

    return response


def _describe_instances(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    _sweep_terminated(stores, env, now)
    _retry_failed_deletes(stores, env, vm)
    instance_ids = _scalars(params, "InstanceId")
    filters = _filters(params)
    instances = _records(stores, env, "instance")
    missing = [i for i in instance_ids if i not in {r["instance_id"] for r in instances}]
    if missing:
        return _not_found_instance(missing[0])
    selected = [
        r for r in instances
        if (not instance_ids or r["instance_id"] in instance_ids)
        and _matches(filters, {
            "instance-id": r["instance_id"], "vpc-id": r.get("vpc_id") or "",
            "subnet-id": r.get("subnet_id") or "", "instance-state-name": r["state_name"],
        })
    ]
    return _response("DescribeInstances", _reservation_set_xml(stores, env, selected))


def _terminate_instances(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    ids = _scalars(params, "InstanceId")
    changes = []
    for instance_id in ids:
        instance = _instance(stores, env, instance_id)
        if instance is None:
            return _not_found_instance(instance_id)
        previous = instance["state_name"]
        if previous == "terminated":
            changes.append((instance_id, previous, previous))
            continue
        _update_instance(stores, env, instance_id, state_name="shutting-down")
        _spawn(_finish_terminate, stores, env, instance_id, vm_name(env, instance_id), vm)
        changes.append((instance_id, previous, "shutting-down"))
    items = "".join(_state_change_xml(i, p, c) for i, p, c in changes)
    return _response("TerminateInstances", f"<instancesSet>{items}</instancesSet>")


def _stop_instances(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    ids = _scalars(params, "InstanceId")
    changes = []
    for instance_id in ids:
        instance = _instance(stores, env, instance_id)
        if instance is None:
            return _not_found_instance(instance_id)
        previous = instance["state_name"]
        _update_instance(stores, env, instance_id, state_name="stopping")
        _spawn(_finish_stop, stores, env, instance_id, vm_name(env, instance_id), vm)
        changes.append((instance_id, previous, "stopping"))
    items = "".join(_state_change_xml(i, p, c) for i, p, c in changes)
    return _response("StopInstances", f"<instancesSet>{items}</instancesSet>")


def _start_instances(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    ids = _scalars(params, "InstanceId")
    changes = []
    for instance_id in ids:
        instance = _instance(stores, env, instance_id)
        if instance is None:
            return _not_found_instance(instance_id)
        previous = instance["state_name"]
        _update_instance(stores, env, instance_id, state_name="pending")
        _spawn(_finish_start, stores, env, instance_id, vm_name(env, instance_id), vm)
        changes.append((instance_id, previous, "pending"))
    items = "".join(_state_change_xml(i, p, c) for i, p, c in changes)
    return _response("StartInstances", f"<instancesSet>{items}</instancesSet>")


# --- in-place attribute edits (field-test finding #2) ----------------------


def _modify_instance_attribute(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    """A real, tolerant success (was an InvalidAction 400 the provider merely
    tolerated). The provider calls this during destroy (DisableApiTermination
    =false) and CAN carry an in-place security-group change (`GroupId.N`); the
    latter updates the stored set so the next describe -- and any re-apply --
    reflects it with no drift."""
    instance_id = params.get("InstanceId", "")
    if _instance(stores, env, instance_id) is None:
        return _not_found_instance(instance_id)
    group_ids = _scalars(params, "GroupId")
    if group_ids:
        _update_instance(stores, env, instance_id, security_group_ids=group_ids)
    return _response("ModifyInstanceAttribute", "<return>true</return>")


def _modify_network_interface_attribute(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    """The call terraform-provider-aws actually makes to change a VPC
    instance's `vpc_security_group_ids` (on the primary ENI). Maps the ENI id
    back to its instance and updates the stored set, so a canvas security-group
    change re-applies cleanly instead of stranding on the next plan."""
    eni_id = params.get("NetworkInterfaceId", "")
    instance_id = _instance_from_eni(eni_id)
    group_ids = _scalars(params, "SecurityGroupId")
    if group_ids and _instance(stores, env, instance_id) is not None:
        _update_instance(stores, env, instance_id, security_group_ids=group_ids)
    return _response("ModifyNetworkInterfaceAttribute", "<return>true</return>")


# --- read-only hydration describes (research §2b) --------------------------


def _describe_instance_types(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    names = _scalars(params, "InstanceType") or list(INSTANCE_TYPES.keys())
    items = []
    for name in names:
        cfg = INSTANCE_TYPES.get(name)
        if cfg is None:
            continue
        items.append(
            f"<item><instanceType>{name}</instanceType>"
            f"<vCpuInfo><defaultVCpus>{cfg.cpus}</defaultVCpus></vCpuInfo>"
            f"<memoryInfo><sizeInMiB>{_gib(cfg.memory) * 1024}</sizeInMiB></memoryInfo>"
            "<supportedUsageClasses><item>on-demand</item></supportedUsageClasses></item>"
        )
    return _response("DescribeInstanceTypes", f"<instanceTypeSet>{''.join(items)}</instanceTypeSet>")


def _describe_instance_attribute(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    instance_id = params.get("InstanceId", "")
    instance = _instance(stores, env, instance_id)
    if instance is None:
        return _not_found_instance(instance_id)
    attribute = params.get("Attribute", "")
    if attribute == "userData":
        value = instance.get("user_data_b64", "")
        # An empty `<value></value>` (rather than omitting `<value>`
        # entirely) reads back through the TF AWS provider's Go SDK as a
        # DIFFERENT thing than "no user data" -- found empirically (V3d):
        # it caused a real plan-time drift on every instance with no
        # `user_data` set (the common case). Real AWS's own shape for
        # "attribute not set" omits the child element outright.
        inner = f"<value>{escape(value)}</value>" if value else ""
        return _response("DescribeInstanceAttribute", f"<instanceId>{instance_id}</instanceId><userData>{inner}</userData>")
    value = _INSTANCE_ATTRIBUTE_DEFAULTS.get(attribute, "false")
    return _response(
        "DescribeInstanceAttribute",
        f"<instanceId>{instance_id}</instanceId><{attribute}><value>{value}</value></{attribute}>",
    )


def _describe_instance_credit_specifications(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    instance_ids = _scalars(params, "InstanceId")
    instances = _records(stores, env, "instance")
    selected = [i for i in instances if not instance_ids or i["instance_id"] in instance_ids]
    items = "".join(
        f"<item><instanceId>{i['instance_id']}</instanceId><cpuCredits>standard</cpuCredits></item>" for i in selected
    )
    return _response("DescribeInstanceCreditSpecifications", f"<instanceCreditSpecificationSet>{items}</instanceCreditSpecificationSet>")


def _describe_volumes(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    volume_ids = _scalars(params, "VolumeId")
    filters = _filters(params)
    volumes = [i["root_volume"] for i in _records(stores, env, "instance")]
    selected = [
        v for v in volumes
        if (not volume_ids or v["volume_id"] in volume_ids)
        and _matches(filters, {"volume-id": v["volume_id"], "attachment.instance-id": v["instance_id"]})
    ]
    items = "".join(f"<item>{_volume_xml(v)}</item>" for v in selected)
    return _response("DescribeVolumes", f"<volumeSet>{items}</volumeSet>")


# --- Key pairs --------------------------------------------------------------


def _generate_keypair() -> tuple[str, str]:
    """A REAL RSA keypair via `ssh-keygen` (no mock-only modes) -- the same
    shelling-out style `fabric/nebula.py` uses for `nebula-cert`."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "key"
        subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "2048", "-m", "PEM", "-N", "", "-f", str(path)],
            check=True, capture_output=True, text=True,
        )
        return path.with_suffix(".pub").read_text().strip(), path.read_text()


def _store_keypair(stores: SynthStores, env: str, params: dict[str, str], name: str, public_key: str) -> dict:
    key_pair_id = _mint("key")
    fingerprint = hashlib.md5(public_key.encode()).hexdigest()
    record = {"key_name": name, "key_pair_id": key_pair_id, "fingerprint": fingerprint, "public_key": public_key}
    stores.ec2compute.set(env, _key("keypair", name), record)
    stores.tags.set(env, f"ec2:{key_pair_id}", _spec_tags(params))
    return record


def _import_key_pair(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    name = params.get("KeyName", "")
    if _keypair(stores, env, name) is not None:
        return errors.synth_error("ec2", "InvalidKeyPair.Duplicate", f"The keypair '{name}' already exists.", 400)
    public_key = base64.b64decode(params.get("PublicKeyMaterial", "")).decode("utf-8", "replace").strip()
    record = _store_keypair(stores, env, params, name, public_key)
    tags = _res_tags(stores, env, record["key_pair_id"])
    inner = (
        f"<keyName>{escape(name)}</keyName><keyFingerprint>{record['fingerprint']}</keyFingerprint>"
        f"<keyPairId>{record['key_pair_id']}</keyPairId>" + _tag_set_xml(tags)
    )
    return _response("ImportKeyPair", inner)


def _create_key_pair(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    name = params.get("KeyName", "")
    if _keypair(stores, env, name) is not None:
        return errors.synth_error("ec2", "InvalidKeyPair.Duplicate", f"The keypair '{name}' already exists.", 400)
    public_key, private_key = _generate_keypair()
    record = _store_keypair(stores, env, params, name, public_key)
    tags = _res_tags(stores, env, record["key_pair_id"])
    inner = (
        f"<keyName>{escape(name)}</keyName><keyFingerprint>{record['fingerprint']}</keyFingerprint>"
        f"<keyMaterial>{escape(private_key)}</keyMaterial><keyPairId>{record['key_pair_id']}</keyPairId>" + _tag_set_xml(tags)
    )
    return _response("CreateKeyPair", inner)


def _describe_key_pairs(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    names = _scalars(params, "KeyName")
    keypairs = _records(stores, env, "keypair")
    missing = [n for n in names if n not in {k["key_name"] for k in keypairs}]
    if missing:
        return errors.synth_error("ec2", "InvalidKeyPair.NotFound", f"The key pair '{missing[0]}' does not exist", 400)
    selected = [k for k in keypairs if not names or k["key_name"] in names]
    items = "".join(
        f"<item><keyName>{escape(k['key_name'])}</keyName><keyFingerprint>{k['fingerprint']}</keyFingerprint>"
        f"<keyPairId>{k['key_pair_id']}</keyPairId>" + _tag_set_xml(_res_tags(stores, env, k["key_pair_id"])) + "</item>"
        for k in selected
    )
    return _response("DescribeKeyPairs", f"<keySet>{items}</keySet>")


def _delete_key_pair(params: dict[str, str], env: str, stores: SynthStores, now: float, vm: InstanceVm, keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    # Idempotent-succeed even if the key is already gone -- real AWS's own
    # DeleteKeyPair semantics, and one fewer branch than a NotFound check.
    name = params.get("KeyName", "")
    keypair = _keypair(stores, env, name)
    if keypair is not None:
        stores.tags.set(env, f"ec2:{keypair['key_pair_id']}", {})
        stores.ec2compute.delete(env, _key("keypair", name))
    return _response("DeleteKeyPair", "<return>true</return>")


# --- startup reaper (release finding #4) ------------------------------------

# Anything else -- including unset, `1`, or a typo -- leaves the reaper ON.
# A safety net you disabled by mistyping the value is not a safety net.
_REAPER_OFF_VALUES = ("0", "false", "no", "off")


def _reaper_enabled() -> bool:
    """`ODIN_REAP_EC2_VMS=0` (or `false`/`no`/`off`) turns the startup reaper
    off; it is ON by default.

    Read HERE rather than at the `create_app` call site so it holds for every
    caller -- the `odin` CLI included, which is the whole point: before this,
    `create_app(reap_ec2_vms=False)` was the only seam, so running a second
    isolated odin on one Mac meant bypassing the CLI with a factory wrapper
    (v0.7.0 field test, U7). A second instance has its OWN store, which knows
    nothing about the first instance's instances, so every one of them looks
    orphaned to it.

    What you give up by setting it: the crash-recovery backstop. If odin dies
    between `vm.delete` succeeding and the store update landing, the leftover
    Lima VM stays on disk burning memory and disk until you
    `limactl delete` it yourself. That is the ONLY thing the reaper does --
    it never touches a VM any env's store still expects, and never one
    outside this module's own `odin-ec2-` naming."""
    return os.environ.get("ODIN_REAP_EC2_VMS", "1").strip().lower() not in _REAPER_OFF_VALUES


def reap_orphaned_vms(root: Path, envs: list[str], vm: InstanceVm | None = None) -> list[str]:
    """A one-shot startup safety net for a VM that's on disk with NO
    matching store record anywhere -- e.g. a crash between `vm.delete`
    succeeding and the store update landing, or any other drift between
    "the store thinks this instance is gone" and "the VM is actually gone".
    `limactl list --json` (via `InstanceVm.list_names`) -> delete any VM
    shaped like this module's own `vm_name()` convention whose EXACT name
    isn't one any of `envs`' ec2compute stores currently expects.

    Exact-name discipline throughout: the "expected" set is built by
    calling the SAME `vm_name(env, instance_id)` every real creation uses,
    never a prefix/wildcard match on the delete side -- a user's own Lima
    VM (e.g. `veronica`) or another odin subsystem's is never even a
    candidate, let alone touched. Returns the names of VMs it deleted.

    `ODIN_REAP_EC2_VMS=0` skips the whole pass -- see `_reaper_enabled` for
    what that buys and what it costs. The check is first, so a disabled
    reaper does not so much as enumerate the machine's VMs."""
    if not _reaper_enabled():
        log.info("startup reaper: disabled by ODIN_REAP_EC2_VMS -- leaving every EC2 VM on this machine alone")
        return []
    vm = vm or InstanceVm()
    stores = SynthStores(root)
    expected = {
        vm_name(env, record["instance_id"])
        for env in envs
        for key, record in stores.ec2compute.items(env).items()
        if key.startswith("instance:")
    }
    reaped = []
    for name in vm.list_names():
        if name.startswith(_VM_NAME_PREFIX) and name not in expected:
            log.warning("startup reaper: deleting orphaned EC2 VM %r (no matching store record)", name)
            vm.delete(name)
            reaped.append(name)
    return reaped


# --- dispatch ----------------------------------------------------------------


_Handler = Callable[[dict[str, str], str, SynthStores, float, InstanceVm, KeyStore | None, int | None], Response]

_HANDLERS: dict[str, _Handler] = {
    "RunInstances": _run_instances,
    "DescribeInstances": _describe_instances,
    "TerminateInstances": _terminate_instances,
    "StopInstances": _stop_instances,
    "StartInstances": _start_instances,
    "ModifyInstanceAttribute": _modify_instance_attribute,
    "ModifyNetworkInterfaceAttribute": _modify_network_interface_attribute,
    "DescribeInstanceTypes": _describe_instance_types,
    "DescribeInstanceAttribute": _describe_instance_attribute,
    "DescribeInstanceCreditSpecifications": _describe_instance_credit_specifications,
    "DescribeVolumes": _describe_volumes,
    "CreateKeyPair": _create_key_pair,
    "ImportKeyPair": _import_key_pair,
    "DescribeKeyPairs": _describe_key_pairs,
    "DeleteKeyPair": _delete_key_pair,
}


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    vm: InstanceVm | None = None, keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response | None:
    """The gateway's whole `ec2:*` answer -- this module's own compute
    actions, or a fall-through to `ec2net.pure_answer` (VPC/Subnet/SG/Tags,
    and the InvalidAction envelope for anything neither module knows).
    `vm` is the injectable `InstanceVm` (or a test's fake stand-in with the
    same `boot`/`stop`/`start`/`delete` shape); production callers
    (gateway/synth.py) never pass one, so a real VM manager is used.
    `keystore`/`gateway_port` (threaded from synth.pure_answer, ecr.py's
    `backing_port` precedent) let `_run_instances` bake an `odin:node`-tagged
    instance's own gateway credentials into its cloud-init."""
    op = action.removeprefix("ec2:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return ec2net.pure_answer(action, resource, env, body, stores, now)
    return handler(_params(body), env, stores, now, vm or InstanceVm(), keystore, gateway_port)
