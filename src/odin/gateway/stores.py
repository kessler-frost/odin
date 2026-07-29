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
from functools import partial
from pathlib import Path
from typing import Any

from odin.gateway.records import validate
from odin.spec.store import CONTROL, _load
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

    DE-THREADING VERDICT (re-verified 2026-07-27): this is the ONE lock in odin
    that genuinely DELETES rather than becoming an `asyncio.Lock`, and it is the
    concurrency directive's "file I/O stays SYNCHRONOUS" rule that makes it so.
    Every critical section here is `_data()` (a sync `_load`) plus
    `_persist_locked()` (a sync `atomic_write_text`); this file contains no
    `async def` and no `await` at all, so once every contender shares one event
    loop nothing can preempt a read-modify-write and the lock guards nothing.

    It has NOT come out, and the reason is specific rather than pending work.
    The contenders this docstring used to name -- the daemon threads in
    `gateway/models/*` and the `to_thread` workers in `reconcile/reconciler.py`
    and `server.py` -- are all gone (`to_thread` is at zero). What remains is
    `gateway/app.py::serve_in_thread`, which runs the gateway on a real thread
    for two SYNC integration tests that must dial a real bound port. They do
    blocking boto3/docker work inline, so serving on the caller's loop instead
    would deadlock against the very loop those calls block. Production never
    uses it (`serve_on_loop` does).

    So: delete these locks in the SAME change that deletes `serve_in_thread`,
    and not before -- removing them while that helper exists reintroduces a
    real interleaved read/write in those two tests, which no unit test would
    catch. `tests/test_thread_inventory.py` asserts that dependency both ways,
    because this paragraph is prose and prose cannot fail a build.
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

    def forget_env(self, env: str) -> bool:
        """Drop this env's cached contents (and its lock) without writing.
        Returns whether anything was cached.

        `/envs/rm` deletes `.odin/<env>/gateway/<name>.json` outright, and this
        is what stops the deletion being invisible: `_data` caches the parsed
        dict in `_loaded` FOREVER (there is no invalidation anywhere else in
        this class), so a later env of the same name would be served the removed
        env's records out of memory and, on its first `set`, persist them back
        to a file that had been deleted."""
        self._locks.pop(env, None)
        return self._loaded.pop(env, None) is not None

    def _lock_for(self, env: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(env, threading.Lock())

    def _data(self, env: str) -> dict[str, Any]:
        """Caller must already hold `_lock_for(env)`.

        The read goes through `spec/store.py::_load` so a corrupt file NAMES
        itself. It used to be a bare `json.loads`, and `JSONDecodeError` carries
        no path: one truncated `gateway/<name>.json` made every store-backed kind
        -- ecs, lambda, ec2, elasticache, logs -- raise out of here with nothing
        saying which file, against a `GET /logs` docstring promising "never a
        500". Role CONTROL, because deleting this one is NOT the fix: it is what
        tofu's next refresh reads, so losing it orphans resources that really
        exist.

        VALIDATED, not merely parsed (`records.validate`) -- the same upgrade
        `keys.py` got, for the same reason and by the same mechanism. A file
        that decodes is not yet a file odin can trust: `json.loads` was happy
        with a top-level LIST (the failure then surfaced as `ValueError:
        dictionary update sequence element #0 has length 9; 2 is required` from
        `items()`, and with a bare string as `AttributeError: 'str' object has
        no attribute 'get'` from `get()` -- neither of them a `StoreUnreadable`,
        so neither reached the CONTROL recovery advice this very docstring is
        about), and it was equally happy with `"events:{group}": "boom"`, which
        `logsctl._append_events` then splatted into single characters and wrote
        BACK to disk behind a 200. Every shape `records.py` names is one a real
        reader was measured mis-answering."""
        if env not in self._loaded:
            path = self._path(env)
            parse = partial(validate, self._name)
            self._loaded[env] = _load(path, CONTROL, parse) if path.exists() else {}
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
      `"cluster:{name}"` / `"taskdef:{family}:{revision}"` /
      `"service:{cluster}:{name}"` / `"task:{cluster}:{task_id}"`, persisted at
      `.odin/{env}/gateway/ecsctl.json`. There is NO revision counter: the next
      task-definition revision is derived from the `"taskdef:"` keys themselves
      (`ecsctl._taskdef_revisions`), because a second copy of that truth could
      go missing and let a register overwrite a live revision -- measured doing
      exactly that. Stores written by earlier odins still carry the old
      `"taskdef-rev:{family}"` counter and `records.py` still validates it so
      such a file stays readable; nothing reads its value. Same shape as the
      legacy `"cursor:"` key `logsctl` describes below.
    - `logsctl`: the CloudWatch Logs model's whole state
      (`gateway/models/logsctl.py`, task W2.1) -- flat keys `"group:{name}"` /
      `"stream:{group}:{stream}"` / `"events:{group}"` (the per-group ring
      buffer) / `"barrier:{group}:{stream}"` (how many of that stream's stored
      events predate the container currently behind it -- the substrate
      log-shipping re-anchors on CONTENT and skips past this barrier, so a
      restarted container's log does not duplicate the old one's). Pre-v0.7.1
      stores may still hold a `"cursor:{group}:{stream}"` LINE COUNT under the
      old scheme; nothing reads it -- a legacy line count read as a barrier
      would silently duplicate whole streams -- and `_delete_log_group` sweeps
      it. Persisted at `.odin/{env}/gateway/logsctl.json`. Log-group tags
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
    - `cachectl`: the ElastiCache control-plane model's whole state
      (`gateway/models/cachectl.py`, W2.8) -- flat keys `"cluster:{id}"`,
      persisted at `.odin/{env}/gateway/cachectl.json`. ElastiCache tags live
      in the shared `tags` store above, keyed `"elasticache:{arn}"`.
    - `rdsctl`: the RDS model's whole state (`gateway/models/rdsctl.py`, task
      W2.7) -- flat keys `"db:{identifier}"`, persisted at
      `.odin/{env}/gateway/rdsctl.json`. DB-instance tags live in the shared
      `tags` store above, keyed `"rds:{dbInstanceArn}"`. This record carries the
      instance's MASTER PASSWORD (the DATABASE_URL World fact is built from it
      and the drift sweep's health probe authenticates with it) -- the same
      cleartext value the Stack revision on disk already holds, and this
      sidecar is written 0600 like every other one.
    - `elbv2ctl`: the Elastic Load Balancing v2 model's whole state
      (`gateway/models/elbv2ctl.py`, task W2.5) -- flat keys `"lb:{name}"` /
      `"tg:{name}"` / `"listener:{listenerId}"` / `"targets:{tgName}"` (a
      target group's registered targets), persisted at
      `.odin/{env}/gateway/elbv2ctl.json`. Load-balancer / target-group /
      listener tags live in the shared `tags` store above, keyed
      `"elasticloadbalancing:{arn}"` (elbv2's tag API is ARN-only: AddTags/
      RemoveTags/DescribeTags all take `ResourceArns`, never a typed id). The
      REAL substrate this state describes is an nginx container per load
      balancer (`compute/proxy.py`), never anything in this file.
    - `eventsctl`: the EventBridge model's whole state
      (`gateway/models/eventsctl.py`) -- flat keys `"rule:{bus}:{name}"` /
      `"targets:{bus}:{rule}"` (a LIST of the target dicts terraform sent,
      stored verbatim) / `"bus:{name}"`, persisted at
      `.odin/{env}/gateway/eventsctl.json`. Rule and event-bus tags live in the
      shared `tags` store above, keyed `"events:{arn}"` (EventBridge's tag API
      is ARN-only: Tag/Untag/ListTagsForResource all take `ResourceARN`, never
      a typed id). The DEFAULT event bus has NO `bus:` record on purpose -- it
      always exists, so storing one would only create a way for it to be
      missing (`eventsctl._bus`). A rule's targets are DELIVERED by
      `reconcile/dispatch.py`, whose own bookkeeping lives in `dispatch` below
      rather than in here: this store is EventBridge's control plane, and
      "when did this rule last fire" is not a fact EventBridge's API has.
    - `dispatch`: the event dispatcher's own bookkeeping
      (`reconcile/dispatch.py`) -- flat keys `"fired:{bus}:{rule}"` (a
      scheduled rule's clock anchor) and `"pending:{id}"` (an S3 object write
      that matched a bucket notification and is waiting for the next pass),
      persisted at `.odin/{env}/gateway/dispatch.json`. Deliberately NOT folded
      into `eventsctl`/`s3notify`: those two are CONTROL planes that tofu reads
      back, and mixing a mutable per-tick cursor into a record terraform diffs
      is how a refresh starts reporting drift on odin's own bookkeeping.
    - `s3notify`: the S3 bucket-notification configuration
      (`gateway/models/s3notify.py`) -- flat keys `"notify:{bucket}"`,
      persisted at `.odin/{env}/gateway/s3notify.json`. This is odin's OWN
      state rather than a forward, and the reason is measured: RustFS rejects
      every `PutBucketNotificationConfiguration` ARN form with
      `InvalidArgument` and stores the configuration anyway, so forwarding gave
      a failed `apply`, a clean `plan` and a trigger that never fired -- three
      answers that cannot all be true (docs/limits.md).
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
        self.cachectl = JsonStore(root, "cachectl")
        self.rdsctl = JsonStore(root, "rdsctl")
        self.elbv2ctl = JsonStore(root, "elbv2ctl")
        self.eventsctl = JsonStore(root, "eventsctl")
        self.dispatch = JsonStore(root, "dispatch")
        self.s3notify = JsonStore(root, "s3notify")

    def forget_env(self, env: str) -> list[str]:
        """Drop every store's cached copy of `env`. Returns the store names that
        held one.

        DERIVED from the attributes, never a hand-written list: this class has
        grown from four stores to seventeen, one service at a time, and a
        removal that named them individually would silently miss the
        eighteenth. `vars(self)` also skips `root`, which is a Path, not a
        store.

        That is not hypothetical. This method and `eventsctl` arrived in the
        same merge, from two agents that never saw each other's work, and the
        derived form covered the new store with no edit at all."""
        return sorted(
            name for name, store in vars(self).items()
            if isinstance(store, JsonStore) and store.forget_env(env)
        )
