"""route53 records -> the /etc/hosts entries a CONSUMER can actually use.

Two consumers, two different right answers, and that asymmetry is the whole
reason this module exists rather than the record's stored value being used
verbatim everywhere.

A record's value is `aws_instance.<n>.private_ip` -- the portable, AWS-shaped
answer, and a pure function of the canvas (it must stay that way: a `main.tf`
that depended on runtime mesh state would break round-trip and show drift on
every plan). What that address is WORTH depends on who is asking:

  container -> VM   the private_ip works. MEASURED: a Colima container really
                    does reach a Lima VM's vzNAT address (192.168.64.2, commit
                    0bb7b97, `tests/simulate/test_alb_ec2_target_e2e.py`).
  VM -> VM          the private_ip is USELESS. Stock Lima `vz` NATs every VM
                    into its own isolated address space; a raw ping between two
                    VMs' vzNAT addresses is 100% loss, before nebula is even
                    involved (`fabric/nebula.py`'s R5 note, confirmed live with
                    two real VMs). The only address that works is the Nebula
                    OVERLAY one.

So a VM consumer needs a SUBSTITUTION, and the record cannot help: r53-gw's
stored shape is `{name, type, ttl, set_identifier, values, alias}` where
`values` is a literal address list -- **it does not carry the target instance
id**. The only way back to an instance is to reverse-map the address through
the ec2-compute records, and that map can be ambiguous or empty.

WHICH IS WHY NOTHING HERE GUESSES. Every case that cannot be resolved to a
single instance with a real overlay address becomes an `unresolvable` entry
with a reason, never a silent fall back to `private_ip`. That fallback is the
tempting one and it is the worst available answer: it produces a name that
resolves and then never connects, on the exact address this whole kind is
scoped around being unreachable. A name that does not resolve is an error the
user sees in seconds; a name that resolves to a black hole is an afternoon.

PURE, and deliberately takes plain data rather than `SynthStores`: `compute/`
must not import `gateway/` (the import graph runs the other way), and a pure
function is one a test can drive through every ambiguous case without a store.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Only address records name a host. SOA/NS are the zone's own infrastructure
# (`route53ctl` seeds them on every zone) and an ALIAS record carries no
# literal value at all, so neither can produce a hosts entry -- they are
# skipped rather than reported, because "your NS record did not become an
# /etc/hosts line" is noise, not a finding.
ADDRESS_TYPES = ("A",)


@dataclass(frozen=True)
class HostsPlan:
    """What one consumer should resolve, and what it provably cannot.

    `unresolvable` maps the record name to WHY -- carried rather than
    recomputed at projection time so the substrate and the World verdict can
    never disagree about which names are affected."""

    resolved: dict[str, str] = field(default_factory=dict)
    unresolvable: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.unresolvable))


def _hosts_name(record_name: str) -> str:
    """Route 53 stores `api.internal.`; /etc/hosts wants `api.internal`."""
    return record_name.rstrip(".")


def _address_values(record: dict) -> list[str]:
    return [v for v in (record.get("values") or []) if isinstance(v, str) and v]


def address_records(records: list[dict]) -> list[dict]:
    """The records that name a host at all -- an A record with a literal
    value. An `alias` record is excluded even when its type is A: it points at
    another AWS resource by name, and there is no address in it to write."""
    return [
        r for r in records
        if r.get("type") in ADDRESS_TYPES and not r.get("alias") and _address_values(r)
    ]


def container_hosts(records: list[dict]) -> HostsPlan:
    """For a CONTAINER consumer: the stored value is already the right answer.

    No reverse-map, no mesh, nothing to fail -- container-to-VM on the vzNAT
    address is measured reachable, so this is the one case where the portable
    AWS answer and the locally-useful answer coincide. A multi-value record
    still cannot be expressed (see `vm_hosts`), and is refused here for the
    same reason rather than being quietly collapsed on one path and refused on
    the other."""
    resolved: dict[str, str] = {}
    unresolvable: dict[str, str] = {}
    for record in address_records(records):
        name = _hosts_name(record["name"])
        values = _address_values(record)
        if len(values) > 1:
            unresolvable[name] = _ROUND_ROBIN.format(name=name, count=len(values))
            continue
        resolved[name] = values[0]
    return HostsPlan(resolved=resolved, unresolvable=unresolvable)


def vm_hosts(
    records: list[dict],
    instances: list[dict],
    overlay: dict[str, str],
) -> HostsPlan:
    """For a VM consumer: every address substituted for the target's OVERLAY
    address, or reported as unresolvable with the real reason.

    `instances` are ec2-compute records (`{"instance_id", "private_ip",
    "public_ip", ...}`); `overlay` maps instance id -> its sticky Nebula
    address. An env with no mesh passes an EMPTY overlay map, and every record
    then reports `no mesh` rather than silently resolving to an address that
    cannot carry a packet.
    """
    by_address = _addresses_to_instances(instances)
    resolved: dict[str, str] = {}
    unresolvable: dict[str, str] = {}
    for record in address_records(records):
        name = _hosts_name(record["name"])
        values = _address_values(record)
        if len(values) > 1:
            unresolvable[name] = _ROUND_ROBIN.format(name=name, count=len(values))
            continue
        address = values[0]
        owners = sorted(by_address.get(address, ()))
        # AMBIGUOUS and MISSING are separate answers on purpose: they need
        # opposite fixes from a person (deduplicate the instances vs. point the
        # record at something that exists), and collapsing them into one
        # "could not resolve" is the shape of unhelpful error this repo keeps
        # rewriting.
        if len(owners) > 1:
            unresolvable[name] = _AMBIGUOUS.format(name=name, address=address, owners=", ".join(owners))
            continue
        if not owners:
            unresolvable[name] = _NO_INSTANCE.format(name=name, address=address)
            continue
        overlay_ip = overlay.get(owners[0])
        if not overlay_ip:
            unresolvable[name] = _NO_OVERLAY.format(name=name, instance=owners[0])
            continue
        resolved[name] = overlay_ip
    return HostsPlan(resolved=resolved, unresolvable=unresolvable)


def _addresses_to_instances(instances: list[dict]) -> dict[str, set[str]]:
    """Every address an instance answers to -> the instance ids claiming it.

    A SET, not a last-writer-wins dict, because the collision is the thing
    worth knowing about: two instances reporting one address is exactly the
    case `vm_hosts` must refuse instead of picking one. Both `private_ip` and
    `public_ip` are indexed because `ec2compute._finish_boot` writes the SAME
    discovered address into both, so a record generated from either resolves
    to the same VM.
    """
    index: dict[str, set[str]] = {}
    for instance in instances:
        instance_id = instance.get("instance_id")
        if not instance_id:
            continue
        for key in ("private_ip", "public_ip"):
            address = instance.get(key)
            if address:
                index.setdefault(address, set()).add(instance_id)
    return index


_ROUND_ROBIN = (
    "{name} has {count} addresses, and /etc/hosts cannot express DNS "
    "round-robin -- a resolver would silently answer with whichever line came "
    "first. Give the record a single value, or reach the targets by their own names"
)
_AMBIGUOUS = (
    "{name} points at {address}, and {owners} both report that address, so odin "
    "cannot tell which instance the record means"
)
_NO_INSTANCE = (
    "{name} points at {address}, which no EC2 instance in this environment "
    "reports -- the record may name an instance that has been terminated, or an "
    "address odin did not assign"
)
_NO_OVERLAY = (
    "{name} points at instance {instance}, which has no Nebula overlay address "
    "yet, and a VM can only reach another VM over the overlay (a VM-to-VM vzNAT "
    "address is 100% loss). It resolves as soon as that instance joins the mesh"
)
