"""odin FastAPI app factory.

The canvas authors a desired-state Stack; a continuous Reconciler drives reality
(per-env backing containers for the AWS-shaped resources, via Colima) and
projects what `tofu apply` created through the gateway (every TF-owned kind,
`rds` among them since W2.7); the World projects back to the canvas over
a Server-Sent Events stream.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from odin.agent import ai, chat
from odin.api.canvas import write_canvas
from odin.agent import import_tf as import_tf_mod
from odin.agent import translate as translate_mod
from odin.agent.hcl import TfProject, generate_tf, parse_tf, resource_set, unquote
from odin.api.canvas import CanvasGraph, create_canvas_router
from odin.api.debug import create_debug_router
from odin.api.logs import create_logs_router
from odin.api.events import SSE_HEADERS, ConnectionManager, event_stream
from odin.aws.backings import PROVISIONED, BackingAws, BackingUnavailable
from odin.aws.rds import volume_name as rds_volume_name
from odin.compute.tasks import TaskRuntime
from odin.fabric.localhost import LocalhostFabric
from odin.fabric.nebula import mesh_state, reap_orphaned_lighthouses
from odin.fabric.sidecar import MeshSidecar
from odin.gateway import DEFAULT_GATEWAY_PORT, GATEWAY_PORT_ENV, wiring
from odin.gateway.app import GatewayState, create_gateway_app, serve_on_loop
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore, Principal
from odin.gateway.models import ec2compute, ec2net, ecsctl, lambdactl, rdsctl
from odin.gateway.stores import SynthStores
from odin.reconcile import admission, drift
from odin.reconcile.dispatch import Dispatcher
from odin.reconcile.drift import DriftSweeper
from odin.reconcile.reconciler import LoopHealth, Reconciler
from odin.reconcile.tf_status import stranded_in_tf_state
from odin.runtime.colima import ColimaRuntime
from odin.simulate.runner import SimulateBusy, TfRunner, TofuNotInstalled
from odin.simulate.workspace import tf_dir
from odin.spec import store as store_mod
from odin.spec.models import Stack, World
from odin.spec.capacity import overcommitted
from odin.spec.store import SpecStore, StoreUnreadable
from odin.spec.translate import MODELLED_NODE_TYPES, canvas_to_stack, drawn_node_types, skipped_node_types
from odin.util import STORE_LOCK_NAME, StoreLock, atomic_write_text, hold_store_lock, odin_version

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
    """Whether `value` names a loopback host — i.e. whether this guard lets the
    request through.

    `urlparse` RAISES on a malformed authority, which was the cheapest 500 in
    odin: no credentials, no body, one header. Probed against this Python rather
    than assumed:

        urlparse('http://[::1').hostname        -> ValueError: Invalid IPv6 URL
        urlparse('http://a:b').hostname         -> 'a'
        urlparse('garbage').hostname            -> None

    It ran in a `BaseHTTPMiddleware` ahead of every route, so `curl -X POST -H
    'Origin: http://[::1' …/apply-full` reached no route at all and still
    answered `Internal Server Error`.

    An origin odin cannot PARSE is not one it can prove is loopback, so this
    answers False and the caller gets the same 403 every other cross-origin
    request gets — fail closed, and never a server error for what is entirely a
    malformed request."""
    try:
        return urlparse(value).hostname in _LOOPBACK_HOSTS
    except ValueError:  # a malformed authority is not provably loopback
        return False


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
    container/VM, never after. `ensure_host()` shells to `docker info`, which
    is a SUBPROCESS and therefore natively async (`run_command_async`): it is
    awaited, and nothing is hidden behind a thread. (v0.7.7: this used to say
    `asyncio.to_thread` kept it off the loop; there are zero `to_thread` call
    sites left in src, so that sentence described a mechanism that no longer
    exists.) Returns None when admitted, else the 409 JSONResponse
    the caller should return VERBATIM (named numbers, never a bare
    "rejected")."""
    host = await runtime.ensure_host()
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
    """Every managed resource tofu's own state still holds for `env` -- TOFU'S
    HALF of "what is still standing" after a destroy that didn't finish.

    Not the whole answer, and this docstring used to claim it was ("the
    authoritative answer"). tofu's state knows nothing about the s3/sqs/sns/
    dynamodb resources the reconciler authors, so a resource a partial destroy
    really deleted -- and the loop then re-created -- is in neither this list nor
    the container list. Field test 6, F2: that omission is why the failure report
    could under-describe an env. Always read with `_tf_state_readable`, which is
    what separates "holds nothing" from "odin could not tell"."""
    return sorted(f"{r['type']}.{r['name']}" for r in _managed_resources(root, env))


def _tf_state_readable(root: Path, env: str) -> bool:
    """False only when `terraform.tfstate` is THERE and did not parse -- i.e.
    when an empty `_tf_state_addresses` means "unknown", not "nothing".

    tofu rewrites that file in place, so a run killed mid-write leaves exactly
    this. `_tf_state` folds it into `{}` deliberately (a read must not 500), and
    folding it into an empty ANSWER is what let a failed destroy report
    `0 resource(s) []` and read like a success."""
    state = tf_dir(root, env) / "terraform.tfstate"
    return not (state.is_file() and state.read_text().strip() and not _tf_state(root, env))


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



def _capacity_rejection(problems: list[str], env: str) -> JSONResponse:
    """Refuse an apply that asks an instance to hold more than it has.

    Same shape as `_wiring_rejection`: a 409 before anything is built, naming
    what is wrong and what to do. The alternative is not "it works" -- it is
    containers OOM-killed inside a VM some minutes later, reported as task
    failures that say nothing about the instance being too small.
    """
    named = "; ".join(problems)
    return JSONResponse(
        {
            "status": "refused",
            "note": (
                f"refusing to apply: {len(problems)} instance(s) in env {env!r} are asked to hold "
                f"more than they have -- {named}"
            ),
            "capacity_problems": problems,
        },
        status_code=409,
    )


def _wiring_rejection(wiring_errors: list[str], env: str) -> JSONResponse:
    """A canvas wiring reference that can never be given a value.

    NOT a coverage problem, and deliberately not reported as one (field test 5,
    F5-8: it used to ride in `unsupported`, so `not_covered` -- the one field a
    CI gate reads -- failed under a COVERAGE label for a node odin builds fine).
    It is a user error, and it is fatal: the workload cannot start without that
    variable, so `gateway/wiring.py::_resolve` raises `UnresolvedRef` for it at
    launch and the apply fails anyway. Refusing here reaches the same verdict
    before any container is created, naming the node and the ref instead of a
    stopped task.

    THIS PREAMBLE SAYS NOTHING ABOUT THE CAUSE, on purpose. It used to assert
    that the refs "name a node that is not on the canvas" -- true of the only
    fault `_unwired_refs` could report when it was written, and a fresh lie the
    moment field test 6 added the second one (a ref against a node that IS on the
    canvas and is a kind nothing can reference). Each entry states its own
    reason; the wrapper only counts them."""
    named = "; ".join(wiring_errors)
    return JSONResponse(status_code=409, content={
        "error": (
            f"refusing to apply: {len(wiring_errors)} canvas wiring reference(s) in env {env!r} "
            f"can never be given a value, so the workload(s) below would fail to start. Each one "
            f"says why: {named}. Fix the reference(s) and re-apply. Nothing was changed."
        ),
        "wiring_errors": wiring_errors,
        "env": env,
    })


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


async def _surviving_containers(runtime, env: str) -> list[str] | None:
    """Odin containers this env still has, by odin's own container naming --
    `odin-aws-{backing}-{env}` carries the env as a SUFFIX, `odin-rds-{env}-…`
    / `odin-ecs-{env}-…` / `odin-lambda-{env}-…` as an INFIX, both anchored on
    `-` so a longer env sharing this one's prefix never matches (the rule
    tests/containers.py documents and relies on).

    Best-effort by design, and `reconcile/drift.py::_listing`'s exact
    reasoning: this runs only when a destroy has ALREADY failed, so a docker
    daemon that won't answer must degrade to "couldn't tell" rather than
    replace a real failure report with a traceback.

    ...and "couldn't tell" is `None`, not `[]` (field test 6, F4's sibling).
    Returning `[]` for a docker daemon that would not answer put the SAME text
    in front of the user as a genuinely clean machine -- `0 container(s) []` --
    with the real reason in the server log only, in the one report whose whole
    job is naming what survived a failed teardown."""
    try:
        names = await runtime.container_names()
    except Exception as exc:  # noqa: BLE001 -- any CLI/parse failure means "unknown"
        log.warning("could not list containers while reporting a failed destroy (%s)", exc)
        return None
    return sorted(name for name in names if name.endswith(f"-{env}") or f"-{env}-" in name)


# --- the last line of defence: no odin route may answer with a non-JSON body ---
#
# Field test 6. Deleting a backing container while `/apply-full` was booting it
# (reproduced for real: `docker rm -f odin-aws-rustfs-<env>` racing the ensure
# phase) let `BackingUnavailable` out of the route unhandled, and Starlette
# answered `HTTP 500` with the five plain-text words `Internal Server Error`.
# The CLI's own honest fallback then printed
#
#     odin server returned HTTP 500 with a non-JSON body: Internal Server Error
#
# -- a traceback where a verdict belongs, naming neither the container that went
# missing nor anything the user could act on.
#
# Fixed as a SHAPE, not as one `except` in one route, because it was never one
# route: `reclaim_env_instances` (`ReclaimFailed`), `ensure_instance_mesh`
# (`MeshRefreshFailed`) and the trailing `reconciler.tick()` of all three apply
# routes could each do the same thing, and `/destroy`'s own comment already
# CLAIMED `ReclaimFailed` produced "500 with the VM names" when the VM names only
# ever reached the server log. This is `_DESTROY_STATUS`'s lesson applied one
# level up: the verdict is DERIVED from the exception type through a map, and an
# exception with no entry falls through to `_UNEXPECTED` -- which is a failure.
# A new way for a route to blow up is therefore reported as a failure, in JSON,
# naming the real exception, by default.
#
# What this deliberately does NOT do is say what was or was not committed. A
# `BackingUnavailable` from the ensure phase happens before any store write; the
# identical exception from the trailing `tick()` happens after it. Nothing here
# can tell those apart, so it names the witnesses to consult instead of guessing
# -- the same rule that makes `/destroy` report `still_standing` rather than a
# reassurance.


class _Verdict(BaseModel):
    model_config = {"frozen": True}
    status: str
    code: int
    advice: str


_BACKING_ADVICE = (
    "the backing container named above is not serving this env. Re-run Apply "
    "(`odin apply --env {env}`): odin re-creates a missing or stopped backing on every "
    "apply. Nothing in this response is a claim about what was applied before it failed "
    # Field test 6's advice sweep: `odin tf plan` used to be recommended here as
    # a witness. Under THIS condition it is a trap -- `/tf/plan` deliberately
    # does not ensure backings, so tofu's refresh reads go through the gateway to
    # a backing that is not there, get a real ServiceUnavailable, and aws-sdk-go-v2
    # retries each one ~25 times with silent backoff. That is the 8m26s
    # "hang with no progress" `simulate/runner.py::_WEDGED_DESTROY_HINT`
    # documents. `odin world` reads the store and answers instantly.
    "-- check `odin world --env {env}` before retrying (not `odin tf plan`: with a backing "
    "down, its refresh reads hang on the gateway's ServiceUnavailable retries for minutes)"
)
_UNEXPECTED_ADVICE = (
    "odin has no specific verdict for that failure, so this is reported as a plain "
    "FAILURE -- nothing here verified otherwise. The server log has the full traceback. "
    "Check `odin world --env {env}` and `odin tf plan --env {env}` for what env {env!r} "
    "actually holds before re-applying"
)

# --- a store file odin cannot read (field test 6, F5) ---
#
# The old behaviour: `world.json` overwritten with invalid UTF-8 fell through to
# `_UNEXPECTED_ADVICE`, which sent the user to `odin world` -- the command that
# reads that very file. Measured on a real server: `GET /world` answered 500
# with `UnicodeDecodeError ... position 57` and told the user to run `odin
# world`, which fails identically and recommends itself. A loop, and the message
# named no file, because `UnicodeDecodeError` carries no path.
#
# `spec/store.py::StoreUnreadable` now raises from the read site with the path
# and the file's ROLE, and the recovery is looked up from the role -- the
# `_DESTROY_STATUS` shape, so a role nobody mapped states that instead of
# formatting an empty instruction. Both recoveries below are measured or read
# out of source, not guessed:
#   * `rm world.json` -- on a 4-resource env, all four were back `healthy` with
#     byte-identical facts ONE tick later, `consecutive_failures` 20 -> 0.
#   * `odin events` survives, because `events.jsonl` is a separate append-only
#     file (api/ws.py), and it holds the last World odin observed.
#   * `odin tf plan` survives a corrupt `world.json` because it reads HEAD +
#     `stacks/<rev>.json` + the tofu workspace and never calls `current_world`
#     -- and for that same reason it is NOT offered for a corrupt DESIRED file.
_STORE_RECOVERY = {
    store_mod.CACHE: (
        "that file is odin's OBSERVED-state cache, not your desired state -- delete it "
        "(`rm {path}`) and this env's reconciler rebuilds it from the real containers on its next "
        "tick (measured: a 4-resource env came back complete, with identical facts, one tick "
        "later). `odin tf plan --env {env}` works right now and reads none of it"
    ),
    # Honesty rule 3, and the sharpest form of it yet: this text used to end
    # "...otherwise `odin destroy --env {env}` tears the env down through the
    # records that still parse, and a fresh Apply rebuilds it". Every clause of
    # that was wrong, and MEASURED wrong on a real server (:5250, a real
    # `gateway/lambdactl.json` holding one bad record):
    #
    #   POST /destroy?env=p1bctl  ->  500  {"status": "store_unreadable", ...}
    #
    # There is no "records that still parse" any more -- `_load` runs
    # `records.validate` over the WHOLE file, so one bad record refuses all of
    # it (and a decode error always did). And `destroy` does not survive that:
    # `reclaim_env_instances` reads this very store, so the route raises before
    # it tears anything down. Worst of all, the advice was printed BY the
    # failing destroy, so `odin destroy` answered its own failure by
    # recommending `odin destroy` -- the exact self-referential loop the
    # comment above this dict says it fixed for `odin world`, reintroduced one
    # role over.
    #
    # What replaces it is verified the same way: repairing the one named record
    # and re-running the same POST answered 200, with no restart -- the store
    # does not cache a failed load (`JsonStore._data` assigns only on success).
    store_mod.CONTROL: (
        "that file is the gateway's record of the AWS resources odin CREATED for this env -- what "
        "tofu's next refresh reads -- so do NOT delete it: odin would forget resources that really "
        "exist and leave them orphaned. It is also all-or-nothing: odin validates the whole file on "
        "each read, so a single bad record fails every gateway call for this env -- including "
        "`odin destroy --env {env}`, which reads it too and stops on it rather than tearing "
        "anything down. Restore it with `odin import <archive>` if you have an `odin export` of "
        "this env. Otherwise repair it in place: it is plain JSON, the error above names the one "
        "record that was rejected and what was wrong with it, and odin re-reads the file on the "
        "next call -- no restart. Deleting just that entry works too, at the price of orphaning "
        "whatever it described"
    ),
    store_mod.CREDENTIALS: (
        "that file holds the gateway credentials odin issued to this env's workloads. A fresh Apply "
        "mints new ones, but a container that is ALREADY running was launched with the old pair and "
        "will fail auth (`InvalidClientTokenId`) until it is recreated -- so restore it with "
        "`odin import <archive>` if you can, and otherwise expect to re-Apply and let the workloads "
        "be replaced"
    ),
    store_mod.DESIRED: (
        "that file IS this env's desired state -- the only record of what you asked for -- so do "
        "NOT delete it. Restore it with `odin import <archive>` if you have an `odin export` of "
        "this env, or draw the canvas again and Apply to author a fresh revision. `odin world "
        "--env {env}` still works, and reports what odin last OBSERVED"
    ),
}
_STORE_RECOVERY_UNKNOWN = (
    "odin has no specific recovery for that file, and does not invent one: the server log has "
    "the traceback that names where it was read"
)
_STORE_ADVICE = (
    "odin cannot read one of its own store files for env {env}. Do NOT run `odin world --env {env}` "
    "on the strength of this message -- if that is the unreadable file, it fails the same way and "
    "tells you the same thing. What survives any corrupt store file: `odin events --env {env}` "
    "(a separate append-only log, holding the last state odin observed) and `odin status`. To fix "
    "it: {recovery}"
)
# Deliberately NOT "this is an odin bug": the biggest population here is a
# backing container that never became ready (`BackingAws._await_ready` raises a
# plain RuntimeError carrying the container's own log tail), which is an
# environment failure and not odin's fault. Claiming a cause odin has not
# established is the exact habit these rules exist to break.

# 503 for the backing case (a real service-unavailable condition, the same
# vocabulary the gateway's own `backing_unavailable` event uses); 500 for the
# two "odin tried and could not finish" reclaim/mesh failures and for anything
# unmapped.
_EXCEPTION_VERDICTS: dict[type[BaseException], _Verdict] = {
    BackingUnavailable: _Verdict(status="backing_unavailable", code=503, advice=_BACKING_ADVICE),
    # Both of these already name what is standing and what fixes it in their
    # OWN message (`ec2compute.reclaim_env_instances` /
    # `ensure_instance_mesh`), which is why the advice here adds the one thing
    # they cannot know: that the request as a whole did not complete. Before
    # this map they raised into the bare-text 500 -- and `/destroy`'s comment
    # said `ReclaimFailed` produced "500 with the VM names" while the VM names
    # only ever reached the server log.
    # Field test 6, F2's sibling -- and the worse copy of it. This used to read
    # "the env's desired state was left as it was, so the retry above picks up
    # exactly here", which is wrong twice: there is no "retry above" in a
    # `_failure_body` sentence, and the unchanged desired state is exactly why
    # the state does NOT stay put (`_RECREATED_BY_THE_LOOP`).
    ec2compute.ReclaimFailed: _Verdict(
        status="reclaim_failed", code=500,
        advice=(
            "the rest of this request did not finish. The env's desired state was left as it was, "
            "which means odin's reconciler goes on converging it: any s3/sqs/sns/dynamodb resource "
            "this request already removed is re-created within about one tick. Fix the cause named "
            "above and run the command again -- it starts over rather than resuming. `odin world "
            "--env {env}` is what says which resources exist right now"
        ),
    ),
    # `role` decides the recovery, so the advice carries a `{recovery}` slot
    # `_advice_fields` fills in -- see `_STORE_ADVICE`.
    StoreUnreadable: _Verdict(status="store_unreadable", code=500, advice=_STORE_ADVICE),
    ec2compute.MeshRefreshFailed: _Verdict(
        status="mesh_refresh_failed", code=500,
        advice=(
            "the firewall on the wire is NOT the one drawn on the canvas until this succeeds, and "
            "the rest of this apply did not finish"
        ),
    ),
}
_UNEXPECTED = _Verdict(status="server_error", code=500, advice=_UNEXPECTED_ADVICE)


def _advice_fields(exc: Exception, env: str) -> dict[str, str]:
    """The extra `{...}` slots one exception's advice needs, read off the
    exception itself. Only `StoreUnreadable` has any today; `str.format` ignores
    keys a template doesn't use, so every other verdict is unaffected. The
    recovery comes through a map keyed on the file's ROLE, and an unmapped role
    falls through to `_STORE_RECOVERY_UNKNOWN` rather than to an empty
    instruction -- the one thing worse than no advice."""
    path = getattr(exc, "path", "")
    role = getattr(exc, "role", "")
    return {
        "path": str(path),
        "recovery": _STORE_RECOVERY.get(role, _STORE_RECOVERY_UNKNOWN).format(path=path, env=env),
    }


def _failure_body(request: Request, exc: Exception) -> tuple[int, dict]:
    """The JSON verdict for an exception that reached the ASGI boundary.

    Pure string building on purpose -- no disk, no docker, no store. This is
    the handler of last resort; an exception raised INSIDE it would put the
    bare "Internal Server Error" straight back."""
    verdict = _EXCEPTION_VERDICTS.get(type(exc), _UNEXPECTED)
    # A route that takes env in its BODY records it on `request.state` (see
    # `api/debug.py`), because this handler cannot await a request body -- it is
    # the last resort, and an exception raised inside it puts the bare
    # "Internal Server Error" straight back. Query param next, then the default.
    env = getattr(request.state, "env", None) or request.query_params.get("env") or ENV
    # Field test 6, F4's sibling: `str(exc)` is empty for any exception built
    # with no args (`TimeoutError()`, `KeyError()`, and `TofuNotInstalled()`
    # right here in this tree), which rendered `-- TimeoutError: . odin has no
    # specific verdict...` -- a bare colon where the reason belongs, in the
    # handler that catches EVERY route. Name the class as the reason when the
    # class is all there is.
    detail = str(exc) or f"raised with no message; {type(exc).__name__} is the whole of what odin knows"
    body = {
        "status": verdict.status,
        "env": env,
        "error": (
            f"{request.method} {request.url.path} did not complete for env {env!r} -- "
            f"{type(exc).__name__}: {detail}. {verdict.advice.format(env=env, **_advice_fields(exc, env))}"
        ),
    }
    # Structured, not scraped out of `error`: the container name and the state
    # odin really observed ride on the exception itself (aws/backings.py).
    container = getattr(exc, "container", "")
    if container:
        body["backing"] = {"container": container, "observed": getattr(exc, "observed", "")}
    store_path = getattr(exc, "path", "")
    if store_path:
        body["store"] = {"path": str(store_path), "role": getattr(exc, "role", "")}
    return verdict.code, body


async def _unhandled_failure(request: Request, exc: Exception) -> JSONResponse:
    code, body = _failure_body(request, exc)
    log.error("%s %s failed: %s", request.method, request.url.path, body["error"])
    return JSONResponse(status_code=code, content=body)


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

# --- what a FAILED destroy really leaves behind (field test 6, F2) ---
#
# The behaviour is deliberate and does NOT change here: `store.apply(Stack(env=
# env))` stays gated on `destroyed`, because committing an empty desired state
# over a failed teardown BRICKED the env (field test 5 -- the note below says
# how). What changes is the sentence, which used to end
#
#     "The env's desired state was left as it was, so re-running the destroy
#      once the cause above is fixed picks up exactly here."
#
# telling the user progress was preserved while odin was reversing it. MEASURED
# on a real server at the shipped cadence -- `poll_interval=1.0`,
# ODIN_DRIFT_SWEEP_TICKS never set anywhere -- with the desired state still
# asking for them, a queue and a bucket deleted out from under odin were both
# back in the REAL backings (probed with boto3 directly, not via `/world`)
# **0.76s** later. The path: `reconciler.tick()` below -> `_converge` ->
# `_observe_provisioned` reads the missing resource as `crashed` ->
# `plan()`'s pending/crashed branch emits `ProvisionResource` -> a real
# `create_queue`/`create_bucket`. The background loop redoes it every second
# regardless of the explicit tick.
#
# Two things the old sentence also got wrong by omission, both fixed below:
# `still_standing` is tofu's own state plus real containers, so a resource the
# destroy DID delete and the loop then re-created appears in NEITHER field; and
# "re-running picks up exactly here" is not resumption -- the retry starts over.
#
# DERIVED, not asserted (the `_DESTROY_STATUS` rule one level up): the sentence
# is built from the desired Stack that is still committed, so an env with no
# PROVISIONED resource is not warned about a re-creation that cannot happen to
# it, and a future PROVISIONED kind is covered without editing this text.
_RECREATED_BY_THE_LOOP = (
    "The env's desired state was deliberately left as it was -- that is what makes a retry "
    "possible at all -- so this destroy did NOT preserve progress. odin's reconciler keeps "
    "converging this env, and it RE-CREATES any of these that the destroy already removed, within "
    "about one tick (~1s at the default poll interval; measured 0.76s): {recreated}. `still "
    "standing` above is tofu's own state plus real containers -- it does NOT list a resource the "
    "destroy deleted and the loop has since put back, so `odin world --env {env}` is what says "
    "what exists now. What to do: fix the cause above and re-run `odin destroy --env {env}`; it "
    "starts over rather than resuming, which is safe. To stop the re-creation while you diagnose, "
    "run `odin stop` first -- nothing converges this env while odin is not running."
)
_NOTHING_RECREATED = (
    "The env's desired state was deliberately left as it was -- that is what makes a retry "
    "possible at all. This env's desired state holds no s3/sqs/sns/dynamodb resource, so odin's "
    "reconciler is not re-creating anything behind you; but nothing was resumed either. Fix the "
    "cause above and re-run `odin destroy --env {env}`, which starts over against whatever is "
    "still there."
)


# `still standing` must never render a bare `[]`, and must never let "odin could
# not tell" wear the same words as "there is nothing there" (field test 6, F4's
# sibling). `None` from either witness is UNKNOWN; `[]` from both is a real
# emptiness that still is not a success, and says so.
_STANDING_UNKNOWN = "odin could not read {source}, so that half is UNKNOWN rather than zero"
_TF_STATE_SOURCE = "tofu's own state file"
_CONTAINER_SOURCE = "the machine's container list"
_NOTHING_STANDING = (
    "still standing: nothing odin can see -- tofu's state holds no resource for this env and no "
    "container of odin's is left. That is not a success: the destroy did not complete (cause "
    "above), so whatever it was in the middle of went unverified"
)


def _standing(source: str, names: list[str] | None, noun: str) -> str:
    return (
        _STANDING_UNKNOWN.format(source=source) if names is None
        else f"{len(names)} {noun}(s) {names}"
    )


def _still_standing_text(tf_state: list[str] | None, containers: list[str] | None) -> str:
    """Both witnesses in one clause. `== []` deliberately, not falsiness: `None`
    is unknown and must not be folded into the nothing-standing sentence."""
    if tf_state == [] and containers == []:
        return _NOTHING_STANDING
    return (
        f"still standing: {_standing(_TF_STATE_SOURCE, tf_state, 'resource')} in tofu state, "
        f"{_standing(_CONTAINER_SOURCE, containers, 'container')}"
    )


def _loop_leftover(stack: Stack, env: str) -> str:
    """The closing sentence of a FAILED destroy: what the still-committed
    desired state means for what happens NEXT. Selected by whether that Stack
    holds anything the reconciler re-creates -- see `_RECREATED_BY_THE_LOOP`."""
    recreated = sorted(f"{r.id} ({r.kind})" for r in stack.resources if r.kind in PROVISIONED)
    template = _RECREATED_BY_THE_LOOP if recreated else _NOTHING_RECREATED
    return template.format(recreated=", ".join(recreated), env=env)


# --- `/envs/rm`: outcome -> status, the ONE place the status is decided ---
#
# The same shape as `_DESTROY_STATUS` above, adopted from the START rather than
# after three releases of exit-0-over-a-standing-env. `_remove_env` below
# performs the removal and reports an OUTCOME; it never names a status, and its
# signature makes forgetting to report one impossible (the outcome IS the first
# half of what it returns). The status is looked up here, once. An outcome this
# map has no entry for -- including a typo -- lands on `_REMOVE_FAILED`, so a
# new way for a removal not to happen fails loudly by default and can only be
# scored as success by being added here on purpose.
_REMOVE_FAILED = "remove_failed"
_REMOVE_STATUS = {
    "removed": "removed",                     # teardown ok, loop ended, no container, state gone
    "never_existed": "not_found",             # nothing was removed AND nothing was created
    "unsafe_env_name": "remove_refused",      # `env` does not name a child of the store root
    "busy": "remove_refused",                 # a tofu run holds this env
    "destroy_failed": "remove_failed_teardown",
    "loop_still_running": "remove_failed_loop_running",
    "containers_standing": "remove_failed_containers_standing",
    "containers_unknown": "remove_unverified",
    "state_survived": "remove_failed_state_survives",
}
# The two statuses that mean the end state HOLDS. Everything else -- including
# the fallback -- gets an `error`, which is what makes `odin env rm` exit
# nonzero (`cli/http.body_or_fail` keys on it).
_REMOVE_OK = {"removed", "not_found"}
_REMOVE_HTTP = {"removed": 200, "never_existed": 200, "unsafe_env_name": 400, "busy": 409}

# What each failing outcome REALLY was, and what is still standing because of
# it. Every one of them leaves `.odin/<env>/` INTACT on purpose: a removal that
# could not finish must stay retryable, and deleting the desired state over a
# half-finished teardown is the same mistake `/destroy` makes a point of not
# making (see `_RECREATED_BY_THE_LOOP`).
_REMOVE_CAUSE = {
    "unsafe_env_name": (
        "{env!r} does not name an environment directly under odin's store, so it is not an "
        "environment odin can remove -- nothing was read, written or deleted"
    ),
    "busy": (
        "a tofu run is in progress for this env, so the teardown was not even attempted -- nothing "
        "was removed. Wait for it (`odin tf status --env {env}`) and re-run"
    ),
    "destroy_failed": (
        "the teardown failed, so nothing was forgotten -- the env, its state directory, its "
        "credentials and its reconciler are all exactly as they were. The `teardown` field carries "
        "the full report, including what tofu left standing; fix that cause and re-run"
    ),
    "loop_still_running": (
        "odin asked this env's reconciler to stop and its loop task had NOT finished when asked -- "
        "so nothing was deleted. Removing the state under a live loop would let it re-create "
        "`world.json` and real containers inside the directory being deleted. The env is still "
        "registered and still converging; restart odin (`odin stop && odin start`) and re-run"
    ),
    "containers_standing": (
        "the teardown reported success but odin can still see container(s) of this env's on the "
        "machine, so its state was NOT deleted -- removing it would leave them with nothing left "
        "that names them. Remove them (`docker rm -f ...`) and re-run"
    ),
    "containers_unknown": (
        "odin could not list this machine's containers, so it cannot say this env left none behind "
        "-- and it will not call a removal complete on a half it could not read. NOTHING was "
        "deleted; the env is exactly as it was. Fix the container runtime (`odin doctor`) and "
        "re-run"
    ),
    "state_survived": (
        "`{state_dir}` still exists after odin deleted it, so this env's desired state, credentials "
        "and gateway records are still on disk. Its reconciler HAS been stopped and it is no longer "
        "registered, so nothing is converging it -- check the directory's permissions and remove it "
        "by hand, or restart odin to pick it back up"
    ),
}
_UNKNOWN_REMOVE_CAUSE = (
    "the removal finished without reporting any outcome, which is an odin bug -- reported as a "
    "failure rather than assumed to have worked, because nothing here verified that it did"
)


def _env_dir(root: Path, env: str) -> Path | None:
    """`env`'s own directory under `root`, or None if `env` does not name one.

    `/envs/rm` ends in `shutil.rmtree`, and every other route in this file
    interpolates `env` straight into a path. A resolution check rather than a
    name pattern, because it answers the question that actually matters -- is
    this a CHILD of the store root -- and answers it for the cases a charset
    rule gets wrong in both directions:

        _env_dir(root, "..")        -> None   (root's parent)
        _env_dir(root, "a/b")       -> None   (a grandchild)
        _env_dir(root, "")          -> None   (the root itself)
        _env_dir(root, "/etc")      -> None   (absolute: `root / "/etc"` IS "/etc")
        _env_dir(root, "my env")    -> the directory  (legal today; a pattern would refuse it)

    A symlinked `<root>/<env>` resolves to its target, whose parent is not the
    root, so it is refused rather than followed out of the store."""
    parent = root.resolve()
    target = (root / env).resolve()
    return target if target.parent == parent and target != parent else None


async def _loop_health(reconcilers: dict[str, Reconciler], env: str) -> LoopHealth:
    """This env's reconciler health, WITHOUT creating one.

    `reconciler_for` starts a real loop as a side effect, which a read must
    never do (`/world?env=typo` would mint a reconciler for an env that does
    not exist). An env with no reconciler is reported as not ticking with that
    as the reason -- true, and for a never-applied env its World is empty
    anyway, so there is no phase for it to mislabel."""
    reconciler = reconcilers.get(env)
    if reconciler is None:
        return LoopHealth(
            env=env, ticking=False,
            verdict=f"no reconciler is running for env {env!r} -- nothing is converging it. If "
                    f"something has been applied to this env, restart odin (`odin stop && odin start`).",
        )
    return await reconciler.health()


def _stale_resource(resource: dict, health: LoopHealth) -> dict:
    """One `/world` resource, with the loop's staleness carried ON it.

    The top-level `reconciler` block alone is not enough: a reader (or a
    script) that iterates `resources` and sees `phase: "healthy"` with an empty
    verdict concludes "converging", which is precisely the wrong conclusion
    while the loop is dead -- that phase is a frozen snapshot of unknown age.
    So every resource's verdict carries it too, and the existing verdict is
    KEPT (prefixed, never replaced): a crashed resource's real reason is not
    less true because the loop then died. A per-request overlay, like
    `stranded_in_tf_state` above -- nothing is written to World, because
    nothing about the resource itself changed."""
    prior = resource.get("verdict")
    stale = (
        f"[STALE: odin's reconciler for env {health.env!r} is not ticking, so this phase is a "
        f"frozen snapshot last confirmed "
        f"{'never' if health.last_tick_seconds_ago is None else f'{health.last_tick_seconds_ago:.0f}s ago'}"
        f", not a live reading]"
    )
    return {**resource, "verdict": f"{stale} {prior}" if prior else stale}


def create_apply_router(
    store: SpecStore, reconciler_for, keystore: KeyStore, runner: TfRunner, gateway_port, env_epoch: dict[str, int],
    stores: SynthStores, gateway: GatewayState, runtime, reconcilers: dict[str, Reconciler],
    chat_sessions: dict[str, list[tuple[str, str]]] | None = None,
) -> APIRouter:
    router = APIRouter()
    # `/envs/rm` forgets these too -- see `_remove_env`. Defaulted so every
    # existing caller (the api tests build this router directly) keeps working;
    # `create_app` passes the real ones the chat route and the stream use.
    _chat_sessions = {} if chat_sessions is None else chat_sessions

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

    async def _destroy(env: str) -> JSONResponse:
        """The whole teardown, as one callable. `/destroy` IS this, and so is
        the first half of `/envs/rm` -- which is why it is a function rather
        than a route body copied into a second route. A removal that tore an
        env down its own way would be a second teardown to keep honest, and the
        four rounds of `_DESTROY_STATUS` above are the argument against having
        two of them."""
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
                    #
                    # Field test 6, F4's sibling: the witness is only allowed to
                    # score the SUCCESS side when it was actually readable. An
                    # empty `_tf_state_addresses` used to be enough, and a state
                    # file caught mid-rewrite folds to empty -- so "odin could
                    # not tell" would have bought `nothing_to_destroy`, which is
                    # a success. Unknown now goes the failure way.
                    empty_state = _tf_state_readable(store.root, env) and not _tf_state_addresses(store.root, env)
                    tf_outcome = "nothing_to_destroy" if empty_state else "unavailable"
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
            # `destroyed` (`ReclaimFailed`). That claim was HALF true until
            # field test 6: the exception escaped this route unhandled, so the
            # VM names reached the server log and the caller got the bare text
            # `Internal Server Error`. `_EXCEPTION_VERDICTS` is what actually
            # puts them in a 500 JSON body now.
            reclaimed = await ec2compute.reclaim_env_instances(stores, env)
            if reclaimed:
                body["reclaimed_vms"] = reclaimed
            # ...and the network records the same interruption left behind,
            # which `tofu destroy` likewise never reaches. They are what kept
            # `/world` listing a VPC and subnets for a destroyed env -- and,
            # because the lighthouse stop hangs off the VPC-delete path, a VPC
            # record that is never deleted is a lighthouse never stopped
            # (HIGH-A through HIGH-B's back door).
            forgotten = await ec2net.purge_env(stores, env)
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
        tf_state = _tf_state_addresses(store.root, env) if _tf_state_readable(store.root, env) else None
        containers = await _surviving_containers(runtime, env)
        body["still_standing"] = {"tf_state": tf_state, "containers": containers}
        cause = _DESTROY_CAUSE.get(tf_outcome, _UNKNOWN_DESTROY_CAUSE).format(
            exit_code=(body["tf"] or {}).get("exit_code"),
        )
        body["error"] = (
            f"destroy did not finish for env {env!r}: {cause}. "
            f"{_still_standing_text(tf_state, containers)}. "
            # The desired state that is STILL COMMITTED decides what happens
            # next, so the sentence is derived from it -- see `_loop_leftover`.
            f"{_loop_leftover(store.get_stack(env), env)}"
        )
        return JSONResponse(status_code=500, content=body)

    @router.post("/destroy")
    async def destroy(env: str = ENV) -> JSONResponse:
        return await _destroy(env)

    async def _remove_env(env: str) -> tuple[str, dict]:
        """Remove `env` entirely, and report the OUTCOME -- never a status.

        Deliberately has no way to say "it worked": it returns an outcome
        string, `create_apply_router`'s caller looks that up in
        `_REMOVE_STATUS`, and anything unmapped is a failure. A branch added
        here that returns the wrong word therefore fails loudly instead of
        inheriting a success (`/destroy`'s hard-won shape, one level up).

        ORDER is the whole design, and every step is a gate on the next:

          1. teardown (`_destroy`) -- while the loop is still ALIVE, because
             its trailing `tick()` is what gc's the backing containers.
          2. STOP the loop, and verify with `Reconciler.loop_finished()` that
             the task really ended. A live loop re-creates `world.json` and
             real containers inside a directory being deleted.
          3. ask the machine what containers survived, the same witness
             `/destroy`'s failure report uses.
          4. only THEN forget the in-memory per-env state and `rmtree` the
             directory.

        Anything that goes wrong before step 4 leaves `.odin/<env>/` untouched,
        so the removal is retryable and no half-removed env can exist.

        `still_standing` from step 3 errs toward refusing: `_surviving_containers`
        matches on odin's container NAMING, so an env whose name is a `-`-suffix
        of this one's (`a` inside `b-a`) reads as this env's container and
        refuses the removal (MEASURED, and recorded in `docs/limits.md`).
        Refusing a legitimate removal is recoverable; deleting the last record
        of a running container is not.

        What is deliberately NOT forgotten: `TranslateCache`. It is keyed by a
        Stack's content hash, and a Stack carries its own `env`, so a removed
        env's entries are unreachable unless the identical env and canvas come
        back -- in which case the cached translation is the right answer, not a
        stale one."""
        target = _env_dir(store.root, env)
        if target is None:
            return "unsafe_env_name", {}
        # Nothing to remove AND nothing to create -- the same rule `/destroy`
        # follows for an env that never existed. `reconcilers` is asked too, so
        # an env whose directory was deleted by hand still gets its loop
        # stopped rather than left ticking over nothing.
        if not target.exists() and env not in reconcilers:
            return "never_existed", {}

        teardown = await _destroy(env)
        detail: dict = {"teardown": json.loads(teardown.body)}
        if teardown.status_code == 409:
            return "busy", detail
        if teardown.status_code != 200:
            return "destroy_failed", detail

        reconciler = reconcilers.get(env)
        if reconciler is not None:
            await reconciler.stop()
            if not reconciler.loop_finished():
                return "loop_still_running", detail

        containers = await _surviving_containers(runtime, env)
        if containers is None:
            return "containers_unknown", detail
        if containers:
            return "containers_standing", {**detail, "still_standing": {"containers": containers}}

        # ...and from here to the `rmtree` there is deliberately NO `await`.
        # On one event loop that makes forgetting the caches and deleting the
        # directory a single uninterruptible step, so no request can land in
        # the middle and re-persist a file (`JsonStore._persist_locked` and
        # `KeyStore._persist` both write on every mutation) into a directory
        # that is on its way out.
        reconcilers.pop(env, None)
        detail["forgotten"] = {
            "reconciler": reconciler is not None,
            # Normally `[]`, and that is the CORRECT reading rather than a
            # miss: the teardown above ends in `keystore.revoke_env(env)`, so
            # by here there is usually nothing left to drop. `forget_env` is
            # still the right call and not `revoke_env` -- revoke PERSISTS an
            # empty `keys.json`, which would re-create a file one line before
            # the directory holding it is deleted. A non-empty list here means
            # a credential was issued between the teardown and this line.
            "keys": keystore.forget_env(env),
            "gateway_stores": stores.forget_env(env),
            "gateway_policy": gateway.forget_env(env),
            "tf_runs": runner.forget_env(env),
            "apply_epoch": env_epoch.pop(env, None) is not None,
            "chat_turns": len(_chat_sessions.pop(env, [])),
        }
        detail["state_dir"] = str(target)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            return "state_survived", detail
        return "removed", detail

    @router.post("/envs/rm")
    async def env_rm(env: str = ENV) -> JSONResponse:
        """Tear an env down AND forget it: no directory, no credentials, no
        gateway records, no reconciler, and gone from `GET /envs`.

        `odin destroy --env X` deliberately keeps the env -- its desired state
        is what makes a retry possible, and its loop is what converges the next
        apply. That is right for a teardown and wrong for a decommission, which
        is what this is: seven envs accumulated in one field-test session, each
        with a reconciler ticking forever over nothing.

        The status is DERIVED, once, from `_REMOVE_STATUS` -- see `_remove_env`.
        """
        outcome, detail = await _remove_env(env)
        status = _REMOVE_STATUS.get(outcome, _REMOVE_FAILED)
        body = {"status": status, "env": env, **detail}
        if status in _REMOVE_OK:
            # The SERVER log, deliberately, and not `ws.broadcast`. The durable
            # event log is PER ENV (`<root>/<env>/events.jsonl`) and
            # `secure_append_line` mkdirs its parent, so broadcasting a
            # removal would re-create the very directory this route just
            # deleted -- an env that came back from the dead one line after it
            # went away. The UI re-reads `GET /envs` on its own poll instead.
            log.warning("removed env %r: %s", env, detail.get("forgotten"))
            return JSONResponse(status_code=_REMOVE_HTTP.get(outcome, 200), content=body)
        cause = _REMOVE_CAUSE.get(outcome, _UNKNOWN_REMOVE_CAUSE).format(
            env=env, state_dir=detail.get("state_dir", store.root / env),
        )
        body["error"] = f"env {env!r} was NOT removed: {cause}."
        return JSONResponse(status_code=_REMOVE_HTTP.get(outcome, 500), content=body)

    @router.get("/world")
    async def world(env: str = ENV) -> dict:
        """The env's observed World, plus the resources tofu really created
        that odin can currently see nowhere else (field test 3, P2-5: after a
        failed apply the s3/sqs/sns/dynamodb nodes had NO BADGE AT ALL while
        tofu's state listed them and every call answered ServiceUnavailable).

        `reachable` is the gateway's own routing table -- the very thing that
        decides between forwarding a call and refusing it -- so this reports
        exactly the resources the gateway would genuinely refuse right now, and
        nothing during a healthy apply. See `stranded_in_tf_state` for why this
        is a per-request overlay rather than a World write.

        ...and `reconciler`, which says whether these phases are LIVE READINGS
        at all. Every phase here is authored by the reconciler loop, so a loop
        that is dead, hung or failing every tick makes this whole document a
        frozen snapshot -- and until now nothing in the response said so, which
        is why a dead loop looked exactly like a converged env (see
        `Reconciler.health`). `_stale_resource` carries the same fact down onto
        each resource, because a reader that iterates `resources` must not be
        able to miss it."""
        observed = store.current_world(env)
        reachable = {kind for kind in PROVISIONED if gateway.backing_port(env, kind) is not None}
        stranded = stranded_in_tf_state(store.root, env, observed, reachable)
        health = await _loop_health(reconcilers, env)
        body = World(env=observed.env, resources=(*observed.resources, *stranded)).model_dump()
        resources = body["resources"] if health.ticking else [
            _stale_resource(resource, health) for resource in body["resources"]
        ]
        return {**body, "resources": resources, "reconciler": health.model_dump()}

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


class AiRequest(BaseModel):
    enabled: bool


class ChatRequest(BaseModel):
    message: str
    dry_run: bool = False


class ImportTfRequest(BaseModel):
    source: Literal["hcl", "live"]
    hcl: str = ""
    resources: list[dict] = []  # [{"type": "s3", "id": "uploads"}, ...] -- see import_tf.LiveResource
    # v0.8.4: `{filename: base64(zip)}`. A lambda's CODE lives in a zip beside
    # `main.tf`, never in the HCL, and this route is how `odin import-tf` reaches
    # the parser -- so without carrying the archives the directory-mode recovery
    # in `parse_hcl_dir` could not reach the product at all, only its own unit
    # tests. Base64 because the body is JSON. Optional, so an older client and a
    # single-file import both keep working exactly as before.
    archives: dict[str, str] = {}


def _saved_canvas(path: Path) -> dict:
    """The canvas currently on disk (`GET /canvas`'s own source), or an empty
    one when nobody has drawn yet — a plan must not fail for that.

    It does NOT swallow a canvas that is on disk and unparseable, and the
    docstring used to claim "never raises" for both. `GET /canvas` returns this
    file verbatim and never re-validates it (api/canvas.py), so a hand-edited or
    truncated `.odin/canvas.json` is a supported state, and `json.loads` raises
    on it. Left raising deliberately: `skipped`/`canvas_drift` describe THIS
    file, and quietly reporting a corrupt canvas as an empty one would make
    `/tf/plan` say the saved canvas differs from the applied Stack for a reason
    that is not the real one. `_unhandled_failure` now turns it into a JSON
    failure naming the parse error and the byte offset, which is the actionable
    answer; before, it was a bare `Internal Server Error`."""
    return json.loads(path.read_text()) if path.is_file() else {}


# How many (message, reply) turns the chat agent remembers per env. In MEMORY,
# so a server restart clears it -- which is one of the two resets the owner asked
# for; `POST /chat/clear` is the other. Bounded because a conversation is
# replayed into every prompt: unbounded history would grow the prompt without
# limit and quietly start costing more than the canvas it is editing.
_CHAT_HISTORY_TURNS = 12


def create_tf_router(
    store: SpecStore, runner: TfRunner, keystore: KeyStore, gateway_port,
    translate_cache: translate_mod.TranslateCache, runtime, stores: SynthStores,
    canvas_for: Callable[[str], Path] = lambda env: ODIN_DIR / env / CANVAS_NAME,
    ws=None,
    chat_sessions: dict[str, list[tuple[str, str]]] | None = None,
) -> APIRouter:
    """`/tf/*` -- Simulate's own apply/destroy/status, independent of the
    canvas `/apply`/`/destroy` above (S2 CONTRACT ADDENDUM: routes named
    `/tf/*`, not `/simulate/*` -- "the owner renamed the user surface to
    Apply"). `gateway_port` is a zero-arg callable rather than a plain int:
    the real port is only known once the gateway's uvicorn listener starts
    in `create_app`'s `lifespan`, resolved AFTER this router is built."""
    router = APIRouter()
    # Per-env, per-process. Not persisted on purpose: a conversation is a
    # working context, not a record, and one that outlived a restart would be a
    # surprise nobody asked for. OWNED BY `create_app` now rather than by this
    # closure, so `/envs/rm` can forget a removed env's conversation too --
    # otherwise an env of the same name, recreated in the same process, would
    # resume a conversation about an architecture that no longer exists.
    _chat_sessions: dict[str, list[tuple[str, str]]] = {} if chat_sessions is None else chat_sessions

    async def save_canvas_now(env: str, canvas: dict) -> dict[str, str]:
        """Save an agent-authored canvas through the SAME writer the UI's own
        save uses, so it gets identical validation, the same 0600 mode, and the
        same `canvas_updated` broadcast that makes an open tab redraw."""
        return await write_canvas(canvas_for(env), CanvasGraph.model_validate(canvas), env, ws)

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
            "wiring_errors": project.wiring_errors,
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
        # This env's OWN canvas. It used to read one global file and compare it
        # against a per-env stack, so `canvas_drift` was meaningless for every
        # env but the default.
        canvas = _saved_canvas(canvas_for(env))
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
            "wiring_errors": project.wiring_errors,
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

    @router.post("/chat")
    async def chat_route(body: ChatRequest, env: str = ENV) -> dict:
        """Plain English -> the canvas is CHANGED, live, and you review it there.

        Owner decision, 2026-07-28: the agent edits the canvas directly rather
        than handing back a proposal to confirm. The canvas is the review
        surface -- the change appears where you are already looking, the UI's
        own undo stack picks it up (Canvas.tsx records history from a `[nodes,
        edges]` effect, so a stream-driven `setNodes` lands on it exactly
        like a drag), and Cmd-Z reverses it.

        What it still does NOT do is APPLY: nothing here provisions anything.
        `/apply-full` remains the user's own button, which is the line that
        matters -- an agent may rearrange the drawing, never build from it.

        `dry_run` keeps the old preview behaviour for a caller that wants to
        look first (`odin chat --dry-run`).

        Every failure lands on the same body (`note` set, canvas untouched), so
        a client never distinguishes "nothing to do" from "it broke" by catching
        an exception.
        """
        # Normalised to the canvas SHAPE rather than passed through: an env
        # nobody has drawn in yet reads back as `{}` (`_saved_canvas`), and
        # "add a bucket" before drawing anything is the first thing a person
        # tries. Everything downstream is written against {nodes, edges}.
        saved = _saved_canvas(canvas_for(env))
        canvas = {"nodes": saved.get("nodes") or [], "edges": saved.get("edges") or []}
        session = _chat_sessions.setdefault(env, [])
        proposal = await chat.propose(canvas, body.message, history=list(session))
        # The turn is remembered whatever happened, INCLUDING a refusal: "no, do
        # it the other way" is the most common second message, and it is
        # meaningless without the first.
        session.append((body.message, proposal.reply))
        del session[:-_CHAT_HISTORY_TURNS]

        if body.dry_run or not proposal.changes:
            return proposal.model_dump()
        return {**proposal.model_dump(), **await save_canvas_now(env, proposal.canvas)}

    @router.get("/ai")
    async def ai_state() -> dict:
        """Whether model calls are allowed, and who decided.

        `source` matters to the UI: when `ODIN_AI` is set the switch cannot
        override it, so the control renders disabled with the reason rather than
        pretending to work.
        """
        forced = os.environ.get(ai.ENV_VAR, "").strip()
        return {
            "enabled": ai.off_reason() is None,
            "source": "env" if forced else "switch",
            "reason": ai.off_reason(),
        }

    @router.post("/ai")
    async def set_ai_state(body: AiRequest) -> dict:
        """Turn model calls on or off from the UI. Ignored while `ODIN_AI` is
        set, which is reported rather than silently obeyed."""
        ai.set_runtime_enabled(body.enabled)
        return await ai_state()

    @router.post("/chat/clear")
    async def chat_clear_route(env: str = ENV) -> dict:
        """Forget the conversation. The CANVAS is untouched.

        The session is in memory, so a server restart clears it too -- this is
        the deliberate version of that, for starting a fresh line of thought
        without one. It deliberately does NOT roll the canvas back: the agent's
        edits are yours now, they are on the undo stack, and silently reverting
        work you may have built on since would be a far worse surprise than a
        stale conversation.
        """
        turns = len(_chat_sessions.pop(env, []))
        return {"status": "cleared", "env": env, "turns_forgotten": turns}

    @router.post("/import-tf")
    async def import_tf_route(body: ImportTfRequest, env: str = ENV) -> dict:
        """S4: TF -> canvas, the reverse direction. `source="hcl"` parses the
        given text deterministically; `source="live"` resolves `resources`
        against the env's real backings through the gateway (operator creds,
        same as /tf/apply)."""
        if body.source == "hcl":
            archives = {
                name: base64.b64decode(blob) for name, blob in body.archives.items()
            }
            result = import_tf_mod.parse_hcl_text(body.hcl, archives)
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


# A fault with no recorded reason still has to say something true. Field test 6,
# F4's sibling: `reason` is `str | None` on both `FunctionFault` and
# `DatabaseFault`, and the old line simply dropped the parenthesis -- so
# "rds app-db is failed" was the whole verdict, indistinguishable from a fault
# whose reason odin had and chose not to print.
_NO_REASON_RECORDED = "no reason was recorded on the record; this is the observed status only"


def _unhealthy_line(item: dict) -> str:
    # `.split()` collapses whitespace, and it earns its keep: the real reason
    # measured for F4 is psycopg's own multi-line text ("server closed the
    # connection unexpectedly\n\tThis probably means..."), and `note` is echoed
    # as ONE line by `odin apply`. Verbatim it arrived as a paragraph wrapped
    # into the middle of a sentence.
    reason = " ".join((item["reason"] or _NO_REASON_RECORDED).split())
    return f"{item['kind']} {item['node']} is {item['observed']} ({reason})"


# What a recovery COST, keyed by (kind, did its data survive?).
#
# This used to be a one-line WARNING with no second case, because there was no
# second case to have: an rds container kept its data on the image's anonymous
# volume, `RuntimeDriver.stop` (`docker rm -f -v`) deleted it with the
# container, and re-creating one therefore returned an EMPTY database. v0.8.14
# gave each instance a NAMED volume (`aws/rds.py::volume_name`) that outlives
# its container, so the ordinary repair is non-destructive and the sentence is a
# footnote.
#
# It is a footnote that is CHECKED, not assumed. `_recovering_resources` asks
# the runtime whether that volume is really still there at the one moment the
# answer matters -- after the sweep marked the database dead and before the
# converge re-creates it -- so if someone removed the volume too (or an older
# instance predates the volume entirely), the apply goes back to saying the
# data is gone. Asserting the good case would be exactly the guard-that-reads-
# no-signal shape honesty rule 1 is about.
#
# `None` is lambda's data_kept: a rebuilt RIE container has no data to keep,
# and a `(kind, data_kept)` combination this map does not know falls through to
# a neutral truth rather than inheriting either claim.
_RECOVERY_COST = {
    ("rds", True): "its data survived — the container is new, the volume holding the database is not",
    ("rds", False): "its data did not survive — its volume was gone too, so the database is new and empty",
    ("lambda", None): "its execution environment was rebuilt from the deployed code",
}


# The drift sweep ends its verdicts with a call to action -- "container X is not
# running (exit 137) — re-Apply to recreate" -- which is right where a user reads
# it (`/world`, a crashed node) and WRONG quoted into a recovery line, where the
# apply has already done the thing it asks for. Field test 7 caught it reading
# "was re-created because ... — re-Apply to recreate", which invites someone to
# repeat an action that just happened. Only the CAUSE belongs here.
_ADVICE_SUFFIX = " — re-Apply to recreate"


def _recovered_line(item: dict) -> str:
    cause = (item["reason"] or "").removesuffix(_ADVICE_SUFFIX).rstrip(" —-")
    cost = _RECOVERY_COST.get((item["kind"], item.get("data_kept")), "it was rebuilt")
    return f"{item['kind']} {item['node']} was re-created because {cause} ({cost})"


async def _recovering_resources(stores: SynthStores, env: str, runtime) -> list[dict]:
    """The lambda/rds resources this apply is ABOUT to re-create, named.

    ## Why an apply that fixes itself still has to say so

    Before v0.8.2 the drift sweep ran only AFTER the converges, so the apply
    that discovered a dead container never repaired it: `/world` said
    `crashed — re-Apply to recreate`, and the re-Apply the user was told to run
    converged nothing. Sweeping FIRST closed that (recovery now takes one apply,
    measured 302.8s -> 5.9s), but it opened a quieter dishonesty in its place:
    the apply silently destroyed and rebuilt a container, and for rds that meant
    it silently destroyed a DATABASE. A green `applied` is a true statement
    about the END STATE and a misleading one about what happened on the way --
    exactly the shape honesty rule 2 is about.

    `tests/simulate/test_false_green_window_e2e.py` originally asserted the
    opposite contract (fail first, recover on a second apply) precisely to keep
    that data loss visible; its own words were "no operator should learn about
    that from a green apply". This is how the recover-in-one behaviour keeps
    that promise instead of trading it away.

    v0.8.14 removed the data loss itself (`aws/rds.py::volume_name` -- a named
    volume per instance, so the replacement container remounts the same
    database), which is a reason to keep this function, not to delete it: an
    apply still rebuilds a container nobody asked it to touch, and the operator
    still has to be told which one and why. What changed is the SENTENCE, and
    `data_kept` below is why it can now be a footnote without becoming a
    reassurance nobody checked.

    Read BETWEEN the sweep and the converges, which is the only moment the mark
    exists: the sweep sets `failed`/`Failed`, and each converge clears it to
    `creating`/`InProgress` as the first thing it does. Reading later would
    report nothing; reading earlier would report the last apply's news.

    Scoped to rds and lambda deliberately. Those two are re-created WHOLESALE by
    an apply because their execution container is not a terraform resource.
    `ecsctl.converge_services` reconciles ACTIVE services toward a desired count
    rather than resurrecting a failed record, so it has no equivalent moment to
    report and is not claimed here.

    ## `data_kept`: the one fact here that is measured, not derived

    Since v0.8.14 an rds container's PGDATA is a NAMED volume that survives the
    container, so the ordinary repair keeps the data -- and `_RECOVERY_COST`
    could just say so. It doesn't, because a user can `docker volume rm` too,
    and an instance created before v0.8.14 has no named volume at all; in both
    cases the re-create really does hand back an empty database and the apply
    would be reassuring them about data it was in the middle of losing.

    So the volume listing is READ, once, from the same runtime that is about to
    do the re-creating -- and read HERE, in the same instant the failure marks
    exist, because `create_db` starts mounting (and thereby creating) the volume
    moments later, at which point the question is unanswerable. One `docker
    volume ls` for the whole env, and only when there is a failed database to
    report: an env with none pays nothing, exactly like `drift`'s own listing.
    """
    failed = [r for r in rdsctl.records(stores, env) if r["status"] == rdsctl.FAILED]
    volumes = frozenset(await runtime.volume_names()) if failed else frozenset()
    databases = [
        {
            "kind": "rds", "node": r["db_instance_identifier"],
            "reason": r.get("status_reason") or "it was not running",
            "data_kept": rds_volume_name(env, r["db_instance_identifier"]) in volumes,
        }
        for r in failed
    ]
    functions = [
        {"kind": "lambda", "node": fn["function_name"], "reason": fn.get("state_reason") or "it was not running"}
        for key, fn in stores.lambdactl.items(env).items()
        if key.startswith("fn:") and fn["state"] == "Failed" and fn["last_update_status"] != "InProgress"
    ]
    return databases + functions


def _known_faults(stores: SynthStores, env: str) -> list[dict]:
    """Every lambda/rds fault odin's OWN gateway records already hold for `env`.

    Two callers now, and the second is field test 6's F4. The post-apply
    verification block reads these only when everything else went clean
    (`body["status"] == "applied"`), which is right for the WAITS it guards --
    they are slow, and a tofu failure has already failed the apply. It was wrong
    for the READ. When tofu failed *because* a database never came up, these
    records are the only place the real reason exists, and skipping them left
    the user reading the AWS provider's own

        Error: waiting for RDS DB Instance (srvfixdb) create: unexpected state
        'failed', wanted target 'available, storage-optimization'.
        last error: %!s(<nil>)

    -- reproduced verbatim on a real apply against a paused Postgres, at the
    default 180s readiness budget. That `%!s(<nil>)` is Go's rendering of a nil
    error and it belongs to the provider, not to odin: the format string
    `unexpected state '%s', wanted target '%s'. last error: %s` is in the
    `terraform-provider-aws` v5.100.0 binary, odin's own source has no "last
    error" string anywhere, and the provider's RDS status refresher never
    returns an error for it to carry. Odin cannot fix that sentence. What it can
    do is print the reason it was holding the entire time --
    `"Postgres never became ready: connection to server at ... timeout expired"`.

    READ WHERE THE SIGNAL STILL EXISTS, which is the load-bearing part: the
    caller below runs INSIDE the hold, straight after tofu returns and BEFORE
    `converge_db_instances`, which re-creates every `failed` instance and clears
    exactly this reason. Measured after the fact on the same env, the record had
    already gone back to `status=available, status_reason=None` -- so reading it
    any later would find nothing.

    Pure store reads: no docker, no waits, nothing that can slow an
    already-failed apply."""
    return (
        [_unhealthy_wire("lambda", f.node, f.state, f.reason) for f in lambdactl.function_faults(stores, env)]
        + [_unhealthy_wire("rds", f.node, f.status, f.reason) for f in rdsctl.db_faults(stores, env)]
    )


_TF_FAILED_NOTE = "desired state not committed; fix and re-apply"


def _tf_failed_note(faults: list[dict]) -> str:
    """The tf-failed note, carrying odin's own diagnosis when it has one.

    `odin apply` echoes `note` verbatim, so this is where a reason reaches a
    human without reading JSON -- and where it goes so that tofu's tail (which
    stays untouched: it is tofu's own output and odin does not rewrite it) is no
    longer the only reason on offer."""
    named = "; ".join(map(_unhealthy_line, faults))
    return f"{_TF_FAILED_NOTE}. odin's own records say why: {named}" if named else _TF_FAILED_NOTE


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
        # Field test 5, F5-8: a wiring ref naming a node not on the canvas can
        # NEVER resolve, so the launch path fails the apply anyway
        # (wiring.py::_resolve -> UnresolvedRef). Refusing here reaches the same
        # verdict before any container exists, and -- the actual bug -- keeps it
        # out of `not_covered`, which is a COVERAGE field. Beside the uncovered
        # refusal so both land before anything is touched.
        if skeleton.wiring_errors:
            return _wiring_rejection(skeleton.wiring_errors, env)

        # CAPACITY: an EC2 node is a real Lima VM with a real memory size, and a
        # placed ECS task gets a real memory cap. "three services of two tasks
        # each, drawn inside a t3.micro" is arithmetic odin cannot honour, so it
        # is refused here -- beside the wiring guard, before any container or VM
        # exists -- rather than discovered as OOM-killed containers minutes
        # later. Silent for every canvas that places nothing.
        capacity_problems = overcommitted(stack)
        if capacity_problems:
            return _capacity_rejection(capacity_problems, env)

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
            "wiring_errors": translated.wiring_errors,
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
                # HERE, not after the hold: `converge_db_instances` below
                # re-creates every `failed` database and wipes the very
                # `status_reason` this reads. See `_known_faults`.
                faults = _known_faults(stores, env)
                body["unhealthy_resources"] = faults
                body["note"] = _tf_failed_note(faults)
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
        # Correct the records BEFORE converging anything.
        #
        # `converge_services`/`converge_functions`/`converge_db_instances` only
        # ever act on a record already marked failed, and the only things that
        # write that mark are this sweep and `DriftSweeper`'s cadence. The sweep
        # below used to run ONLY after the converges, so an Apply converged
        # whatever the PREVIOUS sweep had marked and recovery needed a SECOND
        # Apply. Measured end to end on a killed database: `/world` showed
        # `crashed` with `container ... is not running (exit 137) — re-Apply to
        # recreate`, the Apply that followed logged no convergence at all, and
        # the database never came back. The status told the user to do the
        # exact thing they had just done.
        #
        # Safe here precisely because `_db_records`/`_function_records` only
        # sweep what CLAIMS to be up (`available`/`Active` with a real
        # endpoint): a resource this apply is still creating is `creating` and
        # is skipped, which is the concern the post-apply sweep's own comment
        # raises. That later sweep stays exactly where it is -- it verifies
        # what this one enabled.
        await drift.sweep_compute(stores, env)
        # Read WHO is broken between the sweep that marks it and the converges
        # that clear the mark -- this is the only instant both are true.
        recovering = await _recovering_resources(stores, env, runtime)
        converging = await ecsctl.converge_services(stores, env, TaskRuntime(), keystore, gateway_port())
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
        await ec2compute.ensure_instance_mesh(stores, env)
        # ...then push each live database's SG-compiled firewall into its mesh
        # sidecar. An apply is exactly the right cadence -- security groups are
        # TF-owned, so an edited `db-sg` only reaches the gateway here. Also
        # heals a sidecar that was killed under a still-running database, and
        # carries the membership revision that closes the flows above. See
        # rdsctl.ensure_db_mesh.
        await rdsctl.ensure_db_mesh(stores, env)
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
            shortfalls = await ecsctl.wait_for_steady_services(stores, env, TaskRuntime(), converging)
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
                lambdactl.wait_for_active_functions(stores, env, deploying),
                rdsctl.wait_for_available_instances(stores, env, booting),
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
            await drift.sweep_compute(stores, env)
            unhealthy = _known_faults(stores, env)
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
        # The recovery disclosure, on EVERY exit path rather than only the green
        # one -- see `_recovering_resources` for what it is and why it exists.
        #
        # Placed out here deliberately. It first lived inside the
        # `status == "applied"` block, which meant an unrelated ECS shortfall
        # (`applied_services_unhealthy`, set earlier and skipping everything
        # after it) SUPPRESSED it -- so a database odin had just emptied went
        # unmentioned precisely when something else was already going wrong,
        # which is when a user can least afford a missing fact.
        #
        # It is also the more honest shape. What was re-created does not depend
        # on the OUTCOME: `create_db` clears the same-name remnant before it
        # boots a replacement, so the container is gone the moment the
        # re-create starts, whether or not the new one ever comes up. Reporting
        # it only on success would have hidden it in exactly the failure case
        # where it matters most. Whether the result is HEALTHY is a separate
        # question, answered separately by `unhealthy_resources`.
        #
        # `data_kept` was read BEFORE any of that (see `_recovering_resources`),
        # which is what keeps this true now that an rds recovery is normally
        # non-destructive: the volume question is answered while the answer
        # still exists, not inferred afterwards from a container that has since
        # been replaced.
        #
        # APPENDS to any existing note rather than replacing it: the note may
        # already carry a tofu failure or an unhealthy resource, and those are
        # not this fact's to overwrite.
        if recovering:
            body["recovered_resources"] = recovering
            recovered = "; ".join(map(_recovered_line, recovering))
            prior = body.get("note")
            body["note"] = f"{prior} Also: {recovered}" if prior else f"desired state applied; {recovered}"
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

    Called INLINE, not through `asyncio.to_thread` (v0.7.7 de-threading).
    `reassert` is a handful of non-blocking local syscalls -- `stat` + `fstat`
    on the steady path, and `mkdir`/`open`/`flock(LOCK_NB)`/`write`/`close` on
    the repair path -- and `LOCK_NB` is what makes the flock incapable of
    waiting. Judged by DURATION, as the concurrency directive requires, and
    measured on this machine rather than argued: the call is **0.0046 ms**
    median (0.116 ms on the repair branch, which this takes at most once per
    deletion), while the thread hop it used to pay was **0.030 ms** -- the hop
    cost 6.5x the work it was hiding. It is also the whole body of a task that
    sleeps for a second between iterations, so there is nothing here for a
    thread to overlap with.
    """
    while True:
        await asyncio.sleep(interval)
        if not lock.reassert():
            log.warning(
                "the store lock file was gone or was not ours -- re-established it. Something "
                "deleted %s under a live server (`odin clean --all`, `rm -rf`); until this ran, "
                "`odin status` reported no server and `odin import` would have restored into a "
                "live store.", lock.root / STORE_LOCK_NAME,
            )


# How often the server asks its own reconcilers whether they are still
# ticking. Only the LOG and the WS line wait for this -- `/world`, `/health`
# and `odin status` each compute `LoopHealth` at read time, so no user-facing
# surface depends on this cadence having come round (honesty rule 1b).
RECONCILER_WATCH_INTERVAL = 5.0


async def _watch_reconcilers(
    reconcilers: dict[str, Reconciler], ws: ConnectionManager, interval: float = RECONCILER_WATCH_INTERVAL,
) -> None:
    """Say it OUT LOUD when a reconciler stops converging, once per transition.

    The read surfaces cannot cover this on their own: a dead loop is only
    noticed by somebody who happens to look, and the whole failure mode is that
    odin looks healthy so nobody does. So this makes the server itself notice
    -- an ERROR in `.odin/server.log`, plus the same `type:"log"` line the
    crash path already broadcasts, which puts it in the UI's Logs tab and in
    the env's durable event log (`odin events`).

    REPORT, NOT RESTART, and deliberately -- the same rule `reconcile/drift.py`
    keeps for drifted infrastructure. A loop only dies from cancellation or a
    BaseException, i.e. from an odin BUG, and a bounded auto-restart would keep
    the lights on while making that bug invisible in exactly the surfaces this
    change exists to make honest. The remedy the verdict names (`odin stop &&
    odin start`) is one command and it is the operator's to run.

    Once per TRANSITION, not once per check: a permanent condition reported
    every 5s is the flap v0.7.1 killed in the delta path, and it would bury the
    line it is trying to make visible. The recovery is announced too, so a log
    reader never has to infer that it came back.
    """
    down: set[str] = set()
    while True:
        await asyncio.sleep(interval)
        for env, reconciler in list(reconcilers.items()):
            try:
                health = await reconciler.health()
                if health.ticking:
                    if env in down:
                        down.discard(env)
                        log.warning("reconciler for env %r is converging again (%d ticks)", env, health.ticks)
                    continue
                if env in down:
                    continue
                down.add(env)
                log.error("%s", health.verdict)
                await ws.broadcast({
                    "type": "log", "env": env, "text": health.verdict,
                    "source": "reconciler", "level": "error",
                })
            except Exception:
                # A watchdog that dies silently is the bug being fixed here, so
                # nothing one env raises may take the whole pass down.
                log.exception("reconciler watchdog failed for env %r", env)


async def _reap_orphaned_ec2_vms(root: Path, envs: list[str], stores: SynthStores) -> None:
    """Best-effort (release finding #4): `limactl` being unavailable, or
    any other reaper failure, must never block server startup -- this is a
    one-shot cleanup pass, not something reconciling depends on. Runs off
    the event loop thread (`limactl list`/`delete` are blocking subprocess
    calls that can take real wall-clock time for however many VMs exist)."""
    try:
        reaped = await ec2compute.reap_orphaned_vms(root, envs)
        if reaped:
            log.warning("startup reaper deleted %d orphaned EC2 VM(s): %s", len(reaped), reaped)
        # Field test 3 HIGH-B: the reaper above builds its "expected" set from
        # the gateway store, so an interrupted apply -- which leaves VMs
        # Running and tofu's state empty -- is exactly the case it spares. The
        # second witness is tofu's own state; anything the store claims and
        # the state has forgotten is unreachable by terraform forever.
        forgotten = await ec2compute.reclaim_tf_forgotten_vms(stores, envs)
        if forgotten:
            log.warning(
                "startup reclaimed %d EC2 VM(s) tofu's state no longer knew about: %s", len(forgotten), forgotten,
            )
        # Field test 3 HIGH-A: and the lighthouse PROCESSES of envs that were
        # destroyed before teardown learned to stop them -- each one still
        # holding a port out of the 4342-4441 pool.
        lighthouses = await reap_orphaned_lighthouses(root)
        if lighthouses:
            log.warning("startup reaper stopped %d orphaned nebula lighthouse(s): %s", len(lighthouses), lighthouses)
    except Exception:
        log.exception("startup EC2 VM reaper failed (continuing without it)")


_LEGACY_CANVAS_SUFFIX = ".pre-per-env"


def _migrate_global_canvas(root: Path) -> list[str]:
    """Seed every existing env's canvas from the old GLOBAL one, once.

    Before this release the canvas lived at `.odin/canvas.json` and was shared
    by every environment. Moving it to `.odin/<env>/canvas.json` without a
    migration would make a user's architecture appear to VANISH on upgrade --
    the file is still on disk, but nothing reads it any more, which is exactly
    the "silently empty canvas" failure v0.7.7 was spent fixing.

    So: copy it into `default` and into every env directory that already
    exists and has no canvas of its own. Every env was showing that same canvas
    before, so this preserves what each of them showed. Then RENAME the global
    file rather than deleting it -- if this migration ever guesses wrong, the
    original is still there under `canvas.json.pre-per-env`, and a rename also
    makes the migration idempotent without needing a marker file.

    An env whose canvas already exists is never touched.
    """
    legacy = root / CANVAS_NAME
    if not legacy.exists():
        return []
    payload = legacy.read_text()
    envs = {ENV, *(child.name for child in root.iterdir() if child.is_dir())}
    seeded: list[str] = []
    for env in sorted(envs):
        target = root / env / CANVAS_NAME
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, payload, mode=0o600)
        seeded.append(env)
    legacy.rename(legacy.with_suffix(legacy.suffix + _LEGACY_CANVAS_SUFFIX))
    if seeded:
        log.info("canvas is per-env now: seeded %s from the previous global canvas", ", ".join(seeded))
    return seeded


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
    # The chat agent's per-env conversation, owned HERE because two routers
    # need it: `/chat` reads and appends, `/envs/rm` forgets it along with
    # everything else that env owns. In memory, per process (see
    # `_CHAT_HISTORY_TURNS`).
    chat_sessions: dict[str, list[tuple[str, str]]] = {}

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
            # The event dispatcher really INVOKES lambdas (a real RIE
            # container) and really dials this env's goaws backing, so it is
            # gated on the same `backings` flag every other real-runtime
            # dependency is: an app built with `backings=False` is the
            # fake-substrate one, and its hand-seeded records must not drive
            # actual container work on this machine.
            dispatcher=Dispatcher() if backings else None,
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

        `recovery` is now keyed on WHO made the call, because "run Apply" is
        only advice for someone who is not already inside one. A tofu run holds
        the OPERATOR key -- `/apply-full` and `/destroy` are its only issuers --
        so an event carrying that principal is being emitted from inside an
        apply or destroy that is 503-ing RIGHT NOW, and the old single sentence
        told that user to start the command they were in the middle of.

        The branch reads a signal that actually arrives, probed against the
        real gateway before it was written rather than assumed: a genuinely
        SigV4-signed `s3:ListBucket` made with the operator key, against an env
        with no s3 backing registered, reached this callback with
        `principal.node_id == "__operator__"` and put it on the wire as the
        event's own `resource_id`. The retry count in the operator text is the
        one botocore reported on that same call.

        The FIRST clause is deliberately the same words `reconcile/tf_status.py
        ::_STRANDED_VERDICT` uses -- one down backing, one vocabulary. Only the
        advice diverges, and only because the two are sent at different
        moments (that one is a `/world` overlay read after the fact)."""
        env_name = principal.env if principal else "default"
        node_id = principal.node_id if principal else None
        recovery = (
            f"no {service} backing container is running for this env, and this call came from the "
            f"tofu run of an apply or destroy that is IN FLIGHT -- so it is already failing: "
            f"aws-sdk-go-v2 retries each ServiceUnavailable with backoff (botocore gave up after 4 "
            f"on the same call) before the operation errors. Starting another Apply on the strength "
            f"of this event will not help; wait for the one in flight to return and fix the error "
            f"IT reports."
            if node_id == OPERATOR_NODE_ID else
            f"no {service} backing container is running for this env -- run Apply (or "
            f"`odin apply --env {env_name}`) to start it"
        )
        await ws_manager.broadcast({
            "type": "backing_unavailable",
            "env": env_name,
            "resource_id": node_id,
            "action": action,
            "target": resource,
            "service": service,
            "recovery": recovery,
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
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal gateway_port_actual
        # The one piece of evidence that proves THIS store has a live server, to
        # anyone who asks the kernel rather than `ps`: `odin status`/`stop` and
        # `odin import`'s live-store refusal. Held for the whole run; released
        # in the outer `finally` AFTER the reconcilers stop AND after the
        # gateway stops, so the store is only advertised free once nothing is
        # writing to it -- and the gateway writes to it (SynthStores) for as
        # long as it is accepting. (Never a reason to fail startup: an
        # unlockable store answers "free", see util._flock.)
        #
        # v0.7.7 de-threading: this used to be claimed AFTER the gateway
        # listener, and both were torn down in one `finally` in the order
        # gateway-then-lock. `serve_on_loop` is a context manager, so the two
        # can only nest, and nesting is LIFO -- claiming the lock FIRST is what
        # makes the shutdown order come out unchanged. Nothing between them
        # reads either, and a gateway bind that fails now simply releases it on
        # the way out.
        store_lock = hold_store_lock(_store.root)
        try:
            # The gateway listener starts FIRST (of the things that matter to
            # them): reconcilers (built below, for envs resumed on restart) need
            # the ACTUAL resolved port to point BackingAws's goaws.yaml at.
            #
            # v0.7.7: a TASK on THIS loop, not a second uvicorn on a second
            # thread -- odin ran two event loops in two threads until now. See
            # `gateway/app.py::serve_on_loop`.
            # NO `await` before `serve_on_loop(...)`: it is decorated
            # `@contextlib.asynccontextmanager`, so calling it returns the
            # context manager SYNCHRONOUSLY and `async with` does the awaiting.
            # `async with await ...` raised TypeError inside the lifespan, i.e.
            # no server started at all.
            async with serve_on_loop(gateway_app, port=_resolved_gateway_port) as gateway_port_actual:
                # ...and a watchdog that puts the lock FILE back if anything
                # deletes it (field test 4 -- see `_keep_store_lock`). The lock
                # itself survives the deletion; only the evidence odin can find
                # by path does not.
                # `create_task(coro)`, never `create_task(await coro)`: these
                # watchdogs run until cancelled, so awaiting one here would
                # never return and the lifespan would never finish starting.
                lock_watch = asyncio.create_task(_keep_store_lock(store_lock))
                # ...and the same shape for the loops themselves: nothing used to
                # notice a reconciler that had stopped ticking (see
                # `_watch_reconcilers` and `Reconciler.health`).
                loop_watch = asyncio.create_task(_watch_reconcilers(reconcilers, ws_manager))
                envs = _store.list_envs()
                if _reap_ec2_vms:
                    await _reap_orphaned_ec2_vms(_store.root, envs, gateway_stores)
                for env in envs:  # resume reconciling existing environments
                    await reconciler_for(env)
                try:
                    yield
                finally:
                    # FIRST: end every open SSE stream. uvicorn's graceful
                    # shutdown waits for in-flight requests, and a live stream
                    # never finishes on its own -- measured, the server survived
                    # two SIGTERMs and held its gateway port for minutes with one
                    # tab open. See `ConnectionManager.close_all`.
                    ws_manager.close_all()
                    lock_watch.cancel()
                    loop_watch.cancel()
                    for reconciler in reconcilers.values():
                        await reconciler.stop()
                    # Leaving this block is what stops the gateway -- after the
                    # reconcilers, exactly where `stop_in_thread` used to sit.
        finally:
            store_lock.release()

    app = FastAPI(title="odin", version=odin_version(), lifespan=lifespan)
    # Registered on EVERY app this factory builds (there is only one place an
    # app is built, which is what makes this reach every route including the
    # ones added later) -- see `_unhandled_failure`. Starlette still re-raises
    # after the response is sent, so uvicorn logs the full traceback exactly as
    # it did before; the difference is only in what the CALLER receives.
    app.add_exception_handler(Exception, _unhandled_failure)
    app.middleware("http")(_csrf_guard)
    # The saved canvas belongs to the STORE, not to the process's cwd: in
    # production `_store.root` IS `.odin`, so this is the same
    # `.odin/canvas.json` `odin backup`'s archive already resolves as
    # `root / CANVAS_NAME` -- but a caller that brought its own store (every
    # test) now reads and writes its OWN canvas instead of the real one under
    # the checkout. The module constant stays as the documented location.
    # PER-ENV canvas (owner decision, 2026-07-27): `.odin/<env>/canvas.json`,
    # alongside that env's stacks/world/events, instead of one global file that
    # made `?env=` a no-op on this one route.
    def canvas_for(env: str) -> Path:
        return _store.root / env / CANVAS_NAME

    _migrate_global_canvas(_store.root)
    app.include_router(create_canvas_router(canvas_for, ws=ws_manager))
    app.include_router(
        create_apply_router(
            _store, reconciler_for, gateway_keystore, tf_runner, lambda: gateway_port_actual, env_epoch,
            gateway_stores, gateway_state, _runtime, reconcilers, chat_sessions,
        )
    )
    app.include_router(
        create_tf_router(
            _store, tf_runner, gateway_keystore, lambda: gateway_port_actual,
            translate_cache, _runtime, gateway_stores, canvas_for, ws_manager, chat_sessions,
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

    @app.get("/stream")
    async def stream_endpoint() -> StreamingResponse:
        """The live event stream (SSE). Replaced `/ws` in v0.8.7.

        The socket it replaces was strictly server->client -- the UI never sent
        anything and this handler discarded everything it received -- so the
        duplex half was pure cost, most visibly a hand-rolled reconnect in the
        browser. `EventSource` reconnects itself, and on reconnect the UI
        backfills from `/events` below, so a gap self-heals.
        """
        return StreamingResponse(
            event_stream(ws_manager), media_type="text/event-stream", headers=SSE_HEADERS,
        )

    @app.get("/events")
    def get_events(env: str = ENV):
        return ws_manager.get_events(env)

    @app.get("/health")
    async def health():
        """Still 200, and `ok` still means "this HTTP server answered" -- that
        is what `odin start`'s readiness wait and the UI's Backend LED ask, and
        a server whose reconciler died is still serving. The reconciler answer
        is a SEPARATE field rather than a status code, so neither question can
        be mistaken for the other; `odin status` and the TopBar chip read it."""
        return {
            "ok": True,
            "gateway": {"port": gateway_port_actual},
            # Parenthesised: `await r.health().model_dump()` binds as
            # `await (r.health().model_dump())`, i.e. `.model_dump()` on the
            # COROUTINE -- an AttributeError on every /health call, which is
            # the endpoint `odin start`'s readiness wait polls.
            "reconcilers": [(await reconciler.health()).model_dump() for reconciler in reconcilers.values()],
        }

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
