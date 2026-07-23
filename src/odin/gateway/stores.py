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
from pathlib import Path
from typing import Any


class JsonStore:
    """A flat `key -> value` dict, one JSON file per env, loaded lazily and
    rewritten wholesale on every mutation."""

    def __init__(self, root: Path, name: str) -> None:
        self._root = root
        self._name = name
        self._loaded: dict[str, dict[str, Any]] = {}

    def get(self, env: str, key: str, default: Any = None) -> Any:
        return self._data(env).get(key, default)

    def set(self, env: str, key: str, value: Any) -> None:
        self._data(env)[key] = value
        self._persist(env)

    def delete(self, env: str, key: str) -> None:
        self._data(env).pop(key, None)
        self._persist(env)

    def items(self, env: str) -> dict[str, Any]:
        """A copy of the env's whole flat dict -- the "describe all" read the
        EC2-network model's list answers need, without callers reaching into
        `_data` directly."""
        return dict(self._data(env))

    def _data(self, env: str) -> dict[str, Any]:
        if env not in self._loaded:
            path = self._path(env)
            self._loaded[env] = json.loads(path.read_text()) if path.exists() else {}
        return self._loaded[env]

    def _path(self, env: str) -> Path:
        return self._root / env / "gateway" / f"{self._name}.json"

    def _persist(self, env: str) -> None:
        path = self._path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._data(env)))


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
