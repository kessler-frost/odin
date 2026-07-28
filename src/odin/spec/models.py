"""The Spec Store data model: Stack (desired) and World (observed).

The Stack is whole-canvas declarative desired state authored by the Canvas and
the Brain; the World is observed state authored only by drivers + the Assertion
Engine. They are kept as separate frozen documents per environment. A Stack
carries no `rev` field — the revision is the sha256 of its canonical JSON,
computed by the SpecStore (carrying it inside would be circular).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Provenance = Literal["user", "ai", "default"]

# Security finding #3: field names that look like they carry a credential --
# used to default `FieldValue.sensitive` so a canvas author doesn't have to
# hand-flag every rds `password`, EC2 `key`, or secret-looking env var.
_SENSITIVE_NAME_HINTS = ("password", "secret", "token", "key")
# Shorter than this, scrubbing risks mangling unrelated legitimate text (a
# tofu log line, an LLM prompt) for a value too short to be a real secret.
_MIN_SCRUBBABLE_LEN = 4
REDACTED = "[REDACTED]"


def is_sensitive_field_name(name: str) -> bool:
    """Case-insensitive substring match against `_SENSITIVE_NAME_HINTS` --
    catches `password`, `db_password`, `apiToken`, `secret_key`, `key_name`,
    etc. without needing an exhaustive field-name list."""
    lowered = name.lower()
    return any(hint in lowered for hint in _SENSITIVE_NAME_HINTS)


def scrub(text: str, secrets: frozenset[str]) -> str:
    """Replace every occurrence of a known-sensitive raw value with a
    placeholder. Plain substring replacement, not regex -- `secrets` are
    opaque user-supplied values, not patterns. The last line of defense for
    any text surface a secret could otherwise ride out on once it's left the
    structured Stack/FieldValue world (an LLM prompt, a `tofu` apply/destroy
    log line)."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text

# A resource's observed lifecycle phase.
Phase = Literal[
    "pending",    # desired but nothing started
    "starting",   # container launched, not yet healthy
    "healthy",    # assertion passed
    "crashed",    # was healthy/started, now down unexpectedly
    "blocked",    # waiting on an unresolved reference / dependency
    "queued",     # batch job waiting for capacity
    "running",    # batch job executing
    "done",       # batch job finished
    "evicted",    # llm intentionally unloaded under memory pressure
    "error",      # terminal failure (e.g. ref never resolved within timeout)
]


class FieldValue(BaseModel):
    """A single resource field plus where its value came from."""

    model_config = {"frozen": True}
    value: Any
    provenance: Provenance = "user"
    # Security finding #3: a field carrying a credential (rds `password`, a
    # secret-looking env var, ...). Never changes how the reconciler/gateway
    # USE the value (they need the real thing) -- only whether a diagnostic
    # surface that echoes the stack back (an LLM prompt, a tofu log line) is
    # allowed to show it verbatim. See `is_sensitive_field_name`.
    sensitive: bool = False


class Ref(BaseModel):
    """A `${{target_id.target_attr}}` reference carried by a node's field."""

    model_config = {"frozen": True}
    var: str          # the env var / field on the consumer, e.g. "DATABASE_URL"
    target_id: str    # the producer node id, e.g. "db"
    target_attr: str  # the attribute to read, e.g. "DATABASE_URL"


# THE canvas kinds a `${{producer.ATTR}}` reference can resolve against. ONE
# definition, imported by both halves that need it: `gateway/wiring.py::
# producer_facts`, which builds the values at launch time, and
# `agent/hcl.py::_unwired_refs`, which refuses a ref against anything else
# BEFORE tofu runs.
#
# Field test 6, F3's sub-finding. The list used to exist only as prose inside
# one `wiring.py` error string, which told the user an sqs node "publishes no
# facts" -- measured against a REAL running server at the same instant:
#
#   /world?env=srvfixf3   sqs srvfix-queue healthy
#                         {"QUEUE_URL": "http://host.docker.internal:4796/…",
#                          "endpoint": "http://host.docker.internal:33983"}
#   wiring.producer_facts(stores, "srvfixf3")  ->  {}
#
# Both readings are correct; the SENTENCE conflated two different fact systems.
# `aws/backings.py::facts` authors OBSERVED facts for s3/sqs/sns/dynamodb --
# `BUCKET`, `QUEUE_URL`, `TOPIC_ARN`, `TABLE` -- and they really are in
# `odin world`. `producer_facts` builds WIRING values out of the gateway's synth
# records, and only these four kinds have one. A node can therefore publish a
# fact you can see and still not be referenceable, which is the thing to say.
#
# (`fabric/localhost.py::resolve` DOES resolve refs out of World facts and would
# make the four PROVISIONED kinds referenceable -- but the reconciler only
# stores its `_fabric` and never calls it, so it is not a live path and must not
# be counted as one here.)
REFERENCEABLE_KINDS = ("rds", "elasticache", "alb", "ec2", "ecr")


class Edge(BaseModel):
    model_config = {"frozen": True}
    src: str
    dst: str
    kind: str = "ref"            # "ref" | "iam" | "network"
    perms: tuple[str, ...] = ()


class ResourceDesired(BaseModel):
    model_config = {"frozen": True}
    id: str
    kind: str                              # canvas node type, from spec/translate.py's
                                            # _KIND map: "rds" | "s3" | "sqs" | "sns" |
                                            # "dynamodb" | "vpc" | "subnet" | "sg" |
                                            # "iam_role" | "ecr" | "ec2" | "lambda" | "ecs"
    fields: dict[str, FieldValue] = {}
    refs: tuple[Ref, ...] = ()

    def sensitive_values(self) -> frozenset[str]:
        """Every sensitive raw value this resource carries, stringified --
        used to `scrub()` a diagnostic text surface. A dict-valued field
        (the `env` block) is inspected key-by-key so one secret-looking
        entry doesn't blanket-redact its non-secret siblings, independent of
        whether the whole field was marked `sensitive` (that flag is coarser,
        meant for the LLM-prompt redaction below, not this collector)."""
        out: set[str] = set()
        for key, fv in self.fields.items():
            if isinstance(fv.value, dict):
                out.update(
                    str(v) for k, v in fv.value.items()
                    if is_sensitive_field_name(k) and v not in (None, "")
                )
            elif fv.sensitive and fv.value not in (None, ""):
                out.add(str(fv.value))
        return frozenset(v for v in out if len(v) >= _MIN_SCRUBBABLE_LEN)


class Stack(BaseModel):
    model_config = {"frozen": True}
    env: str = "default"
    resources: tuple[ResourceDesired, ...] = ()
    edges: tuple[Edge, ...] = ()

    def sensitive_values(self) -> frozenset[str]:
        """The union of every resource's `sensitive_values()` -- the full
        secret set a caller should `scrub()` out of a text surface derived
        from this whole Stack."""
        out: set[str] = set()
        for r in self.resources:
            out |= r.sensitive_values()
        return frozenset(out)


class ResourceObserved(BaseModel):
    model_config = {"frozen": True}
    id: str
    kind: str
    phase: Phase = "pending"
    facts: dict[str, Any] = {}             # endpoint, host_port, cpu, ram, logtail…
    verdict: str | None = None
    restarts: int = 0


class World(BaseModel):
    model_config = {"frozen": True}
    env: str = "default"
    resources: tuple[ResourceObserved, ...] = ()

    def get(self, resource_id: str) -> ResourceObserved | None:
        return next((r for r in self.resources if r.id == resource_id), None)


class WorldDelta(BaseModel):
    """A single observed-state change broadcast to the canvas."""

    model_config = {"frozen": True}
    type: str = "world_delta"
    env: str
    resource_id: str
    kind: str
    phase: Phase
    facts: dict[str, Any] = {}
    verdict: str | None = None
