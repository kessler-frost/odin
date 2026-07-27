"""What a record in each gateway sidecar store IS -- checked on every read.

WHY THIS EXISTS. Two v0.7.6 bugs were the same shape, and neither one crashed:
`keys.json` held `{"db": "AKIAodin1234"}` -- a bare string where an
`[access_key, secret_key]` pair belongs -- and `pair[0], pair[1]` then INDEXED
THE STRING, so odin registered access key `'A'` with secret `'K'`; the debug
route's scrub set iterated the identical shape and redacted single letters out
of the whole model prompt while reporting success. Both were fixed by
validating the shape on read (`gateway/keys.py`'s `_KEYFILE` TypeAdapter). The
other stores were still parsed and never validated, and every consumer does
`record["some_key"]` or iterates a value whose type it never checks.

The same shape is really in here -- PROBED, not assumed, against v0.7.6:

  * `events:{group}` (logsctl's ring buffer) holding the string `"boom"`
    instead of a list made `_append_events`' `[*(current or []), *records]`
    SPLAT IT INTO CHARACTERS: the file came back
    `["b", "o", "o", "m", {...}]`, `PutLogEvents` answered 200 with
    `nextSequenceToken: "2"`, and the corruption was persisted. Every later
    read then died with `TypeError: string indices must be integers` naming
    no file. That is `pair[0]` exactly, one store over.
  * An rds `db:` record whose `endpoint_port` is missing or null while
    `status` is `available` silently disables EVERY liveness guard odin has
    for that database: `drift._db_records` skips it on a falsy port, and
    `_dead`/`live_verdicts`/`sweep_compute` all read through that same
    function. Measured with the real projection and a runtime reporting the
    container GONE: the well-formed record projects `rds CRASHED` with the
    real reason, and the record missing that one field projects
    `rds HEALTHY`, no verdict, no facts. One absent field, four guards off.
  * A `sqs_queues` record whose `attributes` is a string answers
    GetQueueAttributes with `{"Attributes": "VisibilityTimeout=30"}` -- a
    string where the wire shape is a map -- with a 200.
  * `barrier:{group}:{stream}` holding `true` passes `int()` as 1, so the
    log-shipping dedup treats one stored line as already-seen that isn't, and
    duplicates it. `2.9` truncates to 2 the same silent way.

HOW STRICT, AND WHY NOT STRICTER. These files are written by odin and read
back AFTER AN UPGRADE, so a guard that rejects a pre-upgrade record is worse
than the bug it prevents -- it bricks an env that was working. Three rules
keep that from happening, and each is a deliberate limit rather than an
oversight:

 1. **Only load-bearing fields are modelled.** A field is here if a reader
    indexes it (`record["x"]` -- a missing one is a KeyError TODAY, so the
    reader has already committed to its existing) or if a `.get()` on it
    DECIDES something (a phase, a skip, a delete). Everything else is not
    modelled at all. `rdsctl`'s record has ~20 fields; five of them decide
    anything, and those five are what is checked. Fewer required fields is
    fewer ways to be wrong about a record some path writes that no test
    covers.
 2. **`extra="allow"`, always.** A NEWER odin adding a field must not make an
    older record unreadable, and an OLDER record's unknown leftovers must not
    fail either. Nothing here is ever rejected for being unrecognised.
 3. **Unknown store names and unknown key prefixes pass through
    unvalidated.** `logsctl`'s pre-v0.7.1 `cursor:` residue (see
    `stores.py`'s note) is exactly this case: it is dead weight nothing
    reads, and refusing to load a file because of it would strand the env.
    A prefix a future model adds needs no edit here to keep working.

`strict=True` is the whole point of the type annotations: Pydantic's default
lax mode COERCES `"2"` into `2` and would wave through precisely the
string-where-a-number-belongs confusion this module exists to catch. Probed on
the pydantic in this venv (2.13.4): strict rejects `"1"`, `True` and `1.0` for
an `int`, rejects `7`/`["x"]`/`None` for a `str`, ACCEPTS an int for a `float`
(so an integer timestamp is fine), keeps extra keys, and does not mutate the
input -- which matters, because `JsonStore` hands the raw dicts to model
modules that mutate them and re-`json.dumps` them. Validation here CHECKS and
throws the models away; the store keeps the plain dicts it always did.

WHAT IS DELIBERATELY NOT CHECKED, so the report is honest about its own gaps:

  * **Tag VALUES.** A `tags` record must be an object -- that is the shape
    whose absence really crashes (`tags.get("odin:node")` on a string is an
    unnamed `AttributeError`) -- but its values are `Any`. `ecr.py` and
    `synth.py` build tags straight out of a client's JSON
    (`{t["Key"]: t.get("Value", "")}`), so a caller sending a non-string
    `Value` makes odin WRITE a non-string tag. Requiring `str` there would
    let a legal API call brick the env on the next read, which is the
    failure this module is supposed to prevent, not cause.
  * **Cross-field invariants**, specifically "status `available` implies a
    non-zero `endpoint_port`". Every writer that sets `available` sets a real
    port with it (`rdsctl._finish_create`), so the rule would hold -- but it
    is enforced at LOAD time over the whole file, so being wrong about one
    untraced path costs the entire env, while the plain
    `endpoint_port: int` below already rejects all three malformations that
    were actually shown to produce the false green (missing, null, string).
    Value-level truth is the drift sweep's job, not the parser's.
  * **Writes.** `JsonStore.set`/`update` are not validated. Symmetry would be
    tidy, but it would turn a too-strict model here into a 500 on a working
    request -- a new failure on the happy path. Reads are where the wrong
    answers were.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError


# Closed status sets, enumerated from the WRITERS rather than from AWS's own
# (much larger) vocabularies. The validation pass left these `str` deliberately,
# because policing VALUES risks refusing a state a writer legitimately produces
# -- so each set below was derived by grepping every assignment in the owning
# model, and each is exhaustive as of this commit.
#
# Why they are worth closing: every reader compares these by EXACT string
# (`== "RUNNING"`, `!= "STOPPED"`, `!= "ACTIVE"`), so an unknown value is not
# merely unrecognised -- it is invisible in every direction at once. A task whose
# `last_status` is a typo is never counted running, never counted failed, skipped
# by the drift sweep (`!= "RUNNING"`) AND treated as live by `api/logs.py`
# (`!= "STOPPED"`), so the service sits at 0/N forever while the apply reports
# success.
#
# THE COUPLING IS DELIBERATE AND EXPLICIT: adding a state to a writer means
# adding it here in the SAME commit. That is caught immediately -- the test suite
# writes real records, so a new state fails loudly in development rather than
# silently in production, which is the trade this makes.
_TASK_STATUS = Literal["PROVISIONING", "RUNNING", "STOPPED"]
_SERVICE_STATUS = Literal["ACTIVE", "INACTIVE"]   # DeleteService keeps the record, INACTIVE
_LB_STATE = Literal["active", "provisioning", "failed"]


class Record(BaseModel):
    """Strict about the types it names, silent about everything else."""

    model_config = ConfigDict(strict=True, extra="allow")


def strict(annotation) -> TypeAdapter:
    """A `TypeAdapter` that really is strict, for a model OR a bare type.

    Two footguns, one function, because both bit during this module's own
    development. A `TypeAdapter` does NOT inherit `Record`'s config and its
    default is LAX: the first cut shipped `TypeAdapter(int)` for the log
    barrier and the ECS revision counter, lax mode coerced `True` to 1 and
    `"3"` to 3, and both guards silently did nothing -- honesty rule 1, in the
    guard written to serve honesty rule 1. Passing `config=` is the fix, and
    Pydantic then REFUSES it for a `BaseModel` (`PydanticUserError`, because
    the model's own config would win anyway). So every entry in `SCHEMAS`
    calls this and it does the right thing either way, rather than leaving a
    silent-lax adapter one careless line away."""
    return (
        TypeAdapter(annotation) if isinstance(annotation, type) and issubclass(annotation, BaseModel)
        else TypeAdapter(annotation, config=ConfigDict(strict=True))
    )


# --- rds ---------------------------------------------------------------------


class DbInstance(Record):
    """`db:{identifier}` (gateway/models/rdsctl.py).

    `endpoint_port` is the load-bearing one and the reason this store is first:
    `drift._db_records` skips any record whose port is falsy, and
    `_dead`/`live_verdicts`/`sweep_compute` all funnel through it, so a
    missing or null port turns off the container check, the pg_ready probe AND
    the apply-time sweep at once while `/world` keeps saying `healthy`.
    `rdsctl` writes it at creation (as `0`) and never removes it, so requiring
    it cannot reject a record any released odin wrote.

    `master_password`/`master_username`/`db_name` build the DATABASE_URL fact
    the Fabric resolves and the drift probe authenticates with; a non-string
    there is a credential silently formatted into a URL."""

    db_instance_identifier: str
    status: str
    endpoint_port: int
    master_username: str
    master_password: str
    db_name: str
    overlay_ip: str | None = None
    status_reason: str | None = None


# --- lambda ------------------------------------------------------------------


class LambdaFunction(Record):
    """`fn:{name}` (gateway/models/lambdactl.py). `state` drives the World
    phase (`_LAMBDA_PHASE`) and `last_update_status` is the drift sweep's
    mid-redeploy exemption -- a wrong type on either silently exempts the
    function from the check that would have called it dead."""

    function_name: str
    function_arn: str
    state: str
    last_update_status: str | None = None
    state_reason: str | None = None
    last_invocation_error: str | None = None


# --- cloudwatch logs ---------------------------------------------------------


class LogGroup(Record):
    """`group:{name}`. `auto` decides whether the group is projected into
    World at all (a substrate-created group must not strand a phantom node);
    as a string it is truthy, and the canvas's own group vanishes."""

    log_group_name: str
    auto: bool = False
    retention_in_days: int | None = None


class LogStream(Record):
    """`stream:{group}:{stream}`. `upload_sequence_token` is `int()`-ed and
    incremented on every PutLogEvents."""

    log_stream_name: str
    upload_sequence_token: int = 1
    first_event_timestamp: int | None = None
    last_event_timestamp: int | None = None


class LogEvent(Record):
    """One entry in a group's ring buffer. Every reader indexes all three of
    these (`_ordered` sorts on `timestamp`, `_anchor` filters on `stream` and
    reads `message`, `api/logs.py` renders all of them)."""

    stream: str
    timestamp: int
    message: str


# `events:{group}` -- a LIST of events, never a string. This is the entry that
# reproduces the keys.json bug: a string here is splatted into characters by
# `_append_events` and the result is written back to disk behind a 200.
LOG_EVENTS = strict(list[LogEvent])

# `barrier:{group}:{stream}` -- how many stored events predate the container
# currently behind the stream. A bare int: `bool` is rejected (strict), which
# is what stops `int(True) == 1` from silently re-ingesting a line.
BARRIER = strict(int)


# --- ec2 compute -------------------------------------------------------------


class Ec2Instance(Record):
    """`instance:{id}` (gateway/models/ec2compute.py). `state_name` is the
    whole projection: `_EC2_PHASE.get(state_name, "starting")` for World, and
    `drift._vm_records` only sweeps records that read exactly `running`, so an
    unexpected value shows `starting` forever AND is never checked for drift.
    `drifted` is the one flag that keeps a terminated record visible instead
    of pruned."""

    instance_id: str
    state_name: str
    drifted: bool | None = None
    private_ip: str | None = None
    delete_failed: bool | None = None


class KeyPair(Record):
    """`keypair:{name}`."""

    key_pair_id: str


# --- ec2 network -------------------------------------------------------------


class Vpc(Record):
    """`vpc:{id}` (gateway/models/ec2net.py)."""

    vpc_id: str
    cidr_block: str
    default_sg_id: str


class Subnet(Record):
    """`subnet:{id}`."""

    subnet_id: str
    vpc_id: str
    cidr_block: str
    availability_zone: str


class SecurityGroupRule(Record):
    """One rule body inside `sg:{id}`'s `rules` map.

    `is_egress` is modelled -- and modelled STRICTLY -- because it is the only
    thing separating "allow all OUTBOUND" (AWS's benign seeded default) from
    "allow all INBOUND" (the widest firewall there is), and
    `ec2net._compiled_firewall` selected on it with a TRUTHINESS test.

    Measured, not reasoned about. The seeded default egress rule is
    `ip_protocol "-1"`, `from_port`/`to_port` None, `cidr_ipv4 "0.0.0.0/0"`.
    Compile that rule with `is_egress` as `0`, `""` or `None` -- all falsy, all
    plausible in a hand-edited or round-tripped record -- and the firewall comes
    out as

        inbound=[{'port': 'any', 'proto': 'any', 'cidr': '0.0.0.0/0'}]

    where the correct compilation is `inbound=[]`. The failure direction is
    asymmetric, which is what makes it dangerous: the STRING `"false"` is
    truthy, so it reads as egress and fails CLOSED, while `0`/`""`/`None` fail
    OPEN. This is the one record field in odin where a wrong type is a security
    hole rather than a wrong answer.

    Upgrade-safe: `is_egress` has been a literal Python bool since this store's
    birth commit (8353eb5) -- the four route handlers pass `is_egress=True` or
    `False`, and no released odin ever wrote anything else."""

    is_egress: bool
    ip_protocol: str


class SecurityGroup(Record):
    """`sg:{id}`. `rules` is a MAP of rule-id -> rule, and it is compiled into
    the Nebula firewall that really gates an rds container's overlay port --
    the one store value in odin that a network reachability decision is made
    from. A string here would be iterated as characters by the compiler, which
    is why the container type is pinned -- and the rule BODIES are pinned too,
    for the reason `SecurityGroupRule` documents."""

    group_id: str
    group_name: str
    vpc_id: str
    rules: dict[str, SecurityGroupRule]


# --- iam ---------------------------------------------------------------------


class IamRole(Record):
    """`role:{name}` (gateway/models/iamctl.py).

    `attached_policy_arns` and `inline_policies` are membership-tested
    (`if arn not in role["attached_policy_arns"]`) and iterated. As a STRING
    that test silently becomes a SUBSTRING match and the iteration yields
    single characters -- AttachRolePolicy would decide a policy is already
    attached because its text appears somewhere, and DeleteRole's
    "is this role still in use" guard reads a non-empty string as in-use.
    Neither is an authorization input (`gateway/policy.py` compiles the
    enforced statements from the Stack, never from this store), so the damage
    is a wrong answer to a describe/attach, not a widened permission."""

    role_name: str
    arn: str
    attached_policy_arns: list[str]
    inline_policies: dict[str, str]


class IamPolicy(Record):
    """`policy:{arn}`."""

    policy_name: str
    arn: str


class InstanceProfile(Record):
    """`instance-profile:{name}`. `roles` carries the same substring hazard as
    `IamRole.attached_policy_arns`, and it gates whether a role can be
    deleted."""

    instance_profile_name: str
    arn: str
    roles: list[str]


# --- ecr ---------------------------------------------------------------------


class EcrRepository(Record):
    """`repo:{name}` (gateway/models/ecr.py)."""

    repository_name: str
    repository_arn: str


# --- ecs ---------------------------------------------------------------------


class EcsCluster(Record):
    """`cluster:{name}` (gateway/models/ecsctl.py). `settings` is stored
    verbatim from the payload and echoed onto the wire unexamined."""

    cluster_name: str
    cluster_arn: str
    settings: list[Any]


class EcsService(Record):
    """`service:{cluster}:{name}`. `desired_count` is compared to a counted
    int (`running == record["desired_count"]`), so the string `"2"` never
    equals 2 and the service reads `starting` forever -- wrong, though in the
    safe direction; elsewhere the same string raises (`int >= str`).
    `deleted_at` is worse: `now - deleted_at` on a string makes EVERY
    DescribeServices 500, which breaks plan, apply and destroy for the env at
    once. `status` gates whether the service is projected at all."""

    service_name: str
    cluster_name: str
    status: _SERVICE_STATUS
    desired_count: int
    task_definition_arn: str
    created_at: float
    launch_type: str
    node_label: str | None = None
    load_balancers: list[Any] | None = None
    deleted_at: float | None = None


class EcsTask(Record):
    """`task:{cluster}:{task_id}`. `last_status` decides both the World
    rollup and whether the drift sweep looks at the task's container, and
    `host_ports` is `.items()`-ed straight into the wire answer."""

    task_id: str
    cluster_name: str
    service_name: str
    container_name: str
    task_arn: str
    task_definition_arn: str
    last_status: _TASK_STATUS
    desired_status: str
    host_ports: dict[str, int]
    started_at: float | None = None
    stopped_at: float | None = None
    exit_code: int | None = None
    stopped_reason: str | None = None


class EcsTaskDefinition(Record):
    """`taskdef:{family}:{revision}`. `container_definitions` is indexed `[0]`
    on the task-launch path (v1 is single-container), so a double-encoded
    JSON string here becomes `"["[0]` -- and through `service_image` that one
    500s `GET /world` itself."""

    family: str
    revision: int
    status: str
    network_mode: str
    container_definitions: list[dict]
    requires_compatibilities: list[Any]
    volumes: list[Any]
    registered_at: float


# `taskdef-rev:{family}` -- a bare per-family revision counter.
TASKDEF_REVISION = strict(int)


# --- elastic load balancing v2 -----------------------------------------------


class LoadBalancer(Record):
    """`lb:{name}` (gateway/models/elbv2ctl.py). `state` is the phase the
    provider's own waiter polls for.

    `security_groups` is elbv2's own instance of the keys.json bug and the
    reason it is pinned to `list[str]`: `[escape(sg) for sg in
    record["security_groups"]]` iterates a bare `"sg-0abc123"` as CHARACTERS,
    `escape()` accepts each, and DescribeLoadBalancers answers with nine
    single-letter `<member>` entries. botocore parses them happily, and
    terraform-provider-aws then writes `['s','g','-','0',...]` into
    `aws_lb.security_groups` state -- every later plan dirty against fiction.
    `availability_zones` is iterated the same way with `zone["ZoneName"]`,
    and `attributes` is `**`-splatted."""

    name: str
    arn: str
    state: _LB_STATE
    lb_id: str
    scheme: str
    type: str
    ip_address_type: str
    vpc_id: str
    created_time: str
    security_groups: list[str]
    availability_zones: list[dict]
    attributes: dict[str, Any]
    state_reason: str | None = None
    endpoints: dict[str, int] | None = None


class TargetGroup(Record):
    """`tg:{name}`. `health_check` is a MAPPING and is checked as no more than
    that: `_tg_xml` hard-indexes all eight `_HEALTH_CHECK_DEFAULTS` keys out
    of it, so requiring them here would be right about today's writer and
    would brick any record written before a key was added -- the read is the
    thing that should have been tolerant, and that read is not this module's
    to change."""

    name: str
    arn: str
    target_type: str
    health_check: dict[str, Any]
    attributes: dict[str, Any]
    port: int | None = None
    matcher: dict[str, Any] | None = None


class Listener(Record):
    """`listener:{id}`. `port` is a sort key across every listener on a load
    balancer, so one stored as a string raises mid-sort; `default_actions` is
    iterated on four paths."""

    listener_id: str
    arn: str
    lb_arn: str
    lb_name: str
    port: int
    protocol: str
    default_actions: list[dict]


class Target(Record):
    """One registered target inside `targets:{tg_name}`."""

    id: str
    port: int | None = None


# `targets:{tg_name}` -- a LIST of registered targets, the elbv2 store's own
# instance of the shape that made `events:` splat.
TARGETS = strict(list[Target])


# --- elasticache -------------------------------------------------------------


class CacheCluster(Record):
    """`cluster:{id}` (gateway/models/cachectl.py)."""

    cache_cluster_id: str
    arn: str
    status: str
    port: int | None = None


# --- secrets manager / ssm ---------------------------------------------------


class Secret(Record):
    """`secret:{name}` (gateway/models/secretsctl.py)."""

    name: str
    arn: str


class SecretVersion(Record):
    """`version:{name}:{versionId}`. `version_stages` is membership-tested to
    find AWSCURRENT; as a string that is a substring match, and
    GetSecretValue would hand back the wrong version's cleartext."""

    version_id: str
    version_stages: list[str]
    secret_string: str | None = None


class SsmParameter(Record):
    """`param:{canonicalName}` (gateway/models/ssmctl.py)."""

    name: str
    version: int


# --- the four synth.py stores ------------------------------------------------


class SqsQueue(Record):
    """One entry in `sqs_queues`, keyed by queue name.

    `deleted_at` is the delete-confirmation grace marker, and the shim does
    `now - deleted_at`: a string raises `TypeError` mid-request, and a record
    that LOST the field reads as never-deleted, so GetQueueAttributes answers
    for a queue that is gone. `attributes` is echoed to the caller verbatim --
    as a string it goes out as `{"Attributes": "..."}`, a string where the AWS
    wire shape is a map, behind a 200."""

    attributes: dict[str, str] = {}
    deleted_at: float | None = None


# `tags` -- `"{service}:{resource}"` -> a flat tag map. The VALUES are `Any`
# on purpose (see the module docstring): what has to hold is that the record
# is a MAPPING, because `tags.get("odin:node")` on a string is the unnamed
# `AttributeError` that breaks the whole World projection.
TAG_SET = strict(dict[str, Any])

# `sns_topics` -- topic name -> its attribute map, spread into the
# GetTopicAttributes answer (`{**defaults, **stored}` raises on a non-mapping).
TOPIC_ATTRIBUTES = strict(dict[str, Any])

# `sns_subscriptions` -- subscription ARN -> the `now` Unsubscribe fired. Only
# its PRESENCE is ever read, so no malformation of the value changes an
# answer; it is pinned because writing the shape down costs nothing and the
# next reader to reach for the timestamp should find it real.
UNSUBSCRIBED_AT = strict(float)


# store name -> key prefix -> what a value under that prefix must be. A store
# absent from this map, or a key matching no prefix in it, is not validated
# (module docstring, rule 3). `""` matches every key, for the stores whose keys
# carry no prefix at all.
SCHEMAS: dict[str, dict[str, TypeAdapter]] = {
    "rdsctl": {"db:": strict(DbInstance)},
    "lambdactl": {"fn:": strict(LambdaFunction)},
    "logsctl": {
        "group:": strict(LogGroup),
        "stream:": strict(LogStream),
        "events:": LOG_EVENTS,
        "barrier:": BARRIER,
    },
    "ec2compute": {"instance:": strict(Ec2Instance), "keypair:": strict(KeyPair)},
    "ec2net": {
        "vpc:": strict(Vpc),
        "subnet:": strict(Subnet),
        "sg:": strict(SecurityGroup),
    },
    "iamctl": {
        "role:": strict(IamRole),
        "policy:": strict(IamPolicy),
        "instance-profile:": strict(InstanceProfile),
    },
    "ecr": {"repo:": strict(EcrRepository)},
    "ecsctl": {
        "cluster:": strict(EcsCluster),
        "service:": strict(EcsService),
        "task:": strict(EcsTask),
        "taskdef:": strict(EcsTaskDefinition),
        "taskdef-rev:": TASKDEF_REVISION,
    },
    "elbv2ctl": {
        "lb:": strict(LoadBalancer),
        "tg:": strict(TargetGroup),
        "listener:": strict(Listener),
        "targets:": TARGETS,
    },
    "cachectl": {"cluster:": strict(CacheCluster)},
    "secretsctl": {"secret:": strict(Secret), "version:": strict(SecretVersion)},
    "ssmctl": {"param:": strict(SsmParameter)},
    "tags": {"": TAG_SET},
    "sqs_queues": {"": strict(SqsQueue)},
    "sns_topics": {"": TOPIC_ATTRIBUTES},
    "sns_subscriptions": {"": UNSUBSCRIBED_AT},
}

# The file itself, before any record is looked at. `json.loads` alone is happy
# with a top-level list or a bare string, and `JsonStore` then stored it: the
# failure surfaced later as `ValueError: dictionary update sequence element #0
# has length 9; 2 is required` out of `items()`, or `AttributeError: 'str'
# object has no attribute 'get'` out of `get()` -- neither of which is a
# `StoreUnreadable`, so neither ever reached the recovery advice server.py
# already had ready for this file.
_FILE = strict(dict[str, Any])


def _adapter_for(store: str, key: str) -> TypeAdapter | None:
    """The most specific prefix that matches, so `taskdef-rev:` is never read
    as a `taskdef:` (they are already disjoint -- ecsctl's own convention --
    but the longest match makes that a property of this function rather than
    of a coincidence between two string literals)."""
    matches = [
        (prefix, adapter)
        for prefix, adapter in SCHEMAS.get(store, {}).items()
        if key.startswith(prefix)
    ]
    return max(matches, key=lambda m: len(m[0]))[1] if matches else None


def _detail(exc: ValidationError) -> str:
    """The FIRST complaint, named by field. Pydantic's full rendering is a
    multi-line report with a docs URL in it; what belongs in a store-read
    failure is which field and what was wrong with it."""
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "the record itself"
    return f"{where}: {first['msg']} (got {first['input']!r})"


def validate(store: str, text: str) -> dict[str, Any]:
    """The parsed store file, or `ValueError` naming the offending KEY.

    Handed to `spec/store.py::_load`, which is what turns this into a
    `StoreUnreadable` carrying the PATH and the CONTROL role -- so a bad
    record reaches the same recovery advice `server.py` already renders for a
    corrupt spec store, instead of a `TypeError` from whichever model module
    happened to touch it first.

    `json.loads` does the DECODING, deliberately, rather than Pydantic's own
    `validate_json`: a truncated file (`{"service:app": ` -- exactly what an
    interrupted write leaves) has to keep raising `JSONDecodeError`, because
    that name is what `api/logs.py`'s corrupt-store test asserts on and what
    tells an operator the file is CUT OFF rather than the wrong shape. Pydantic
    then checks the shape of what decoded. Two different faults, two different
    sentences.

    ONE try/except, for the same reason `_load` has exactly one: every way a
    record can be the wrong shape arrives as a `ValidationError`, and the only
    thing to add is which key it was."""
    data = _FILE.validate_python(json.loads(text))
    for key, value in data.items():
        adapter = _adapter_for(store, key)
        if adapter is None:
            continue
        try:
            adapter.validate_python(value)
        except ValidationError as exc:
            raise ValueError(
                f"the record under {key!r} is not the shape odin writes -- {_detail(exc)}"
            ) from exc
    return data
