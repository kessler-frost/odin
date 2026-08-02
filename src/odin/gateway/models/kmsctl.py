"""The gateway's KMS model -- and the ONE reason it exists, stated before the
API surface, because the obvious version of this feature is theatre.

The obvious build was "emit `kms_key_id` on buckets, databases and queues".
odin encrypts none of those: RustFS's SSE is unverified, a Postgres container
has no storage encryption, dynalite has none. A green `kms` node beside them
would claim a property odin does not have -- the same promise `ui/src/lib/
catalog.ts` refused when it denied the tile any `iamActions`, on the stated
grounds that "a permission odin can neither enforce nor reach is a promise the
engine cannot keep, and offering it is worse than offering nothing".

So this key encrypts the ONE thing odin really holds: the values in
`.odin/{env}/gateway/secretsctl.json` and `ssmctl.json`. Both files stored
CLEARTEXT until v0.8.18 -- `KmsKeyId`/`KeyId` were "accepted, stored and echoed
back for TF fidelity and encrypt NOTHING", in those modules' own words -- and
now the field is load-bearing: `secretsctl._new_version` and
`ssmctl._put_parameter` seal through `seal()` below, and the value only becomes
readable again through `unseal()`. `gateway/kms.py`'s docstring states the
bounds of that claim (what it buys, and what it does not); read it before
repeating the claim anywhere else.

WIRE SHAPE, verified against botocore's own `kms` service model rather than
assumed: protocol `json`, jsonVersion 1.1, targetPrefix **`TrentService`**
(KMS's internal name -- NOT `kms`, which is the endpointPrefix this gateway
dispatches on). Like iamctl/ecr/logsctl/secretsctl/ssmctl there is no backing
container: this module is the whole answer for every `kms:*` action.

Modeled: CreateKey / DescribeKey / ListKeys / ScheduleKeyDeletion (the control
plane `aws_kms_key` drives), the reads the TF provider makes on every refresh
(GetKeyPolicy / GetKeyRotationStatus / ListResourceTags), the writes it makes
on an update (PutKeyPolicy / UpdateKeyDescription / EnableKeyRotation /
DisableKeyRotation / EnableKey / DisableKey / TagResource / UntagResource), and
the data plane Encrypt / Decrypt / GenerateDataKey -- which are real, use the
same AES-256-GCM material, and are therefore the only `kms:*` permissions the
catalog offers.

Not modeled, and each is a protocol-correct `InvalidAction` rather than a
silent 200: ALIASES (CreateAlias/ListAliases/DeleteAlias), grants, key
policies as anything but an opaque round-tripped string, asymmetric keys
(`KeySpec` other than SYMMETRIC_DEFAULT is stored and echoed; the material is
always AES-256), multi-region keys, and imported key material. Recorded in
docs/limits.md.

THREE DELIBERATE DEVIATIONS from real AWS, each for the same reason the
Secrets Manager model already deviates -- the canvas is the source of truth and
Apply must converge:

1. **The KeyId IS the canvas label**, not a UUID. Real `CreateKey` carries no
   name at all, so the label rides in on the `odin:node` tag `agent/hcl.py`
   stamps on every resource, and the ARN is `...:key/{label}`. This is
   secretsctl's own "ARNs carry no random suffix ... deterministic per env and
   a name<->ARN round trip needs no lookup table" applied one service over, and
   it is what lets `classify.py::_kms_resource` reduce every KeyId form to the
   bare label with no store access -- which is what makes an IAM edge drawn to
   a `kms` node enforce for real. A CreateKey with no such tag falls back to a
   uuid4 hex, exactly as AWS would, and is then simply not addressable by a
   canvas edge.
2. **ScheduleKeyDeletion is IMMEDIATE.** `PendingWindowInDays` is accepted and
   ignored; the material is gone when the call returns and `DeletionDate`
   reports that moment. Real KMS schedules 7-30 days out and keeps the key
   usable until then. Without this, "empty canvas + Apply = full teardown"
   followed by a re-Apply would wedge, the same way a scheduled-for-deletion
   secret wedges (secretsctl deviation 1). The consequence is REAL and is the
   point: anything encrypted under that key is unreadable from that moment, and
   `GetSecretValue` answers `DecryptionFailure` naming the key rather than an
   empty string.
3. **Key rotation is a FLAG, not a rotation.** `EnableKeyRotation` stores
   `true` and `GetKeyRotationStatus` echoes it, so terraform's
   `enable_key_rotation` round-trips without drift, and nothing ever re-keys.
   Recorded in docs/limits.md rather than implied away -- a rotation odin
   pretended to perform would leave the old material in place under a claim it
   had been replaced.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from collections.abc import Callable

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.kms import KeyUnavailable, envelope_key_id, is_envelope
from odin.gateway.stores import SynthStores

# The tag `agent/hcl.py::_tags_block` stamps on every resource it emits; for a
# kms key it is the ONLY carrier of the canvas label (deviation 1 above).
NODE_TAG = "odin:node"

# The key every secret and parameter is sealed under when nothing named one --
# which is the whole reason "odin encrypts these sidecars" is unconditional
# rather than "encrypts them if you drew a kms node". It is a REAL key: it is
# created on first use, `ListKeys`/`DescribeKey` report it, and deleting it
# destroys exactly as much as deleting any other. A `kms -> secret` edge
# overrides it, which is what makes the tile mean something specific.
DEFAULT_KEY_ID = "odin-default"
DEFAULT_KEY_DESCRIPTION = (
    "odin's default envelope key -- seals every secret/parameter no kms node was drawn for"
)

_DEFAULT_KEY_SPEC = "SYMMETRIC_DEFAULT"
_DEFAULT_KEY_USAGE = "ENCRYPT_DECRYPT"
# ListKeys' own AWS default page size.
DEFAULT_LIMIT = 100
# What `GetKeyPolicy` answers when nobody has put one. A real account gets the
# `default` policy document; odin stores whatever terraform sends and hands
# back this when it sent nothing, so `aws_kms_key.policy` round-trips either way.
_DEFAULT_POLICY = json.dumps({"Version": "2012-10-17", "Statement": []})


def key_arn(key_id: str) -> str:
    return f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{key_id}"


def bare_key_id(value: str) -> str:
    """The bare key id out of every form a `KeyId` arrives in -- kept in
    lock-step with `classify.py::_kms_resource`, which must reach the same
    answer without touching the store. `arn:aws:kms:...:key/{id}` -> id;
    `alias/{name}` -> name (odin models no aliases, but a caller that spells one
    still lands on a comprehensible id rather than the literal "alias/x");
    anything else unchanged."""
    tail = value.rpartition(":key/")[2] or value
    return tail.removeprefix("alias/")


def _json(payload: dict) -> Response:
    return Response(json.dumps(payload), media_type="application/x-amz-json-1.1")


def _not_found(key_id: str) -> Response:
    return errors.synth_error(
        "kms", "NotFoundException", f"Key '{key_id}' does not exist", 400,
    )


def _invalid(message: str) -> Response:
    return errors.synth_error("kms", "ValidationException", message, 400)


def _drop_none(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if v is not None}


# --- store keys ------------------------------------------------------------


def _key_key(key_id: str) -> str:
    return f"key:{key_id}"


def _record(stores: SynthStores, env: str, key_id: str) -> dict | None:
    return stores.kmsctl.get(env, _key_key(key_id))


def _records(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.kmsctl.items(env).items() if k.startswith("key:")]


def _tags_for(stores: SynthStores, env: str, key_id: str) -> dict[str, str]:
    return stores.tags.get(env, f"kms:{key_arn(key_id)}", {})


def _set_tags(stores: SynthStores, env: str, key_id: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"kms:{key_arn(key_id)}", tags)


def _tags_from_list(items: object) -> dict[str, str]:
    """KMS spells a tag `{"TagKey": ..., "TagValue": ...}`, NOT the
    `{"Key": ..., "Value": ...}` every other service modeled here uses --
    verified against botocore's own `Tag` shape. Read the common spelling and
    `CreateKey` loses the `odin:node` tag, which is the ONLY thing carrying the
    canvas label, and every key on the canvas becomes a uuid nothing can name."""
    entries = items if isinstance(items, list) else []
    return {
        e["TagKey"]: e.get("TagValue", "")
        for e in entries
        if isinstance(e, dict) and e.get("TagKey")
    }


def _tags_to_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"TagKey": key, "TagValue": value} for key, value in sorted(tags.items())]


# --- wire shapes (member names verified against botocore's `kms` model) -----


def _wire_metadata(record: dict) -> dict:
    return _drop_none({
        "AWSAccountId": ACCOUNT,
        "KeyId": record["key_id"],
        "Arn": record["arn"],
        "CreationDate": record["creation_date"],
        "Enabled": record["enabled"],
        "Description": record["description"],
        "KeyUsage": record["key_usage"],
        "KeyState": record["key_state"],
        "Origin": "AWS_KMS",
        "KeyManager": "CUSTOMER",
        "KeySpec": record["key_spec"],
        "CustomerMasterKeySpec": record["key_spec"],
        "EncryptionAlgorithms": ["SYMMETRIC_DEFAULT"],
        "MultiRegion": False,
    })


# --- odin's own control plane (never the SigV4 wire) -----------------------
#
# `seal`/`unseal` are the ONLY way a secret or parameter value crosses between
# plaintext and storage. Two functions, five call sites (secretsctl's
# `_new_version`/`_wire_value`/`current_value`, ssmctl's `_put_parameter`/
# `_wire_parameter`+`parameter_value`), so the boundary is small enough to
# audit by eye -- which matters more here than anywhere else in the gateway,
# because a path that forgot to call them would store a plaintext behind a
# green test.


def aad(env: str, service: str, name: str) -> str:
    """The AEAD's additional authenticated data: WHERE this ciphertext lives.
    Binding it means a blob copied from one record to another -- or from
    another env's file -- fails to decrypt instead of silently answering with
    the wrong secret. `|` is not a legal character in an AWS service prefix and
    the env comes first, so no two (env, service, name) triples collide."""
    return f"{env}|{service}|{name}"


def ensure_key(stores: SynthStores, env: str, key_id: str, description: str) -> None:
    """Mint material AND a metadata record for `key_id` if it has neither.
    Idempotent, and deliberately the only place the two are created together:
    a key with material and no record would be invisible to `ListKeys`, and a
    record with no material would describe a key that cannot decrypt."""
    stores.kms.create(env, key_id)
    if _record(stores, env, key_id) is None:
        stores.kmsctl.set(env, _key_key(key_id), {
            "key_id": key_id,
            "arn": key_arn(key_id),
            "description": description,
            "enabled": True,
            "key_state": "Enabled",
            "key_usage": _DEFAULT_KEY_USAGE,
            "key_spec": _DEFAULT_KEY_SPEC,
            "rotation_enabled": False,
            "policy": None,
            "creation_date": time.time(),
        })


def seal(stores: SynthStores, env: str, key_id: str | None, service: str, name: str, plaintext: str) -> str:
    """`plaintext` as an envelope, under `key_id` or the env default.

    Raises `KeyUnavailable` when a NAMED key does not exist. That is deliberate
    and is the honest half of the feature: silently falling back to the default
    would encrypt a user's secret under a key they did not choose and report
    success, which is this repo's most-repeated bug in a new costume. The
    default key, by contrast, is created on demand -- nobody asked for it by
    name, so there is nothing to be wrong about.
    """
    if not key_id:
        ensure_key(stores, env, DEFAULT_KEY_ID, DEFAULT_KEY_DESCRIPTION)
        return stores.kms.seal(env, DEFAULT_KEY_ID, aad(env, service, name), plaintext)
    resolved = bare_key_id(key_id)
    if not stores.kms.exists(env, resolved):
        raise KeyUnavailable(
            resolved,
            f"was named as the encryption key for {service} {name!r} in env {env!r} and does "
            f"not exist -- draw a kms node (or create the key) before the resource that uses it",
        )
    return stores.kms.seal(env, resolved, aad(env, service, name), plaintext)


def unseal(stores: SynthStores, env: str, stored: str | None, service: str, name: str) -> str | None:
    """The plaintext behind a stored value, or None for a record that has none.
    Raises `KeyUnavailable` (naming the key) when the material is gone."""
    if stored is None:
        return None
    return stores.kms.open_envelope(env, stored, aad(env, service, name))


def key_of(stored: str | None) -> str | None:
    """Which key a stored value is sealed under, without decrypting it -- what
    an error path quotes, and what proves at rest that a value went under the
    key the canvas drew rather than the default."""
    return envelope_key_id(stored) if stored and is_envelope(stored) else None


# --- control plane ---------------------------------------------------------


def _create_key(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    tags = _tags_from_list(payload.get("Tags"))
    # Deviation 1: the canvas label rides in on `odin:node`; no tag -> a uuid,
    # like real AWS, and then simply not addressable from the canvas.
    key_id = tags.get(NODE_TAG) or uuid.uuid4().hex
    # REJECTED, not normalised, and the choice is the interesting part.
    #
    # This line used to store the RAW tag while every other op in this module
    # resolves through `bare_key_id`. The two agree only while `bare_key_id` is
    # the identity, which it is not for `alias/...` or an ARN -- MEASURED
    # against these handlers before the fix:
    #
    #   CreateKey   -> 200, KeyId 'alias/prod-key'
    #   DescribeKey -> 400 NotFoundException "Key 'prod-key' does not exist"
    #   Encrypt     -> 400 NotFoundException "Key 'prod-key' does not exist"
    #
    # A green create for a key that is dead on arrival, and then a secret
    # naming it fails quoting an id the user never typed. `agent/hcl.py`
    # declines such a LABEL on the canvas, but a direct SDK `CreateKey` and an
    # `odin import-tf` of a hand-written `aws_kms_key` both reach here without
    # passing it, so the gateway has to be right on its own.
    #
    # NORMALISING (`key_id = bare_key_id(...)`) makes create and lookup agree
    # and then moves the same defect one layer out: the canvas would show
    # `alias/prod-key` while the key is `prod-key`, so an IAM edge to that node
    # emits `.../key/alias/prod-key`, `policy.arn_label` reduces it to
    # `alias/prod-key`, `classify` reports `prod-key`, and the grant DENIES
    # SILENTLY. Rewriting a user's identifier behind their back is what buys
    # that. So: if the label has to change to be usable, say so.
    # `TagException` is real KMS's own code for a tag it will not accept
    # (botocore's `kms` model lists it on CreateKey).
    if key_id != bare_key_id(key_id):
        return errors.synth_error(
            "kms", "TagException",
            f"the {NODE_TAG!r} tag is {key_id!r}, which is not usable as a key id: odin keys a "
            f"KMS key by that tag (a canvas node's label IS its KeyId), and this one reduces to "
            f"{bare_key_id(key_id)!r} on every lookup, so the key would be created and then "
            f"never found. Use a plain name -- no 'alias/' prefix and no ARN.",
            400,
        )
    description = payload.get("Description") or ""
    # Re-CreateKey for a label that already has material KEEPS that material
    # (`ensure_key` is idempotent) -- re-applying a canvas must not orphan
    # ciphertext written under the previous apply's key. The METADATA is
    # authored fresh from this request, because CreateKey is what states it.
    ensure_key(stores, env, key_id, description)
    record = {
        **_record(stores, env, key_id),
        "description": description,
        "key_usage": payload.get("KeyUsage") or _DEFAULT_KEY_USAGE,
        "key_spec": payload.get("KeySpec") or payload.get("CustomerMasterKeySpec") or _DEFAULT_KEY_SPEC,
        "policy": payload.get("Policy"),
    }
    stores.kmsctl.set(env, _key_key(key_id), record)
    if tags:
        _set_tags(stores, env, key_id, {**_tags_for(stores, env, key_id), **tags})
    return _json({"KeyMetadata": _wire_metadata(record)})


def _describe_key(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    record = _record(stores, env, key_id)
    if record is None:
        return _not_found(key_id)
    return _json({"KeyMetadata": _wire_metadata(record)})


def _list_keys(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    records = sorted(_records(stores, env), key=lambda r: r["key_id"])
    limit = int(payload.get("Limit") or DEFAULT_LIMIT)
    # No pagination state: `NextMarker`/`Truncated` are never emitted, so a
    # paginator stops after this page (`Limit` truncates).
    return _json({
        "Keys": [{"KeyId": r["key_id"], "KeyArn": r["arn"]} for r in records[:limit]],
        "Truncated": False,
    })


def dependents(stores: SynthStores, env: str, key_id: str) -> list[str]:
    """Every secret version and parameter in `env` still SEALED under `key_id`,
    read off the envelopes themselves rather than off any record's
    `kms_key_id`/`key_id` field -- what is really unreadable if the key goes,
    not what was asked for.

    Lives here, scanning the two stores directly, because `secretsctl` and
    `ssmctl` both import this module: reaching back through their helpers
    would be a cycle.
    """
    versions = [
        f"secret {v['secret_name']!r} (version {v['version_id']})"
        for k, v in stores.secretsctl.items(env).items()
        if k.startswith("version:")
        and key_id in {key_of(v.get("secret_string")), key_of(v.get("secret_binary"))}
    ]
    parameters = [
        f"parameter {v['name']!r}"
        for k, v in stores.ssmctl.items(env).items()
        if k.startswith("param:") and key_of(v.get("value")) == key_id
    ]
    return sorted(versions) + sorted(parameters)


def _schedule_key_deletion(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """Deviation 2: IMMEDIATE. The material is destroyed here, which is the
    whole observable consequence of the tile being real.

    IT REFUSES WHILE ANYTHING IS STILL SEALED UNDER THE KEY, and that guard was
    added because a FIELD TEST wedged an environment permanently without it.
    Measured, through the real CLI against a real server: delete the `kms` node
    from a canvas whose secret still names it, `odin apply` -> `applied`, exit 0
    -- and from that moment `odin destroy` can NEVER succeed, because tofu's
    refresh reads `aws_secretsmanager_secret_version` and gets the
    `DecryptionFailure` this module correctly returns:

        Error: reading Secrets Manager Secret Version (...): GetSecretValue,
        StatusCode: 400, DecryptionFailure: KMS key 'kms-app-key' has no key
        material ... cannot be recovered

    That is honesty rule 2's exact shape one layer up: an apply that reports
    success and leaves an env nothing can tear down. It is also the same wedge
    `DeleteSecret`'s immediate deviation exists to prevent, so the fix belongs
    on the same principle -- refuse the operation that would create it, and NAME
    WHAT IS STILL STANDING.

    This does NOT weaken "a destroyed key destroys the data": remove the secret
    first (or destroy the whole env, where tofu's own dependency order does it
    for you, because `kms_key_id = aws_kms_key.x.key_id` makes the secret depend
    on the key) and the deletion goes through and the data is gone for good.
    `KMSInvalidStateException` is real KMS's own code for "this key cannot do
    that in its current state" (botocore's `kms` model lists it on
    ScheduleKeyDeletion).
    """
    key_id = bare_key_id(payload.get("KeyId") or "")
    record = _record(stores, env, key_id)
    if record is None:
        return _not_found(key_id)
    still_sealed = dependents(stores, env, key_id)
    if still_sealed:
        return errors.synth_error(
            "kms", "KMSInvalidStateException",
            f"Key '{key_id}' cannot be deleted: {len(still_sealed)} value(s) in env {env!r} are "
            f"still encrypted under it and would become unreadable -- "
            f"{', '.join(still_sealed)}. Delete those first (removing them from the canvas and "
            f"re-applying does it), or destroy the whole environment, where the dependency "
            f"ordering removes them before the key.",
            400,
        )
    stores.kms.delete(env, key_id)
    stores.kmsctl.delete(env, _key_key(key_id))
    _set_tags(stores, env, key_id, {})
    return _json({"KeyId": key_id, "KeyState": "PendingDeletion", "DeletionDate": time.time()})


def _update(payload: dict, env: str, stores: SynthStores, changes: dict) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    record = _record(stores, env, key_id)
    if record is None:
        return _not_found(key_id)
    stores.kmsctl.set(env, _key_key(key_id), {**record, **changes})
    return _json({})


def _update_key_description(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _update(payload, env, stores, {"description": payload.get("Description") or ""})


def _put_key_policy(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _update(payload, env, stores, {"policy": payload.get("Policy")})


def _enable_key_rotation(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _update(payload, env, stores, {"rotation_enabled": True})


def _disable_key_rotation(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _update(payload, env, stores, {"rotation_enabled": False})


def _enable_key(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _update(payload, env, stores, {"enabled": True, "key_state": "Enabled"})


def _disable_key(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    return _update(payload, env, stores, {"enabled": False, "key_state": "Disabled"})


def _get_key_policy(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    record = _record(stores, env, key_id)
    if record is None:
        return _not_found(key_id)
    return _json({"Policy": record["policy"] or _DEFAULT_POLICY, "PolicyName": "default"})


def _get_key_rotation_status(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    record = _record(stores, env, key_id)
    if record is None:
        return _not_found(key_id)
    return _json({"KeyId": key_id, "KeyRotationEnabled": bool(record["rotation_enabled"])})


def _list_resource_tags(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    if _record(stores, env, key_id) is None:
        return _not_found(key_id)
    return _json({"Tags": _tags_to_list(_tags_for(stores, env, key_id)), "Truncated": False})


def _tag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    if _record(stores, env, key_id) is None:
        return _not_found(key_id)
    _set_tags(stores, env, key_id, {**_tags_for(stores, env, key_id), **_tags_from_list(payload.get("Tags"))})
    return _json({})


def _untag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = bare_key_id(payload.get("KeyId") or "")
    if _record(stores, env, key_id) is None:
        return _not_found(key_id)
    remove = set(payload.get("TagKeys") or [])
    _set_tags(stores, env, key_id, {k: v for k, v in _tags_for(stores, env, key_id).items() if k not in remove})
    return _json({})


# --- data plane ------------------------------------------------------------
#
# Real, on the same AES-256-GCM material the sidecars are sealed with, which is
# what makes `kms:Encrypt`/`kms:Decrypt`/`kms:GenerateDataKey` the only three
# permissions the catalog offers on a kms node: each one is classified,
# answered, and therefore enforceable. (`ecr`'s three layer verbs are the
# counter-example this rule was written from -- tickable, classifiable, and
# answered by nothing.)
#
# A caller's ciphertext blob is the SAME envelope the sidecars use, base64'd
# again by the JSON wire, so a value can be decrypted by whichever of the two
# paths holds it. The aad is fixed (`kms|<KeyId>`) rather than the caller's
# `EncryptionContext`: odin does not model encryption context, and binding an
# unmodeled input into the tag would make Decrypt fail for a reason no error
# names. Recorded in docs/limits.md.


def _wire_key_of(payload: dict) -> str:
    return bare_key_id(payload.get("KeyId") or "")


def _encrypt(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = _wire_key_of(payload)
    if _record(stores, env, key_id) is None:
        return _not_found(key_id)
    plaintext = payload.get("Plaintext")
    if not isinstance(plaintext, str) or not plaintext:
        return _invalid("Plaintext is required")
    envelope = stores.kms.seal(env, key_id, aad(env, "kms", key_id), plaintext)
    return _json({
        "KeyId": key_arn(key_id),
        "CiphertextBlob": base64.b64encode(envelope.encode()).decode(),
        "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
    })


def _decrypt(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    blob = payload.get("CiphertextBlob")
    if not isinstance(blob, str) or not blob:
        return _invalid("CiphertextBlob is required")
    envelope = base64.b64decode(blob).decode(errors="replace")
    key_id = envelope_key_id(envelope) if is_envelope(envelope) else _wire_key_of(payload)
    try:
        plaintext = stores.kms.open_envelope(env, envelope, aad(env, "kms", key_id))
    except KeyUnavailable as exc:
        return decryption_failure("kms", "InvalidCiphertextException", exc)
    return _json({
        "KeyId": key_arn(key_id),
        "Plaintext": plaintext,
        "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
    })


def _generate_data_key(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    key_id = _wire_key_of(payload)
    if _record(stores, env, key_id) is None:
        return _not_found(key_id)
    length = int(payload.get("NumberOfBytes") or (32 if payload.get("KeySpec") != "AES_128" else 16))
    data_key = base64.b64encode(os.urandom(length)).decode()
    envelope = stores.kms.seal(env, key_id, aad(env, "kms", key_id), data_key)
    return _json({
        "KeyId": key_arn(key_id),
        "Plaintext": data_key,
        "CiphertextBlob": base64.b64encode(envelope.encode()).decode(),
    })


def decryption_failure(service: str, code: str, exc: KeyUnavailable) -> Response:
    """The one shape every service answers a lost key with: a protocol-correct
    error whose MESSAGE NAMES THE KEY. Shared so secretsctl's
    `DecryptionFailure` and ssmctl's `InvalidKeyId` cannot drift into a generic
    "decryption failed" that leaves the user guessing which key they destroyed
    -- and so that neither can ever degrade to an empty string or the raw
    envelope, which is what "fail loudly" has to mean here."""
    return errors.synth_error(service, code, str(exc), 400)


# --- dispatch --------------------------------------------------------------

_Handler = Callable[[dict, str, SynthStores, float], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateKey": _create_key,
    "DescribeKey": _describe_key,
    "ListKeys": _list_keys,
    "ScheduleKeyDeletion": _schedule_key_deletion,
    "UpdateKeyDescription": _update_key_description,
    "PutKeyPolicy": _put_key_policy,
    "GetKeyPolicy": _get_key_policy,
    "EnableKeyRotation": _enable_key_rotation,
    "DisableKeyRotation": _disable_key_rotation,
    "GetKeyRotationStatus": _get_key_rotation_status,
    "EnableKey": _enable_key,
    "DisableKey": _disable_key,
    "ListResourceTags": _list_resource_tags,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
    "Encrypt": _encrypt,
    "Decrypt": _decrypt,
    "GenerateDataKey": _generate_data_key,
}

# Every op whose identifier is REQUIRED by botocore's own model, so a raw HTTP
# client omitting it gets "KeyId is required" instead of the vacuous
# `NotFoundException: Key '' does not exist` -- secretsctl's `_REQUIRED` lesson,
# which was found by calling nine real handlers with the identifier omitted and
# watching each blame a name the caller never sent. `Decrypt` is absent
# deliberately: its identifier rides inside `CiphertextBlob`, and its own
# handler already says so.
_REQUIRES_KEY_ID = frozenset(_HANDLERS) - {"CreateKey", "ListKeys", "Decrypt"}


async def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """The whole KMS answer -- same no-backing contract as iamctl/ecr/logsctl/
    secretsctl/ssmctl: an unmodeled action (CreateAlias, CreateGrant, every
    asymmetric op) gets a protocol-correct error, never a 503 and never a
    silent forward."""
    op = action.removeprefix("kms:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("kms", "InvalidAction", f"The action {op} is not valid.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    if op in _REQUIRES_KEY_ID and not str(payload.get("KeyId") or "").strip():
        return _invalid("KeyId is required")
    return handler(payload, env, stores, now)
