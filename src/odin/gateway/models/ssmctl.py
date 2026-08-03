"""The gateway's SSM Parameter Store model (task W2.4): the
`aws_ssm_parameter` CONTROL plane *and* the value read plane -- the other
half of "where do secrets live", with an IAM edge as the thing that grants
access to a parameter.

Like ec2net/iamctl/ecr/lambdactl/ecsctl/logsctl/secretsctl, `ssm` has no
backing container to forward to: this module is the whole answer for every
`ssm:*` action (classified by `classify.py`'s `_classify_ssm` -- the
JSON-target wire shape, `X-Amz-Target: AmazonSSM.*`, verified against
botocore's own `ssm` service model: protocol `json`, jsonVersion 1.1,
targetPrefix `AmazonSSM`).

Modeled: PutParameter / GetParameter / GetParameters / GetParametersByPath /
DeleteParameter / DeleteParameters / DescribeParameters, plus tag CRUD
(AddTagsToResource / ListTagsForResource / RemoveTagsFromResource) -- the
provider reads `description`/`tier`/`allowed_pattern`/`key_id` from
DescribeParameters and tags from ListTagsForResource, so both are load-bearing
for apply -> plan zero-drift, not extras.

**EVERY PARAMETER IS ENCRYPTED AT REST (v0.8.18) -- INCLUDING A PLAIN
`String`.** Until v0.8.17 this paragraph read: "`SecureString` IS NOT ENCRYPTED
AT REST. A SecureString parameter is stored byte-for-byte the same as a String
one ... There is no KMS in odin: `KeyId` is accepted, stored and echoed back for
TF fidelity and encrypts NOTHING." That was true, and it is what this change
retracts.

A parameter's `Value` is now stored as an `odin-kms-v1:{keyId}:{base64}`
envelope, AES-256-GCM, under real material odin generates and keeps at
`.odin/{env}/kms.json` (0600, one directory above this sidecar). `KeyId` names
WHICH key; a parameter that names none is sealed under the env's default key.

NOTE THE ONE SENTENCE THAT SURVIVES INTACT: a `SecureString` is still stored
byte-for-byte the same way a `String` is. odin encrypts BOTH rather than making
the type the protection, so `SecureString` still buys nothing over `String`
here -- it is just that what both get is now real. Claiming otherwise would
reintroduce the same lie one level down.

`WithDecryption` still changes nothing about the answer: odin holds the key and
decrypts on every read regardless, because a parameter odin cannot decrypt is
one nobody can (see `docs/limits.md`). What a lost key DOES change is that
`GetParameter` answers `InvalidKeyId` NAMING THE KEY rather than a blank value.
The protection remains the file mode and the machine boundary
(`gateway/kms.py` states the bounds precisely), plus the fact that a value only
ever leaves this process through a `GetParameter*` whose principal an IAM edge
allowed.

NAME CANONICALIZATION (`canonical_name`): AWS treats a ROOT-level
parameter's leading slash as optional (`db-url` and `/db-url` are the same
parameter) while a HIERARCHICAL name's leading slash is part of the name
(`/odin/db-url`). One rule covers both, and it's the same rule
`classify.py`'s `_ssm_resource` applies -- which is what makes an IAM edge to
an `ssm` canvas node enforce correctly (the canvas label IS the parameter
name; `iac/hcl.py`'s `_ssm` builder emits `name = <label>`). The record
keeps the name EXACTLY as the caller sent it, so the wire echo can never
differ from what terraform put in the config (which would read as drift).

Not modeled (each recorded in ROADMAP.md): parameter POLICIES (`Policies` is
accepted, stored and echoed, and nothing expires/notifies on it), version
LABELS (`LabelParameterVersion`, `Selector`), `GetParameterHistory` (only the
current version is kept -- `Version` still increments on every overwrite so
terraform sees a real version change), and the `Advanced`/`Intelligent-
Tiering` tiers behaving any differently from `Standard`.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.kms import KeyUnavailable
from odin.gateway.models import kmsctl
from odin.gateway.stores import SynthStores

# The service half of the AEAD's additional data (`kmsctl.aad`). The CANONICAL
# name, not the caller's spelling, because `/db` and `db` are one parameter and
# a value written under one must open under the other.
_AAD_SERVICE = "ssm"

DEFAULT_TYPE = "String"
DEFAULT_TIER = "Standard"
DEFAULT_DATA_TYPE = "text"
VALID_TYPES = ("String", "StringList", "SecureString")
# `DescribeParameters`/`GetParametersByPath`'s own AWS default page size.
DEFAULT_MAX_RESULTS = 50


def canonical_name(name: str) -> str:
    """The one name a parameter is keyed by -- see the module docstring's
    canonicalization note. Accepts an ARN too (`arn:aws:ssm:...:parameter/x`),
    reduced to the same name form, so a caller that passes one is neither
    denied by classify nor missed by the store."""
    _prefix, sep, path = name.partition(":parameter")
    bare = (path if sep else name).lstrip("/")
    return bare if "/" not in bare else f"/{bare}"


def parameter_arn(name: str) -> str:
    canonical = canonical_name(name)
    return f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/{canonical.lstrip('/')}"


def _json(payload: dict) -> Response:
    return Response(json.dumps(payload), media_type="application/x-amz-json-1.1")


def _not_found(name: str) -> Response:
    return errors.synth_error("ssm", "ParameterNotFound", f"Parameter {name} not found.", 400)


def _already_exists(name: str) -> Response:
    return errors.synth_error(
        "ssm", "ParameterAlreadyExists",
        f"The parameter already exists. To overwrite this value, set the overwrite option in the request to true: {name}",
        400,
    )


def _validation(message: str) -> Response:
    return errors.synth_error("ssm", "ValidationException", message, 400)


def _drop_none(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if v is not None}


# --- store keys ------------------------------------------------------------


def _param_key(name: str) -> str:
    return f"param:{canonical_name(name)}"


def _param(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.ssmctl.get(env, _param_key(name))


def _params(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.ssmctl.items(env).items() if k.startswith("param:")]


def _tags_for(stores: SynthStores, env: str, name: str) -> dict[str, str]:
    return stores.tags.get(env, f"ssm:{canonical_name(name)}", {})


def _set_tags(stores: SynthStores, env: str, name: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"ssm:{canonical_name(name)}", tags)


def _tags_from_list(items: object) -> dict[str, str]:
    entries = items if isinstance(items, list) else []
    return {e["Key"]: e.get("Value", "") for e in entries if isinstance(e, dict) and e.get("Key")}


def _tags_to_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


# --- wire shapes (member names verified against botocore's `ssm` model) ----


def _wire_parameter(stores: SynthStores, env: str, record: dict) -> dict:
    """A `Parameter` -- the VALUE-carrying shape (GetParameter/GetParameters/
    GetParametersByPath), and the ONE place a stored envelope becomes the
    string the caller asked for.

    `WithDecryption` is still irrelevant, for a NEW reason: every parameter is
    encrypted now, odin holds the key, and it decrypts on every read. A
    SecureString still reads back the same way a String does (module
    docstring). What can change the answer is a LOST key -- `kmsctl.unseal`
    raises `KeyUnavailable` naming it, and the sole `except` in this module
    turns that into an `InvalidKeyId`, never a blank value."""
    return _drop_none({
        "Name": record["name"],
        "Type": record["type"],
        "Value": kmsctl.unseal(stores, env, record["value"], _AAD_SERVICE, canonical_name(record["name"])),
        "Version": record["version"],
        "LastModifiedDate": record["last_modified_date"],
        "ARN": record["arn"],
        "DataType": record["data_type"],
    })


def _wire_metadata(record: dict) -> dict:
    """A `ParameterMetadata` -- DescribeParameters' shape, which carries NO
    value (real AWS's own boundary: metadata reads need no decrypt right).
    This is where the TF provider reads `description`/`tier`/
    `allowed_pattern`/`key_id`/`policies` from, so every one of them has to
    round-trip or every plan drifts."""
    return _drop_none({
        "Name": record["name"],
        "ARN": record["arn"],
        "Type": record["type"],
        "KeyId": record["key_id"],
        "LastModifiedDate": record["last_modified_date"],
        "Description": record["description"],
        "AllowedPattern": record["allowed_pattern"],
        "Version": record["version"],
        "Tier": record["tier"],
        "DataType": record["data_type"],
    })


# --- filters ---------------------------------------------------------------

# `ParameterStringFilter.Key` -> the record field it reads. `Path` is handled
# by GetParametersByPath's own Path argument, never as a field comparison.
_FILTER_FIELDS = {
    "Name": "name", "Type": "type", "KeyId": "key_id",
    "Tier": "tier", "DataType": "data_type",
}


def _string_filter_matches(record: dict, entry: dict) -> bool:
    """One `ParameterFilters` entry. Supported keys are `_FILTER_FIELDS` with
    Option `Equals` (the default) or `BeginsWith`. An unrecognized key or
    option matches NOTHING -- failing closed, because silently dropping a
    filter would hand back parameters the caller explicitly excluded."""
    field = _FILTER_FIELDS.get(entry.get("Key", ""))
    option = entry.get("Option") or "Equals"
    values = [v for v in (entry.get("Values") or []) if isinstance(v, str)]
    if field is None or option not in ("Equals", "BeginsWith"):
        return False
    actual = record.get(field) or ""
    # A Name filter compares canonically, so `/db` and `db` don't miss each
    # other (the same equivalence `canonical_name` encodes).
    if field == "name":
        actual = canonical_name(actual)
        values = [canonical_name(v) for v in values]
    return any(actual == v if option == "Equals" else actual.startswith(v) for v in values)


def _legacy_filter_matches(record: dict, entry: dict) -> bool:
    """One entry of DescribeParameters' older `Filters` list, whose semantics
    are a PREFIX match on Name/Type/KeyId (AWS's own documented behaviour for
    that field). Any other key fails closed, same reasoning as above."""
    field = _FILTER_FIELDS.get(entry.get("Key", ""))
    values = [v for v in (entry.get("Values") or []) if isinstance(v, str)]
    if field is None:
        return False
    actual = record.get(field) or ""
    return any(actual.startswith(v) for v in values)


def _matches_all(record: dict, payload: dict) -> bool:
    legacy = payload.get("Filters") if isinstance(payload.get("Filters"), list) else []
    modern = payload.get("ParameterFilters") if isinstance(payload.get("ParameterFilters"), list) else []
    return (
        all(_legacy_filter_matches(record, e) for e in legacy)
        and all(_string_filter_matches(record, e) for e in modern)
    )


# --- handlers --------------------------------------------------------------


def _put_parameter(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("Name") or ""
    if not name:
        return _validation("Name is required")
    param_type = payload.get("Type") or DEFAULT_TYPE
    if param_type not in VALID_TYPES:
        return _validation(f"Type must be one of {', '.join(VALID_TYPES)}")
    value = payload.get("Value")
    if not isinstance(value, str) or value == "":
        return _validation("Value is required")
    existing = _param(stores, env, name)
    if existing is not None and not payload.get("Overwrite"):
        return _already_exists(name)
    key_id = payload.get("KeyId")
    record = {
        "name": name,
        "arn": parameter_arn(name),
        "type": param_type,
        # SEALED, not the plaintext -- and sealed BEFORE anything is written,
        # so a `KeyId` naming a key that does not exist leaves the store
        # untouched instead of half-updating a live parameter.
        "value": kmsctl.seal(stores, env, key_id, _AAD_SERVICE, canonical_name(name), value),
        "version": int(existing["version"]) + 1 if existing else 1,
        "description": payload.get("Description"),
        # NO LONGER DECORATIVE (v0.8.18): this is the key the value above is
        # really sealed under, not a string kept for terraform's benefit.
        "key_id": key_id,
        "allowed_pattern": payload.get("AllowedPattern"),
        "tier": payload.get("Tier") or DEFAULT_TIER,
        "data_type": payload.get("DataType") or DEFAULT_DATA_TYPE,
        "policies": payload.get("Policies"),
        "last_modified_date": time.time(),
    }
    stores.ssmctl.set(env, _param_key(name), record)
    tags = _tags_from_list(payload.get("Tags"))
    if tags:
        _set_tags(stores, env, name, {**_tags_for(stores, env, name), **tags})
    return _json({"Version": record["version"], "Tier": record["tier"]})


def _get_parameter(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("Name") or ""
    record = _param(stores, env, name)
    if record is None:
        return _not_found(name)
    return _json({"Parameter": _wire_parameter(stores, env, record)})


def _get_parameters(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    names = [n for n in (payload.get("Names") or []) if isinstance(n, str)]
    found = [(n, _param(stores, env, n)) for n in names]
    return _json({
        "Parameters": [_wire_parameter(stores, env, r) for _n, r in found if r is not None],
        "InvalidParameters": [n for n, r in found if r is None],
    })


def _slash_name(name: str) -> str:
    """The canonical name in its always-slash-prefixed HIERARCHY form
    (`db-url` -> `/db-url`, `/odin/db` unchanged) -- what path prefixes are
    compared against. A `Path` is NOT a name: its leading slash is structural,
    so it never goes through `canonical_name` (which would strip a root-level
    one and make `Path="/app"` match nothing)."""
    return f"/{canonical_name(name).lstrip('/')}"


def _get_parameters_by_path(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    path = "/" + (payload.get("Path") or "/").strip("/")
    prefix = path if path.endswith("/") else f"{path}/"
    recursive = bool(payload.get("Recursive"))
    matched = [
        r for r in _params(stores, env)
        if _slash_name(r["name"]).startswith(prefix)
        # Non-recursive = only the immediate children of `path`.
        and (recursive or "/" not in _slash_name(r["name"])[len(prefix):])
        and _matches_all(r, payload)
    ]
    matched.sort(key=lambda r: _slash_name(r["name"]))
    limit = int(payload.get("MaxResults") or DEFAULT_MAX_RESULTS)
    # No pagination state: `NextToken` is never emitted (a paginator stops
    # after this page); `MaxResults` truncates.
    return _json({"Parameters": [_wire_parameter(stores, env, r) for r in matched[:limit]]})


def _describe_parameters(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    matched = [r for r in _params(stores, env) if _matches_all(r, payload)]
    matched.sort(key=lambda r: _slash_name(r["name"]))
    limit = int(payload.get("MaxResults") or DEFAULT_MAX_RESULTS)
    return _json({"Parameters": [_wire_metadata(r) for r in matched[:limit]]})


def _delete_parameter(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("Name") or ""
    if _param(stores, env, name) is None:
        return _not_found(name)
    stores.ssmctl.delete(env, _param_key(name))
    _set_tags(stores, env, name, {})
    return _json({})


def _delete_parameters(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    names = [n for n in (payload.get("Names") or []) if isinstance(n, str)]
    deleted = [n for n in names if _param(stores, env, n) is not None]
    for name in deleted:
        stores.ssmctl.delete(env, _param_key(name))
        _set_tags(stores, env, name, {})
    return _json({
        "DeletedParameters": deleted,
        "InvalidParameters": [n for n in names if n not in deleted],
    })


def _tagged_parameter(payload: dict, env: str, stores: SynthStores) -> tuple[str, dict | None]:
    """SSM's tag calls are typed by `ResourceType` ("Parameter") and carry the
    parameter NAME as `ResourceId` -- not an ARN, unlike every other service's
    tag API modeled here."""
    name = payload.get("ResourceId") or ""
    return name, _param(stores, env, name)


def _add_tags(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name, record = _tagged_parameter(payload, env, stores)
    if record is None:
        return _not_found(name)
    _set_tags(stores, env, name, {**_tags_for(stores, env, name), **_tags_from_list(payload.get("Tags"))})
    return _json({})


def _list_tags(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name, record = _tagged_parameter(payload, env, stores)
    if record is None:
        return _not_found(name)
    return _json({"TagList": _tags_to_list(_tags_for(stores, env, name))})


def _remove_tags(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name, record = _tagged_parameter(payload, env, stores)
    if record is None:
        return _not_found(name)
    remove = set(payload.get("TagKeys") or [])
    _set_tags(stores, env, name, {k: v for k, v in _tags_for(stores, env, name).items() if k not in remove})
    return _json({})


# --- reads odin's own control plane uses (never the SigV4 wire) ------------


def parameter_exists(stores: SynthStores, env: str, name: str) -> bool:
    return _param(stores, env, name) is not None


def parameter_value(stores: SynthStores, env: str, name: str) -> str | None:
    """The stored value, DECRYPTED, for odin's own tests -- deliberately NOT on
    any HTTP route, so the only way a value leaves this process is a
    `GetParameter*` whose principal an IAM edge allowed.

    Goes through the same `kmsctl.unseal` the wire path does (so a test on it
    proves the round trip a caller gets) and RAISES `KeyUnavailable` on a lost
    key rather than answering None -- None already means "no such parameter",
    and one value for two very different facts is how an at-rest test ends up
    unable to tell a destroyed key from an empty store."""
    record = _param(stores, env, name)
    if record is None:
        return None
    return kmsctl.unseal(stores, env, record["value"], _AAD_SERVICE, canonical_name(name))


def stored_key_id(stores: SynthStores, env: str, name: str) -> str | None:
    """Which KMS key the value is actually SEALED UNDER, read off the envelope
    rather than off the record's `key_id` -- what was DONE, not what was asked
    for. See `secretsctl.stored_key_id`."""
    record = _param(stores, env, name)
    return kmsctl.key_of(record["value"]) if record else None


# --- dispatch --------------------------------------------------------------

_Handler = Callable[[dict, str, SynthStores, float], Response]

_HANDLERS: dict[str, _Handler] = {
    "PutParameter": _put_parameter,
    "GetParameter": _get_parameter,
    "GetParameters": _get_parameters,
    "GetParametersByPath": _get_parameters_by_path,
    "DescribeParameters": _describe_parameters,
    "DeleteParameter": _delete_parameter,
    "DeleteParameters": _delete_parameters,
    "AddTagsToResource": _add_tags,
    "ListTagsForResource": _list_tags,
    "RemoveTagsFromResource": _remove_tags,
}


async def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """The whole SSM answer -- same no-backing contract as
    ec2net/iamctl/ecr/logsctl/secretsctl: an unmodeled action
    (LabelParameterVersion, GetParameterHistory, every non-parameter SSM API)
    gets a protocol-correct error, never a 503 and never a silent forward."""
    op = action.removeprefix("ssm:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("ssm", "InvalidAction", f"The action {op} is not valid.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    # The ONE `except` for the crypto path, and it exists so a lost or wrong KMS
    # key can never be answered with a plausible-looking value. `InvalidKeyId`
    # is SSM's own error code for exactly this (botocore's `ssm` model lists it
    # on GetParameter/PutParameter), and `kmsctl.decryption_failure` puts the
    # key id in the message so the operator knows which one they destroyed.
    try:
        return handler(payload, env, stores, now)
    except KeyUnavailable as exc:
        return kmsctl.decryption_failure("ssm", "InvalidKeyId", exc)
