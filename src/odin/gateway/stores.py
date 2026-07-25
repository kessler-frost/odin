"""Per-env JSON sidecar stores for the gateway's synthesized control-plane
(synth.py) -- tags, SNS topic attributes, SQS queue state (attributes +
delete-confirmation marker), SNS subscription delete markers.

Each store is a flat `key -> value` dict persisted at
`.odin/{env}/gateway/{name}.json`, the same lazy-load/persist-on-mutation
shape `keys.py` already uses for credentials. Unlike `GatewayState` (rebuilt
wholesale on every Apply/tick -- "the gateway is stateless"), these stores
MUST outlive a tick: a tag set via `TagQueue` has to still be there the next
time `ListQueueTags` asks, which is the whole point (research: "without it
every plan drifts on tags"). Nothing here is cleared on Apply/destroy today
-- queues/topics/tables are re-created wholesale on every canvas Apply, so a
stale entry is simply orphaned (never wrong), not actively torn down.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from odin.util import atomic_write_text

# A mutator passed to `JsonStore.update` returns this to mean "leave the
# store untouched" (no write, no persist) -- the shape every "the record is
# already gone, nothing to update" guard across the gateway model modules
# needs, without conflating "no-op" with "set the key to None".
NO_CHANGE = object()


class JsonStore:
    """A flat `key -> value` dict, one JSON file per env, loaded lazily and
    rewritten wholesale on every mutation.

    Every env has its OWN `threading.Lock`, held for the full duration of
    `get`/`set`/`delete`/`items`/`update` -- release finding #3: multiple
    reconciler/gateway-model threads calling these concurrently for the SAME
    env (e.g. a background boot-completion thread racing a synchronous
    Terminate handler) could otherwise interleave a read with another
    thread's write, or hand back a dict that changes size mid-`json.dumps`.
    Different envs never contend (separate locks), matching the store's own
    per-env file isolation.
    """

    def __init__(self, root: Path, name: str) -> None:
        self._root = root
        self._name = name
        self._loaded: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def get(self, env: str, key: str, default: Any = None) -> Any:
        with self._lock_for(env):
            return self._data(env).get(key, default)

    def set(self, env: str, key: str, value: Any) -> None:
        with self._lock_for(env):
            self._data(env)[key] = value
            self._persist_locked(env)

    def delete(self, env: str, key: str) -> None:
        with self._lock_for(env):
            self._data(env).pop(key, None)
            self._persist_locked(env)

    def items(self, env: str) -> dict[str, Any]:
        """A copy of the env's whole flat dict -- the "describe all" read the
        EC2-network model's list answers need, without callers reaching into
        `_data` directly."""
        with self._lock_for(env):
            return dict(self._data(env))

    def update(self, env: str, key: str, mutator: Callable[[Any], Any]) -> Any:
        """Atomic read-modify-write: `mutator` receives the CURRENT value for
        `key` (or `None` if absent) and returns either the new value to
        store, or the `NO_CHANGE` sentinel to leave the store untouched.
        The whole read + mutate + persist happens under the env's lock, so
        no other `get`/`set`/`delete`/`items`/`update` for this env can
        interleave with it -- the primitive every gateway-model
        read-modify-write (an instance's state transition, a function's
        redeploy bookkeeping, a task's status update) now funnels through
        instead of a bare `get()` ... `set()` pair. Returns the new value,
        or `NO_CHANGE` if nothing was written."""
        with self._lock_for(env):
            data = self._data(env)
            new_value = mutator(data.get(key))
            if new_value is NO_CHANGE:
                return NO_CHANGE
            data[key] = new_value
            self._persist_locked(env)
            return new_value

    def _lock_for(self, env: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(env, threading.Lock())

    def _data(self, env: str) -> dict[str, Any]:
        # Caller must already hold `_lock_for(env)`.
        if env not in self._loaded:
            path = self._path(env)
            self._loaded[env] = json.loads(path.read_text()) if path.exists() else {}
        return self._loaded[env]

    def _path(self, env: str) -> Path:
        return self._root / env / "gateway" / f"{self._name}.json"

    def _persist_locked(self, env: str) -> None:
        # Caller must already hold `_lock_for(env)` -- the snapshot copy is
        # kept anyway (defensive, and it's what `atomic_write_text` needs a
        # stable `text` for regardless of the lock). `mode=0o600`: this
        # sidecar can carry another env's IAM/EC2 state, not just public
        # catalog data -- never briefly world-readable.
        text = json.dumps(dict(self._data(env)))
        atomic_write_text(self._path(env), text, mode=0o600)


class SynthStores:
    """The sidecar stores synth.py (and the gateway model modules under
    `gateway/models/`) need, grouped for one-arg wiring into
    `create_gateway_app`. `root` is public so a model module can reach
    non-store per-env state under the same `.odin` tree (ec2net's Nebula
    network bootstrap needs it).

    - `tags`: keyed `"{service}:{resource}"` -> flat `{tag_key: tag_value}`,
      shared by sqs/sns/dynamodb (each service's resource-name namespace is
      disjoint by construction, but the prefix avoids any ambiguity anyway).
    - `sqs_queues`: keyed by queue name -> `{"attributes": {...},
      "deleted_at": float | None}` -- the attribute set CreateQueue seeds and
      GetQueueAttributes echoes (research: goaws's slow/incomplete
      convergence means the gateway owns this read entirely), plus the
      delete-confirmation shim's grace-window marker.
    - `sns_topics`: keyed by topic name -> `{attribute_name: value}`, seeded
      on CreateTopic, read by GetTopicAttributes, mutated by
      SetTopicAttributes (goaws has neither call at all).
    - `sns_subscriptions`: keyed by the full subscription ARN (NOT the
      classify()-derived topic-name resource -- multiple subscriptions can
      share a topic) -> the `now` Unsubscribe fired, so
      GetSubscriptionAttributes can answer NotFound immediately after.
    - `ec2net`: the EC2-network model's whole state
      (`gateway/models/ec2net.py`) -- flat keys `"vpc:{id}"` /
      `"subnet:{id}"` / `"sg:{id}"`, persisted at
      `.odin/{env}/gateway/ec2net.json`. EC2 tags live in the shared `tags`
      store above, keyed `"ec2:{resource_id}"`.
    - `iamctl`: the IAM control-plane model's whole state
      (`gateway/models/iamctl.py`) -- flat keys `"role:{name}"` /
      `"policy:{arn}"` / `"instance-profile:{name}"`, persisted at
      `.odin/{env}/gateway/iamctl.json`. IAM tags live in the shared `tags`
      store above, keyed `"iam:{arn}"`.
    - `ecr`: the ECR control-plane model's whole state
      (`gateway/models/ecr.py`) -- flat keys `"repo:{name}"`, persisted at
      `.odin/{env}/gateway/ecr.json`. ECR tags live in the shared `tags`
      store above, keyed `"ecr:{repositoryArn}"`.
    - `ec2compute`: the EC2-compute model's whole state
      (`gateway/models/ec2compute.py`, task V3) -- flat keys
      `"instance:{id}"` / `"keypair:{name}"`, persisted at
      `.odin/{env}/gateway/ec2compute.json`. Instance/key-pair tags live in
      the shared `tags` store above, keyed `"ec2:{resource_id}"` -- the SAME
      namespace ec2net.py's vpc/subnet/sg tags use (a bare EC2 resource id is
      unique across the whole `ec2:*` family by construction).
    - `lambdactl`: the Lambda control-plane model's whole state
      (`gateway/models/lambdactl.py`, task V4a) -- flat keys `"fn:{name}"`,
      persisted at `.odin/{env}/gateway/lambdactl.json`. The function's CODE
      (zip bytes) is deliberately NOT in this JSON sidecar -- it lives on
      disk at `.odin/{env}/gateway/lambda/{name}.zip` (lambdactl.py mints
      that path directly off `root`, the same way ec2net's Nebula bootstrap
      reaches non-store state). Lambda tags live in the shared `tags` store
      above, keyed `"lambda:{functionArn}"`.
    - `ecsctl`: the ECS control-plane model's whole state
      (`gateway/models/ecsctl.py`, task V5a) -- flat keys
      `"cluster:{name}"` / `"taskdef-rev:{family}"` (revision counter) /
      `"taskdef:{family}:{revision}"` / `"service:{cluster}:{name}"` /
      `"task:{cluster}:{task_id}"`, persisted at
      `.odin/{env}/gateway/ecsctl.json`.
    - `logsctl`: the CloudWatch Logs model's whole state
      (`gateway/models/logsctl.py`, task W2.1) -- flat keys `"group:{name}"` /
      `"stream:{group}:{stream}"` / `"events:{group}"` (the per-group ring
      buffer) / `"cursor:{group}:{stream}"` (the substrate log-shipping dedup
      cursor), persisted at `.odin/{env}/gateway/logsctl.json`. Log-group tags
      live in the shared `tags` store above, keyed `"logs:{logGroupArn}"` (the
      wildcard-less ARN form -- see logsctl.py's own ARN note).
    - `secretsctl`: the Secrets Manager model's whole state
      (`gateway/models/secretsctl.py`, task W2.4) -- flat keys
      `"secret:{name}"` / `"version:{name}:{versionId}"`, persisted at
      `.odin/{env}/gateway/secretsctl.json`. Secret tags live in the shared
      `tags` store above, keyed `"secretsmanager:{secretArn}"`. This sidecar
      holds secret VALUES in cleartext -- `_persist_locked`'s `mode=0o600`
      below is what keeps it owner-only, the same protection `keys.json` gets
      (see secretsctl.py's PLAINTEXT RULE and SECURITY.md's Secrets section).
    - `ssmctl`: the SSM Parameter Store model's whole state
      (`gateway/models/ssmctl.py`, task W2.4) -- flat keys `"param:{name}"`
      (the canonicalized name -- see `ssmctl.canonical_name`), persisted at
      `.odin/{env}/gateway/ssmctl.json`. Parameter tags live in the shared
      `tags` store above, keyed `"ssm:{canonicalName}"` (SSM's tag API carries
      the parameter NAME as `ResourceId`, not an ARN). A `SecureString`
      parameter's value lives here in cleartext too -- odin has no KMS; 0600
      is the protection, recorded as a limit rather than implied away.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tags = JsonStore(root, "tags")
        self.sqs_queues = JsonStore(root, "sqs_queues")
        self.sns_topics = JsonStore(root, "sns_topics")
        self.sns_subscriptions = JsonStore(root, "sns_subscriptions")
        self.ec2net = JsonStore(root, "ec2net")
        self.iamctl = JsonStore(root, "iamctl")
        self.ecr = JsonStore(root, "ecr")
        self.ec2compute = JsonStore(root, "ec2compute")
        self.lambdactl = JsonStore(root, "lambdactl")
        self.ecsctl = JsonStore(root, "ecsctl")
        self.logsctl = JsonStore(root, "logsctl")
        self.secretsctl = JsonStore(root, "secretsctl")
        self.ssmctl = JsonStore(root, "ssmctl")
