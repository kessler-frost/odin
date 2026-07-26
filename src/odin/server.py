"""odin FastAPI app factory.

The canvas authors a desired-state Stack; a continuous Reconciler drives reality
(per-env backing containers for the AWS-shaped resources, via Colima) and
projects what `tofu apply` created through the gateway (every TF-owned kind,
`rds` among them since W2.7); the World projects back to the canvas over
WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from odin.agent import import_tf as import_tf_mod
from odin.agent import translate as translate_mod
from odin.agent.hcl import TfProject, generate_tf, parse_tf, resource_set, unquote
from odin.api.canvas import CanvasGraph, create_canvas_router
from odin.api.debug import create_debug_router
from odin.api.logs import create_logs_router
from odin.api.ws import ConnectionManager
from odin.aws.backings import PROVISIONED, BackingAws
from odin.compute.tasks import TaskRuntime
from odin.fabric.localhost import LocalhostFabric
from odin.fabric.nebula import mesh_state, reap_orphaned_lighthouses
from odin.fabric.sidecar import MeshSidecar
from odin.gateway import DEFAULT_GATEWAY_PORT, GATEWAY_PORT_ENV, wiring
from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore, Principal
from odin.gateway.models import ec2compute, ec2net, ecsctl, lambdactl, rdsctl
from odin.gateway.stores import SynthStores
from odin.reconcile import admission, drift
from odin.reconcile.drift import DriftSweeper
from odin.reconcile.reconciler import Reconciler
from odin.reconcile.tf_status import stranded_in_tf_state
from odin.runtime.colima import ColimaRuntime
from odin.simulate.runner import SimulateBusy, TfRunner, TofuNotInstalled
from odin.simulate.workspace import tf_dir
from odin.spec.models import Stack, World
from odin.spec.store import SpecStore
from odin.spec.translate import MODELLED_NODE_TYPES, canvas_to_stack, drawn_node_types, skipped_node_types
from odin.util import STORE_LOCK_NAME, StoreLock, hold_store_lock, odin_version

ODIN_DIR = Path(".odin")
CANVAS_NAME = "canvas.json"
CANVAS_PATH = ODIN_DIR / CANVAS_NAME
ENV = "default"

log = logging.getLogger("odin")

# Security finding #1c: CSRF defense-in-depth. odin has no authentication
# of its own (see __main__.py's loopback-default fix) -- a browser tab open
# on ANY other site could POST straight to this server's /apply-full and it
# would just run it. A browser ALWAYS sends `Origin` (and normally `Referer`)
# on a cross-site state-changing request; curl, the `odin` CLI, and an
# agent's own HTTP client send neither -- so this only ever blocks a browser
# acting on a page odin didn't serve, never a legitimate CLI/agent caller.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_origin(value: str) -> bool:
    return urlparse(value).hostname in _LOOPBACK_HOSTS


async def _csrf_guard(request: Request, call_next):
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and not _is_loopback_origin(origin):
            return JSONResponse(
                status_code=403,
                content={"error": "cross-origin request rejected (odin has no authentication; only same-origin requests are trusted)"},
            )
    return await call_next(request)


def not_covered(skipped: list[str], unsupported: list[str]) -> list[str]:
    """Everything a request did NOT act on, in ONE array — the field a CI gate
    should read, published by the API itself.

    Fresh-user MISLEAD-1: the README told CI to gate on `.unsupported`, but a
    node whose KIND odin has no model for at all lands in `.skipped` and
    `.unsupported` stayed `[]`. `jq -e '.unsupported | length == 0'` returned
    true — exit 0 — while two drawn nodes were silently dropped. Two arrays
    with adjacent meanings is a gate you can get right and still be wrong.

    v0.7.3 computed this union in `cli/apply.py`, which left `curl /apply-full`
    — how an agent or a CI job without the odin CLI consumes odin, and an equal
    citizen per NORTHSTAR directive 8 — with the original trap. It lives here
    now, and the CLI reads it rather than recomputing it: one source of truth.

    `skipped` = a canvas node type that never became a Stack resource (a kind
    odin doesn't model, or a typo). `unsupported` = a resource odin models but
    can't generate Terraform for, with the reason. Both are still emitted
    verbatim alongside; this is a union, not a replacement."""
    return [*skipped, *unsupported]


def _bump_epoch(env_epoch: dict[str, int], env: str) -> int:
    """Release finding #4: a per-env, in-memory generation counter. A client
    disconnect does NOT cancel the in-flight server-side request -- a stale
    /apply-full can still be mid-tofu when a NEWER /destroy (or an
    empty-canvas apply, which is also a teardown) commits. Bumping here and
    re-checking against the value captured at the stale request's own entry
    (create_apply_full_router's `apply_full`) is what lets that request
    notice it's been superseded instead of going on to undo the teardown."""
    env_epoch[env] = env_epoch.get(env, 0) + 1
    return env_epoch[env]


async def _admission_rejection(runtime, store: SpecStore, stack: Stack) -> JSONResponse | None:
    """Owner directive B1: the pre-apply admission check, shared by
    `/apply-full` and `/tf/apply` -- both must reject BEFORE touching any
    container/VM, never after. `ensure_host()` shells to `docker info`
    (blocking); `asyncio.to_thread` keeps that off the event loop, same
    precaution `_reap_orphaned_ec2_vms` already takes for its own blocking
    `limactl` calls. Returns None when admitted, else the 409 JSONResponse
    the caller should return VERBATIM (named numbers, never a bare
    "rejected")."""
    host = await asyncio.to_thread(runtime.ensure_host)
    result = admission.check_admission(stack, host, store.root)
    if result.ok:
        return None
    return JSONResponse(status_code=409, content={
        "error": result.reason,
        "estimated_mib": result.estimated_mib,
        "budget_mib": result.budget_mib,
        "free_disk_gib": result.free_disk_gib,
    })


def _tf_state(root: Path, env: str) -> dict:
    """tofu's OWN state for `env`, or `{}` when there is nothing to read.

    Read straight out of `terraform.tfstate` (structured JSON) rather than
    shelled out to `tofu state list`, which would want the very per-env lock a
    failed run just released and would cost another process. STRICT in the same
    direction as `tf_status._tf_state`: a state file that is missing, empty or
    caught mid-rewrite is NO evidence, never an error -- every caller here is
    either already on a failure path or is a pre-apply guard that must not 500
    because it read the file during a concurrent apply."""
    state = tf_dir(root, env) / "terraform.tfstate"
    text = state.read_text().strip() if state.is_file() else ""
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def _managed_resources(root: Path, env: str) -> list[dict]:
    return [r for r in _tf_state(root, env).get("resources", []) if r.get("mode") != "data"]


def _tf_state_addresses(root: Path, env: str) -> list[str]:
    """Every managed resource tofu's own state still holds for `env` -- the
    authoritative answer to "what is still standing" after a destroy that
    didn't finish."""
    return sorted(f"{r['type']}.{r['name']}" for r in _managed_resources(root, env))


# The tag `agent/hcl.py::_tags_block` stamps on EVERY primary canvas-node-backed
# resource block (never on a companion -- a task definition, an sns->sqs
# subscription, a lambda's auto-role), carrying the canvas label. Already the
# mechanism `reconcile/tf_status.py` and `gateway/keys.py::workload_env` key
# off; this is the third reader, and the reason the guard below can answer
# "which NODE does this terraform resource belong to" without re-deriving
# hcl.py's private HCL-name assignment.
_ODIN_NODE_TAG = "odin:node"


def _tagged_node(tags: object) -> str:
    """The canvas node a `tags` map names, or "". python-hcl2 keeps a quoted
    literal's own quotes on BOTH the map key and the value when it parses HCL
    (probed against the real parser, not assumed -- see `hcl.unquote`), while
    the same map read back out of `terraform.tfstate` is plain JSON with no
    quotes at all. `unquote` is a no-op on the latter, so one reader serves
    both."""
    if not isinstance(tags, dict):
        return ""
    return next((unquote(v) or "" for k, v in tags.items() if unquote(k) == _ODIN_NODE_TAG), "")


def _covered_nodes(files: dict[str, str]) -> set[str]:
    """The canvas nodes a generated Terraform project ACTUALLY builds a
    resource for -- i.e. the nodes that will still exist after `tofu apply`
    runs it.

    Read off the real generated HCL rather than inferred from `unsupported`,
    which is NOT the same set: `_unwired_refs` and the alb-target check both
    append entries for resources hcl.py did build, so keying the guard on
    `unsupported` would refuse applies that destroy nothing (honesty rule 1 --
    a guard must read a signal that actually means what it is being read to
    mean). `unsupported` is used for the human REASON only, below."""
    return {node for _type, _name, attrs in parse_tf(files) if (node := _tagged_node(attrs.get("tags")))}


def _tf_state_nodes(root: Path, env: str) -> set[str]:
    """Every canvas node tofu's own state still holds a real resource for."""
    return {
        node
        for resource in _managed_resources(root, env)
        for instance in resource.get("instances", [])
        if (node := _tagged_node((instance.get("attributes") or {}).get("tags")))
    }


def _existing_nodes(store: SpecStore, env: str) -> set[str]:
    """Every canvas node odin can PROVE exists right now, from its two real
    witnesses: what the reconciler has actually observed (World) and what
    tofu's own state holds.

    NOT the last-applied Stack, which is the tempting reading and the wrong
    one: `/apply-full` commits a Stack whenever tofu SUCCEEDS, and an
    unbuildable resource does not fail an apply -- so a node that has never
    existed for a single second is in the last-applied Stack from the very
    first apply onward. Guarding on that would refuse every subsequent apply
    of a canvas that has one permanently-unsupported node on it, while
    protecting nothing. Existence is the thing being protected, so existence
    is the thing that gets measured."""
    return {r.id for r in store.current_world(env).resources} | _tf_state_nodes(store.root, env)


# Query parameter for the one caller who genuinely means it. Nothing in the UI
# or the CLI ever sets it, so it cannot be reached by a mis-click or a stale
# script; the refusal below names it, which is the only place a user learns it
# exists.
ALLOW_UNCOVERED = "allow_destroying_uncovered"


def _uncovered_reason(node: str, requested_as: str, kind: str | None, unsupported: list[str]) -> str:
    """WHAT about this node isn't covered, in the user's own vocabulary.

    Two genuinely different failures: the canvas `type` isn't a kind odin
    models at all (so the node never became a Stack resource -- `!r` because a
    trailing space is invisible without the quotes), or it did become one and
    the Terraform builder declined it, in which case hcl.py's own reason is
    reproduced verbatim. The reason is cosmetic: the guard has already fired on
    the coverage signal, so a change to hcl.py's message format degrades this
    line and never the protection."""
    if kind is None:
        return (
            f"its type {requested_as!r} is not a kind odin models"
            + ("" if requested_as in MODELLED_NODE_TYPES else " (a typo?)")
            + " -- it is not in the desired state at all"
        )
    prefix = f"{node} ({kind}): "
    declined = [entry.removeprefix(prefix) for entry in unsupported if entry.startswith(prefix)]
    return declined[0] if declined else f"odin generated no Terraform resource for it ({kind})"


def _uncovered_destroys(
    requested: dict[str, str], existing: set[str], covered: set[str],
    kinds: dict[str, str], unsupported: list[str],
) -> list[dict]:
    """Field test 5 (HIGHEST, silent data loss): the resources this apply would
    DESTROY without ever being asked to.

    `count: "2"` -> `"two"` on a live ECS node destroyed the service and both
    task containers; `type: "s3"` -> `"s3 "` destroyed a real bucket, the object
    inside it and the rustfs backing. Both reported `status: applied`, `tf: ok`,
    exit 0, in under four seconds. The only signal was a line in `not_covered`,
    a field whose documented meaning is "a node odin didn't act on".

    The distinction, and it is the whole point:

    * `requested` -- still on the canvas (or, on `/tf/apply`, still in the Stack
      being applied). A node the user REMOVED is deliberately absent from this
      map, so removing a node still destroys it: "empty canvas = full teardown"
      is odin's documented teardown story and is untouched by this.
    * `existing` -- odin can prove it exists (World or tofu's state). A node
      that was never successfully applied has nothing to lose, so it stays on
      today's behavior: skipped, and reported in `not_covered`.
    * `covered` -- this apply keeps it. Everything else about a node is
      irrelevant here; the question is only whether it survives.

    Still asked for + really exists + not covered = destruction the user did not
    ask for. That is a refusal, not a status."""
    return [
        {
            "node": node, "requested_as": requested_as, "kind": kinds.get(node),
            "reason": _uncovered_reason(node, requested_as, kinds.get(node), unsupported),
        }
        for node, requested_as in sorted(requested.items())
        if node in existing and node not in covered
    ]


def _uncovered_rejection(uncovered: list[dict], env: str) -> JSONResponse:
    """The refusal itself: names every node, says what about it isn't covered,
    and says plainly what applying anyway would do. `error` is what makes the
    CLI exit nonzero (`cli/http.body_or_fail`), the same convention every other
    honest refusal in this file uses."""
    named = "; ".join(f"{item['node']} — {item['reason']}" for item in uncovered)
    return JSONResponse(status_code=409, content={
        "error": (
            f"refusing to apply: {len(uncovered)} resource(s) that env {env!r} really has right now "
            f"are still on the canvas but are NOT covered by this apply, so applying would DESTROY "
            f"them: {named}. Fix the node(s) above and re-apply. If you genuinely want them gone, "
            f"delete them from the canvas (that is odin's teardown story and it still works) or "
            f"re-send with ?{ALLOW_UNCOVERED}=true. Nothing was changed."
        ),
        "would_destroy": uncovered,
        "env": env,
    })


def _surviving_containers(runtime, env: str) -> list[str]:
    """Odin containers this env still has, by odin's own container naming --
    `odin-aws-{backing}-{env}` carries the env as a SUFFIX, `odin-rds-{env}-…`
    / `odin-ecs-{env}-…` / `odin-lambda-{env}-…` as an INFIX, both anchored on
    `-` so a longer env sharing this one's prefix never matches (the rule
    tests/containers.py documents and relies on).

    Best-effort by design, and `reconcile/drift.py::_listing`'s exact
    reasoning: this runs only when a destroy has ALREADY failed, so a docker
    daemon that won't answer must degrade to "couldn't tell" rather than
    replace a real failure report with a traceback."""
    try:
        names = runtime.container_names()
    except Exception as exc:  # noqa: BLE001 -- any CLI/parse failure means "unknown"
        log.warning("could not list containers while reporting a failed destroy (%s)", exc)
        return []
    return sorted(name for name in names if name.endswith(f"-{env}") or f"-{env}-" in name)


# --- `/destroy`: outcome -> status, the ONE place the status is decided ---
#
# Field test 5 (HIGH). The route no longer initialises `status` optimistically
# and then hopes every branch revises it -- three releases of that shape
# produced three different `odin destroy` runs that exited 0 over a fully
# standing env. The status is looked up here, once, from the outcome the tofu
# half actually reported, and ANYTHING this map has no entry for -- including
# `None`, i.e. a branch that returned without reporting an outcome at all --
# falls through to a failure. A new way for a destroy not to happen now fails
# loudly by default; it can only be scored as success by being added here on
# purpose.
_DESTROY_STATUS = {
    "ok": "destroyed",                 # tofu ran and destroyed everything it owned
    "nothing_to_destroy": "destroyed",  # tofu owns nothing here: no workspace, or an empty state
    "failed": "destroy_failed",         # tofu ran, tofu lost
    "timed_out": "destroy_timed_out",   # THIS runner killed it at its own deadline
    "unavailable": "destroy_unavailable",  # tofu isn't installed, and its state still holds resources
}

# What each failing outcome REALLY was, in the words that name the right knob.
_DESTROY_CAUSE = {
    "failed": "tofu exited {exit_code}",
    "timed_out": (
        "tofu was killed at its whole-call deadline (ODIN_TOFU_DESTROY_TIMEOUT), so nothing was "
        "diagnosed -- it ran out of time"
    ),
    "unavailable": (
        "`tofu` is not on this server's PATH, so `tofu destroy` never ran at all -- nothing was "
        "even attempted (install it with `brew install opentofu`; a server started outside a login "
        "shell often has no /opt/homebrew/bin, so check the PATH odin itself was launched with)"
    ),
}
_UNKNOWN_DESTROY_CAUSE = (
    "the destroy finished without reporting any outcome, which is an odin bug -- reported as a "
    "failure rather than assumed to have worked, because nothing here verified that it did"
)


def create_apply_router(
    store: SpecStore, reconciler_for, keystore: KeyStore, runner: TfRunner, gateway_port, env_epoch: dict[str, int],
    stores: SynthStores, gateway: GatewayState, runtime,
) -> APIRouter:
    router = APIRouter()

    @router.post("/apply")
    async def apply(graph: CanvasGraph, env: str = ENV, allow_destroying_uncovered: bool = False) -> JSONResponse:
        canvas = graph.model_dump()
        stack = canvas_to_stack(canvas, env=env)
        # Field test 5 (HIGHEST): the same refusal `/apply-full` makes, for the
        # same reason -- this route commits the desired state too, and the
        # reconciler's own prune/gc is what deleted the rustfs backing (and the
        # object inside it) when one node's `type` grew a trailing space. No
        # Terraform is generated here, so "covered" is simply everything that
        # became a Stack resource: the reconciler acts on all of them.
        uncovered = _uncovered_destroys(
            drawn_node_types(canvas), _existing_nodes(store, env),
            {r.id for r in stack.resources}, {r.id: r.kind for r in stack.resources}, [],
        )
        if uncovered and not allow_destroying_uncovered:
            return _uncovered_rejection(uncovered, env)
        reconciler = await reconciler_for(env)
        rev = store.apply(stack)
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        skipped = skipped_node_types(canvas)
        # No `unsupported` half here: this route never generates Terraform, so
        # the union is the skipped list -- still published under the same name
        # every other apply surface uses, so a gate reads ONE field everywhere.
        return JSONResponse(status_code=200, content={
            "status": "applied", "rev": rev, "env": env,
            "skipped": skipped, "not_covered": not_covered(skipped, []),
        })

    @router.post("/destroy")
    async def destroy(env: str = ENV) -> JSONResponse:
        # Release finding #5: `/destroy` used to only ever prune the
        # reconciler half, leaving anything tofu created (vpc/subnet/sg have
        # NO reconciler-driven teardown path at all -- see
        # create_apply_full_router's own note on this) permanently orphaned.
        # Busy guard BEFORE any mutation (mirrors /tf/destroy's own
        # SimulateBusy message, and apply_full's identical guard) -- no
        # reconcile, no store write while a tofu run holds the env's lock.
        status = runner.status(env)
        if status["running"]:
            return JSONResponse(
                status_code=409,
                content={"error": f"a tofu run is already in progress for env {env!r}"},
            )
        _bump_epoch(env_epoch, env)  # finding #4: invalidate any older in-flight apply-full for this env
        # Field test 5 (LOW): destroying nothing must CREATE nothing. `odin
        # destroy --env typo` used to mint `.odin/<env>/` with a HEAD, an empty
        # Stack revision and -- through `keystore.revoke_env` -- a real
        # `keys.json` of gateway credentials, so a typo issued credentials for
        # an environment that had never existed and put it in `odin envs`
        # permanently. Everything BELOW this line writes that directory;
        # everything above it is in-memory or read-only, and the epoch bump in
        # particular has to stay above it -- the very first apply for an env is
        # what creates the directory, so a teardown racing it would find no
        # directory and must still supersede the apply it is racing.
        if not (store.root / env).exists():
            return JSONResponse(status_code=200, content={
                "status": "nothing_to_destroy", "env": env, "tf": None,
                "note": f"env {env!r} has never existed -- nothing was destroyed, and nothing was created",
            })

        # Field test 5 (HIGH, the FOURTH form of the same lie): `body["status"]`
        # used to be initialised to "destroyed" right here, at the top, and each
        # branch that could go wrong was expected to remember to revise it.
        # Three separate fixes each taught one more branch to remember, and a
        # fourth branch that hadn't been taught kept inheriting the success --
        # most recently `TofuNotInstalled`, which set `body["tf"]` and left
        # `status` alone, so a server launched outside a login shell (no
        # /opt/homebrew/bin on PATH) answered `destroyed` with every
        # Terraform-managed resource still in state, and `odin destroy` exited
        # 0. Keying on the exit code, which fixed the previous form, cannot
        # reach this one: tofu never ran, so there is no exit code.
        #
        # So the status is no longer INITIALISED at all -- it is DERIVED, once,
        # from `tf_outcome` at the bottom, through `_DESTROY_STATUS`. `None` is
        # the starting value and means "nothing has reported an outcome", which
        # maps to a failure. Any future branch that forgets to set an outcome
        # therefore fails loudly instead of quietly inheriting a success it
        # never earned; that is the shape, not another branch.
        tf_outcome: str | None = None
        body: dict = {"env": env, "tf": None}
        reconciler = await reconciler_for(env)
        # hold(): field test 2, finding B6. `tofu destroy` has to REACH the
        # backings the resources it is deleting live in -- an s3 bucket is
        # deleted by a real DeleteBucket forwarded to RustFS -- so this path has
        # to boot them, exactly like /apply-full's ensure phase. That puts it
        # squarely in the gc-versus-ensure race hold() exists for, and in the
        # sharper form: gc's whole job is to stop the backings of an env that's
        # going away. Holding the tick lock across ensure + the WHOLE destroy +
        # the empty-Stack commit makes both halves impossible:
        #   (a) no tick (so no gc) can run between booting a backing and tofu
        #       finishing with it, and
        #   (b) the empty Stack is committed INSIDE the hold, so the very first
        #       tick after it -- the explicit one below -- gc's every backing
        #       this ensure just started. Nothing is left running.
        # The trailing tick() is deliberately OUTSIDE the hold: `tick()` takes
        # the same non-reentrant lock (the /apply-full path has the identical
        # shape and the identical reason).
        async with reconciler.hold():
            if not status["workspace_exists"]:
                tf_outcome = "nothing_to_destroy"  # tofu was never applied for this env
            else:
                access_key, secret_key = keystore.issue(env, OPERATOR_NODE_ID)
                # Security finding #3: scrub any sensitive field's raw value out
                # of tofu's own destroy log before it reaches the tail/WS/events.
                last_applied = store.get_stack(env)
                secrets = last_applied.sensitive_values()
                # Without this, a RESTORED env (which boots no containers, as
                # documented) has no registered `backing_port`, so the gateway
                # answers every AWS call the destroy makes with a real
                # 503/ServiceUnavailable and aws-sdk-go-v2 retries each one ~25
                # times with backoff -- silently, since retries never reach
                # tofu's stdout. That is the 8m26s "hang with no progress" the
                # field test hit, and telling the user to Apply first was making
                # them do by hand what this line does. Same call /apply-full
                # makes, same no-resource-CRUD guarantee (`ensure_backing` only
                # starts a container).
                await reconciler.ensure_backings(last_applied)
                try:
                    result = await runner.destroy(env, gateway_port(), access_key, secret_key, secrets=secrets)
                except TofuNotInstalled:
                    body["tf"] = {"status": "unavailable", "exit_code": None, **_TOFU_NOT_INSTALLED}
                    # Field test 5: tofu missing is a destroy that DID NOT
                    # HAPPEN -- unless tofu's own state proves it owned nothing
                    # to begin with, which is the one case where "install tofu"
                    # would be busywork. That witness is read here rather than
                    # assumed in either direction: the previous code assumed
                    # harmless and reported success over six live resources; a
                    # blanket failure would make an env tofu never touched
                    # un-destroyable without a tofu install it does not need.
                    tf_outcome = "unavailable" if _tf_state_addresses(store.root, env) else "nothing_to_destroy"
                except SimulateBusy as exc:  # a second call won the race after our guard passed
                    return JSONResponse(status_code=409, content={"error": str(exc)})
                else:
                    body["tf"] = {"status": "ok" if result.ok else "failed", "exit_code": result.exit_code}
                    # Field test 5 (MED): `timed_out` comes from the runner,
                    # which is the only frame that knows -- it is the thing that
                    # sent the signal. The old test, `result.exit_code < 0`,
                    # rested on "only odin's own killpg produces a negative
                    # code", which is false: ANY kill gives -9, so an external
                    # `kill -9` 0.87 seconds into a destroy was reported as a
                    # 300-second deadline expiry and sent the user to tune
                    # ODIN_TOFU_DESTROY_TIMEOUT for something unrelated.
                    tf_outcome = "ok" if result.ok else ("timed_out" if result.timed_out else "failed")
                    if not result.ok:
                        body["tf"]["tail"] = list(result.tail)

            # Field test 3 HIGH-B: whatever tofu did or did not manage to
            # destroy, an EC2 instance this env's gateway store still claims
            # is a REAL Lima VM burning the user's RAM and disk -- and after
            # an interrupted apply (Ctrl-C, an OOM, a closed laptop) tofu's
            # state is empty, so `tofu destroy` honestly destroys nothing and
            # reports success. Destroy is unambiguous about intent, so it
            # reclaims them directly; if it CANNOT, it refuses to say
            # `destroyed` (`ReclaimFailed` -> 500 with the VM names).
            reclaimed = await asyncio.to_thread(ec2compute.reclaim_env_instances, stores, env)
            if reclaimed:
                body["reclaimed_vms"] = reclaimed
            # ...and the network records the same interruption left behind,
            # which `tofu destroy` likewise never reaches. They are what kept
            # `/world` listing a VPC and subnets for a destroyed env -- and,
            # because the lighthouse stop hangs off the VPC-delete path, a VPC
            # record that is never deleted is a lighthouse never stopped
            # (HIGH-A through HIGH-B's back door).
            forgotten = await asyncio.to_thread(ec2net.purge_env, stores, env)
            if forgotten:
                body["reclaimed_network_records"] = forgotten

            # THE one derivation. Unset (`None`) is not in the map and is
            # therefore a failure -- see the note at the top of this route.
            body["status"] = _DESTROY_STATUS.get(tf_outcome, "destroy_failed")
            if body["status"] == "destroyed":
                # ...and the empty desired state is committed ONLY when the
                # teardown really happened -- the tick below is what prunes on
                # the strength of it. Field test 5: committing it regardless
                # BRICKED the env. The next destroy's `ensure_backings(last
                # applied)` got an empty Stack, started no backing containers,
                # and every AWS call tofu made 503-retried until the 300s
                # deadline (measured: 5:00.38); recovery meant re-applying the
                # original canvas, which assumes the user still has it. It is
                # also the same rule `/apply-full` already follows in the other
                # direction ("desired state not committed; fix and re-apply"):
                # the desired state changes when the action succeeded, never
                # because it was attempted.
                store.apply(Stack(env=env))

        await reconciler.tick()
        keystore.revoke_env(env)  # gateway-issued keys die with the env they belong to
        if body["status"] == "destroyed":
            return JSONResponse(status_code=200, content=body)
        # A destroy that failed says so, and says WHAT SURVIVED -- "destroy
        # failed" on its own is nearly as unhelpful as the false success it
        # replaces. Both witnesses, gathered only here on the failure path:
        # tofu's own state (what terraform still owns and a retry must delete)
        # and the real containers still on the machine. `error` is what makes
        # `odin destroy` exit nonzero: `cli/http.body_or_fail` keys on it, the
        # same convention every other honest failure in this file uses.
        body["still_standing"] = {
            "tf_state": _tf_state_addresses(store.root, env),
            "containers": await asyncio.to_thread(_surviving_containers, runtime, env),
        }
        cause = _DESTROY_CAUSE.get(tf_outcome, _UNKNOWN_DESTROY_CAUSE).format(
            exit_code=(body["tf"] or {}).get("exit_code"),
        )
        body["error"] = (
            f"destroy did not finish for env {env!r}: {cause}. "
            f"still standing: {len(body['still_standing']['tf_state'])} resource(s) in tofu state "
            f"{body['still_standing']['tf_state']}, "
            f"{len(body['still_standing']['containers'])} container(s) {body['still_standing']['containers']}. "
            f"The env's desired state was left as it was, so re-running the destroy once the cause "
            f"above is fixed picks up exactly here."
        )
        return JSONResponse(status_code=500, content=body)

    @router.get("/world")
    def world(env: str = ENV) -> dict:
        """The env's observed World, plus the resources tofu really created
        that odin can currently see nowhere else (field test 3, P2-5: after a
        failed apply the s3/sqs/sns/dynamodb nodes had NO BADGE AT ALL while
        tofu's state listed them and every call answered ServiceUnavailable).

        `reachable` is the gateway's own routing table -- the very thing that
        decides between forwarding a call and refusing it -- so this reports
        exactly the resources the gateway would genuinely refuse right now, and
        nothing during a healthy apply. See `stranded_in_tf_state` for why this
        is a per-request overlay rather than a World write."""
        observed = store.current_world(env)
        reachable = {kind for kind in PROVISIONED if gateway.backing_port(env, kind) is not None}
        stranded = stranded_in_tf_state(store.root, env, observed, reachable)
        return World(
            env=observed.env, resources=(*observed.resources, *stranded),
        ).model_dump()

    @router.get("/mesh")
    def mesh(env: str = ENV) -> dict:
        """The env's Nebula overlay membership — the read model a mesh UI builds
        on. Empty until hosts join (single-host today)."""
        return mesh_state(store.root, env, store.current_world(env)).model_dump()

    @router.get("/envs")
    def envs() -> dict:
        return {"envs": store.list_envs()}

    return router


_TOFU_NOT_INSTALLED = {"error": "tofu not installed", "fix": "brew install opentofu"}


# `tofu plan -detailed-exitcode`, as odin's own vocabulary. Anything not in
# here (1, a signal, ...) is a real error -- see `/tf/plan`.
_PLAN_STATUS = {0: "no_changes", 2: "changes"}


class ImportTfRequest(BaseModel):
    source: Literal["hcl", "live"]
    hcl: str = ""
    resources: list[dict] = []  # [{"type": "s3", "id": "uploads"}, ...] -- see import_tf.LiveResource


def _saved_canvas(path: Path) -> dict:
    """The canvas currently on disk (`GET /canvas`'s own source), or an empty
    one. Never raises: a plan must not fail because nobody has drawn yet."""
    return json.loads(path.read_text()) if path.is_file() else {}


def create_tf_router(
    store: SpecStore, runner: TfRunner, keystore: KeyStore, gateway_port,
    translate_cache: translate_mod.TranslateCache, runtime, stores: SynthStores,
    canvas_path: Path = CANVAS_PATH,
) -> APIRouter:
    """`/tf/*` -- Simulate's own apply/destroy/status, independent of the
    canvas `/apply`/`/destroy` above (S2 CONTRACT ADDENDUM: routes named
    `/tf/*`, not `/simulate/*` -- "the owner renamed the user surface to
    Apply"). `gateway_port` is a zero-arg callable rather than a plain int:
    the real port is only known once the gateway's uvicorn listener starts
    in `create_app`'s `lifespan`, resolved AFTER this router is built."""
    router = APIRouter()

    def _issue_operator(env: str) -> tuple[str, str]:
        return keystore.issue(env, OPERATOR_NODE_ID)

    @router.post("/tf/apply")
    async def tf_apply(env: str = ENV, allow_destroying_uncovered: bool = False) -> JSONResponse:
        stack = store.get_stack(env)
        project = generate_tf(stack)
        # Field test 5 (HIGHEST): the same refusal the canvas routes make. There
        # is no canvas here -- this route applies the STORED Stack, so the Stack
        # itself is what the user is still asking for, and a resource in it that
        # generates no Terraform is one `tofu apply` deletes out of its own state
        # while the user is asking for it. Same three conditions, same helper.
        uncovered = _uncovered_destroys(
            {r.id: r.kind for r in stack.resources}, _existing_nodes(store, env),
            _covered_nodes(project.files), {r.id: r.kind for r in stack.resources}, project.unsupported,
        )
        if uncovered and not allow_destroying_uncovered:
            return _uncovered_rejection(uncovered, env)
        # Owner directive B1: reject BEFORE tofu ever runs, not after it's
        # already spawned real containers/VMs that then fail one-by-one.
        rejection = await _admission_rejection(runtime, store, stack)
        if rejection is not None:
            return rejection
        # Canvas wiring: same publish `/apply-full` does, from the stack this
        # route applies -- otherwise a Simulate run would launch containers
        # against whatever the LAST /apply-full staged.
        wiring.stage(stores, env, stack)
        access_key, secret_key = _issue_operator(env)
        try:
            result = await runner.apply(
                env, project, gateway_port(), access_key, secret_key, secrets=stack.sensitive_values(),
            )
        except TofuNotInstalled:
            return JSONResponse(status_code=409, content=_TOFU_NOT_INSTALLED)
        except SimulateBusy as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        body = {
            "status": "applied" if result.ok else "failed", "env": env,
            "exit_code": result.exit_code, "unsupported": project.unsupported,
            # This route applies the STORED Stack -- no canvas is read, so
            # there is no `skipped` half and the union is `unsupported`. The
            # field is published anyway so one gate shape covers every route.
            "not_covered": not_covered([], project.unsupported),
        }
        if not result.ok:
            body["tail"] = list(result.tail)
        return JSONResponse(status_code=200 if result.ok else 500, content=body)

    @router.post("/tf/plan")
    async def tf_plan(env: str = ENV) -> JSONResponse:
        """Field test 3 (safety): the SAFE drift check. Everything `/tf/apply`
        does to keep tofu pointed at odin's own gateway -- the injected
        `AWS_ENDPOINT_URL`, this env's operator credentials, the same
        workspace -- with `tofu plan -detailed-exitcode` in place of the
        apply. The alternative a user is otherwise pushed to (hand-running
        `tofu plan` in `.odin/<env>/tf`, whose main.tf is deliberately
        portable and therefore endpoint-less) reaches REAL AWS.

        Read-only, unlike `/tf/apply`: no admission control (nothing is
        provisioned), no `wiring.stage`, no Stack commit -- the only writes
        are the regenerated `main.tf`/`override.tf` (so the plan is against
        the CURRENT canvas, which is what makes drift meaningful) and tofu's
        own refresh, which it does not persist.

        `exit_code` rides through verbatim so a CI gate can use tofu's own
        contract: 0 no changes, 2 changes present, anything else an error.

        COVERAGE, and what it describes (v0.7.4). `unsupported` comes from the
        very Stack this plan runs on, so it is exact. `skipped` cannot: a
        canvas node whose KIND odin has no model for never became a Stack
        resource at all, so the SAVED CANVAS is the only place it still exists
        -- and the saved canvas is one global file (`.odin/canvas.json`) while
        a Stack is per-env, so it is not necessarily what this env last
        applied. Rather than quietly describe a different thing than the plan
        (v0.7.3's bug: the CLI fetched the canvas and unioned it in with no
        check at all), the two are compared here and `canvas_drift` + `note`
        say so, in the payload and in the CLI's own output, whenever they
        differ."""
        stack = store.get_stack(env)
        canvas = _saved_canvas(canvas_path)
        skipped = skipped_node_types(canvas)
        canvas_drift = canvas_to_stack(canvas, env=env) != stack
        project = generate_tf(stack)
        access_key, secret_key = _issue_operator(env)
        try:
            result = await runner.plan(
                env, project, gateway_port(), access_key, secret_key, secrets=stack.sensitive_values(),
            )
        except TofuNotInstalled:
            return JSONResponse(status_code=409, content=_TOFU_NOT_INSTALLED)
        except SimulateBusy as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        body = {
            "status": _PLAN_STATUS.get(result.exit_code, "failed"), "env": env,
            "exit_code": result.exit_code, "unsupported": project.unsupported, "tail": list(result.tail),
            "skipped": skipped, "not_covered": not_covered(skipped, project.unsupported),
            "canvas_drift": canvas_drift,
        }
        if canvas_drift:
            body["note"] = (
                f"the saved canvas is not what env {env!r} last applied — this plan covers the "
                "last-applied Stack (so `unsupported` describes the plan), while `skipped` "
                "describes the saved canvas. Apply to make them the same."
            )
        return JSONResponse(status_code=200 if result.ok else 500, content=body)

    @router.post("/tf/destroy")
    async def tf_destroy(env: str = ENV) -> JSONResponse:
        access_key, secret_key = _issue_operator(env)
        secrets = store.get_stack(env).sensitive_values()
        try:
            result = await runner.destroy(env, gateway_port(), access_key, secret_key, secrets=secrets)
        except TofuNotInstalled:
            return JSONResponse(status_code=409, content=_TOFU_NOT_INSTALLED)
        except SimulateBusy as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        body = {"status": "destroyed" if result.ok else "failed", "env": env, "exit_code": result.exit_code}
        if not result.ok:
            body["tail"] = list(result.tail)
        return JSONResponse(status_code=200 if result.ok else 500, content=body)

    @router.get("/tf/status")
    def tf_status(env: str = ENV) -> dict:
        return runner.status(env)

    @router.post("/translate")
    async def translate_route(graph: CanvasGraph | None = None, env: str = ENV) -> dict:
        """S3b: the canvas -> TF review pass, for the UI to show before
        Apply runs it (`translate` is always best-effort; see its own
        docstring for the fallback chain -- this route never fails).

        `graph`, when given, is the CURRENT (unsaved) canvas -- the same
        payload /apply-full takes -- so the preview matches what Apply would
        actually run instead of lagging behind to the last-applied Stack.
        Omitting it keeps the original API-compat behavior (the stored
        Stack)."""
        stack = canvas_to_stack(graph.model_dump(), env=env) if graph is not None else store.get_stack(env)
        result = await translate_mod.translate(stack, cache=translate_cache)
        # Release finding #1: strip the (non-JSON-serializable) lambda zip bytes
        # -- the code panel needs only the .tf text + notes/unsupported/refined.
        return result.for_display()

    @router.post("/import-tf")
    async def import_tf_route(body: ImportTfRequest, env: str = ENV) -> dict:
        """S4: TF -> canvas, the reverse direction. `source="hcl"` parses the
        given text deterministically; `source="live"` resolves `resources`
        against the env's real backings through the gateway (operator creds,
        same as /tf/apply)."""
        if body.source == "hcl":
            result = import_tf_mod.parse_hcl_text(body.hcl)
        else:
            resources = [import_tf_mod.LiveResource(type=r["type"], id=r["id"]) for r in body.resources]
            access_key, secret_key = _issue_operator(env)
            result = await import_tf_mod.import_live(resources, gateway_port(), access_key, secret_key)
        return result.model_dump()

    return router


_SUPERSEDED = {"error": "superseded by a newer teardown/apply"}


def _unhealthy_wire(kind: str, node: str, observed: str, reason: str | None) -> dict:
    """One post-apply fault, in ONE wire shape across kinds. Lambda calls its
    field `State` and RDS calls its `DBInstanceStatus` -- each model keeps its
    own AWS vocabulary internally (`lambdactl.FunctionFault`,
    `rdsctl.DatabaseFault`), and this is where they become one list a client
    can read without a per-kind branch."""
    return {"kind": kind, "node": node, "observed": observed, "reason": reason}


def _unhealthy_line(item: dict) -> str:
    reason = f" ({item['reason']})" if item["reason"] else ""
    return f"{item['kind']} {item['node']} is {item['observed']}{reason}"


def create_apply_full_router(
    store: SpecStore, reconciler_for, runner: TfRunner, keystore: KeyStore, gateway_port, env_epoch: dict[str, int],
    translate_cache: translate_mod.TranslateCache, runtime, stores: SynthStores,
) -> APIRouter:
    """S5 -- the UI's single Apply button: /apply's exact canvas->Stack->tick
    semantics, then translate (S3b) and, when the canvas has TF-supported
    resources, `tofu apply` through the gateway (S2). Every non-busy outcome
    is a 200 with an honest per-half status -- the reconciler half can
    genuinely succeed while tofu fails ("applied_tf_failed"), and BOTH halves
    can succeed while a service is still short of its desired task count
    ("applied_services_unhealthy", field test 3) or while a lambda/rds node
    this apply tried to converge never came up ("applied_resources_unhealthy");
    409 only when a tofu run is already in flight for the env. Only `applied`
    is a clean apply; every other status is a nonzero exit in
    `cli/apply.py`."""
    router = APIRouter()

    @router.post("/apply-full")
    async def apply_full(graph: CanvasGraph, env: str = ENV, allow_destroying_uncovered: bool = False) -> JSONResponse:
        # Busy guard BEFORE any mutation (mirrors SimulateBusy's own message):
        # no reconcile, no store write while a tofu run holds the env's lock.
        if runner.status(env)["running"]:
            return JSONResponse(
                status_code=409,
                content={"error": f"a tofu run is already in progress for env {env!r}"},
            )
        canvas = graph.model_dump()
        stack = canvas_to_stack(canvas, env=env)

        # Field test 5 (HIGHEST): refuse an apply that would silently DESTROY a
        # resource this env really has, because the node describing it stopped
        # being coverable (a typo'd `type`, a field value that makes its builder
        # decline). See `_uncovered_destroys` for the removed-versus-uncovered
        # distinction, which is the whole of it.
        #
        # FIRST, and before `_bump_epoch` in particular: this refusal changes
        # nothing, so it must not supersede an in-flight apply on its way out.
        # The coverage set comes from `generate_tf` rather than the `translate`
        # call below because it must be known before the epoch is read, and the
        # two are the same set by construction: `TranslateResult` carries the
        # SKELETON's `unsupported` on both its paths, and the agent-refinement
        # guardrail rejects any output whose resource set differs from the
        # skeleton's, so the refined files it eventually applies build exactly
        # these nodes. Deterministic and local -- no agent call, no I/O.
        skeleton = generate_tf(stack)
        uncovered = _uncovered_destroys(
            drawn_node_types(canvas), _existing_nodes(store, env), _covered_nodes(skeleton.files),
            {r.id: r.kind for r in stack.resources}, skeleton.unsupported,
        )
        if uncovered and not allow_destroying_uncovered:
            return _uncovered_rejection(uncovered, env)

        # Owner directive B1: reject BEFORE ensure_backings/translate/tofu
        # ever touch a container or VM, not after 20 of them have already
        # started thrashing the host.
        rejection = await _admission_rejection(runtime, store, stack)
        if rejection is not None:
            return rejection

        # Release finding #4: an empty-canvas apply IS a teardown (see the
        # hold() block below) -- it must invalidate any older, still-in-flight
        # apply-full for a non-empty canvas the same way /destroy does, or
        # that stale request's own store.apply() re-creates what this
        # teardown just removed once it finally completes. A non-empty apply
        # just captures the current epoch -- it doesn't own a bump itself.
        my_epoch = _bump_epoch(env_epoch, env) if not stack.resources else env_epoch.get(env, 0)

        translated = await translate_mod.translate(stack, cache=translate_cache)
        skipped = skipped_node_types(canvas)
        body = {
            "status": "applied", "rev": None, "env": env,
            "skipped": skipped,
            "refined": translated.refined, "unsupported": translated.unsupported,
            # The ONE array a CI gate should read -- see `not_covered`'s own
            # docstring for the green-while-dropping-nodes trap it closes.
            "not_covered": not_covered(skipped, translated.unsupported),
            "tf": None,
        }

        # Three phases, in this exact order (load-bearing -- root-caused
        # against real backings, S5 night-freeze e2e failure): (1) ENSURE the
        # backing containers this Stack needs are up and the gateway has a
        # route to them, WITHOUT creating any resource yet (`ensure_backing`
        # only boots the container -- see Reconciler.ensure_backings) --
        # a never-before-applied env has no registered backing_port, so
        # skipping this makes the gateway 503 every forward and tofu's own
        # retry/backoff turn that into a long opaque hang rather than a fast
        # failure. (2) tofu AUTHORS the actual resources through the
        # now-routable gateway. (3) ONLY THEN does `store.apply(stack)` make
        # this Stack the env's desired state -- which is also the ONLY
        # signal the reconciler's OWN background loop (already running,
        # ticking every `poll_interval` seconds independent of this request
        # -- see Reconciler._run/start, started by reconciler_for()) uses to
        # decide there's work to do. Committing the store any earlier (the
        # original bug: right after canvas_to_stack, before ensure_backings/
        # tofu even started) let that background tick observe the new desired
        # s3/sqs/sns/dynamodb resources and provision them ITSELF, via
        # BackingAws.provision() -- concurrently with, and typically faster
        # than, tofu's own multi-phase init+plan+apply. Tofu's AWS-provider
        # creates are NOT idempotent the way SQS/SNS's happen to be, so tofu
        # then lost the race for real: "BucketAlreadyExists" /
        # "ResourceInUseException" on S3/DynamoDB, and an SQS queue stuck
        # forever waiting for attributes (tags) the reconciler's bare-create
        # never set. translate()/ensure_backings/tofu all operate on the
        # in-memory `stack`, never the store, so deferring the commit changes
        # nothing about what they see -- it only delays when the desired
        # state becomes visible to the env's independent background loop.
        # The final `reconciler.tick()` below (same request) then converges
        # rds (untouched by tofu either way) and observes what tofu already
        # created into World -- BackingAws.provision() tolerating an
        # already-exists conflict is what makes that safe.
        reconciler = await reconciler_for(env)
        # hold(): the background loop must not tick during ensure/tofu/commit.
        # A tick still sees the OLD stack (empty on a fresh env) and its gc
        # stops the very backing containers ensure_backings is booting — the
        # S5 e2e "rustfs never became ready with empty logs" failure. The
        # store commit stays INSIDE the hold so no tick can ever run between
        # tofu's creates and the new desired state becoming visible.
        async with reconciler.hold():
            # The gate is "any TF-supported resource NOW, or tofu already
            # manages something for this env" -- not resource_set(translated.
            # files) alone. V1 cross-layer e2e finding: vpc/subnet/sg have NO
            # reconciler-driven teardown path at all (plan.py NoOps them
            # forever -- they're never even entered into World, so the
            # "observed but no longer desired" prune in plan() can never see
            # them either); tofu is the ONLY thing that can ever remove them.
            # An empty canvas has an empty resource_set, so without the
            # workspace_exists half a prior VPC/Subnet/SG stayed orphaned in
            # ec2net.json AND in tofu's own state file forever -- the
            # "empty canvas + Apply = full teardown" NORTHSTAR promise broke
            # silently for this whole resource family. Safe to broaden for
            # every kind (not just ec2net's): running an empty-project tofu
            # apply is a no-op destroy against tofu's own state, ordered
            # entirely inside this same hold() before the reconciler's own
            # prune step (below, via the trailing tick()) ever runs, so it
            # never races a container-deprovision teardown for s3/sqs/sns/
            # dynamodb -- it only makes tofu's state stop lying about what
            # still exists.
            # Release finding #2: a tofu run that actually FAILED must never
            # become the env's new desired state. store.apply(stack)
            # unconditionally used to run here regardless of tf's outcome --
            # the reconciler's own next background tick then saw that new
            # desired state and provisioned the same s3/sqs/... backings
            # ITSELF (BackingAws.provision, non-idempotent the way tofu's
            # AWS-provider creates are), so a user's very next retry lost the
            # race against its own prior failure: BucketAlreadyExists /
            # ResourceInUseException. `tofu not installed` is NOT this case
            # (tofu never ran -- nothing to collide with -- and the
            # reconciler half committing is the pre-existing, desired
            # behavior; see test_no_tofu_installed_reports_tf_unavailable).
            tf_failed = False
            if resource_set(translated.files) or runner.status(env)["workspace_exists"]:
                # Finding #4, checkpoint 1: a newer teardown/apply may have
                # already landed while translate() (a claude-agent-sdk call,
                # genuinely slow) was running -- catch it before tofu starts.
                if env_epoch.get(env, 0) != my_epoch:
                    return JSONResponse(status_code=409, content=_SUPERSEDED)
                await reconciler.ensure_backings(stack)
                # Canvas wiring (field test 2, the product hole): publish the
                # authored `env`/refs where the GATEWAY can read them DURING
                # this tofu run -- CreateService/CreateFunction launch the real
                # container that consumes them, and `store.apply(stack)` below
                # deliberately does not happen until tofu has succeeded. See
                # `gateway/wiring.py::stage`.
                wiring.stage(stores, env, stack)
                project = TfProject(
                    files=translated.files, unsupported=translated.unsupported,
                    binary_files=translated.binary_files,
                )
                access_key, secret_key = keystore.issue(env, OPERATOR_NODE_ID)
                try:
                    result = await runner.apply(
                        env, project, gateway_port(), access_key, secret_key, secrets=stack.sensitive_values(),
                    )
                except TofuNotInstalled:
                    # Not a request-level error: the reconciler half still applies below.
                    body["tf"] = {"status": "unavailable", "exit_code": None, **_TOFU_NOT_INSTALLED}
                    body["status"] = "applied_tf_failed"
                except SimulateBusy as exc:  # a second call won the race after our guard passed
                    return JSONResponse(status_code=409, content={"error": str(exc)})
                else:
                    body["tf"] = {"status": "ok" if result.ok else "failed", "exit_code": result.exit_code}
                    if not result.ok:
                        body["tf"]["tail"] = list(result.tail)
                        body["status"] = "applied_tf_failed"
                        tf_failed = True

            # Finding #4, checkpoint 2: the epoch can also change WHILE tofu
            # itself was running (a slow apply racing a fast concurrent
            # /destroy) -- re-check right before the commit that makes this
            # request's Stack live.
            if env_epoch.get(env, 0) != my_epoch:
                return JSONResponse(status_code=409, content=_SUPERSEDED)
            if tf_failed:
                body["note"] = "desired state not committed; fix and re-apply"
            else:
                body["rev"] = store.apply(stack)  # the desired state goes live before any tick can run
        # W2.2: an Apply is also the recovery for drift the reality sweep
        # reported. An ECS task is not a TF resource -- nothing in an
        # `aws_ecs_service`'s config changes when its container is destroyed
        # out of band, so tofu's plan is empty and tofu will never fix it (in
        # real AWS the service SCHEDULER, not terraform, replaces a lost
        # task). This is odin's equivalent, triggered by the user's Apply
        # rather than a background timer, and idempotent: a service already at
        # desiredCount launches nothing.
        # A bare `TaskRuntime()` (not this app's `runtime`) deliberately: it
        # must be the SAME substrate that launched these containers, and
        # ecsctl's own `runtime or TaskRuntime()` default is what did.
        converging = ecsctl.converge_services(stores, env, TaskRuntime(), keystore, gateway_port())
        # The same recovery for lambda, and for the same reason: a function's
        # RIE container is its EXECUTION ENVIRONMENT, not a TF resource -- an
        # `aws_lambda_function`'s config doesn't change when its container is
        # destroyed out of band (and the provider has no state attribute to
        # diff on), so tofu's plan is empty forever. Real Lambda's own control
        # plane replaces a dead sandbox; this is odin's equivalent. Idempotent:
        # only a `Failed` function is re-`ensure`d, an Active one is untouched.
        deploying = lambdactl.converge_functions(stores, env, keystore=keystore, gateway_port=gateway_port())
        # W2.7: and the same recovery for rds. A Postgres container is odin's
        # execution substrate for a resource whose terraform config is
        # unchanged (`status` is read-only Computed in the provider's schema),
        # so tofu's plan is empty and only this can bring a killed database
        # back. Idempotent: an `available` instance is untouched, a `failed`
        # one is re-created and re-`pg_ready`-gated. This is what makes the
        # scenario-2 crash/recover behavior survive the move off the
        # reconciler -- see reconcile/drift.py's rds notes.
        booting = rdsctl.converge_db_instances(stores, env)
        # W2.6/field test 2 HIGH-1: push every RUNNING EC2 VM's CURRENT
        # security groups into its already-booted VM. An SG edit reached the
        # gateway and the newly-created VMs but never the already-running ones,
        # so one drawn group enforced two different firewalls on the wire.
        # Idempotent and cheap: an instance whose compiled rules and membership
        # are unchanged is one local file comparison -- no `limactl`, no
        # signal. See ec2compute.ensure_instance_mesh.
        #
        # BEFORE the database pass, and that order is load-bearing (field test
        # 4). A revoke closes an ALREADY-OPEN flow by making the ADMITTING
        # member re-check it against the peer's CURRENT certificate -- so the
        # peer (the VM) has to be holding its new certificate before the
        # admitter (usually the database) reloads. This pass re-signs the VM,
        # restarts its daemon and pokes it into re-handshaking with every peer,
        # all synchronously, so by the time it returns the database is looking
        # at the new identity.
        await asyncio.to_thread(ec2compute.ensure_instance_mesh, stores, env)
        # ...then push each live database's SG-compiled firewall into its mesh
        # sidecar. An apply is exactly the right cadence -- security groups are
        # TF-owned, so an edited `db-sg` only reaches the gateway here. Also
        # heals a sidecar that was killed under a still-running database, and
        # carries the membership revision that closes the flows above. See
        # rdsctl.ensure_db_mesh.
        rdsctl.ensure_db_mesh(stores, env)
        # Field test 3 (HIGH): an Apply may not report success while a service
        # is short of its desired task count. tofu's own `wait_for_steady_state`
        # only runs when tofu UPDATES the service, so every apply tofu sees as a
        # NO-OP -- a re-apply on an already-broken service, or an edit that only
        # touches the launch-time `env` map -- reported `applied / tf: ok` at
        # 0-of-3 tasks. Nothing tofu-side can close that (tofu has nothing to
        # do), so this is odin's own post-apply verification, placed LAST so the
        # convergence above overlaps every other recovery pass and a healthy env
        # costs one store read. Only when everything else went clean: a tofu
        # failure has already failed this apply, and adding a second wait to it
        # would only make an already-honest failure slower. Off the event loop:
        # `wait_for_steady_services` joins real threads and sleeps.
        if body["status"] == "applied":
            shortfalls = await asyncio.to_thread(
                ecsctl.wait_for_steady_services, stores, env, TaskRuntime(), converging,
            )
            if shortfalls:
                body["status"] = "applied_services_unhealthy"
                body["unhealthy"] = [s._asdict() for s in shortfalls]
                body["note"] = (
                    "desired state committed, but the service(s) above are not running "
                    "their desired task count — fix and re-apply"
                )
        # ...and the identical verification for the OTHER two kinds with the
        # same fire-and-verify-later shape. `converge_functions` and
        # `converge_db_instances` above each START real work and return, so
        # without this an apply reported `applied` the instant a redeploy was
        # spawned -- field test 3's exact bug, in the two places its fix didn't
        # reach. Concurrently, because they are independent waits and a slow
        # lambda pull should not be charged on top of a slow database boot; both
        # return after one store read when nothing is coming up, so a healthy
        # apply pays approximately nothing. Same `if applied` gate, for the same
        # reason: a tofu failure has already failed this apply honestly.
        if body["status"] == "applied":
            await asyncio.gather(
                asyncio.to_thread(lambdactl.wait_for_active_functions, stores, env, deploying),
                asyncio.to_thread(rdsctl.wait_for_available_instances, stores, env, booting),
            )
            # ...and THEN ask reality, once, before believing any of it (field
            # test 5). The two waits above settle the convergence and answer
            # off the RECORD, and a record is refreshed by the drift sweep on a
            # ~10-tick cadence: measured at the default cadence, four
            # consecutive applies reported `applied` / exit 0 over ~8s with the
            # function's container already removed, and none of them recreated
            # it. `sweep_compute` is one bulk `docker ps` (none at all for an
            # env with no lambda/rds records) that corrects every record whose
            # container is gone, exited or paused -- so this apply establishes
            # liveness ITSELF instead of inheriting another loop's cadence.
            #
            # AFTER the waits, never before: a container being (re)created by
            # this very apply is legitimately absent for a moment, and the
            # honest answer is the one taken once the work it verifies has
            # finished. `tf_status.project()` calls the SAME function, so
            # `/world` and this apply read one corrected record rather than two
            # checks that can disagree.
            await asyncio.to_thread(drift.sweep_compute, stores, env)
            faulted_fns = lambdactl.function_faults(stores, env)
            faulted_dbs = rdsctl.db_faults(stores, env)
            unhealthy = (
                [_unhealthy_wire("lambda", f.node, f.state, f.reason) for f in faulted_fns]
                + [_unhealthy_wire("rds", f.node, f.status, f.reason) for f in faulted_dbs]
            )
            if unhealthy:
                body["status"] = "applied_resources_unhealthy"
                body["unhealthy_resources"] = unhealthy
                # Named in the NOTE as well as the structured list: `odin apply`
                # echoes `note` verbatim, so an operator sees WHICH resource and
                # WHY without having to read the JSON body.
                body["note"] = (
                    "desired state committed, but "
                    + "; ".join(map(_unhealthy_line, unhealthy))
                    + " — fix and re-apply"
                )
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        return JSONResponse(status_code=200, content=body)

    return router


# How often a live server re-checks that its own store lock is still reachable
# by path. One `stat` per second; the window it bounds is how long odin can
# lie about whether a server is up after something deletes `.odin/lock`.
LOCK_WATCH_INTERVAL = 1.0


async def _keep_store_lock(lock: StoreLock, interval: float = LOCK_WATCH_INTERVAL) -> None:
    """Put the store lock FILE back if anything deletes it under this server.

    Field test 4: `rm -rf .odin` releases no lock (flock lives on the inode)
    but makes it unreachable by path, so `odin status` said "not running"
    while the server was still serving, `odin import` restored into the live
    store, and a SECOND server was started on it. `StoreLock.reassert` is the
    repair; this is the only thing that has to run for it to happen. A warning
    every interval is the correct volume for the one case it cannot repair --
    another process already holding the file that replaced ours means two
    servers really are on this store.
    """
    while True:
        await asyncio.sleep(interval)
        if not await asyncio.to_thread(lock.reassert):
            log.warning(
                "the store lock file was gone or was not ours -- re-established it. Something "
                "deleted %s under a live server (`odin clean --all`, `rm -rf`); until this ran, "
                "`odin status` reported no server and `odin import` would have restored into a "
                "live store.", lock.root / STORE_LOCK_NAME,
            )


async def _reap_orphaned_ec2_vms(root: Path, envs: list[str], stores: SynthStores) -> None:
    """Best-effort (release finding #4): `limactl` being unavailable, or
    any other reaper failure, must never block server startup -- this is a
    one-shot cleanup pass, not something reconciling depends on. Runs off
    the event loop thread (`limactl list`/`delete` are blocking subprocess
    calls that can take real wall-clock time for however many VMs exist)."""
    try:
        reaped = await asyncio.to_thread(ec2compute.reap_orphaned_vms, root, envs)
        if reaped:
            log.warning("startup reaper deleted %d orphaned EC2 VM(s): %s", len(reaped), reaped)
        # Field test 3 HIGH-B: the reaper above builds its "expected" set from
        # the gateway store, so an interrupted apply -- which leaves VMs
        # Running and tofu's state empty -- is exactly the case it spares. The
        # second witness is tofu's own state; anything the store claims and
        # the state has forgotten is unreachable by terraform forever.
        forgotten = await asyncio.to_thread(ec2compute.reclaim_tf_forgotten_vms, stores, envs)
        if forgotten:
            log.warning(
                "startup reclaimed %d EC2 VM(s) tofu's state no longer knew about: %s", len(forgotten), forgotten,
            )
        # Field test 3 HIGH-A: and the lighthouse PROCESSES of envs that were
        # destroyed before teardown learned to stop them -- each one still
        # holding a port out of the 4342-4441 pool.
        lighthouses = await asyncio.to_thread(reap_orphaned_lighthouses, root)
        if lighthouses:
            log.warning("startup reaper stopped %d orphaned nebula lighthouse(s): %s", len(lighthouses), lighthouses)
    except Exception:
        log.exception("startup EC2 VM reaper failed (continuing without it)")


def create_app(
    runtime=None,
    store: SpecStore | None = None,
    rds=None,
    aws=None,
    backings: bool = True,
    gateway_port: int | None = None,
    reap_ec2_vms: bool | None = None,
) -> FastAPI:
    _runtime = runtime or ColimaRuntime()
    _store = store or SpecStore(ODIN_DIR)
    # The startup EC2-VM reaper (release finding #4) cross-references
    # REAL, machine-global `limactl` VMs against this app's OWN store --
    # unsafe to run against anything but the one true `.odin` tree (a
    # second store, e.g. a test's own `tmp_path`, has no way to know about
    # VMs that legitimately belong to a DIFFERENT store/process on the same
    # machine, and would reap them as "orphaned"). Default it to "on" only
    # when `store` wasn't overridden -- i.e. only for the real production
    # app, never for a test or any other caller that brought its own store.
    _reap_ec2_vms = reap_ec2_vms if reap_ec2_vms is not None else store is None
    ws_manager = ConnectionManager(_store.root)
    _resolved_gateway_port = gateway_port if gateway_port is not None else int(os.environ.get(GATEWAY_PORT_ENV, DEFAULT_GATEWAY_PORT))

    # The gateway: workload SDK calls carry per-node creds and land here
    # (checked reverse proxy -> real backing), never the backing directly.
    # Stateless routing table + key registry, rebuilt every tick from
    # (Stack, issued keys) -- never a cache that outlives an Apply.
    gateway_state = GatewayState()
    gateway_keystore = KeyStore(_store.root)
    # The synthesized control-plane's tag/attribute/delete-marker stores
    # (gateway/synth.py) -- unlike gateway_state, this must OUTLIVE a tick.
    gateway_stores = SynthStores(_store.root)
    # port=0 (the test default) resolves to an ephemeral port; lifespan fills
    # this in with the ACTUAL bound port before any reconciler is made, so
    # BackingAws/`/health` never advertise the possibly-0 request instead.
    gateway_port_actual: int | None = None
    # Simulate (S2): materializes .odin/{env}/tf/ and drives tofu through the
    # gateway above under the OPERATOR principal. No lifespan hook of its own
    # (unlike reconcilers) -- routes only ever run once lifespan has resolved
    # gateway_port_actual, so a plain closure over it is enough.
    tf_runner = TfRunner(_store.root, ws_manager)
    # Release finding #4: a per-env generation counter /destroy and an
    # empty-canvas /apply-full bump -- see _bump_epoch's own docstring.
    env_epoch: dict[str, int] = {}
    # Release finding #5: shared across every /translate and /apply-full call
    # for the app's lifetime -- see TranslateCache's own docstring. It both
    # caches successful refinements per canvas-revision AND owns the background
    # refine tasks, so no request ever blocks on the (slow) claude-agent-sdk
    # pass; a later same-revision call serves the refined output once ready.
    translate_cache = translate_mod.TranslateCache()

    # One reconciler per environment, created lazily. Each gets its own
    # env-scoped backing containers, so AWS state stays isolated. (The rds
    # substrate is no longer one of them -- W2.7 moved it to the gateway, whose
    # own model builds one per env; see the `rds=` argument to
    # create_gateway_app below.)
    reconcilers: dict[str, Reconciler] = {}

    def _make_reconciler(env: str) -> Reconciler:
        # W2.6: the env's backing containers join its Nebula overlay through a
        # sidecar (`fabric/sidecar.py`). The sidecar's root is the STORE root,
        # since that's where the env's Nebula CA/overlay actually live
        # (`ensure_network(stores.root, ...)` in the gateway's VPC model) --
        # injected rather than defaulted so `BackingAws._root` keeps its own
        # meaning (the goaws config mount, deliberately CWD-relative)
        # untouched. The rds substrate joins the SAME mesh, but it isn't built
        # here any more (W2.7): `rdsctl` builds it per request off
        # `stores.root`, which is that same directory.
        env_aws = aws or (BackingAws(
            _runtime, env, gateway_port=gateway_port_actual,
            mesh=MeshSidecar(_runtime, env, _store.root),
        ) if backings else None)
        return Reconciler(
            _store, _runtime, aws=env_aws, gateway=gateway_state, fabric=LocalhostFabric(),
            ws=ws_manager, env=env, poll_interval=1.0, stores=gateway_stores,
            # W2.2's reality sweep shells out to the REAL `limactl`/`docker`,
            # so it's gated on the same `backings` flag every other real-
            # runtime dependency is: an app built with `backings=False` is
            # explicitly the fake-substrate one (every non-integration test),
            # and its hand-seeded synth records must not be measured against
            # this machine's actual VMs/containers.
            drift=DriftSweeper() if backings else None,
        )

    async def reconciler_for(env: str) -> Reconciler:
        if env not in reconcilers:
            reconcilers[env] = _make_reconciler(env)
            await reconcilers[env].start()
        return reconcilers[env]

    async def on_deny(principal: Principal | None, action: str | None, resource: str | None, reason: str) -> None:
        await ws_manager.broadcast({
            "type": "access_denied",
            "env": principal.env if principal else "default",
            "resource_id": principal.node_id if principal else None,
            "action": action,
            "target": resource,
            "reason": reason,
        })

    async def on_backing_unavailable(
        principal: Principal | None, action: str | None, resource: str | None, service: str,
    ) -> None:
        """Field test 2, finding B6: a DOWN backing gets its own event type. It
        is a service-unavailable condition, not an authorization verdict (the
        policy check has already passed), and mixing it into `access_denied`
        polluted the exact stream a security review reads for real denials --
        agent A watched thousands of them accumulate during a wedged destroy.
        `recovery` names what actually fixes it, since a down backing always has
        the same fix."""
        await ws_manager.broadcast({
            "type": "backing_unavailable",
            "env": principal.env if principal else "default",
            "resource_id": principal.node_id if principal else None,
            "action": action,
            "target": resource,
            "service": service,
            "recovery": f"no {service} backing container is running for this env -- run Apply (or `odin apply --env {principal.env if principal else 'default'}`) to start it",
        })

    gateway_app = create_gateway_app(
        gateway_state, gateway_keystore, gateway_stores, on_deny,
        gateway_port=lambda: gateway_port_actual,
        on_unavailable=on_backing_unavailable,
        # W2.7: `rds` used to be the RECONCILER's Postgres provisioner; it's
        # now the gateway's RDS-model substrate, because `aws_db_instance` is
        # what creates a database today. A caller's stand-in (every api test)
        # lands here; None (production) lets rdsctl build a per-env real
        # `PostgresRds` from the request's own env.
        rds=rds,
    )
    gateway_server = None
    gateway_thread = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal gateway_server, gateway_thread, gateway_port_actual
        # The gateway listener starts FIRST: reconcilers (built below, for
        # envs resumed on restart) need the ACTUAL resolved port to point
        # BackingAws's goaws.yaml at.
        gateway_server, gateway_thread, gateway_port_actual = serve_in_thread(gateway_app, port=_resolved_gateway_port)
        # The one piece of evidence that proves THIS store has a live server, to
        # anyone who asks the kernel rather than `ps`: `odin status`/`stop` and
        # `odin import`'s live-store refusal. Held for the whole run; released
        # below AFTER the reconcilers stop, so the store is only advertised free
        # once nothing is writing to it. (Never a reason to fail startup: an
        # unlockable store answers "free", see util._flock.)
        store_lock = hold_store_lock(_store.root)
        # ...and a watchdog that puts the lock FILE back if anything deletes it
        # (field test 4 -- see `_keep_store_lock`). The lock itself survives the
        # deletion; only the evidence odin can find by path does not.
        lock_watch = asyncio.create_task(_keep_store_lock(store_lock))
        envs = _store.list_envs()
        if _reap_ec2_vms:
            await _reap_orphaned_ec2_vms(_store.root, envs, gateway_stores)
        for env in envs:  # resume reconciling existing environments
            await reconciler_for(env)
        try:
            yield
        finally:
            lock_watch.cancel()
            for reconciler in reconcilers.values():
                await reconciler.stop()
            stop_in_thread(gateway_server, gateway_thread)
            store_lock.release()

    app = FastAPI(title="odin", version=odin_version(), lifespan=lifespan)
    app.middleware("http")(_csrf_guard)
    # The saved canvas belongs to the STORE, not to the process's cwd: in
    # production `_store.root` IS `.odin`, so this is the same
    # `.odin/canvas.json` `odin backup`'s archive already resolves as
    # `root / CANVAS_NAME` -- but a caller that brought its own store (every
    # test) now reads and writes its OWN canvas instead of the real one under
    # the checkout. The module constant stays as the documented location.
    canvas_path = _store.root / CANVAS_NAME
    app.include_router(create_canvas_router(canvas_path))
    app.include_router(
        create_apply_router(
            _store, reconciler_for, gateway_keystore, tf_runner, lambda: gateway_port_actual, env_epoch,
            gateway_stores, gateway_state, _runtime,
        )
    )
    app.include_router(
        create_tf_router(
            _store, tf_runner, gateway_keystore, lambda: gateway_port_actual,
            translate_cache, _runtime, gateway_stores, canvas_path,
        )
    )
    app.include_router(
        create_apply_full_router(
            _store, reconciler_for, tf_runner, gateway_keystore, lambda: gateway_port_actual, env_epoch,
            translate_cache, _runtime, gateway_stores,
        )
    )
    app.include_router(create_logs_router(_store, gateway_stores, _runtime))
    # W2.9/M8: "what's wrong here?" -- reads the same store/stores/runtime the
    # logs route does, plus the ws_manager's durable per-env event log.
    app.include_router(create_debug_router(_store, gateway_stores, _runtime, ws_manager))

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    @app.get("/events")
    def get_events(env: str = ENV):
        return ws_manager.get_events(env)

    @app.get("/health")
    def health():
        return {"ok": True, "gateway": {"port": gateway_port_actual}}

    app.state.store = _store
    app.state.runtime = _runtime
    app.state.ws_manager = ws_manager
    app.state.reconcilers = reconcilers
    app.state.gateway = gateway_state
    app.state.gateway_keys = gateway_keystore
    app.state.gateway_stores = gateway_stores
    app.state.tf_runner = tf_runner
    app.state.env_epoch = env_epoch
    app.state.translate_cache = translate_cache

    bundled_ui = Path(__file__).resolve().parent / "_ui"
    source_ui = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"
    ui_dist = bundled_ui if bundled_ui.exists() else source_ui
    if ui_dist.exists():
        @app.get("/")
        def serve_index():
            return FileResponse(ui_dist / "index.html")

        app.mount("/assets", StaticFiles(directory=str(ui_dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            static_file = ui_dist / full_path
            return FileResponse(static_file if static_file.is_file() else ui_dist / "index.html")

    return app
