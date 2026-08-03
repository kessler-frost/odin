"""**The file that proves the `kms` tile is not theatre.**

Every other test in this repo asks the API a question and checks the answer.
That is exactly the test this feature could pass with the encryption DELETED:
seal and unseal are inverses, so an API round trip is green whether or not
anything was ever encrypted. So this file does the one thing an API test
cannot -- it opens `.odin/{env}/gateway/secretsctl.json` and `ssmctl.json` with
`Path.read_text()` and asserts the user's plaintext is NOT IN THE BYTES.

Read `src/odin/gateway/kms.py`'s docstring before extending this file: it
states what the encryption buys (the plaintext is not in the sidecar; a
destroyed key is destroyed data; a ciphertext is bound to its record) and what
it does NOT (protection from anyone who can read `.odin/`, since the key file
lives in the same tree). Nothing here should be read as proving the second.

MUTATION-TESTED, and the numbers are recorded because a proof of this shape is
worthless unproven. Mutation: `kmsctl.seal` replaced by the identity function,
which is exactly what deleting the encryption would do. MEASURED:

    tests/gateway/test_kms_at_rest.py    13 failed, 2 passed   (of 15)
    tests/gateway/test_secretsctl.py
      + tests/gateway/test_ssmctl.py     52 passed             (of 52)

Fifty-two API tests could not tell. The two survivors here are the two that
deliberately only ask the API -- `..._still_reads_back_through_the_api` and the
legacy-cleartext one -- and they are split out precisely so it is visible which
assertions are load-bearing and which are not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.classify import classify
from odin.gateway.kms import ENVELOPE_PREFIX
from odin.gateway.models import kmsctl, secretsctl, ssmctl
from odin.gateway.stores import SynthStores
from odin.util import SECRET_FILE_MODE

from .conftest import split_url

ENV = "kms-at-rest"
SECRET = "db-password"
# Deliberately LOW-ENTROPY, all dictionary words. The first version of these
# read `pw-9f3a-NEVER-ON-DISK`, which the repo's own gitleaks pre-commit hook
# flagged as a `generic-api-key` (entropy 3.82) -- correctly, by its rules. A
# file whose entire purpose is grepping a value OUT of a sidecar has no need of
# a realistic-looking one, and silencing the scanner to keep a prettier fixture
# would be teaching the hook to be ignored. They stay unique enough that a hit
# in `secretsctl.json` can only have come from here.
SECRET_VALUE = "this-secret-must-never-appear-on-disk"
PARAM = "/odin/api-token"
PARAM_VALUE = "this-parameter-must-never-appear-on-disk"
KEY_LABEL = "app-key"


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


async def _answer(stores: SynthStores, service: str, req) -> Response:
    """One request through the REAL pipeline -- classify() then
    synth.pure_answer() -- so nothing here can pass by calling a handler the
    product never reaches."""
    path, query = split_url(req.url)
    classified = classify(service, req.method, path, query, req.headers, req.body)
    assert classified is not None, f"a {service} request must never be unmappable"
    action, resource = classified
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, f"{service} is all-synth: pure_answer must never fall through"
    return response


async def _create_key(stores, sink, kms, label=KEY_LABEL) -> Response:
    return await _answer(stores, "kms", sink.call(lambda: kms.create_key(
        Description=f"key for {label}",
        # The `odin:node` tag is the ONLY carrier of the canvas label, because
        # real CreateKey takes no name. `iac/hcl.py` stamps it for real.
        Tags=[{"TagKey": kmsctl.NODE_TAG, "TagValue": label}],
    )))


async def _create_secret(stores, sink, secretsmanager, **kwargs) -> Response:
    return await _answer(stores, "secretsmanager", sink.call(
        lambda: secretsmanager.create_secret(Name=SECRET, SecretString=SECRET_VALUE, **kwargs)))


async def _get_secret(stores, sink, secretsmanager) -> Response:
    return await _answer(stores, "secretsmanager", sink.call(
        lambda: secretsmanager.get_secret_value(SecretId=SECRET)))


async def _put_parameter(stores, sink, ssm, **kwargs) -> Response:
    return await _answer(stores, "ssm", sink.call(
        lambda: ssm.put_parameter(Name=PARAM, Value=PARAM_VALUE, Type="SecureString", **kwargs)))


def _sidecar(stores: SynthStores, name: str) -> Path:
    return stores.root / ENV / "gateway" / f"{name}.json"


def _keyfile(stores: SynthStores) -> Path:
    return stores.root / ENV / "kms.json"


# --- the on-disk proof ------------------------------------------------------


async def test_a_secret_value_is_not_readable_in_the_sidecar(stores, sink, secretsmanager):
    """THE assertion. Written through the real gateway, read back with
    `Path.read_text()`."""
    await _create_secret(stores, sink, secretsmanager)

    raw = _sidecar(stores, "secretsctl").read_text()
    assert SECRET_VALUE not in raw, (
        f"the secret's plaintext is sitting in {_sidecar(stores, 'secretsctl')} -- "
        "the seal did not happen"
    )
    # And it is not merely ABSENT (an empty record would also pass the line
    # above): the record holds a real envelope naming a real key.
    stored = json.loads(raw)
    versions = [v for k, v in stored.items() if k.startswith("version:")]
    assert len(versions) == 1
    assert versions[0]["secret_string"].startswith(ENVELOPE_PREFIX)


async def test_the_same_secret_still_reads_back_through_the_api(stores, sink, secretsmanager):
    """The other half, and it has to be a SEPARATE test: a single test that
    wrote, read the file, and read the API would pass with the file check
    deleted and nobody would notice which half was carrying it."""
    await _create_secret(stores, sink, secretsmanager)
    response = await _get_secret(stores, sink, secretsmanager)

    assert response.status_code == 200
    assert json.loads(response.body)["SecretString"] == SECRET_VALUE


async def test_an_ssm_parameter_value_is_not_readable_in_the_sidecar(stores, sink, ssm):
    await _put_parameter(stores, sink, ssm)

    raw = _sidecar(stores, "ssmctl").read_text()
    assert PARAM_VALUE not in raw, (
        f"the parameter's plaintext is sitting in {_sidecar(stores, 'ssmctl')} -- "
        "the seal did not happen"
    )
    assert json.loads(raw)[f"param:{PARAM}"]["value"].startswith(ENVELOPE_PREFIX)
    assert ssmctl.parameter_value(stores, ENV, PARAM) == PARAM_VALUE


async def test_a_plain_String_parameter_is_encrypted_too(stores, sink, ssm):
    """The `SecureString` trap, inverted. odin used to store both types
    identically in CLEARTEXT and say so; it now stores both identically
    SEALED. A reader who assumes `String` means "not worth encrypting" would be
    wrong in the other direction, so this pins it."""
    await _answer(stores, "ssm", sink.call(
        lambda: ssm.put_parameter(Name="/odin/plain", Value=PARAM_VALUE, Type="String")))

    raw = _sidecar(stores, "ssmctl").read_text()
    assert PARAM_VALUE not in raw
    assert ssmctl.parameter_value(stores, ENV, "/odin/plain") == PARAM_VALUE


async def test_the_key_material_is_not_in_any_gateway_sidecar(stores, sink, secretsmanager):
    """The split that makes the previous tests worth anything: if the AES bytes
    lived in `gateway/kmsctl.json` beside the ciphertext, a leak of the gateway
    directory would carry both and "encrypted at rest" would mean nothing at
    all. They live one directory up, in the file `keys.py` writes credentials
    to."""
    await _create_secret(stores, sink, secretsmanager)
    material = json.loads(_keyfile(stores).read_text())
    assert material, "no key material was written at all"

    gateway_dir = stores.root / ENV / "gateway"
    for path in sorted(gateway_dir.iterdir()):
        raw = path.read_text()
        for key_id, encoded in material.items():
            assert encoded not in raw, f"key material for {key_id!r} leaked into {path.name}"


async def test_the_key_file_is_owner_only(stores, sink, secretsmanager):
    """0600, the same mode `keys.json` gets -- verified against the real
    filesystem rather than against `atomic_write_text`'s argument, because the
    argument is what SECURITY.md's whole secrets claim rests on and this is the
    only place it is checked for this file."""
    await _create_secret(stores, sink, secretsmanager)

    assert _keyfile(stores).stat().st_mode & 0o777 == SECRET_FILE_MODE


# --- which key, not just "a" key -------------------------------------------


async def test_a_named_kms_key_is_the_one_actually_used(stores, sink, kms, secretsmanager):
    """The edge's whole meaning. `stored_key_id` reads the key off the
    ENVELOPE -- what was done -- rather than off the record's `kms_key_id`,
    which is only what was asked for. A seal path that accepted the key and
    quietly used the default would pass every other test in the repo."""
    await _create_key(stores, sink, kms)
    await _create_secret(stores, sink, secretsmanager, KmsKeyId=KEY_LABEL)

    assert secretsctl.stored_key_id(stores, ENV, SECRET) == KEY_LABEL
    assert secretsctl.current_value(stores, ENV, SECRET) == SECRET_VALUE


async def test_an_unkeyed_secret_lands_under_the_env_default_key(stores, sink, secretsmanager):
    """Encryption is UNCONDITIONAL, not opt-in: a canvas with no kms node still
    gets a sealed sidecar. That is what lets docs/limits.md say "the sidecars
    are encrypted" without a qualifier."""
    await _create_secret(stores, sink, secretsmanager)

    assert secretsctl.stored_key_id(stores, ENV, SECRET) == kmsctl.DEFAULT_KEY_ID


async def test_naming_a_key_that_does_not_exist_fails_loudly_and_writes_nothing(stores, sink, secretsmanager):
    """Never a silent fallback to the default. A user who asked for key X and
    got key `odin-default` would have been told their secret is under their own
    key while it is not -- the exact shape honesty rule 2 is about."""
    response = await _create_secret(stores, sink, secretsmanager, KmsKeyId="no-such-key")

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["__type"] == "EncryptionFailure"
    assert "no-such-key" in body["message"]
    assert not secretsctl.secret_exists(stores, ENV, SECRET), (
        "a failed CreateSecret left a half-made secret behind"
    )


# --- key loss ---------------------------------------------------------------


async def test_losing_the_key_makes_the_secret_unreadable_and_says_which_key(
    stores, sink, kms, secretsmanager,
):
    """The observable consequence that makes the tile mean something. The
    material is dropped directly (a lost `kms.json`, a partial restore, a key
    deleted after its dependents were), NOT through `ScheduleKeyDeletion` --
    that call now REFUSES while a value is still sealed, which is the test
    below and a field-test finding rather than a design choice.

    The requirement pinned here is the sharp one: the failure must NAME THE
    KEY, and must never degrade to an empty string or to the envelope text --
    a caller cannot tell either of those from a real secret."""
    await _create_key(stores, sink, kms)
    await _create_secret(stores, sink, secretsmanager, KmsKeyId=KEY_LABEL)
    assert json.loads((await _get_secret(stores, sink, secretsmanager)).body)["SecretString"] == SECRET_VALUE

    assert stores.kms.delete(ENV, KEY_LABEL), "the fault injection did nothing"
    response = await _get_secret(stores, sink, secretsmanager)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["__type"] == "DecryptionFailure"
    assert KEY_LABEL in body["message"], "the error must name the key that was destroyed"
    assert SECRET_VALUE not in response.body.decode()
    assert ENVELOPE_PREFIX not in response.body.decode(), (
        "the raw envelope was handed back as if it were the secret"
    )


async def test_losing_the_key_makes_the_parameter_unreadable_and_says_which_key(stores, sink, kms, ssm):
    """The sibling. Honesty rule 2's "fix the SHAPE not the instance": both
    value planes carry the same failure, so both get the same guard."""
    await _create_key(stores, sink, kms)
    await _put_parameter(stores, sink, ssm, KeyId=KEY_LABEL)
    assert stores.kms.delete(ENV, KEY_LABEL), "the fault injection did nothing"

    response = await _answer(stores, "ssm", sink.call(lambda: ssm.get_parameter(Name=PARAM)))

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["__type"] == "InvalidKeyId"
    assert KEY_LABEL in body["message"]
    assert PARAM_VALUE not in response.body.decode()


async def test_deleting_a_key_that_still_seals_something_is_REFUSED_and_names_it(
    stores, sink, kms, secretsmanager, ssm,
):
    """**Found by the field test, not by review, and it wedged a real env.**

    Without this guard: delete the `kms` node from a canvas whose secret still
    names it and `odin apply` reports `applied`, exit 0 — and from that moment
    `odin destroy` can NEVER succeed, because tofu's refresh reads
    `aws_secretsmanager_secret_version` and gets the `DecryptionFailure` this
    model correctly returns. Measured through the real CLI against a real
    server:

        Error: reading Secrets Manager Secret Version (...): GetSecretValue,
        StatusCode: 400, DecryptionFailure: KMS key 'kms-app-key' has no key
        material ... cannot be recovered

    An apply that reports success and leaves an environment nothing can tear
    down is honesty rule 2's shape exactly, and it is the same wedge
    `DeleteSecret`'s immediate deviation exists to prevent. So the refusal
    names WHAT IS STILL STANDING, which is what makes it actionable rather
    than merely safe."""
    await _create_key(stores, sink, kms)
    await _create_secret(stores, sink, secretsmanager, KmsKeyId=KEY_LABEL)
    await _put_parameter(stores, sink, ssm, KeyId=KEY_LABEL)

    response = await _answer(stores, "kms", sink.call(
        lambda: kms.schedule_key_deletion(KeyId=KEY_LABEL)))

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["__type"] == "KMSInvalidStateException"
    assert SECRET in body["message"] and PARAM in body["message"], (
        "the refusal must name every value that is still sealed, not just the count"
    )
    # And the key is STILL USABLE -- a refusal that half-deleted would be worse
    # than the wedge it prevents.
    assert stores.kms.exists(ENV, KEY_LABEL)
    assert secretsctl.current_value(stores, ENV, SECRET) == SECRET_VALUE


async def test_deleting_the_key_AFTER_its_dependents_are_gone_succeeds(stores, sink, kms, secretsmanager):
    """The other half, and it has to be here: a guard that made the key
    undeletable would have replaced a wedge with a leak. Remove the secret and
    the key really does go — which is also the ordering `tofu destroy` follows
    on its own, because `kms_key_id = aws_kms_key.x.key_id` makes the secret
    depend on the key."""
    await _create_key(stores, sink, kms)
    await _create_secret(stores, sink, secretsmanager, KmsKeyId=KEY_LABEL)
    await _answer(stores, "secretsmanager", sink.call(
        lambda: secretsmanager.delete_secret(SecretId=SECRET)))

    response = await _answer(stores, "kms", sink.call(
        lambda: kms.schedule_key_deletion(KeyId=KEY_LABEL)))

    assert response.status_code == 200
    assert not stores.kms.exists(ENV, KEY_LABEL)


async def test_losing_the_key_FILE_is_the_same_named_failure(stores, sink, secretsmanager):
    """Not just the API deletion path: an operator who loses `kms.json` (a bad
    restore, a partial `odin export`) must get the same answer. The store
    caches material in memory, so the file is removed AND the cache dropped --
    which is exactly what a fresh process would see."""
    await _create_secret(stores, sink, secretsmanager)
    _keyfile(stores).unlink()
    stores.kms.forget_env(ENV)

    response = await _get_secret(stores, sink, secretsmanager)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["__type"] == "DecryptionFailure"
    assert kmsctl.DEFAULT_KEY_ID in body["message"]
    assert "cannot be recovered" in body["message"]


# --- the ciphertext is bound to WHERE IT LIVES -----------------------------


async def test_moving_a_ciphertext_into_another_secret_fails_to_decrypt(stores, sink, secretsmanager):
    """The AEAD's additional data is `{env}|{service}|{name}`, so a blob copied
    from one record into another does not open. Without it, anyone who could
    edit the sidecar could swap a low-value secret's ciphertext into a
    high-value name and odin would serve it as that secret."""
    await _create_secret(stores, sink, secretsmanager)
    await _answer(stores, "secretsmanager", sink.call(lambda: secretsmanager.create_secret(
        Name="other-secret", SecretString="a-different-value")))

    path = _sidecar(stores, "secretsctl")
    stored = json.loads(path.read_text())
    victim = next(k for k in stored if k.startswith(f"version:{SECRET}:"))
    thief = next(k for k in stored if k.startswith("version:other-secret:"))
    stored[thief]["secret_string"] = stored[victim]["secret_string"]
    path.write_text(json.dumps(stored))
    stores.secretsctl.forget_env(ENV)

    response = await _answer(stores, "secretsmanager", sink.call(
        lambda: secretsmanager.get_secret_value(SecretId="other-secret")))

    assert response.status_code == 400
    assert json.loads(response.body)["__type"] == "DecryptionFailure"
    assert SECRET_VALUE not in response.body.decode()


async def test_a_ciphertext_from_another_env_does_not_open(stores, sink, secretsmanager, tmp_path):
    """Same binding, one axis over. Two envs are meant to be isolated; a
    ciphertext that opened across them would make the isolation cosmetic."""
    await _create_secret(stores, sink, secretsmanager)
    envelope = json.loads(_sidecar(stores, "secretsctl").read_text())
    blob = next(v["secret_string"] for k, v in envelope.items() if k.startswith("version:"))

    # Same key id, same secret name, same material -- only the env differs.
    stores.kms.create("other-env", kmsctl.DEFAULT_KEY_ID)
    material = json.loads(_keyfile(stores).read_text())
    other = stores.root / "other-env" / "kms.json"
    other.write_text(json.dumps({kmsctl.DEFAULT_KEY_ID: material[kmsctl.DEFAULT_KEY_ID]}))
    stores.kms.forget_env("other-env")

    with pytest.raises(Exception) as excinfo:
        stores.kms.open_envelope("other-env", blob, kmsctl.aad("other-env", "secretsmanager", SECRET))

    assert kmsctl.DEFAULT_KEY_ID in str(excinfo.value)


# --- a store written by an older odin --------------------------------------


async def test_a_legacy_cleartext_value_is_still_served_and_is_logged(stores, sink, secretsmanager, caplog):
    """`records.py` holds the rule that an older store stays readable, and this
    is that rule for the value plane -- refusing would make every pre-v0.8.18
    env unopenable. It is LOGGED rather than silent because a value odin served
    without decrypting is precisely the case where odin must not imply it
    decrypted something."""
    await _create_secret(stores, sink, secretsmanager)
    path = _sidecar(stores, "secretsctl")
    stored = json.loads(path.read_text())
    version = next(k for k in stored if k.startswith("version:"))
    stored[version]["secret_string"] = "written-by-an-older-odin"
    path.write_text(json.dumps(stored))
    stores.secretsctl.forget_env(ENV)

    with caplog.at_level("WARNING", logger="odin.gateway.kms"):
        response = await _get_secret(stores, sink, secretsmanager)

    assert json.loads(response.body)["SecretString"] == "written-by-an-older-odin"
    assert "CLEARTEXT" in caplog.text
