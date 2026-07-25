"""Canvas WIRING (field test 2, "the product hole"): deliver a node's `env`
map -- static entries PLUS resolved `${{producer.ATTR}}` references -- into the
REAL container the workload runs in.

Before this, an ECS node carrying
`env: {DATABASE_URL: "${{db.DATABASE_URL}}", REDIS_URL: "${{cache.REDIS_URL}}"}`
had that map silently dropped: `spec/translate.py` DID parse it (static entries
into `fields["env"]`, `${{...}}` entries lifted into `ResourceDesired.refs`),
but the only consumer of either was M8's debug display. `agent/hcl.py` emitted
no `environment` block at all, `fabric/`'s `resolve` had no production caller,
and the container came up with the four AWS_* vars and nothing else. So you
could provision the whole production stack and have no canvas-driven way to hand
the app its connection strings -- while ROADMAP claimed "a container consumes
`${{cache.REDIS_URL}}`".

WHY LAUNCH TIME, NOT THE GENERATED HCL. The alternative was emitting
`environment` into `container_definitions` / `aws_lambda_function.environment`.
Four reasons not to:

1. SECRETS. `${{db.DATABASE_URL}}` resolves to
   `postgresql://app:<password>@host:port/db`. In the HCL it would be written
   into `main.tf` AND into `terraform.tfstate` -- in plaintext, in files whose
   only protection is their mode, and which v0.6.0's redaction rules
   deliberately keep secrets out of. At launch time the value exists only in the
   container's environment.
2. THE SEAM ALREADY EXISTS, for exactly this class of value. Both consumers
   already inject the workload's ISSUED GATEWAY CREDENTIALS into the real
   container at launch (`gateway/keys.py::workload_env`), keyed off the
   `odin:node` tag, with an explicit "never into the stored taskdef /
   `fn['environment']`" mandate. Resolved refs are the same kind of thing and
   ride the same seam.
3. ZERO DRIFT. `containerDefinitions` is echoed back byte-for-byte, and the
   resolved value embeds a Docker-assigned HOST PORT that changes whenever the
   backing container is recreated. Baking it into the taskdef would produce a
   plan diff on every apply -- the exact class of bug the lambda zip's
   nondeterministic timestamp caused.
4. FRESHNESS. Resolving at launch means a recreated database's NEW port is
   picked up by the next task, instead of a stale value frozen in tofu state.

The cost of that choice is ORDERING: with no value interpolated into the HCL,
tofu has no reason to create the database before the service. `agent/hcl.py`
therefore emits `depends_on` for each ref'd producer -- a real, portable
Terraform argument that carries NO values -- so a producer is fully created (and,
for rds/elasticache, `available`) before its consumer's tasks ever launch.

FACTS COME FROM THE GATEWAY'S OWN LIVE STATE, not from World. World is written
by the reconciler's tick, so during the SAME apply that creates both the
database and the service it does not have the database's facts yet -- resolution
would fail on every first apply. The gateway's synth stores are authoritative
the instant the producer's create call returns, which is exactly when the
consumer's launch happens.

NEVER AN EMPTY STRING: an unresolvable ref raises `UnresolvedRef`, which both
call sites turn into their existing terminal-failure shape (an ECS task STOPPED
with the reason; a Lambda `State: Failed` with it), so it surfaces as a
`crashed` node with a verdict naming the ref -- and, because a short service
never reaches steady state, as a FAILED apply.

WHICH KINDS PRODUCE: rds, elasticache, alb and ec2 -- the same four
`reconcile/tf_status.py` projects facts for, held to the same gates (see
`producer_facts`). ec2 arrived a beat late and is worth recording as a lesson:
the mesh work published `PRIVATE_IP`/`MESH_IP` into World while this injector
was being built in parallel, the two halves coordinated only on the NAMES, and
the result was a fact that visibly existed in `/world` and could not be
consumed by anything. The rule that closed it is the one to keep -- a ref
resolves if and only if `/world` shows that fact on that node, asserted by
holding the two projections to each other rather than to a hand-written
expectation.
"""
from __future__ import annotations

import json
from pathlib import Path

from odin.aws.rds import POSTGRES_PORT
from odin.fabric.nebula import NebulaManager
from odin.gateway.models import cachectl, elbv2ctl, rdsctl
from odin.gateway.stores import SynthStores
from odin.reconcile import mesh_health
from odin.runtime.colima import CONTAINER_HOST
from odin.runtime.lima import LIMA_HOST
from odin.spec.models import Ref, Stack
from odin.spec.store import SpecStore
from odin.util import atomic_write_text

# The canvas-label tag `agent/hcl.py::_tags_block` stamps on every
# canvas-node-backed resource -- the same identity bridge `workload_env` and
# `reconcile/tf_status.py` already use to get from an AWS resource back to the
# node the user drew.
_NODE_TAG = "odin:node"


class UnresolvedRef(Exception):
    """A `${{producer.ATTR}}` reference that cannot be given a real value right
    now. Raised, never swallowed: injecting an empty string would hand the
    workload a connection string to nowhere and let it fail somewhere far away
    from the cause (northstar directive 5)."""


def _ref_text(ref: Ref) -> str:
    return "${{" + f"{ref.target_id}.{ref.target_attr}" + "}}"


def db_facts(record: dict) -> dict[str, str]:
    """An rds node's published endpoints, from the gateway's own DB-instance
    record.

    DUPLICATED, DELIBERATELY, from `reconcile/tf_status.py::_db_facts`: this
    module cannot import `tf_status` (it imports `gateway/models/ecsctl.py`,
    which imports THIS module -- a cycle), and `cachectl`/`elbv2ctl` already
    keep their own fact builders next to their records, so this is the same
    convention rather than a new one. `tests/gateway/test_wiring.py` asserts
    the two produce identical output for the same record, so they cannot drift
    apart silently."""
    port = record.get("endpoint_port")
    if not port:
        return {}
    user, password, db = record["master_username"], record["master_password"], record["db_name"]
    addr, vm_addr = f"{CONTAINER_HOST}:{port}", f"{LIMA_HOST}:{port}"
    overlay_ip = record.get("overlay_ip")
    mesh_addr = f"{overlay_ip}:{POSTGRES_PORT}" if overlay_ip else None
    mesh = {
        "DATABASE_URL_MESH": f"postgresql://{user}:{password}@{mesh_addr}/{db}",
        "endpoint_mesh": mesh_addr,
    } if mesh_addr else {}
    return {
        "DATABASE_URL": f"postgresql://{user}:{password}@{addr}/{db}",
        "endpoint": addr,
        "DATABASE_URL_VM": f"postgresql://{user}:{password}@{vm_addr}/{db}",
        "endpoint_vm": vm_addr,
        **mesh,
    }


def _label(tags: dict, native_name: str) -> str:
    return tags.get(_NODE_TAG) or native_name


# An ec2 node's two published addresses -- DUPLICATED, DELIBERATELY, from
# `reconcile/tf_status.py::_ec2_facts` for the same import-cycle reason
# `db_facts` above is, and guarded the same way (`tests/gateway/
# test_wiring.py` holds the pair to identical output).
#   PRIVATE_IP -- the VM's real private address. Host-reachable, NOT SG-gated.
#   MESH_IP    -- its Nebula overlay address: sticky across recreation, and
#                 the ONE path a drawn security group actually gates.
_EC2_RUNNING = "running"
_EC2_MESH_KEYS = ("MESH_IP",)


def ec2_facts(record: dict, overlay: dict[str, str]) -> dict[str, str]:
    private_ip, overlay_ip = record.get("private_ip"), overlay.get(record["instance_id"])
    return {
        **({"PRIVATE_IP": private_ip} if private_ip else {}),
        **({"MESH_IP": overlay_ip} if overlay_ip else {}),
    }


def _overlay_assignments(stores: SynthStores, env: str) -> dict[str, str]:
    """host_id -> overlay IP for this env. Read-only: no mkdir, no allocation
    -- `NebulaManager.load_overlay`'s own contract, and the same read
    `tf_status` does per projection."""
    overlay = NebulaManager(Path(stores.root) / env / "nebula").load_overlay()
    hosts = overlay.subnets.get("hosts") if overlay else None
    return dict(hosts.assignments) if hosts else {}


def _ec2_producers(stores: SynthStores, env: str) -> dict[str, dict[str, str]]:
    """The RUNNING, canvas-labelled instances and the addresses each publishes.

    Every gate `tf_status._ec2_instances` applies before a fact reaches World
    is applied here too, so a ref resolves if and only if `/world` shows that
    fact on that node:

      - only `running` publishes anything. `pending`/`stopping` project no
        facts at all, and `stopped`/`terminated` are dead or gone -- which
        also means the terminated-record handling `tf_status` needs for the
        World BADGE (a drifted corpse must still show `crashed`) is moot here:
        a corpse has no address worth handing out either way.
      - no `odin:node` tag, no producer: a ref is written against the canvas
        label, and EC2 has no AWS-native name field to fall back to.
      - `mesh_health.gate` decides whether `MESH_IP` survives. If the env's
        lighthouse is down, no peer can find or relay to the overlay address,
        so it is WITHHELD rather than injected -- and the consumer's ref then
        fails honestly through `_resolve`, exactly like any other
        unresolvable ref. `PRIVATE_IP` is untouched: the VM really is still
        reachable that way. The check is cached process-wide on the same
        (root, env, member) key the projection uses, so a launch that happens
        between two reconciler ticks re-uses the tick's verdict instead of
        probing again -- and costs nothing at all for an env with no mesh."""
    instances = [
        (label, record)
        for key, record in stores.ec2compute.items(env).items()
        if key.startswith("instance:") and record["state_name"] == _EC2_RUNNING
        for label in [stores.tags.get(env, f"ec2:{record['instance_id']}", {}).get(_NODE_TAG)]
        if label
    ]
    if not instances:
        return {}
    overlay = _overlay_assignments(stores, env)
    published: dict[str, dict[str, str]] = {}
    for label, record in instances:
        candidate = ec2_facts(record, overlay)
        _kind, _phase, gated, _verdict = mesh_health.gate(
            ("ec2", "healthy", candidate, None), root=stores.root, env=env,
            member=record["instance_id"], overlay_ip=candidate.get("MESH_IP"),
            mesh_keys=_EC2_MESH_KEYS,
        )
        published[label] = gated
    return published


def producer_facts(stores: SynthStores, env: str) -> dict[str, dict[str, str]]:
    """`node label -> published facts`, for every kind that publishes any: rds,
    elasticache, alb and ec2 (the same four `reconcile/tf_status.py` projects
    facts for -- every other kind projects `{}`).

    GATED ON REALLY BEING UP, per kind: an rds record with no `endpoint_port`
    or a status other than `available`, a cache cluster that isn't
    `available`, and an instance that isn't `running` publish NOTHING -- so a
    consumer referencing them fails honestly instead of receiving a half-built
    address. Same rule `tf_status` applies before publishing facts into
    World."""
    facts: dict[str, dict[str, str]] = {}
    for record in rdsctl.records(stores, env):
        identifier = record["db_instance_identifier"]
        if record["status"] != rdsctl.AVAILABLE:
            continue
        tags = stores.tags.get(env, f"rds:{rdsctl.db_arn(identifier)}", {})
        facts[_label(tags, identifier)] = db_facts(record)
    for record in cachectl.clusters(stores, env):
        if record["status"] != cachectl.STATUS_AVAILABLE:
            continue
        tags = stores.tags.get(env, f"elasticache:{record['arn']}", {})
        facts[_label(tags, record["cache_cluster_id"])] = cachectl.facts(record)
    for key, record in stores.elbv2ctl.items(env).items():
        endpoint = elbv2ctl.endpoint_url(record) if key.startswith("lb:") else None
        if endpoint is None:
            continue
        tags = stores.tags.get(env, f"{elbv2ctl.SERVICE}:{record['arn']}", {})
        facts[_label(tags, record["name"])] = {"ALB_ENDPOINT": endpoint}
    facts.update(_ec2_producers(stores, env))
    return facts


def _resolve(ref: Ref, facts: dict[str, dict[str, str]]) -> str:
    producer = facts.get(ref.target_id)
    if not producer:
        raise UnresolvedRef(
            f"{ref.var} references {_ref_text(ref)}, but {ref.target_id!r} publishes no endpoint "
            "in this env yet -- it is either not healthy yet or a kind that publishes no facts "
            "(only rds, elasticache, alb and ec2 do)"
        )
    value = producer.get(ref.target_attr)
    if value is None:
        raise UnresolvedRef(
            f"{ref.var} references {_ref_text(ref)}, but {ref.target_id!r} publishes no "
            f"{ref.target_attr!r} -- it publishes {', '.join(sorted(producer))}"
        )
    return str(value)


def _staged_path(stores: SynthStores, env: str) -> Path:
    return Path(stores.root) / env / "gateway" / "wiring.json"


def stage(stores: SynthStores, env: str, stack: Stack) -> None:
    """Publish the canvas's authored `env`/refs where the GATEWAY can read them
    during the very `tofu apply` that is about to create these resources.

    Needed because `/apply-full` commits the Stack to the store only AFTER tofu
    succeeds -- deliberately and load-bearingly so (server.py documents the bug
    it fixes: an early commit lets the reconciler's background tick provision
    the same backings tofu is creating, and tofu's provider creates are not
    idempotent). But CreateService/CreateFunction -- and therefore the container
    launch that needs this env -- happen DURING that tofu run, when
    `get_stack(env)` still returns the previous (on a first apply, empty) Stack.

    So the desired wiring is staged just before tofu, next to the gateway's own
    per-env state. 0600, because a static `env` entry can itself be a secret --
    the same reason `canvas.json` and the Stack revisions are 0600. Written on
    EVERY apply including an empty-canvas teardown, so a removed node's entry
    never lingers."""
    staged = {
        res.id: {
            "env": res.fields["env"].value if "env" in res.fields else {},
            "refs": [{"var": r.var, "target_id": r.target_id, "target_attr": r.target_attr} for r in res.refs],
        }
        for res in stack.resources
    }
    atomic_write_text(_staged_path(stores, env), json.dumps(staged, indent=2), mode=0o600)


def _authored(stores: SynthStores, env: str, node_label: str) -> tuple[dict[str, str], tuple[Ref, ...]]:
    """`(static env, refs)` for one node, from the staged wiring if this env has
    any (every apply since v0.7.1 writes it), else from the applied Stack -- the
    fallback that keeps an env applied by an older build, or through a route
    that never staged, working."""
    path = _staged_path(stores, env)
    if path.exists():
        entry = json.loads(path.read_text()).get(node_label) or {}
        return entry.get("env") or {}, tuple(Ref(**ref) for ref in entry.get("refs") or [])
    resource = next((r for r in SpecStore(stores.root).get_stack(env).resources if r.id == node_label), None)
    if resource is None:
        return {}, ()
    static = resource.fields.get("env")
    return (dict(static.value) if static is not None and isinstance(static.value, dict) else {}), resource.refs


def node_env(stores: SynthStores, env: str, node_label: str) -> dict[str, str]:
    """The environment a workload node's REAL container should be launched with:
    its static `env` entries plus every `${{producer.ATTR}}` ref resolved to a
    live value.

    `node_label` is the canvas label (== the Stack resource id == the
    `odin:node` tag). A node with nothing authored -- e.g. a resource created by
    an imported HCL project that no canvas node backs -- contributes nothing
    rather than failing: there is no `env` to deliver.

    Everything is reached through `SynthStores.root`, which IS the store root
    (`server.py` builds it from `SpecStore.root`) and is public precisely so a
    gateway model can reach non-store per-env state.

    v1 limit, recorded: `ResourceDesired.refs` does not record whether a ref
    came from the `env` map or from a top-level field, so a top-level
    `${{...}}` field (`image: "${{db.endpoint}}"`) also arrives as an env var
    named after that field. Exotic, and honest about what it does with the one
    piece of information available."""
    static, refs = _authored(stores, env, node_label)
    resolved = dict(static)
    if not refs:
        return resolved
    facts = producer_facts(stores, env)
    for ref in refs:
        resolved[ref.var] = _resolve(ref, facts)
    return resolved
