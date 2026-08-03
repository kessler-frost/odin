"""The gateway's Secrets Manager model (task W2.4): the
`aws_secretsmanager_secret` (+ `aws_secretsmanager_secret_version`) CONTROL
plane *and* the value data plane -- odin's answer to "where do secrets live",
with an IAM edge as the thing that grants access to one.

Like ec2net/iamctl/ecr/lambdactl/ecsctl/logsctl, `secretsmanager` has no
backing container to forward to: this module is the whole answer for every
`secretsmanager:*` action (classified by `classify.py`'s
`_classify_secretsmanager` -- the JSON-target wire shape,
`X-Amz-Target: secretsmanager.*`, verified against botocore's own
`secretsmanager` service model: protocol `json`, jsonVersion 1.1,
targetPrefix `secretsmanager`).

Control plane (what the TF AWS provider drives): CreateSecret /
DescribeSecret / UpdateSecret / DeleteSecret / ListSecrets, tag CRUD
(TagResource / UntagResource -- reads ride DescribeSecret's own `Tags`
member, since Secrets Manager has no ListTagsForResource at all), plus
GetResourcePolicy so the provider's read path gets a real answer instead of
an unmodeled-action error.

Data plane: GetSecretValue / PutSecretValue / UpdateSecretVersionStage over
a per-env JSON sidecar (`stores.secretsctl`, at
`.odin/{env}/gateway/secretsctl.json`) -- the same JsonStore shape every
other model module uses, written `0600` like `keys.json` (JsonStore's own
`_persist_locked` sets the mode; nothing here is world-readable, even
briefly).

**THE PLAINTEXT RULE IS RETIRED (v0.8.18), and the retraction is the point.**
Until v0.8.17 this paragraph read: "A secret's value is stored in that sidecar
as CLEARTEXT. There is no KMS here and no encryption at rest -- `KmsKeyId` is
accepted, stored and echoed back for TF fidelity, and encrypts NOTHING." Every
word of that was true, and `KmsKeyId` was the sharpest instance of it: a field
the wire accepted, the record kept and `DescribeSecret` echoed, so terraform
saw a key and nothing was encrypted by it.

It is load-bearing now. A version's `SecretString`/`SecretBinary` is stored as
an `odin-kms-v1:{keyId}:{base64}` envelope, AES-256-GCM, keyed by real material
odin generates and keeps at `.odin/{env}/kms.json` (0600, one directory above
this sidecar). `KmsKeyId` names WHICH key; a secret that names none is sealed
under the env's default key, so encryption is unconditional rather than opt-in.
`gateway/kms.py`'s docstring states exactly what that buys and what it does
not -- read it before repeating the claim, because the failure mode of this
feature is a second overclaim replacing the first.

Two consequences worth stating here rather than discovering:
  * The file mode (0600) is still the whole protection for the KEY. Encryption
    moved the secret out of one file, not out of `.odin/`.
  * A destroyed key is destroyed data. `ScheduleKeyDeletion` is IMMEDIATE
    (kmsctl deviation 2), so `GetSecretValue` on a secret whose key is gone
    answers `DecryptionFailure` NAMING THE KEY -- never an empty string, never
    the raw envelope, and never a silent fall back to some other key.

This module is still deliberately the ONLY place that can hand a value back,
and only to a principal whose canvas edge grants
`secretsmanager:GetSecretValue` on that secret's node.

Versioning fidelity (v1, deliberately bounded): a version is created by
CreateSecret-with-a-value / PutSecretValue / UpdateSecret-with-a-value, gets
`AWSCURRENT`, and pushes the previous current version to `AWSPREVIOUS` (the
one AWS behaviour terraform's `aws_secretsmanager_secret_version` diffs on).
`UpdateSecretVersionStage` moves/removes an arbitrary label. What is NOT
modeled: rotation (`RotateSecret` is an unmodeled action, `RotationEnabled`
is always false), `RestoreSecret`, and replica regions
(`AddReplicaRegions` is accepted and ignored; `ReplicationStatus` is always
absent). Each is recorded in ROADMAP.md's limits rather than half-answered.

TWO deliberate deviations from real AWS, both in service of "the canvas is
the source of truth and Apply must converge":

1. **DeleteSecret is IMMEDIATE.** Real Secrets Manager schedules deletion
   7-30 days out and refuses to re-create the name in the meantime;
   `RecoveryWindowInDays` is accepted here and ignored, the record is gone
   when the call returns, and `DeletionDate` reports that moment. Without
   this, "empty canvas + Apply = full teardown" followed by a re-Apply would
   wedge on `InvalidRequestException: scheduled for deletion`. (iac/hcl.py
   emits `recovery_window_in_days = 0` for the same reason: the generated HCL
   says out loud what odin actually does, and means the same thing against
   real AWS.)
2. **ARNs carry no random suffix.** Real ARNs end `...:secret:name-AbCdEf`;
   odin's are `...:secret:name`, so they're deterministic per env and a
   name<->ARN round trip needs no lookup table. Everything that takes a
   `SecretId` accepts either the bare name or the ARN (`_secret_name`).
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.kms import KeyUnavailable
from odin.gateway.models import kmsctl
from odin.gateway.stores import SynthStores

# The service half of the AEAD's additional data (`kmsctl.aad`): a stored
# envelope is bound to (env, "secretsmanager", secret name), so one moved into
# another secret's record -- or another env's file -- fails to open rather than
# quietly answering with the wrong secret.
_AAD_SERVICE = "secretsmanager"

CURRENT_STAGE = "AWSCURRENT"
PREVIOUS_STAGE = "AWSPREVIOUS"
# `ListSecrets`'s own AWS default page size; also the cap when a caller asks
# for nothing in particular.
DEFAULT_MAX_RESULTS = 100


def secret_arn(name: str) -> str:
    """The secret's ARN -- suffix-less by design (module docstring), so it's
    also the tags-store key (`"secretsmanager:{arn}"`) and what the TF
    provider stores as the resource's `arn`/`id`."""
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}"


def _secret_name(secret_id: str) -> str:
    """The bare secret NAME out of either form a `SecretId` arrives in (an
    ARN or the name itself) -- the same "accept both, key on one" rule
    logsctl's `_group_from_arn` keeps for log groups."""
    _prefix, sep, name = secret_id.partition(":secret:")
    return name if sep else secret_id


def _json(payload: dict) -> Response:
    return Response(json.dumps(payload), media_type="application/x-amz-json-1.1")


def _not_found(message: str) -> Response:
    return errors.synth_error("secretsmanager", "ResourceNotFoundException", message, 400)


def _exists(message: str) -> Response:
    return errors.synth_error("secretsmanager", "ResourceExistsException", message, 400)


def _invalid(message: str) -> Response:
    return errors.synth_error("secretsmanager", "InvalidParameterException", message, 400)


def _drop_none(payload: dict) -> dict:
    """Omit an unset optional member entirely -- real AWS omits rather than
    sending null, and the TF provider reads a null back as a real value (the
    exact drift `_sns_fix_subscription_attributes` exists to undo for goaws)."""
    return {k: v for k, v in payload.items() if v is not None}


# --- store keys ------------------------------------------------------------


def _secret_key(name: str) -> str:
    return f"secret:{name}"


def _version_key(name: str, version_id: str) -> str:
    return f"version:{name}:{version_id}"


def _secret(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.secretsctl.get(env, _secret_key(name))


def _secrets(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.secretsctl.items(env).items() if k.startswith("secret:")]


def _versions(stores: SynthStores, env: str, name: str) -> list[dict]:
    prefix = f"version:{name}:"
    return [v for k, v in stores.secretsctl.items(env).items() if k.startswith(prefix)]


def _version_by_id(stores: SynthStores, env: str, name: str, version_id: str) -> dict | None:
    return stores.secretsctl.get(env, _version_key(name, version_id))


def _version_by_stage(stores: SynthStores, env: str, name: str, stage: str) -> dict | None:
    matching = [v for v in _versions(stores, env, name) if stage in v["version_stages"]]
    return max(matching, key=lambda v: v["created_date"]) if matching else None


def _tags_for(stores: SynthStores, env: str, name: str) -> dict[str, str]:
    return stores.tags.get(env, f"secretsmanager:{secret_arn(name)}", {})


def _set_tags(stores: SynthStores, env: str, name: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"secretsmanager:{secret_arn(name)}", tags)


def _tags_from_list(items: object) -> dict[str, str]:
    entries = items if isinstance(items, list) else []
    return {e["Key"]: e.get("Value", "") for e in entries if isinstance(e, dict) and e.get("Key")}


def _tags_to_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


# --- wire shapes (member names verified against botocore's model) ----------


def _version_stages_map(stores: SynthStores, env: str, name: str) -> dict[str, list[str]]:
    return {v["version_id"]: list(v["version_stages"]) for v in _versions(stores, env, name)}


def _wire_secret(record: dict, tags: dict[str, str], stages: dict[str, list[str]] | None) -> dict:
    return _drop_none({
        "ARN": record["arn"],
        "Name": record["name"],
        "Description": record["description"],
        "KmsKeyId": record["kms_key_id"],
        "RotationEnabled": False,
        "CreatedDate": record["created_date"],
        "LastChangedDate": record["last_changed_date"],
        "Tags": _tags_to_list(tags),
        "VersionIdsToStages": stages or None,
    })


def _wire_value(stores: SynthStores, env: str, record: dict, version: dict) -> dict:
    """The value shape -- and the ONE place a stored envelope becomes the
    string the caller asked for. Both members go through `kmsctl.unseal`, which
    raises `KeyUnavailable` (naming the key) if the material is gone; the sole
    `except` in this module, in `pure_answer`, turns that into a
    `DecryptionFailure`. Neither ever degrades to an empty value or to the
    envelope text, because a caller cannot tell those from a real secret."""
    name = record["name"]
    return _drop_none({
        "ARN": record["arn"],
        "Name": name,
        "VersionId": version["version_id"],
        "SecretString": kmsctl.unseal(stores, env, version["secret_string"], _AAD_SERVICE, name),
        # A blob arrives base64-encoded on the JSON wire and is sealed exactly
        # as it arrived, so echoing it back needs no decode step -- only the
        # unseal, which returns the same base64 text that was sent.
        "SecretBinary": kmsctl.unseal(stores, env, version["secret_binary"], _AAD_SERVICE, name),
        "VersionStages": list(version["version_stages"]),
        "CreatedDate": version["created_date"],
    })


# --- versions --------------------------------------------------------------


def _seal(stores: SynthStores, env: str, name: str, key_id: str | None, value: object) -> str | None:
    """`value` sealed under the secret's key, or None when it has no value.
    The ONE place a plaintext enters this module's storage."""
    if not isinstance(value, str) or not value:
        return None
    return kmsctl.seal(stores, env, key_id, _AAD_SERVICE, name, value)


def _new_version(
    stores: SynthStores, env: str, name: str, payload: dict, now_epoch: float, key_id: str | None,
) -> str:
    """Store a new version carrying `payload`'s SecretString/SecretBinary and
    return its VersionId. AWS uses the caller's `ClientRequestToken` AS the
    version id when one is supplied (and a random UUID otherwise) -- kept
    faithfully, since terraform's own `aws_secretsmanager_secret_version`
    reads the returned id straight back into state.

    `key_id` is the secret's `KmsKeyId` (None -> the env default key), threaded
    from the RECORD by every caller rather than read out of `payload`: a
    `PutSecretValue` carries no KmsKeyId at all, so reading the payload would
    have silently re-keyed every rewrite to the default and left the first
    version unreadable under a key the record still advertised.

    Stage bookkeeping: the new version takes whatever `VersionStages` the
    caller asked for (`AWSCURRENT` by default) and any stage it claims is
    removed from whichever version held it; a displaced `AWSCURRENT` becomes
    `AWSPREVIOUS`, which is real AWS's own shift and the only stage motion
    v1 models on its own (module docstring)."""
    version_id = payload.get("ClientRequestToken") or uuid.uuid4().hex
    # SEAL FIRST, before any store write. A `KeyUnavailable` here (the canvas
    # named a key that does not exist) must leave the store exactly as it was:
    # sealing after the stage bookkeeping below would displace AWSCURRENT and
    # then raise, leaving a secret with no current version and no new one --
    # odin destroying a working secret while reporting the failure of a
    # different operation.
    sealed_string = _seal(stores, env, name, key_id, payload.get("SecretString"))
    sealed_binary = _seal(stores, env, name, key_id, payload.get("SecretBinary"))
    stages = [s for s in (payload.get("VersionStages") or [CURRENT_STAGE]) if isinstance(s, str)]
    for existing in _versions(stores, env, name):
        kept = [s for s in existing["version_stages"] if s not in stages]
        if CURRENT_STAGE in stages and CURRENT_STAGE in existing["version_stages"]:
            kept = [PREVIOUS_STAGE, *kept]
        if kept != existing["version_stages"]:
            stores.secretsctl.set(
                env, _version_key(name, existing["version_id"]), {**existing, "version_stages": kept},
            )
    stores.secretsctl.set(env, _version_key(name, version_id), {
        "secret_name": name,
        "version_id": version_id,
        "secret_string": sealed_string,
        "secret_binary": sealed_binary,
        "version_stages": stages,
        "created_date": now_epoch,
    })
    return version_id


def _has_value(payload: dict) -> bool:
    return bool(payload.get("SecretString") or payload.get("SecretBinary"))


# --- control plane ---------------------------------------------------------


def _create_secret(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    # No `if not name` guard: `_missing_identifier` (dispatch, below) rejects an
    # empty `Name` for EVERY op now, this one included.
    name = payload.get("Name") or ""
    if _secret(stores, env, name) is not None:
        return _exists(f"The operation failed because the secret {name} already exists.")
    epoch = time.time()
    key_id = payload.get("KmsKeyId")
    # The value is sealed BEFORE the record is written, for the same reason
    # `_new_version` seals before its own bookkeeping: a CreateSecret naming a
    # key that does not exist must leave no half-made secret behind.
    version_id = _new_version(stores, env, name, payload, epoch, key_id) if _has_value(payload) else None
    stores.secretsctl.set(env, _secret_key(name), {
        "name": name,
        "arn": secret_arn(name),
        "description": payload.get("Description"),
        # NO LONGER DECORATIVE (v0.8.18): this is the key `_new_version` above
        # actually sealed with, not a string kept for terraform's benefit.
        "kms_key_id": key_id,
        "resource_policy": None,
        "created_date": epoch,
        "last_changed_date": epoch,
    })
    _set_tags(stores, env, name, _tags_from_list(payload.get("Tags")))
    return _json(_drop_none({"ARN": secret_arn(name), "Name": name, "VersionId": version_id}))


def _describe_secret(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    return _json(_wire_secret(record, _tags_for(stores, env, name), _version_stages_map(stores, env, name)))


def _update_secret(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    epoch = time.time()
    key_id = payload.get("KmsKeyId", record["kms_key_id"])
    # Changing the key re-keys only what is written FROM HERE ON. Older
    # versions stay sealed under whichever key sealed them and stay readable,
    # because the envelope carries its own key id -- so a re-key never
    # retroactively bricks history, and never silently claims to have
    # re-encrypted it either.
    version_id = _new_version(stores, env, name, payload, epoch, key_id) if _has_value(payload) else None
    stores.secretsctl.set(env, _secret_key(name), {
        **record,
        "description": payload.get("Description", record["description"]),
        "kms_key_id": key_id,
        "last_changed_date": epoch,
    })
    return _json(_drop_none({"ARN": record["arn"], "Name": name, "VersionId": version_id}))


def _delete_secret(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    # Immediate, RecoveryWindowInDays ignored -- deviation 1 (module docstring).
    for key in list(stores.secretsctl.items(env)):
        if key == _secret_key(name) or key.startswith(f"version:{name}:"):
            stores.secretsctl.delete(env, key)
    _set_tags(stores, env, name, {})
    return _json({"ARN": record["arn"], "Name": name, "DeletionDate": time.time()})


def _matches_filters(record: dict, tags: dict[str, str], filters: object) -> bool:
    """Every filter must match (AWS ANDs filters, ORs a filter's values).
    v1 matches `name`/`description`/`tag-key`/`tag-value`/`all` as
    case-insensitive SUBSTRINGS; AWS's own word-prefix semantics and its `!`
    negation prefix are NOT modeled (recorded in ROADMAP.md), and an
    unrecognized filter key matches NOTHING rather than being silently
    dropped -- a dropped filter would hand back secrets the caller asked to
    exclude."""
    haystacks = {
        "name": [record["name"]],
        "description": [record["description"] or ""],
        "tag-key": list(tags),
        "tag-value": list(tags.values()),
        "all": [record["name"], record["description"] or "", *tags, *tags.values()],
    }
    for entry in filters if isinstance(filters, list) else []:
        candidates = haystacks.get(entry.get("Key", ""), [])
        values = [v.lower() for v in (entry.get("Values") or [])]
        if not any(v in h.lower() for h in candidates for v in values):
            return False
    return True


def _list_secrets(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    records = sorted(_secrets(stores, env), key=lambda r: r["name"])
    limit = int(payload.get("MaxResults") or DEFAULT_MAX_RESULTS)
    entries = [
        _wire_secret(r, _tags_for(stores, env, r["name"]), None)
        for r in records
        if _matches_filters(r, _tags_for(stores, env, r["name"]), payload.get("Filters"))
    ]
    # No pagination: `NextToken` is never emitted, so a paginator terminates
    # after this one page (`MaxResults` truncates rather than paging).
    return _json({"SecretList": entries[:limit]})


def _tag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    if _secret(stores, env, name) is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    _set_tags(stores, env, name, {**_tags_for(stores, env, name), **_tags_from_list(payload.get("Tags"))})
    return _json({})


def _untag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    if _secret(stores, env, name) is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    remove = set(payload.get("TagKeys") or [])
    _set_tags(stores, env, name, {k: v for k, v in _tags_for(stores, env, name).items() if k not in remove})
    return _json({})


def _get_resource_policy(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    """Modeled purely so the provider's READ path gets a real answer. odin
    has no way to author a secret resource policy (the canvas grants access
    via IAM edges, which the gateway enforces for real) -- so the answer is
    always "there is no policy", never a stored-but-inert document that
    would look enforced and wouldn't be."""
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    return _json({"ARN": record["arn"], "Name": name})


# --- data plane ------------------------------------------------------------


def _get_secret_value(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    version_id = payload.get("VersionId")
    stage = payload.get("VersionStage") or CURRENT_STAGE
    version = (
        _version_by_id(stores, env, name, version_id) if version_id
        else _version_by_stage(stores, env, name, stage)
    )
    if version is None:
        return _not_found(
            f"Secrets Manager can't find the specified secret value for "
            f"{'VersionId' if version_id else 'staging label'}: {version_id or stage}"
        )
    return _json(_wire_value(stores, env, record, version))


def _put_secret_value(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    if not _has_value(payload):
        return _invalid("SecretString or SecretBinary is required")
    # `PutSecretValue` carries no KmsKeyId of its own: the key comes off the
    # RECORD, so a rewrite stays under the key the canvas chose.
    version_id = _new_version(stores, env, name, payload, time.time(), record["kms_key_id"])
    version = _version_by_id(stores, env, name, version_id) or {}
    return _json({
        "ARN": record["arn"], "Name": name, "VersionId": version_id,
        "VersionStages": list(version.get("version_stages") or []),
    })


def _update_secret_version_stage(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = _secret_name(payload.get("SecretId") or "")
    record = _secret(stores, env, name)
    if record is None:
        return _not_found(f"Secrets Manager can't find the specified secret: {name}")
    stage = payload.get("VersionStage") or ""
    remove_from, move_to = payload.get("RemoveFromVersionId"), payload.get("MoveToVersionId")
    for version in _versions(stores, env, name):
        stages = [s for s in version["version_stages"] if s != stage or version["version_id"] != remove_from]
        if version["version_id"] == move_to and stage not in stages:
            stages = [*stages, stage]
        if stages != version["version_stages"]:
            stores.secretsctl.set(
                env, _version_key(name, version["version_id"]), {**version, "version_stages": stages},
            )
    return _json({"ARN": record["arn"], "Name": name})


# --- reads odin's own control plane uses (never the SigV4 wire) ------------


def secret_exists(stores: SynthStores, env: str, name: str) -> bool:
    return _secret(stores, env, name) is not None


def current_value(stores: SynthStores, env: str, name: str) -> str | None:
    """The AWSCURRENT version's `SecretString`, DECRYPTED, or None when the
    secret has no current version (or holds binary only). Used by odin's own
    tests and the World projection's existence check -- deliberately NOT
    exposed on any HTTP route, so the ONLY way a value leaves this process is a
    `GetSecretValue` whose principal an IAM edge allowed.

    It goes through the same `kmsctl.unseal` the wire path does, so a test
    asserting on it proves the same round trip a caller gets -- and RAISES
    `KeyUnavailable` on a lost key rather than answering None, because a helper
    that returned None for "gone" and None for "no version" would make the
    at-rest tests unable to tell a destroyed key from an empty secret."""
    version = _version_by_stage(stores, env, name, CURRENT_STAGE)
    if version is None:
        return None
    return kmsctl.unseal(stores, env, version.get("secret_string"), _AAD_SERVICE, name)


def stored_key_id(stores: SynthStores, env: str, name: str) -> str | None:
    """Which KMS key the AWSCURRENT version is actually SEALED UNDER, read off
    the envelope rather than off the record's `kms_key_id`.

    The distinction is the whole point: the record's field is what was ASKED
    for, and this is what was DONE. A test comparing the two is what would
    catch a seal path that accepted a key and quietly used the default."""
    version = _version_by_stage(stores, env, name, CURRENT_STAGE)
    return kmsctl.key_of(version.get("secret_string")) if version else None


# --- dispatch --------------------------------------------------------------
#
# logsctl's defect, in this module's own dialect, and found the same way: every
# handler reads `_secret_name(payload.get("SecretId") or "")` and exactly ONE
# of them (CreateSecret) used to check the result. Measured by calling the real
# handlers with `SecretId` omitted, nine ops answered
#
#   400 ResourceNotFoundException
#       "Secrets Manager can't find the specified secret: "
#
# -- which blames a name the caller never sent. Fixed HERE, once, for the same
# reason it is fixed once in logsctl: no handler is wrong; nothing stood
# between the wire and the assumption that an identifier had arrived.
#
# The member lists are exactly botocore's OWN `required` metadata for the
# `secretsmanager` model, and a real boto3 client refuses to send a request
# without them (`describe_secret()` -> `ParamValidationError: Missing required
# parameter in input: "SecretId"`), so this path belongs to raw HTTP clients,
# never to terraform.
#
# `VersionStage` is in here for a failure one shade worse than a vacuous
# error: `UpdateSecretVersionStage` with an empty stage matched no version, so
# it moved nothing and answered 200 -- odin reporting a success it had not
# performed. ListSecrets is deliberately absent (a filter-less list is a
# legitimate call), and `PutSecretValue`'s "SecretString or SecretBinary is
# required" stays in the handler: that is a VALUE, not an identifier, and it
# already says what is missing.
#
# `InvalidParameterException` is the code because it is the one this module
# already chose for `CreateSecret`'s own missing `Name`. Real AWS's wire code
# here cannot be observed -- botocore never lets the request leave -- so
# agreeing with ourselves beats inventing a second answer.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "CreateSecret": ("Name",),
    "DescribeSecret": ("SecretId",),
    "UpdateSecret": ("SecretId",),
    "DeleteSecret": ("SecretId",),
    "TagResource": ("SecretId",),
    "UntagResource": ("SecretId",),
    "GetResourcePolicy": ("SecretId",),
    "GetSecretValue": ("SecretId",),
    "PutSecretValue": ("SecretId",),
    "UpdateSecretVersionStage": ("SecretId", "VersionStage"),
}


def _missing_identifier(op: str, payload: dict) -> str | None:
    """The first required identifier this request did not carry, or None."""
    return next(
        (m for m in _REQUIRED.get(op, ()) if not str(payload.get(m) or "").strip()), None,
    )


# Which `secretsmanager` error code a KMS failure wears, per op. Only
# `GetSecretValue` reads a stored value, so it is the only DECRYPTION failure;
# every other op that touches the key is writing one.
_KMS_FAILURE_CODE = {"GetSecretValue": "DecryptionFailure"}
_WRITE_KMS_FAILURE = "EncryptionFailure"

_Handler = Callable[[dict, str, SynthStores, float], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateSecret": _create_secret,
    "DescribeSecret": _describe_secret,
    "UpdateSecret": _update_secret,
    "DeleteSecret": _delete_secret,
    "ListSecrets": _list_secrets,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
    "GetResourcePolicy": _get_resource_policy,
    "GetSecretValue": _get_secret_value,
    "PutSecretValue": _put_secret_value,
    "UpdateSecretVersionStage": _update_secret_version_stage,
}


async def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """The whole Secrets Manager answer -- same no-backing contract as
    ec2net/iamctl/ecr/logsctl: an unmodeled action (RotateSecret,
    RestoreSecret, ...) gets a protocol-correct error, never a 503 and never
    a silent forward."""
    op = action.removeprefix("secretsmanager:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("secretsmanager", "InvalidAction", f"The action {op} is not valid.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    missing = _missing_identifier(op, payload)
    if missing:
        return _invalid(f"{missing} is required")
    # The ONE `except` in this module, and it exists so a lost or wrong KMS key
    # can never be answered with a plausible-looking value. `KeyUnavailable`
    # carries the key id; `kmsctl.decryption_failure` puts it in the message.
    # Both codes are real `secretsmanager` error shapes (botocore's own model):
    # a READ that cannot decrypt is `DecryptionFailure`, a WRITE that cannot
    # encrypt is `EncryptionFailure`.
    try:
        return handler(payload, env, stores, now)
    except KeyUnavailable as exc:
        return kmsctl.decryption_failure(
            "secretsmanager", _KMS_FAILURE_CODE.get(op, _WRITE_KMS_FAILURE), exc,
        )
