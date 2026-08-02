"""Real key material for odin's KMS model -- the bytes that make a secret's
value CIPHERTEXT in `.odin/{env}/gateway/secretsctl.json` instead of the string
the user typed.

WHAT THIS BUYS AND WHAT IT DOES NOT, first, because the `kms` tile spent its
whole life claiming a protection odin did not have and the fix is worth nothing
if it swaps one overclaim for another.

IT BUYS, and each of these is asserted by a test that reads the real file:
  * The plaintext is NOT in the sidecar. `tests/gateway/test_kms_at_rest.py`
    writes a secret through the real handlers and then greps the bytes on disk.
  * `ScheduleKeyDeletion` really destroys readability. Delete the key and
    `GetSecretValue` answers `DecryptionFailure` NAMING THE KEY, forever --
    there is no recovery and odin says so, rather than handing back an empty
    string or the envelope.
  * A ciphertext is bound to WHERE IT LIVES. The AEAD's additional data is
    `{env}|{service}|{name}`, so moving one secret's stored blob into another
    record (or another env's file) fails to decrypt instead of silently
    answering with the wrong secret.

IT DOES NOT BUY protection from someone who can read `.odin/`. The key file
lives one directory ABOVE the ciphertext (`.odin/{env}/kms.json`, mode 0600 via
`atomic_write_text`, exactly where and how `keys.py` writes issued
credentials), which separates the two files but not the two permissions: one
user account owns both. odin runs on your Mac and has nowhere to hide a key
that it can also use unattended -- real KMS's answer is an HSM the caller never
sees, and there is no local substitute for that. Saying "encrypted at rest"
without this paragraph would be the same shape of lie as the tile it replaces.

IT ALSO DOES NOT COVER the other places a secret's plaintext legitimately
lands, all of which SECURITY.md already lists and none of which this change
touches: the canvas JSON, every immutable Stack revision, `tf/main.tf` and
`tf/terraform.tfstate` in the tofu workspace, and an `odin export` archive.
This module's claim is bounded to the two gateway sidecars.

THE ENVELOPE is a string, deliberately, so no store record changes shape:

    odin-kms-v1:{key_id}:{base64(nonce || AESGCM ciphertext+tag)}

`key_id` is a canvas label and may contain `:` or `/` (an ssm-style key name);
the base64 alphabet contains neither, so parsing splits on the LAST colon
(`rpartition`) and the key id can be anything. AES-256-GCM comes from
`cryptography` (Apache-2.0 OR BSD-3-Clause, already resolved in `uv.lock` --
`pyjwt[crypto]` pulls it), which is the only AEAD available without writing
one: the stdlib has `hashlib` and `hmac` and no block cipher at all.

A LEGACY CLEARTEXT VALUE -- one written by an odin before this module existed
-- is returned AS-IS by `open_envelope`, with a WARNING that names the key file
and the record. Refusing it would make every pre-v0.8.18 env unreadable, and
`records.py` already holds the "an older store stays readable" rule for the
same reason. It is logged rather than silent because a value odin serves
without decrypting is exactly the case where odin must not imply it decrypted
something. Rewriting the secret (any `PutSecretValue`/`PutParameter`, which is
what every Apply does) replaces it with an envelope.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import TypeAdapter, constr

from odin.spec.store import CREDENTIALS, _load
from odin.util import SECRET_FILE_MODE, atomic_write_text

log = logging.getLogger("odin.gateway.kms")

ENVELOPE_PREFIX = "odin-kms-v1:"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # AES-GCM's own standard nonce length

# `{key_id: base64(32 raw bytes)}` -- VALIDATED, not merely parsed, for the
# reason `keys.py::_KEYFILE` spells out: a bare string where the key belongs
# parses as JSON and then gets INDEXED, and an AEAD handed one character of a
# key raises somewhere far from the file that caused it. Role CREDENTIALS
# because that is what this is, and because its recovery advice is the right
# one: do NOT delete the file -- ciphertext already on disk needs it.
_KEYFILE = TypeAdapter(dict[str, constr(min_length=1)])


class KeyUnavailable(Exception):
    """An envelope names a key whose material this env does not have.

    Carries `key_id` so every caller can put the KEY in its wire error rather
    than a generic "decryption failed": the whole value of `ScheduleKeyDeletion`
    being real is that the user can tell WHICH key they destroyed.
    """

    def __init__(self, key_id: str, detail: str) -> None:
        super().__init__(f"KMS key {key_id!r} {detail}")
        self.key_id = key_id


def is_envelope(value: str) -> bool:
    return value.startswith(ENVELOPE_PREFIX)


def envelope_key_id(value: str) -> str:
    """The key id an envelope names, without needing the material to read it --
    what an error message quotes when the material is gone."""
    return value[len(ENVELOPE_PREFIX):].rpartition(":")[0]


class KeyMaterial:
    """Per-env AES-256 key bytes, persisted `0600` at `.odin/{env}/kms.json`.

    Shaped like `keys.py::KeyStore` on purpose -- lazy per-env load, rewrite the
    whole file on every mutation, `atomic_write_text` with the shared
    `SECRET_FILE_MODE` so the file is never briefly world-readable between
    create and chmod. The one difference is that a failed load is NOT tolerated
    anywhere: a key that cannot be read is data loss the caller must hear about,
    so `_load` raises and nothing here catches it.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._loaded_envs: set[str] = set()
        self._by_env: dict[str, dict[str, str]] = {}

    # --- lifecycle ---------------------------------------------------------

    def create(self, env: str, key_id: str) -> None:
        """Mint 32 fresh random bytes for `key_id`. Idempotent: an existing key
        keeps its material, so re-applying a canvas never orphans ciphertext
        written under the previous apply's key."""
        self._ensure_loaded(env)
        keys = self._by_env.setdefault(env, {})
        if key_id in keys:
            return
        keys[key_id] = base64.b64encode(os.urandom(_KEY_BYTES)).decode()
        self._persist(env)

    def exists(self, env: str, key_id: str) -> bool:
        self._ensure_loaded(env)
        return key_id in self._by_env.get(env, {})

    def key_ids(self, env: str) -> list[str]:
        self._ensure_loaded(env)
        return sorted(self._by_env.get(env, {}))

    def delete(self, env: str, key_id: str) -> bool:
        """Destroy the material. Returns whether anything was there.

        This is the whole point of `ScheduleKeyDeletion` being modeled: after
        this call every envelope naming `key_id` is UNREADABLE, and
        `open_envelope` says so by name instead of degrading to a blank value.
        """
        self._ensure_loaded(env)
        removed = self._by_env.get(env, {}).pop(key_id, None) is not None
        if removed:
            self._persist(env)
        return removed

    def forget_env(self, env: str) -> bool:
        """Drop this env's cached material without writing -- the `/envs/rm`
        verb, matching `JsonStore.forget_env`. `.odin/<env>/` is about to be
        removed wholesale, so re-minting the file one line before `rmtree`
        would be a setup that outlived its teardown (`keys.py::forget_env`
        measured exactly that)."""
        self._loaded_envs.discard(env)
        return self._by_env.pop(env, None) is not None

    # --- the AEAD ----------------------------------------------------------

    def seal(self, env: str, key_id: str, aad: str, plaintext: str) -> str:
        """`plaintext` as an envelope string. Raises `KeyUnavailable` when the
        key has no material -- never falls back to another key and never stores
        the plaintext, because either would mean odin encrypting with something
        other than what the user asked for and saying nothing."""
        aead = AESGCM(self._material(env, key_id))
        nonce = os.urandom(_NONCE_BYTES)
        blob = nonce + aead.encrypt(nonce, plaintext.encode(), aad.encode())
        return f"{ENVELOPE_PREFIX}{key_id}:{base64.b64encode(blob).decode()}"

    def open_envelope(self, env: str, stored: str, aad: str) -> str:
        """The plaintext behind an envelope. A value that is NOT an envelope is
        a legacy cleartext write and comes back unchanged, loudly (module
        docstring)."""
        if not is_envelope(stored):
            log.warning(
                "gateway kms: a value in env %r is stored as CLEARTEXT (written before odin "
                "encrypted this sidecar) -- it is being served without decryption; rewrite it "
                "(any Apply does) to move it under a key", env,
            )
            return stored
        key_id, _sep, encoded = stored[len(ENVELOPE_PREFIX):].rpartition(":")
        aead = AESGCM(self._material(env, key_id))
        # InvalidTag (a wrong key, a tampered blob, or an envelope moved into a
        # record whose aad differs) and a malformed base64 body (binascii.Error
        # IS a ValueError) become the SAME named failure as missing material:
        # the caller has one thing to answer with, and it names the key.
        try:
            blob = base64.b64decode(encoded, validate=True)
            plaintext = aead.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad.encode())
        except (InvalidTag, ValueError) as exc:
            raise KeyUnavailable(
                key_id,
                f"could not decrypt this value ({type(exc).__name__}) -- the stored ciphertext "
                f"does not belong to this key, or to this record",
            ) from exc
        return plaintext.decode()

    def _material(self, env: str, key_id: str) -> bytes:
        self._ensure_loaded(env)
        encoded = self._by_env.get(env, {}).get(key_id)
        if encoded is None:
            raise KeyUnavailable(
                key_id,
                f"has no key material in {self._path(env)} -- it was deleted "
                f"(ScheduleKeyDeletion) or the file was lost, and anything encrypted "
                f"under it cannot be recovered",
            )
        return base64.b64decode(encoded)

    # --- persistence -------------------------------------------------------

    def _path(self, env: str) -> Path:
        return self._root / env / "kms.json"

    def _ensure_loaded(self, env: str) -> None:
        # `_loaded_envs.add` only on a path that really loaded -- `keys.py`
        # measured the other ordering turning a failed read into silent data
        # loss on the retry, and this file's contents are less recoverable
        # still: a lost credential is reissued, a lost key is gone.
        if env in self._loaded_envs:
            return
        path = self._path(env)
        if not path.exists():
            self._by_env.setdefault(env, {})
            self._loaded_envs.add(env)
            return
        self._by_env[env] = _load(path, CREDENTIALS, _KEYFILE.validate_json)
        self._loaded_envs.add(env)

    def _persist(self, env: str) -> None:
        atomic_write_text(
            self._path(env), json.dumps(self._by_env.get(env, {})), mode=SECRET_FILE_MODE,
        )
