"""odin FastAPI app factory.

The canvas authors a desired-state Stack; a continuous Reconciler drives reality
(per-env backing containers for the AWS-shaped resources, via Colima) and
projects what `tofu apply` created through the gateway (every TF-owned kind,
`rds` among them since W2.7); the World projects back to the canvas over
WebSocket.
"""
from __future__ import annotations

import asyncio
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
from odin.agent.hcl import TfProject, generate_tf, resource_set
from odin.api.canvas import CanvasGraph, create_canvas_router
from odin.api.debug import create_debug_router
from odin.api.logs import create_logs_router
from odin.api.ws import ConnectionManager
from odin.aws.backings import BackingAws
from odin.compute.tasks import TaskRuntime
from odin.fabric.localhost import LocalhostFabric
from odin.fabric.nebula import mesh_state
from odin.fabric.sidecar import MeshSidecar
from odin.gateway import DEFAULT_GATEWAY_PORT, GATEWAY_PORT_ENV, wiring
from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.keys import OPERATOR_NODE_ID, KeyStore, Principal
from odin.gateway.models import ec2compute, ecsctl, lambdactl, rdsctl
from odin.gateway.stores import SynthStores
from odin.reconcile import admission
from odin.reconcile.drift import DriftSweeper
from odin.reconcile.reconciler import Reconciler
from odin.runtime.colima import ColimaRuntime
from odin.simulate.runner import SimulateBusy, TfRunner, TofuNotInstalled
from odin.spec.models import Stack
from odin.spec.store import SpecStore
from odin.spec.translate import canvas_to_stack, skipped_node_types
from odin.util import odin_version

ODIN_DIR = Path(".odin")
CANVAS_PATH = ODIN_DIR / "canvas.json"
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


def create_apply_router(
    store: SpecStore, reconciler_for, keystore: KeyStore, runner: TfRunner, gateway_port, env_epoch: dict[str, int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/apply")
    async def apply(graph: CanvasGraph, env: str = ENV) -> dict:
        reconciler = await reconciler_for(env)
        canvas = graph.model_dump()
        stack = canvas_to_stack(canvas, env=env)
        rev = store.apply(stack)
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        return {"status": "applied", "rev": rev, "env": env, "skipped": skipped_node_types(canvas)}

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

        body: dict = {"status": "destroyed", "env": env, "tf": None}
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
            if status["workspace_exists"]:
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
                    # Not a request-level error: the reconciler half still runs below.
                    body["tf"] = {"status": "unavailable", "exit_code": None, **_TOFU_NOT_INSTALLED}
                except SimulateBusy as exc:  # a second call won the race after our guard passed
                    return JSONResponse(status_code=409, content={"error": str(exc)})
                else:
                    body["tf"] = {"status": "ok" if result.ok else "failed", "exit_code": result.exit_code}
                    if not result.ok:
                        body["tf"]["tail"] = list(result.tail)

            store.apply(Stack(env=env))  # empty desired state -> the tick below prunes all

        await reconciler.tick()
        keystore.revoke_env(env)  # gateway-issued keys die with the env they belong to
        return JSONResponse(status_code=200, content=body)

    @router.get("/world")
    def world(env: str = ENV) -> dict:
        return store.current_world(env).model_dump()

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


class ImportTfRequest(BaseModel):
    source: Literal["hcl", "live"]
    hcl: str = ""
    resources: list[dict] = []  # [{"type": "s3", "id": "uploads"}, ...] -- see import_tf.LiveResource


def create_tf_router(
    store: SpecStore, runner: TfRunner, keystore: KeyStore, gateway_port,
    translate_cache: translate_mod.TranslateCache, runtime, stores: SynthStores,
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
    async def tf_apply(env: str = ENV) -> JSONResponse:
        stack = store.get_stack(env)
        # Owner directive B1: reject BEFORE tofu ever runs, not after it's
        # already spawned real containers/VMs that then fail one-by-one.
        rejection = await _admission_rejection(runtime, store, stack)
        if rejection is not None:
            return rejection
        project = generate_tf(stack)
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
        }
        if not result.ok:
            body["tail"] = list(result.tail)
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


def create_apply_full_router(
    store: SpecStore, reconciler_for, runner: TfRunner, keystore: KeyStore, gateway_port, env_epoch: dict[str, int],
    translate_cache: translate_mod.TranslateCache, runtime, stores: SynthStores,
) -> APIRouter:
    """S5 -- the UI's single Apply button: /apply's exact canvas->Stack->tick
    semantics, then translate (S3b) and, when the canvas has TF-supported
    resources, `tofu apply` through the gateway (S2). Every non-busy outcome
    is a 200 with an honest per-half status -- the reconciler half can
    genuinely succeed while tofu fails ("applied_tf_failed"); 409 only when a
    tofu run is already in flight for the env."""
    router = APIRouter()

    @router.post("/apply-full")
    async def apply_full(graph: CanvasGraph, env: str = ENV) -> JSONResponse:
        # Busy guard BEFORE any mutation (mirrors SimulateBusy's own message):
        # no reconcile, no store write while a tofu run holds the env's lock.
        if runner.status(env)["running"]:
            return JSONResponse(
                status_code=409,
                content={"error": f"a tofu run is already in progress for env {env!r}"},
            )
        canvas = graph.model_dump()
        stack = canvas_to_stack(canvas, env=env)

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
        body = {
            "status": "applied", "rev": None, "env": env,
            "skipped": skipped_node_types(canvas),
            "refined": translated.refined, "unsupported": translated.unsupported,
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
        ecsctl.converge_services(stores, env, TaskRuntime(), keystore, gateway_port())
        # The same recovery for lambda, and for the same reason: a function's
        # RIE container is its EXECUTION ENVIRONMENT, not a TF resource -- an
        # `aws_lambda_function`'s config doesn't change when its container is
        # destroyed out of band (and the provider has no state attribute to
        # diff on), so tofu's plan is empty forever. Real Lambda's own control
        # plane replaces a dead sandbox; this is odin's equivalent. Idempotent:
        # only a `Failed` function is re-`ensure`d, an Active one is untouched.
        lambdactl.converge_functions(stores, env, keystore=keystore, gateway_port=gateway_port())
        # W2.7: and the same recovery for rds. A Postgres container is odin's
        # execution substrate for a resource whose terraform config is
        # unchanged (`status` is read-only Computed in the provider's schema),
        # so tofu's plan is empty and only this can bring a killed database
        # back. Idempotent: an `available` instance is untouched, a `failed`
        # one is re-created and re-`pg_ready`-gated. This is what makes the
        # scenario-2 crash/recover behavior survive the move off the
        # reconciler -- see reconcile/drift.py's rds notes.
        rdsctl.converge_db_instances(stores, env)
        # W2.6: and push each live database's SG-compiled firewall into its mesh
        # sidecar. An apply is exactly the right cadence -- security groups are
        # TF-owned, so an edited `db-sg` only reaches the gateway here, and
        # nebula reads its firewall at startup. Also heals a sidecar that was
        # killed under a still-running database. See rdsctl.ensure_db_mesh.
        rdsctl.ensure_db_mesh(stores, env)
        await reconciler.tick()  # kick an immediate pass; the loop continues it
        return JSONResponse(status_code=200, content=body)

    return router


async def _reap_orphaned_ec2_vms(root: Path, envs: list[str]) -> None:
    """Best-effort (release finding #4): `limactl` being unavailable, or
    any other reaper failure, must never block server startup -- this is a
    one-shot cleanup pass, not something reconciling depends on. Runs off
    the event loop thread (`limactl list`/`delete` are blocking subprocess
    calls that can take real wall-clock time for however many VMs exist)."""
    try:
        reaped = await asyncio.to_thread(ec2compute.reap_orphaned_vms, root, envs)
        if reaped:
            log.warning("startup reaper deleted %d orphaned EC2 VM(s): %s", len(reaped), reaped)
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
        envs = _store.list_envs()
        if _reap_ec2_vms:
            await _reap_orphaned_ec2_vms(_store.root, envs)
        for env in envs:  # resume reconciling existing environments
            await reconciler_for(env)
        try:
            yield
        finally:
            for reconciler in reconcilers.values():
                await reconciler.stop()
            stop_in_thread(gateway_server, gateway_thread)

    app = FastAPI(title="odin", version=odin_version(), lifespan=lifespan)
    app.middleware("http")(_csrf_guard)
    app.include_router(create_canvas_router(CANVAS_PATH))
    app.include_router(
        create_apply_router(_store, reconciler_for, gateway_keystore, tf_runner, lambda: gateway_port_actual, env_epoch)
    )
    app.include_router(
        create_tf_router(
            _store, tf_runner, gateway_keystore, lambda: gateway_port_actual,
            translate_cache, _runtime, gateway_stores,
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
