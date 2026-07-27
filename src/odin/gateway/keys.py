"""Per-(env, node) credential issuance for the odin gateway.

Every workload node gets a stable AWS-shaped access/secret key pair keyed by
(env, node_id): issuing again for the same pair returns the SAME credentials
until revoke_env drops the whole env (odin's /destroy path). Keys persist to
`.odin/{env}/keys.json` on every mutation and load lazily -- a fresh
KeyStore (e.g. after a server restart) resolves keys issued by a prior
instance without needing to reissue them, so a restart never orphans a
running container's creds.
"""
from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

from odin.aws.backings import REGION
from odin.runtime.colima import CONTAINER_HOST
from pydantic import TypeAdapter, constr

from odin.spec.store import CREDENTIALS, _load
from odin.util import atomic_write_text

_URLSAFE_ALPHABET = string.ascii_letters + string.digits + "-_"
_ACCESS_KEY_PREFIX = "AKODIN"
_ACCESS_KEY_SUFFIX_LEN = 14
_SECRET_KEY_LEN = 40

# The tofu runner's per-env identity (S2) -- issued/looked-up exactly like
# any workload node_id, but never authored by a canvas. GatewayState
# special-cases this node_id to a full-allow statement (gateway/app.py),
# so it needs no compiled iam edge to reach create-path AWS calls.
OPERATOR_NODE_ID = "__operator__"


def _random_urlsafe(length: int) -> str:
    return "".join(secrets.choice(_URLSAFE_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class Principal:
    """The (env, node) identity behind a verified access key."""

    env: str
    node_id: str


# access key + secret, exactly two, both non-empty -- the shape `issue` writes.
_KEYFILE = TypeAdapter(dict[str, tuple[constr(min_length=1), constr(min_length=1)]])


class KeyStore:
    """Issues and resolves per-(env, node) credential pairs, persisted per env."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._loaded_envs: set[str] = set()
        self._scanned_root = False
        self._by_env: dict[str, dict[str, tuple[str, str]]] = {}
        self._by_key: dict[str, Principal] = {}

    def issue(self, env: str, node_id: str) -> tuple[str, str]:
        self._ensure_loaded(env)
        nodes = self._by_env.setdefault(env, {})
        existing = nodes.get(node_id)
        if existing is not None:
            return existing
        pair = (_ACCESS_KEY_PREFIX + _random_urlsafe(_ACCESS_KEY_SUFFIX_LEN), _random_urlsafe(_SECRET_KEY_LEN))
        nodes[node_id] = pair
        self._by_key[pair[0]] = Principal(env=env, node_id=node_id)
        self._persist(env)
        return pair

    def lookup(self, access_key: str) -> Principal | None:
        principal = self._by_key.get(access_key)
        if principal is not None:
            return principal
        self._ensure_root_scanned()
        return self._by_key.get(access_key)

    def secret_for(self, access_key: str) -> str | None:
        """The secret half of an issued pair -- the `secret_for` callback
        SigV4 verification needs, without exposing the (env, node_id) ->
        pair mapping itself."""
        principal = self.lookup(access_key)
        if principal is None:
            return None
        pair = self._by_env.get(principal.env, {}).get(principal.node_id)
        return pair[1] if pair else None

    def revoke_env(self, env: str) -> None:
        self._ensure_loaded(env)
        nodes = self._by_env.pop(env, {})
        for access_key, _secret_key in nodes.values():
            self._by_key.pop(access_key, None)
        self._loaded_envs.discard(env)
        self._persist(env)

    def _path(self, env: str) -> Path:
        return self._root / env / "keys.json"

    def _ensure_loaded(self, env: str) -> None:
        if env in self._loaded_envs:
            return
        self._loaded_envs.add(env)
        path = self._path(env)
        if not path.exists():
            self._by_env.setdefault(env, {})
            return
        # VALIDATED, not just parsed, and this one is load-bearing for AUTH.
        # `{"db": "AKIAodin1234"}` -- a bare string where the pair belongs --
        # parses as JSON, and `pair[0], pair[1]` then INDEXES THE STRING: odin
        # registered access key 'A' with secret 'K', so a one-character key
        # would have authenticated as that node. The same shape that made the
        # debug route's scrub set redact single letters, except here it forges a
        # principal. `_load` turns every bad shape into one StoreUnreadable that
        # names the file; role CREDENTIALS carries the recovery, which is not
        # "delete it" -- a container already running holds the old pair.
        raw = _load(path, CREDENTIALS, _KEYFILE.validate_json)
        nodes = {node_id: (pair[0], pair[1]) for node_id, pair in raw.items()}
        self._by_env[env] = nodes
        for node_id, (access_key, _secret_key) in nodes.items():
            self._by_key[access_key] = Principal(env=env, node_id=node_id)

    def _ensure_root_scanned(self) -> None:
        if self._scanned_root:
            return
        self._scanned_root = True
        if not self._root.exists():
            return
        for env_dir in sorted(self._root.iterdir()):
            if env_dir.is_dir():
                self._ensure_loaded(env_dir.name)

    def _persist(self, env: str) -> None:
        # Access/secret key pairs: never briefly world-readable between
        # create and chmod (see util.atomic_write_text's own ordering).
        text = json.dumps(self._by_env.get(env, {}))
        atomic_write_text(self._path(env), text, mode=0o600)


def workload_env(keystore: KeyStore, env: str, node_label: str, gateway_port: int) -> dict[str, str]:
    """The four AWS-SDK env vars a workload substrate injects so it can call
    the gateway AS ITSELF (fix-wave 2b finding #2): a Lima VM's cloud-init
    (`compute/instances.py`), an ECS task container (`compute/tasks.py`), or
    a Lambda RIE container (`compute/functions.py`). `node_label` is the
    canvas node's own label -- the SAME string `agent/hcl.py` stamps as the
    `odin:node` tag on the resource being launched, so every substrate
    resolves it the identical way (see `hcl._tags_block`'s docstring).
    Issuing again for the same (env, node_label) returns the SAME keystore
    credentials (`KeyStore.issue`'s own stability contract) -- a redeploy or
    a service scaling up a second task never mints a second identity.
    `AWS_ENDPOINT_URL` uses `CONTAINER_HOST` (`host.docker.internal`), never
    `127.0.0.1` -- from inside a container/VM that's the container/VM's own
    loopback, not the Mac running the gateway (the same reasoning
    `tf_status._db_facts`'s DATABASE_URL fact and `aws/backings.py`'s
    goaws QUEUE_URL already apply)."""
    access_key, secret_key = keystore.issue(env, node_label)
    return {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_ENDPOINT_URL": f"http://{CONTAINER_HOST}:{gateway_port}",
        "AWS_DEFAULT_REGION": REGION,
    }
