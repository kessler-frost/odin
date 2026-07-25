"""The gateway's Lambda control-plane model (task V4a): functions, built to
the captured `aws_lambda_function` REST surface in
docs/superpowers/research/research-coverage.md §2d (CreateFunction's
Pending->Active waiter, the GetFunction/versions/code-signing-config poll
set) and MiniStack's own Lambda digest (§2.6: "the function model + State
machine" adopted; the RIE-container invoke path adopted as odin's REAL
substrate) -- adopted as a design, never as a dependency (NORTHSTAR
directive 5). Unlike MiniStack's own instant-or-subprocess execution, this
module's `State` is REAL: `Pending` until `compute/functions.py`'s
`FunctionRuntime` reports the function's RIE container genuinely answers, a
background thread away from the request path (the exact async-state-machine
shape `gateway/models/ec2compute.py`'s `RunInstances` uses for Lima boots).

Like ec2net/iamctl/ecr, Lambda's CONTROL PLANE has no backing to forward to:
this module is the whole answer for every `lambda:*` action (classified by
`classify.py`'s `_classify_lambda`, a FOURTH wire shape -- REST method+path,
not query-protocol or an X-Amz-Target header). The DATA plane -- Invoke --
is a real pass-through to the function's own RIE container via
`compute/functions.py::FunctionRuntime.invoke`; every other action is pure
model state.

Model decisions, each traced to the research/brief:
- CreateFunction accepts ONLY an inline `Code.ZipFile` (base64, the shape
  the TF provider's `filename` argument drives per hcl.py's `_lambda`
  builder, V4c) -- `S3Bucket`/`S3Key` deployment is out of scope for v1
  (directive 5's honesty rule: no S3-backed Lambda deploys yet, not a
  silent gap). The zip bytes are written to DISK at
  `.odin/{env}/gateway/lambda/{name}.zip`, never into the JSON sidecar (the
  brief's explicit ask -- a multi-MB deployment package has no business
  living in a JsonStore that gets read+rewritten wholesale on every
  mutation).
- `CodeSha256` is base64(sha256(zip_bytes)) -- the exact value real AWS
  (and the TF provider's own drift check) computes, so an unchanged zip
  across `apply`s never re-triggers UpdateFunctionCode.
- The function record's `State` and `LastUpdateStatus` are TWO INDEPENDENT
  state machines (verified against botocore's own lambda model -- distinct
  enums, distinct reason-code enums with NO "in progress" member in
  either): `State` only ever moves on CreateFunction (Pending -> Active/
  Failed, driven by the FIRST container boot); `LastUpdateStatus` moves on
  every code/config deploy (InProgress -> Successful/Failed) but never
  touches `State` again once a function is Active -- matching real AWS,
  where re-deploying code to an already-Active function doesn't make TF's
  own State-based create-waiter re-poll.
- `StateReasonCode`/`LastUpdateStatusReasonCode` are ONLY ever set to a
  value from their real enum (`Creating`/`Idle`/`InternalError` -- verified
  against botocore's shape definitions): neither enum has an "in progress"
  member, so a record with `LastUpdateStatus=InProgress` carries `None` for
  its reason fields (omitted from the wire, exactly like real AWS), not a
  made-up code that would round-trip wrong.
- Invoke on a non-Active function is `ResourceNotReadyException` (502) --
  a real member of the operation's own captured error set, not a made-up
  code -- rather than hanging on a container that may not even exist yet.
- Config-only updates (UpdateFunctionConfiguration: role/handler/timeout/
  memory/env vars, no new code) still restart the function's container
  (the SAME `substrate.ensure` completion path CreateFunction/
  UpdateFunctionCode use) off the EXISTING code directory -- odin has no
  notion of "just relabel the metadata, don't touch the runtime" because a
  changed `Handler` or `Environment` genuinely needs a new container
  process to pick it up.
- Tags: `TagResource`/`UntagResource`/`ListTags` are pure CRUD on the
  shared `stores.tags` store, keyed `"lambda:{functionArn}"` (same
  convention ec2net/iamctl/ecr use). `UntagResource`'s `TagKeys` is a
  REPEATED querystring param on the real wire, but `gateway/app.py` collapses
  the query string to a last-value-wins dict before any handler ever sees
  it -- a documented v1 limitation (single-key untag per call), not a
  silently dropped one.

Persistence: one `JsonStore` at `.odin/{env}/gateway/lambdactl.json`
(`stores.lambdactl`), flat keys `"fn:{name}"`. Every response wire shape
below was checked against botocore's own lambda `service-2.json`
(`rest-json` protocol -- member names ARE the wire JSON keys, no
serialization-name override anywhere in this shape family) and round-trips
through botocore's `RestJSONParser` in tests/gateway/test_lambdactl.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.compute.functions import DEFAULT_RUNTIME, FunctionRuntime, container_name
from odin.gateway import errors
from odin.gateway.keys import KeyStore, workload_env
from odin.gateway.models import logsctl
from odin.gateway.stores import NO_CHANGE, SynthStores
from odin.runtime.colima import ColimaRuntime

log = logging.getLogger("odin.gateway.lambdactl")

_DEFAULT_HANDLER = "lambda_function.lambda_handler"
_DEFAULT_TIMEOUT = 3
_DEFAULT_MEMORY = 128

# How many trailing lines of the RIE container's output one Invoke reads
# (`docker logs --tail N`): bounded so a chatty handler can't turn an invoke
# into an unbounded read, generous enough that a normal handler's whole
# output for a call fits in one window.
_LOG_TAIL_LINES = 200

_Handler = Callable[
    [str, str, bytes, SynthStores, float, FunctionRuntime, dict[str, str], KeyStore | None, int | None],
    Response,
]


def _key(name: str) -> str:
    return f"fn:{name}"


def _function(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.lambdactl.get(env, _key(name))


def _zip_path(root: Path, env: str, name: str) -> Path:
    return root / env / "gateway" / "lambda" / f"{name}.zip"


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"lambda:{arn}", {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _payload(body: bytes) -> dict:
    try:
        return json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _json(status: int, payload: dict) -> Response:
    body = {k: v for k, v in payload.items() if v is not None}
    return Response(json.dumps(body), status_code=status, media_type="application/json")


def _not_found(name: str) -> Response:
    arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"
    return errors.synth_error("lambda", "ResourceNotFoundException", f"Function not found: {arn}", 404)


def _conflict(message: str) -> Response:
    return errors.synth_error("lambda", "ResourceConflictException", message, 409)


def _invalid_parameter(message: str) -> Response:
    return errors.synth_error("lambda", "InvalidParameterValueException", message, 400)


# --- wire building -------------------------------------------------------


def _configuration_json(fn: dict) -> dict:
    environment = {"Variables": fn["environment"]} if fn["environment"] else None
    return {
        "FunctionName": fn["function_name"],
        "FunctionArn": fn["function_arn"],
        "Runtime": fn["runtime"],
        "Role": fn["role"],
        "Handler": fn["handler"],
        "CodeSize": fn["code_size"],
        "Description": fn["description"] or None,
        "Timeout": fn["timeout"],
        "MemorySize": fn["memory_size"],
        "LastModified": fn["last_modified"],
        "CodeSha256": fn["code_sha256"],
        "Version": "$LATEST",
        "Environment": environment,
        "State": fn["state"],
        "StateReason": fn["state_reason"] or None,
        "StateReasonCode": fn["state_reason_code"] or None,
        "LastUpdateStatus": fn["last_update_status"],
        "LastUpdateStatusReason": fn["last_update_status_reason"] or None,
        "LastUpdateStatusReasonCode": fn["last_update_status_reason_code"] or None,
        "PackageType": "Zip",
        "Architectures": ["x86_64"],
        "RevisionId": fn["revision_id"],
    }


def _get_function_response(fn: dict, stores: SynthStores, env: str) -> dict:
    zip_path = _zip_path(stores.root, env, fn["function_name"])
    return {
        "Configuration": _configuration_json(fn),
        "Code": {"RepositoryType": "S3", "Location": f"file://{zip_path}"},
        "Tags": _tags_for(stores, env, fn["function_arn"]) or None,
    }


# --- background completion: the async state machine (the "never block" /
# "REAL readiness, not a timer" requirement -- every mutating handler below
# returns a transitional status immediately, a daemon thread finishes the
# real container work; same shape as ec2compute.py's `_finish_boot`) -------


def _update_function(stores: SynthStores, env: str, name: str, **fields: object) -> None:
    def mutate(fn: dict | None) -> dict | object:
        if fn is None:  # deleted while the background thread was still running
            return NO_CHANGE
        fn = dict(fn)
        fn.update(fields)
        return fn

    stores.lambdactl.update(env, _key(name), mutate)


def _finish_deploy(
    stores: SynthStores, env: str, name: str, runtime: str, handler: str,
    env_vars: dict[str, str], code_dir: Path, substrate: FunctionRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None, memory_mib: int | None = None,
) -> None:
    # Workload credential injection (fix-wave 2b): resolve the function's own
    # canvas label from its `odin:node` tag (stamped by hcl.py's `_tags_block`
    # at CreateFunction; UpdateFunctionCode/Configuration never resend Tags,
    # but the store still holds them from creation, so a redeploy resolves
    # identically) and merge `workload_env`'s four AWS_* vars into the
    # CONTAINER's env only -- never into `fn["environment"]`, which
    # `_configuration_json` echoes verbatim to the TF provider (merging there
    # would surface four undeclared vars as drift on every `tofu plan`).
    arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"
    label = _tags_for(stores, env, arn).get("odin:node", "")
    container_env = dict(env_vars)
    if keystore is not None and gateway_port is not None and label:
        container_env.update(workload_env(keystore, env, label, gateway_port))
    # `ensure` REPLACES the container (`FunctionRuntime.ensure` stops any
    # remnant first), so its output starts back at line 1 -- the log-shipping
    # cursor for that stream has to be forgotten here or `_ship_logs` would
    # mistake the new container's first lines for already-ingested ones
    # (logsctl.py's `ingest_tail`/`reset_cursor`). Already-stored events are
    # untouched: this resets the read position, not the log.
    logsctl.reset_cursor(stores, env, f"/aws/lambda/{name}", container_name(env, name))
    # Deliberately broad: this runs on a daemon thread with no caller to
    # propagate an exception to -- see ec2compute.py's `_finish_boot` for
    # the identical "silent hang is forbidden" reasoning.
    try:
        substrate.ensure(env, name, runtime, handler, container_env, code_dir, memory_mib=memory_mib)
    except Exception as exc:
        log.warning("lambda container failed for function %s (env %s): %s", name, env, exc)
        _update_function(
            stores, env, name, state="Failed",
            state_reason=str(exc), state_reason_code="InternalError",
            last_update_status="Failed",
            last_update_status_reason=str(exc), last_update_status_reason_code="InternalError",
        )
        return
    _update_function(
        stores, env, name, state="Active",
        state_reason="The function is ready.", state_reason_code="Idle",
        last_update_status="Successful",
        last_update_status_reason=None, last_update_status_reason_code=None,
    )


def _spawn(target: Callable[..., None], *args: object) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


# --- CreateFunction / GetFunction / DeleteFunction ------------------------


def _create_function(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    payload = _payload(body)
    name = payload.get("FunctionName") or resource
    if _function(stores, env, name) is not None:
        return _conflict(f"Function already exists: {name}")
    zip_b64 = (payload.get("Code") or {}).get("ZipFile")
    if not zip_b64:
        return _invalid_parameter("Only an inline Code.ZipFile deployment package is supported (v1) -- S3Bucket/S3Key is not")
    try:
        zip_bytes = base64.b64decode(zip_b64)
    except (ValueError, TypeError):
        return _invalid_parameter("Code.ZipFile is not valid base64")

    zip_path = _zip_path(stores.root, env, name)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(zip_bytes)

    runtime = payload.get("Runtime") or DEFAULT_RUNTIME
    handler = payload.get("Handler") or _DEFAULT_HANDLER
    env_vars = dict((payload.get("Environment") or {}).get("Variables") or {})
    fn = {
        "function_name": name,
        "function_arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}",
        "runtime": runtime,
        "role": payload.get("Role", ""),
        "handler": handler,
        "code_size": len(zip_bytes),
        "code_sha256": _sha256_b64(zip_bytes),
        "description": payload.get("Description", ""),
        "timeout": int(payload.get("Timeout") or _DEFAULT_TIMEOUT),
        "memory_size": int(payload.get("MemorySize") or _DEFAULT_MEMORY),
        "last_modified": _now_iso(),
        "environment": env_vars,
        "state": "Pending",
        "state_reason": "The function is being created.",
        "state_reason_code": "Creating",
        "last_update_status": "InProgress",
        "last_update_status_reason": None,
        "last_update_status_reason_code": None,
        "revision_id": str(uuid.uuid4()),
    }
    stores.lambdactl.set(env, _key(name), fn)
    stores.tags.set(env, f"lambda:{fn['function_arn']}", dict(payload.get("Tags") or {}))

    code_dir = substrate.extract_code(env, name, zip_bytes)
    # Render the `Pending` response BEFORE spawning the deploy thread: the
    # store hands back the SAME dict object it was given (JsonStore keeps
    # references, not copies -- see stores.py), so `fn` here and the record
    # `_finish_deploy` later mutates via `_update_function` are literally
    # the same object. `_json` calls `json.dumps` immediately, which is what
    # actually captures the Pending snapshot -- rendering after `_spawn`
    # risked reading an already-`Active` function back on a fast (fake-
    # substrate) deploy, a real race (found via this module's own test
    # suite), not a test artifact. Same fix ec2compute.py's RunInstances
    # already documents for the identical shape.
    response = _json(201, _configuration_json(fn))
    _spawn(
        _finish_deploy, stores, env, name, runtime, handler, env_vars, code_dir, substrate,
        keystore, gateway_port, fn["memory_size"],
    )
    return response


def _get_function(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    return _json(200, _get_function_response(fn, stores, env))


def _get_function_configuration(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    return _json(200, _configuration_json(fn))


def _delete_function(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    substrate.delete(env, resource)
    _zip_path(stores.root, env, resource).unlink(missing_ok=True)
    stores.lambdactl.delete(env, _key(resource))
    stores.tags.set(env, f"lambda:{fn['function_arn']}", {})
    return Response(status_code=204)


# --- UpdateFunctionCode / UpdateFunctionConfiguration ---------------------


def _redeploy_fields(extra: dict[str, object]) -> dict[str, object]:
    """The bookkeeping every redeploy (UpdateFunctionCode or
    UpdateFunctionConfiguration) stamps on the function record, merged with
    `extra`'s own field changes -- built as one dict so a caller can apply
    the WHOLE thing inside a single `JsonStore.update()` mutator: one atomic
    read-modify-write instead of a separate mutate-then-`set()` pair that
    could interleave with `_finish_deploy`'s own `_update_function` call."""
    return {
        "last_modified": _now_iso(),
        "last_update_status": "InProgress",
        "last_update_status_reason": None,
        "last_update_status_reason_code": None,
        "revision_id": str(uuid.uuid4()),
        **extra,
    }


def _redeploy_response(
    stores: SynthStores, env: str, name: str, fn: dict, code_dir: Path, substrate: FunctionRuntime,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response:
    # `fn` is the fresh dict `JsonStore.update()` already returned (its own
    # private copy, not aliased with the store's internal object -- see
    # stores.py), so unlike CreateFunction's still-`set()`-aliased `fn`
    # there's no shared-reference race to guard here; the render-before-spawn
    # ORDER is kept anyway, for the same "the response reflects the state at
    # the instant this call was made" reasoning.
    response = _json(200, _configuration_json(fn))
    _spawn(
        _finish_deploy, stores, env, name, fn["runtime"], fn["handler"], fn["environment"], code_dir, substrate,
        keystore, gateway_port, fn["memory_size"],
    )
    return response


def _update_function_code(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    payload = _payload(body)
    zip_b64 = payload.get("ZipFile")
    if not zip_b64:
        return _invalid_parameter("Only an inline ZipFile deployment package is supported (v1) -- S3Bucket/S3Key is not")
    zip_bytes = base64.b64decode(zip_b64)
    _zip_path(stores.root, env, resource).write_bytes(zip_bytes)
    code_dir = substrate.extract_code(env, resource, zip_bytes)

    def mutate(current: dict | None) -> dict | object:
        if current is None:  # deleted concurrently between the get() above and now
            return NO_CHANGE
        updated = dict(current)
        updated.update(_redeploy_fields({"code_size": len(zip_bytes), "code_sha256": _sha256_b64(zip_bytes)}))
        return updated

    fn = stores.lambdactl.update(env, _key(resource), mutate)
    if fn is NO_CHANGE:
        return _not_found(resource)
    return _redeploy_response(stores, env, resource, fn, code_dir, substrate, keystore, gateway_port)


def _update_function_configuration(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    payload = _payload(body)

    def mutate(current: dict | None) -> dict | object:
        if current is None:  # deleted concurrently between the get() above and now
            return NO_CHANGE
        updated = dict(current)
        if "Role" in payload:
            updated["role"] = payload["Role"]
        if "Handler" in payload:
            updated["handler"] = payload["Handler"]
        if "Description" in payload:
            updated["description"] = payload["Description"]
        if "Timeout" in payload:
            updated["timeout"] = int(payload["Timeout"])
        if "MemorySize" in payload:
            updated["memory_size"] = int(payload["MemorySize"])
        if "Environment" in payload:
            updated["environment"] = dict((payload.get("Environment") or {}).get("Variables") or {})
        updated.update(_redeploy_fields({}))
        return updated

    fn = stores.lambdactl.update(env, _key(resource), mutate)
    if fn is NO_CHANGE:
        return _not_found(resource)
    code_dir = substrate.code_dir(env, resource)  # config-only: same code, restarted container
    return _redeploy_response(stores, env, resource, fn, code_dir, substrate, keystore, gateway_port)


# --- ListVersionsByFunction / GetFunctionCodeSigningConfig ----------------


def _list_versions_by_function(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    return _json(200, {"Versions": [_configuration_json(fn)], "NextMarker": None})


def _get_function_code_signing_config(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    # v1 never models a real CodeSigningConfig resource -- an empty arn is
    # the honest "none attached" answer (research: this GET is part of the
    # provider's own zero-drift refresh set, so it must exist and 200, not
    # that it needs a real signing-config identity behind it).
    return _json(200, {"FunctionName": fn["function_name"], "CodeSigningConfigArn": ""})


# --- Invoke: the data plane ------------------------------------------------


def _ship_logs(stores: SynthStores, env: str, name: str, substrate: FunctionRuntime) -> None:
    """Ship the function's RIE container tail into `/aws/lambda/{name}` -- the
    exact group real Lambda writes to, so `odin logs --group /aws/lambda/foo`
    (and a canvas `aws_cloudwatch_log_group` drawn for that name, which
    logsctl's CreateLogGroup then ADOPTS) reads what actually ran.

    ONE STREAM PER REAL CONTAINER, named after the container itself -- odin's
    honest deviation from AWS's `{date}/[$LATEST]{requestId}` stream naming.
    RIE reuses a single long-lived container for every invoke of a function,
    so there is no per-request stream boundary to honor and no requestId on
    the container's own stdout to key one off. That naming is also what makes
    `ingest_tail`'s cursor stable: the cursor counts how many lines of THIS
    container's output have been ingested, and a live container's output only
    ever grows, so re-shipping the same tail after a second invoke appends
    nothing. The one edge that costs: a redeploy replaces the container and
    its output restarts at line 1 while the cursor stays put, so the new
    container's first lines (up to the old cursor) are not re-ingested --
    accepted rather than papered over, since the alternative is streaming
    every container continuously.

    No try/except: the read is `FunctionRuntime.logs` -> the driver's
    `check=False` CLI call, which answers "" for a vanished container instead
    of raising, so there is no failure mode here that could break an invoke.
    """
    logsctl.ingest_tail(
        stores, env, f"/aws/lambda/{name}", container_name(env, name),
        substrate.logs(env, name, _LOG_TAIL_LINES),
    )


def _invoke(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    if fn["state"] != "Active":
        return errors.synth_error(
            "lambda", "ResourceNotReadyException",
            f"Function '{resource}' is not ready to be invoked (state={fn['state']})", 502,
        )
    try:
        result = substrate.invoke(env, resource, body)
    except Exception as exc:
        return errors.synth_error("lambda", "ServiceException", str(exc), 500)
    # Both outcomes ship: a handler that RAISED wrote its traceback to the
    # container's stderr, and that traceback is the whole reason CloudWatch
    # Logs exists.
    _ship_logs(stores, env, resource, substrate)
    headers = {"x-amz-function-error": result.function_error} if result.function_error else {}
    return Response(result.payload, status_code=200, media_type="application/json", headers=headers)


# --- Tags (per-function Tag/Untag/List, shared stores.tags) ---------------


def _list_tags(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    return _json(200, {"Tags": _tags_for(stores, env, fn["function_arn"])})


def _tag_resource(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    new_tags = _payload(body).get("Tags") or {}
    tags = {**_tags_for(stores, env, fn["function_arn"]), **new_tags}
    stores.tags.set(env, f"lambda:{fn['function_arn']}", tags)
    return Response(status_code=204)


def _untag_resource(resource: str, env: str, body: bytes, stores: SynthStores, now: float, substrate: FunctionRuntime, query: dict[str, str], keystore: KeyStore | None = None, gateway_port: int | None = None) -> Response:
    fn = _function(stores, env, resource)
    if fn is None:
        return _not_found(resource)
    key = (query or {}).get("tagKeys")  # see module docstring: single-key untag, documented v1 limit
    if key:
        tags = {k: v for k, v in _tags_for(stores, env, fn["function_arn"]).items() if k != key}
        stores.tags.set(env, f"lambda:{fn['function_arn']}", tags)
    return Response(status_code=204)


# --- dispatch --------------------------------------------------------------


_HANDLERS: dict[str, _Handler] = {
    "CreateFunction": _create_function,
    "GetFunction": _get_function,
    "GetFunctionConfiguration": _get_function_configuration,
    "DeleteFunction": _delete_function,
    "UpdateFunctionCode": _update_function_code,
    "UpdateFunctionConfiguration": _update_function_configuration,
    "ListVersionsByFunction": _list_versions_by_function,
    "GetFunctionCodeSigningConfig": _get_function_code_signing_config,
    "Invoke": _invoke,
    "ListTags": _list_tags,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
}


def pure_answer(
    action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float,
    substrate: FunctionRuntime | None = None, query: dict[str, str] | None = None,
    keystore: KeyStore | None = None, gateway_port: int | None = None,
) -> Response | None:
    """The whole Lambda answer -- same no-backing contract as ec2net/iamctl/
    ecr: an unmodeled action still gets a protocol-correct error, never a
    503. `substrate` is the injectable `FunctionRuntime` (or a test's fake
    stand-in with the same `ensure`/`invoke`/`extract_code`/`delete`/
    `code_dir` shape); production callers (gateway/synth.py) never pass one,
    so a real `FunctionRuntime(root=stores.root)` is used, mirroring
    ec2compute.py's `vm or InstanceVm()` default."""
    op = action.removeprefix("lambda:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("lambda", "InvalidAction", f"The action {op} is not valid.", 400)
    return handler(resource, env, body, stores, now, substrate or FunctionRuntime(ColimaRuntime(), stores.root), query or {}, keystore, gateway_port)
