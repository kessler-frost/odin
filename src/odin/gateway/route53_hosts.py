"""The wiring: applied route53 records -> real name resolution on real VMs.

This module is the ONE piece that was missing while every other piece was
built. r53-gw's store held the records, `compute/hosts.py` could turn them into
per-consumer addresses, and `InstanceVm.push_hosts` could land them in a
running guest -- and nothing connected the three, so a drawn record resolved
nowhere. It is deliberately small, because everything hard already lives on one
side or the other of it.

A PURE READ-THEN-APPLY, and that is a constraint rather than a description.
Every input is a store read; nothing here infers, derives or caches. The moment
this module starts deciding something for itself it becomes a second source of
truth about what a name should resolve to, and the first symptom would be the
canvas and the guest disagreeing with no way to tell which is right.

WHY IT LIVES IN `gateway/` AND NOT `compute/`: it needs `SynthStores`, and
`compute/` must not import `gateway/` -- the import graph runs one way. Keeping
the resolver (`compute/hosts.py`) free of the store is what makes it drivable
through every ambiguous case in a unit test; keeping the store read here is
what pays for that.

RUNS ON APPLY, beside `ensure_instance_mesh`, for the same reason that one
does: records are TF-owned, so an edited record only reaches the gateway
through an Apply. `push_hosts` makes an unchanged record set cost one local
file read, so this is cheap enough to run for every instance every time.
"""
from __future__ import annotations

import logging
from pathlib import Path

from odin.compute.hosts import HostsPlan, vm_hosts
from odin.compute.instances import (
    HOSTS_NO_MESH,
    HOSTS_UNRESOLVABLE,
    HostsVerdict,
    InstanceVm,
    vm_name,
)
from odin.fabric.nebula import NebulaManager
from odin.gateway.stores import NO_CHANGE, SynthStores

log = logging.getLogger("odin.gateway.route53_hosts")

# The subnet bucket `fabric/nebula.py` allocates instance overlay addresses
# from. Named here rather than reached for inline so a rename upstream is one
# grep, not a silent empty map -- an empty overlay is indistinguishable from
# "this env has no mesh", which is a real state this module reports.
_HOSTS_SUBNET = "hosts"


def records(stores: SynthStores, env: str) -> list[dict]:
    """Every route53 record in the env, across all zones.

    Records are stored per ZONE (`rrset:{zone_id}` holds a list), so this
    flattens. Zones are not distinguished because /etc/hosts has no notion of
    one -- a name either resolves on this machine or it does not."""
    return [
        record
        for key, zone_records in stores.route53ctl.items(env).items()
        if key.startswith("rrset:")
        for record in (zone_records or [])
    ]


def instances(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.ec2compute.items(env).items() if k.startswith("instance:")]


def overlay_addresses(root: Path, env: str) -> dict[str, str]:
    """host_id -> its sticky Nebula overlay address, or {} when this env has
    no mesh.

    `{}` is a MEANINGFUL answer, not a failure: it is what makes every record
    report `no_mesh` instead of silently resolving to a `private_ip` a VM
    cannot reach. `compute/hosts.py::vm_hosts` treats it that way."""
    network = NebulaManager(Path(root) / env / "nebula").load_overlay()
    subnet = network.subnets.get(_HOSTS_SUBNET) if network else None
    return dict(subnet.assignments) if subnet else {}


def _running(instance: dict) -> bool:
    return instance.get("state_name") == "running"


async def ensure_instance_hosts(
    stores: SynthStores, env: str, vm: InstanceVm | None = None,
) -> dict[str, HostsVerdict]:
    """Make every RUNNING instance's /etc/hosts match this env's route53
    records. Returns `{vm name: verdict}` for projection into World.

    `ensure_instance_mesh`'s twin, and it holds the same two contracts:

    NEVER RAISES. A DNS-wiring failure must not fail an Apply on its own --
    `push_hosts` already turns every failure into a `failed` action, and the
    verdict carries the reason. But `failed` is a real answer a caller acts on,
    not a shrug.

    NO CHURN. An instance whose record set is unchanged costs one local file
    comparison: no `limactl`, no subprocess. That is what makes running this on
    every Apply for every instance affordable.

    An instance that is not `running` is skipped entirely -- there is no guest
    to write to, and reporting a verdict for one would describe a file that
    does not exist yet. It gets its records at boot instead, through
    `generate_cloud_init`.
    """
    machine = vm or InstanceVm()
    zone_records = records(stores, env)
    overlay = overlay_addresses(stores.root, env)
    # TWO different sets, and collapsing them into one is a real regression
    # waiting to be tidied in. `all_instances` is what a record RESOLVES
    # against: a record naming a stopped instance still resolves, because DNS
    # does not model liveness and real Route 53 would return it too. `live` is
    # who gets WRITTEN to: a VM that is not running has no guest to write into.
    # Passing `live` to the resolver would make every name silently stop
    # resolving the moment its target was stopped -- an outage reported as a
    # DNS failure.
    all_instances = instances(stores, env)
    live = [i for i in all_instances if _running(i)]
    if not zone_records or not live:
        return {}

    plan = vm_hosts(zone_records, all_instances, overlay)
    verdicts: dict[str, HostsVerdict] = {}
    for instance in live:
        host_id = instance["instance_id"]
        name = vm_name(env, host_id)
        # The UNRESOLVABLE half never reaches the guest -- it cannot, there is
        # no address to write. It reaches WORLD instead, which is the whole
        # point: withholding an entry is correct, and withholding it silently
        # is the "mesh gate withheld facts that never reached World" defect
        # this repo already paid for once.
        verdict = _verdict(name, plan, bool(overlay), await machine.push_hosts(
            name, stores.root, env, host_id, plan.resolved,
        ))
        _record(stores, env, host_id, verdict)
        verdicts[name] = verdict
    _log(env, verdicts)
    return verdicts


def _record(stores: SynthStores, env: str, host_id: str, verdict: HostsVerdict) -> None:
    """Put the verdict on the instance record, where the World projector reads
    it (`reconcile/tf_status.py::_route53_zones`).

    THIS WRITE IS THE OTHER HALF OF THE SEAM, and it was missing until the
    projector was read rather than assumed: that module reconstructs a
    `HostsVerdict` from `hosts_action`/`hosts_names` "off the instance records
    the resolver writes", and this is the resolver. Returning the verdicts
    without recording them left a projector reading a field nobody wrote --
    a guard whose signal never arrives, which is this repo's rule 1 and would
    have made every zone read `healthy` no matter what a push did.

    `hosts_details` carries the resolver's per-name sentences. Without it an
    `unresolvable` verdict reconstructs with an empty detail slot and falls
    back to a generic "could not be resolved", losing the specific cause that
    action exists to preserve.

    Written through `stores.ec2compute.update` rather than `ec2compute`'s own
    private `_update_instance`: reaching across a module boundary into a
    `_`-name is how two writers of one record start disagreeing."""
    def mutate(record: dict | None) -> dict | object:
        if record is None:  # terminated and swept between the read and here
            return NO_CHANGE
        return {
            **record,
            "hosts_action": verdict.action,
            "hosts_names": list(verdict.names),
            "hosts_details": list(verdict.details),
        }

    stores.ec2compute.update(env, f"instance:{host_id}", mutate)


def _verdict(name: str, plan: HostsPlan, meshed: bool, action: str) -> HostsVerdict:
    """The push's own outcome, unless something could not be resolved at all.

    UNRESOLVABLE WINS over a successful push, and that ordering is the honest
    one: `pushed` is true of the entries that WERE written, and reporting it
    while a name the user drew resolves nowhere would be a success claim over a
    partial result. The reverse cannot lose information -- a failed push is
    re-tried by the next Apply anyway.

    WHY `meshed` IS A PARAMETER AND NOT A STRING TEST. Both no-mesh and
    "points at no instance" arrive here as unresolvable names, and the two need
    opposite fixes from a person. Telling them apart by matching the resolver's
    own sentences would be this repo's honesty rule 5 exactly -- the check and
    its subject sharing a source, so the check cannot fail. `meshed` is instead
    a FACT read from the store (`overlay_addresses` returned nothing), which
    the resolver cannot influence.

    The per-name sentences ride through untouched either way, so the reader
    gets the specific cause even when the action is the coarse one."""
    if plan.unresolvable and not meshed:
        return HostsVerdict(vm=name, action=HOSTS_NO_MESH, names=plan.names)
    if plan.unresolvable:
        return HostsVerdict(
            vm=name, action=HOSTS_UNRESOLVABLE, names=plan.names,
            details=tuple(reason for _n, reason in sorted(plan.unresolvable.items())),
        )
    return HostsVerdict(vm=name, action=action)


def _log(env: str, verdicts: dict[str, HostsVerdict]) -> None:
    unhealthy = {name: v.reason for name, v in verdicts.items() if not v.healthy}
    for name, reason in sorted(unhealthy.items()):
        log.warning("route53 hosts not resolvable on %s (env %s): %s", name, env, reason)
