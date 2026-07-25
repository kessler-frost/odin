"""The gateway's CloudWatch Logs model (task W2.1): the
`aws_cloudwatch_log_group` CONTROL plane *and* the Logs DATA plane -- odin's
one log SINK, which is what makes wave-1's `odin logs` read the same place
regardless of which substrate produced the output.

Like ec2net/iamctl/ecr/lambdactl/ecsctl, `logs` has no backing container to
forward to: this module is the whole answer for every `logs:*` action
(classified by `classify.py`'s `_classify_logs` -- ECR's JSON-target wire
shape, `X-Amz-Target: Logs_20140328.*`, verified against botocore's own
`logs` service model: protocol `json`, targetPrefix `Logs_20140328`).

Control plane (what the TF AWS provider drives for `aws_cloudwatch_log_group`):
CreateLogGroup / DeleteLogGroup / DescribeLogGroups / PutRetentionPolicy /
DeleteRetentionPolicy, plus tag CRUD (`ListTagsForResource` -- and the legacy
`ListTagsLogGroup` the older provider path still uses -- `TagResource`,
`UntagResource`) so `tags` round-trip and apply -> plan is zero-drift. The
tags approach is ecr/ecsctl's, unchanged: the shared `stores.tags` store keyed
`"logs:{arn}"`.

Data plane: CreateLogStream / PutLogEvents / GetLogEvents / FilterLogEvents /
DescribeLogStreams over a per-env JSON sidecar (`stores.logsctl`, at
`.odin/{env}/gateway/logsctl.json`) -- dev-scale logs, so no backing container
of its own (W2.1's own plan: "a per-env JSON sidecar is enough for v1;
revisit if volume demands a real backing"). Two documented consequences of
that choice:

- **Bounded storage.** Each group keeps at most `MAX_EVENTS_PER_GROUP`
  (10_000) events in a ring buffer -- appending past the cap drops the OLDEST
  events, never the newest. The store is rewritten wholesale on every
  mutation (JsonStore's shape), so this cap is what keeps a chatty container
  from turning every append into a multi-MB rewrite.
- **No cross-call pagination state.** `GetLogEvents`'s tokens encode "how
  many of this stream's events you've already been handed" (`"f/{n}"` /
  `"b/{n}"`), so a boto3 paginator terminates naturally; a ring-buffer
  eviction between two paged calls can skip events (real AWS's opaque tokens
  don't have that failure mode). FilterLogEvents/DescribeLogStreams never
  paginate at all (`nextToken` is always absent).

ARNs: real CloudWatch's `DescribeLogGroups` reports a log-group ARN WITH the
trailing `:*` wildcard, which terraform-provider-aws then TRIMS before
storing it as the resource's `arn` (and before using it as a tagging
`resourceArn`) -- so this module emits the `:*` form on the wire and accepts
either form everywhere a `resourceArn` arrives (`_group_from_arn`).

TWO deliberate deviations from real AWS, both in service of "the canvas is
the source of truth and Apply must converge":

1. **Substrate ingestion auto-creates.** `ingest_tail`/`ingest` (the internal
   API the Lambda/ECS substrates call -- NOT the SigV4 wire) create a missing
   group/stream on the fly, exactly like real Lambda auto-creating
   `/aws/lambda/{fn}`. Such a group is flagged `auto`.
2. **CreateLogGroup ADOPTS an auto-created group** instead of failing
   `ResourceAlreadyExistsException`. Without this, invoking a lambda before
   drawing its log group would wedge every later Apply (tofu would try to
   create a group the substrate already made). A group created by a real
   CreateLogGroup still errors AlreadyExists, matching AWS.

`sequenceToken` is accepted and echoed-forward but never validated
(`InvalidSequenceTokenException` is unreachable here) -- matching current real
CloudWatch Logs, which ignores the token entirely.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from starlette.responses import Response

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway import errors
from odin.gateway.stores import SynthStores

# Per-group ring buffer (module docstring). 10k dev-scale events is ~a few MB
# of JSON at worst, the ceiling on one store rewrite.
MAX_EVENTS_PER_GROUP = 10_000
# The default a substrate tail read is bounded to (and `GetLogEvents`'s own
# AWS-default limit).
DEFAULT_LIMIT = 10_000
LOG_GROUP_CLASS = "STANDARD"


def group_arn(name: str) -> str:
    """The CANONICAL (wildcard-less) log-group ARN -- the tags-store key and
    what the TF provider stores as the resource's `arn`."""
    return f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{name}"


def _wire_arn(name: str) -> str:
    """What real `DescribeLogGroups` puts on the wire: the canonical ARN plus
    the `:*` wildcard suffix the provider trims back off."""
    return f"{group_arn(name)}:*"


def _stream_arn(group: str, stream: str) -> str:
    return f"{group_arn(group)}:log-stream:{stream}"


def _group_from_arn(arn: str) -> str:
    """The bare group name out of either ARN form (with or without the `:*`
    wildcard) -- or the input unchanged when it isn't an ARN at all, so a
    caller may pass a plain group name."""
    trimmed = arn[:-2] if arn.endswith(":*") else arn
    _prefix, sep, name = trimmed.partition(":log-group:")
    return name.split(":log-stream:")[0] if sep else trimmed


def _json(payload: dict) -> Response:
    return Response(json.dumps(payload), media_type="application/x-amz-json-1.1")


def _not_found(message: str) -> Response:
    return errors.synth_error("logs", "ResourceNotFoundException", message, 400)


def _already_exists(message: str) -> Response:
    return errors.synth_error("logs", "ResourceAlreadyExistsException", message, 400)


def _invalid(message: str) -> Response:
    return errors.synth_error("logs", "InvalidParameterException", message, 400)


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- store keys ------------------------------------------------------------


def _group_key(name: str) -> str:
    return f"group:{name}"


def _stream_key(group: str, stream: str) -> str:
    return f"stream:{group}:{stream}"


def _events_key(group: str) -> str:
    return f"events:{group}"


def _cursor_key(group: str, stream: str) -> str:
    return f"cursor:{group}:{stream}"


def _group(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.logsctl.get(env, _group_key(name))


def _groups(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.logsctl.items(env).items() if k.startswith("group:")]


def _stream(stores: SynthStores, env: str, group: str, stream: str) -> dict | None:
    return stores.logsctl.get(env, _stream_key(group, stream))


def _streams(stores: SynthStores, env: str, group: str) -> list[dict]:
    prefix = f"stream:{group}:"
    return [v for k, v in stores.logsctl.items(env).items() if k.startswith(prefix)]


def _events(stores: SynthStores, env: str, group: str) -> list[dict]:
    return stores.logsctl.get(env, _events_key(group), [])


def _tags_for(stores: SynthStores, env: str, name: str) -> dict[str, str]:
    return stores.tags.get(env, f"logs:{group_arn(name)}", {})


def _set_tags(stores: SynthStores, env: str, name: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"logs:{group_arn(name)}", tags)


# --- wire shapes (member names verified against botocore's `logs` model) ---


def _wire_group(record: dict, events: list[dict]) -> dict:
    stored_bytes = sum(len(e["message"]) for e in events)
    return {
        "logGroupName": record["log_group_name"],
        "creationTime": record["creation_time"],
        "retentionInDays": record["retention_in_days"],
        "metricFilterCount": 0,
        "arn": _wire_arn(record["log_group_name"]),
        "storedBytes": stored_bytes,
        "logGroupClass": LOG_GROUP_CLASS,
    }


def _wire_stream(record: dict, events: list[dict]) -> dict:
    return {
        "logStreamName": record["log_stream_name"],
        "creationTime": record["creation_time"],
        "firstEventTimestamp": record["first_event_timestamp"],
        "lastEventTimestamp": record["last_event_timestamp"],
        "lastIngestionTime": record["last_ingestion_time"],
        "uploadSequenceToken": str(record["upload_sequence_token"]),
        "arn": _stream_arn(record["log_group"], record["log_stream_name"]),
        "storedBytes": sum(len(e["message"]) for e in events),
    }


def _drop_none(payload: dict) -> dict:
    """Omit unset optional members entirely (real AWS omits, rather than
    sending null) -- the same fidelity rule lambdactl's `_json` keeps, and
    what keeps the provider from reading a null as a real value."""
    return {k: v for k, v in payload.items() if v is not None}


# --- control plane ---------------------------------------------------------


def _create_log_group(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("logGroupName") or ""
    if not name:
        return _invalid("logGroupName is required")
    existing = _group(stores, env, name)
    if existing is not None and not existing.get("auto"):
        return _already_exists(f"The specified log group already exists: {name}")
    # Adoption (module docstring, deviation 2): keep the substrate-created
    # group's events + creation time, just stop calling it auto-created.
    record = {
        "log_group_name": name,
        "creation_time": (existing or {}).get("creation_time") or _now_ms(),
        "retention_in_days": None,
        "auto": False,
    }
    stores.logsctl.set(env, _group_key(name), record)
    _set_tags(stores, env, name, dict(payload.get("tags") or {}))
    return _json({})


def _delete_log_group(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("logGroupName") or ""
    if _group(stores, env, name) is None:
        return _not_found(f"The specified log group does not exist: {name}")
    for key in list(stores.logsctl.items(env)):
        if key in (_group_key(name), _events_key(name)) or key.startswith(f"stream:{name}:") or key.startswith(f"cursor:{name}:"):
            stores.logsctl.delete(env, key)
    _set_tags(stores, env, name, {})
    return _json({})


def _describe_log_groups(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    prefix = payload.get("logGroupNamePrefix") or ""
    pattern = payload.get("logGroupNamePattern") or ""
    records = sorted(_groups(stores, env), key=lambda r: r["log_group_name"])
    matched = [
        r for r in records
        if r["log_group_name"].startswith(prefix) and pattern in r["log_group_name"]
    ]
    limit = int(payload.get("limit") or DEFAULT_LIMIT)
    groups = [_drop_none(_wire_group(r, _events(stores, env, r["log_group_name"]))) for r in matched[:limit]]
    return _json({"logGroups": groups})


def _put_retention_policy(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("logGroupName") or ""
    record = _group(stores, env, name)
    if record is None:
        return _not_found(f"The specified log group does not exist: {name}")
    stores.logsctl.set(env, _group_key(name), {**record, "retention_in_days": int(payload.get("retentionInDays") or 0)})
    return _json({})


def _delete_retention_policy(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name = payload.get("logGroupName") or ""
    record = _group(stores, env, name)
    if record is None:
        return _not_found(f"The specified log group does not exist: {name}")
    stores.logsctl.set(env, _group_key(name), {**record, "retention_in_days": None})
    return _json({})


def _tagged_group(payload: dict, env: str, stores: SynthStores) -> tuple[str, dict | None]:
    name = _group_from_arn(payload.get("resourceArn") or payload.get("logGroupName") or "")
    return name, _group(stores, env, name)


def _list_tags(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name, record = _tagged_group(payload, env, stores)
    if record is None:
        return _not_found(f"The specified log group does not exist: {name}")
    return _json({"tags": _tags_for(stores, env, name)})


def _tag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name, record = _tagged_group(payload, env, stores)
    if record is None:
        return _not_found(f"The specified log group does not exist: {name}")
    _set_tags(stores, env, name, {**_tags_for(stores, env, name), **(payload.get("tags") or {})})
    return _json({})


def _untag_resource(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    name, record = _tagged_group(payload, env, stores)
    if record is None:
        return _not_found(f"The specified log group does not exist: {name}")
    remove = set(payload.get("tagKeys") or [])
    _set_tags(stores, env, name, {k: v for k, v in _tags_for(stores, env, name).items() if k not in remove})
    return _json({})


# --- data plane ------------------------------------------------------------


def _new_stream(group: str, stream: str) -> dict:
    return {
        "log_group": group,
        "log_stream_name": stream,
        "creation_time": _now_ms(),
        "first_event_timestamp": None,
        "last_event_timestamp": None,
        "last_ingestion_time": None,
        "upload_sequence_token": 1,
    }


def _create_log_stream(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    group = payload.get("logGroupName") or ""
    stream = payload.get("logStreamName") or ""
    if _group(stores, env, group) is None:
        return _not_found(f"The specified log group does not exist: {group}")
    if _stream(stores, env, group, stream) is not None:
        return _already_exists(f"The specified log stream already exists: {stream}")
    stores.logsctl.set(env, _stream_key(group, stream), _new_stream(group, stream))
    return _json({})


def _append_events(stores: SynthStores, env: str, group: str, stream: str, events: list[dict]) -> int:
    """Append `events` (already-wire-shaped `{timestamp, message}` dicts) to
    the group's ring buffer and advance the stream's timestamp bookkeeping.
    Returns the new upload sequence token. The whole append is ONE
    `JsonStore.update` so two substrates shipping into the same group can't
    interleave a read with the other's write."""
    ingestion = _now_ms()
    records = [
        {
            "stream": stream,
            "timestamp": int(e.get("timestamp") or ingestion),
            "message": str(e.get("message") or ""),
            "ingestion_time": ingestion,
            "event_id": uuid.uuid4().hex,
        }
        for e in events
    ]

    def mutate(current: list | None) -> list:
        return ([*(current or []), *records])[-MAX_EVENTS_PER_GROUP:]

    stores.logsctl.update(env, _events_key(group), mutate)

    def mutate_stream(current: dict | None) -> dict:
        record = dict(current or _new_stream(group, stream))
        stamps = [r["timestamp"] for r in records]
        record["first_event_timestamp"] = record["first_event_timestamp"] or (min(stamps) if stamps else None)
        record["last_event_timestamp"] = max(stamps) if stamps else record["last_event_timestamp"]
        record["last_ingestion_time"] = ingestion
        record["upload_sequence_token"] = int(record["upload_sequence_token"]) + 1
        return record

    updated = stores.logsctl.update(env, _stream_key(group, stream), mutate_stream)
    return int(updated["upload_sequence_token"])


def _put_log_events(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    group = payload.get("logGroupName") or ""
    stream = payload.get("logStreamName") or ""
    if _group(stores, env, group) is None:
        return _not_found(f"The specified log group does not exist: {group}")
    if _stream(stores, env, group, stream) is None:
        return _not_found(f"The specified log stream does not exist: {stream}")
    token = _append_events(stores, env, group, stream, list(payload.get("logEvents") or []))
    return _json({"nextSequenceToken": str(token)})


def _ordered(events: list[dict]) -> list[dict]:
    """Timestamp order, ties broken by ingestion order -- `sorted` is stable,
    so equal timestamps keep the order they were appended in (which is the
    order the substrate's own container printed them)."""
    return sorted(events, key=lambda e: e["timestamp"])


def _in_window(event: dict, start: int | None, end: int | None) -> bool:
    # AWS's own window semantics: startTime inclusive, endTime exclusive.
    return (start is None or event["timestamp"] >= start) and (end is None or event["timestamp"] < end)


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _token_offset(token: object) -> int | None:
    """`"f/{n}"` -> n (see the module docstring's pagination note); anything
    unrecognized reads as "no token"."""
    if not isinstance(token, str) or "/" not in token:
        return None
    _side, _sep, digits = token.partition("/")
    return int(digits) if digits.isdigit() else None


def _get_log_events(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    group = payload.get("logGroupName") or _group_from_arn(payload.get("logGroupIdentifier") or "")
    stream = payload.get("logStreamName") or ""
    if _group(stores, env, group) is None:
        return _not_found(f"The specified log group does not exist: {group}")
    if _stream(stores, env, group, stream) is None:
        return _not_found(f"The specified log stream does not exist: {stream}")
    start, end = _int_or_none(payload.get("startTime")), _int_or_none(payload.get("endTime"))
    events = [
        e for e in _ordered(_events(stores, env, group))
        if e["stream"] == stream and _in_window(e, start, end)
    ]
    limit = int(payload.get("limit") or DEFAULT_LIMIT)
    offset = _token_offset(payload.get("nextToken"))
    if offset is not None:
        window = events[offset:offset + limit]
        first = offset
    elif payload.get("startFromHead"):
        window, first = events[:limit], 0
    else:  # AWS's default: the MOST RECENT `limit` events
        first = max(0, len(events) - limit)
        window = events[first:]
    return _json({
        "events": [{"timestamp": e["timestamp"], "message": e["message"], "ingestionTime": e["ingestion_time"]} for e in window],
        "nextForwardToken": f"f/{first + len(window)}",
        "nextBackwardToken": f"b/{first}",
    })


def _matches_pattern(message: str, pattern: str) -> bool:
    """v1 filter patterns are a plain SUBSTRING match (quotes stripped, since
    a real `"ERROR"` term-literal is the common case). CloudWatch's full
    filter-pattern grammar -- JSON selectors, space-delimited field
    positions, `?`/`-` term composition, metric filters -- is deliberately
    NOT modeled (W2.1 records it as an unsupported edge, never silently
    wrong: an unmodeled construct simply won't match rather than being
    reinterpreted)."""
    return pattern.strip('"') in message


def _filter_log_events(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    group = payload.get("logGroupName") or _group_from_arn(payload.get("logGroupIdentifier") or "")
    if _group(stores, env, group) is None:
        return _not_found(f"The specified log group does not exist: {group}")
    names = set(payload.get("logStreamNames") or [])
    prefix = payload.get("logStreamNamePrefix") or ""
    pattern = payload.get("filterPattern") or ""
    start, end = _int_or_none(payload.get("startTime")), _int_or_none(payload.get("endTime"))
    events = [
        e for e in _ordered(_events(stores, env, group))
        if (not names or e["stream"] in names)
        and e["stream"].startswith(prefix)
        and _in_window(e, start, end)
        and (not pattern or _matches_pattern(e["message"], pattern))
    ]
    limit = int(payload.get("limit") or DEFAULT_LIMIT)
    window = events[:limit]
    return _json({
        "events": [
            {
                "logStreamName": e["stream"], "timestamp": e["timestamp"],
                "message": e["message"], "ingestionTime": e["ingestion_time"], "eventId": e["event_id"],
            }
            for e in window
        ],
        "searchedLogStreams": [
            {"logStreamName": name, "searchedCompletely": True}
            for name in sorted({e["stream"] for e in window})
        ],
    })


def _describe_log_streams(payload: dict, env: str, stores: SynthStores, now: float) -> Response:
    group = payload.get("logGroupName") or _group_from_arn(payload.get("logGroupIdentifier") or "")
    if _group(stores, env, group) is None:
        return _not_found(f"The specified log group does not exist: {group}")
    prefix = payload.get("logStreamNamePrefix") or ""
    records = [r for r in _streams(stores, env, group) if r["log_stream_name"].startswith(prefix)]
    by_time = (payload.get("orderBy") or "LogStreamName") == "LastEventTime"
    records.sort(key=lambda r: (r["last_event_timestamp"] or 0) if by_time else r["log_stream_name"])
    if payload.get("descending"):
        records.reverse()
    events = _events(stores, env, group)
    streams = [
        _drop_none(_wire_stream(r, [e for e in events if e["stream"] == r["log_stream_name"]]))
        for r in records[:int(payload.get("limit") or DEFAULT_LIMIT)]
    ]
    return _json({"logStreams": streams})


# --- the internal ingestion API the substrates use (never the wire) -------


def ensure_group(stores: SynthStores, env: str, group: str) -> None:
    """Create `group` if it doesn't exist, flagged `auto` so a later real
    CreateLogGroup ADOPTS it rather than failing (module docstring)."""
    if _group(stores, env, group) is None:
        stores.logsctl.set(env, _group_key(group), {
            "log_group_name": group, "creation_time": _now_ms(),
            "retention_in_days": None, "auto": True,
        })


def ingest(stores: SynthStores, env: str, group: str, stream: str, lines: list[str]) -> int:
    """Append one event per line to `group`/`stream`, auto-creating both.
    Returns the number of events appended. The substrate-side entry point
    (`gateway/models/lambdactl.py`'s Invoke, `ecsctl.py`'s task sweep) --
    NOT reachable over the wire, so it deliberately skips the
    ResourceNotFound guards `PutLogEvents` keeps."""
    if not lines:
        return 0
    ensure_group(stores, env, group)
    _append_events(stores, env, group, stream, [{"message": line} for line in lines])
    return len(lines)


def ingest_tail(stores: SynthStores, env: str, group: str, stream: str, text: str) -> int:
    """Ship a container's log TAIL into `group`/`stream`, appending only the
    lines this stream hasn't seen before -- the dedup every repeated
    sweep/invoke needs.

    The cursor is simply "how many lines of this container's output have
    already been ingested" (`cursor:{group}:{stream}`), and `text` is a
    bounded `docker logs --tail N` read, so:
      - a re-read of the same tail appends NOTHING (the common case: an ECS
        sweep every reconciler tick, a lambda invoked twice);
      - a burst LARGER than the caller's tail window loses the oldest lines
        of that burst rather than duplicating anything -- the deliberate
        trade-off of keeping the cursor a plain line count instead of
        streaming every container continuously.

    A cursor is only valid for as long as the container behind the stream is:
    a REPLACED container starts its output back at line 1, so whoever replaces
    it must call `reset_cursor` (see `lambdactl.py`'s redeploy path) or the new
    container's first lines would be mistaken for already-ingested ones.
    """
    lines = text.splitlines()
    cursor = int(stores.logsctl.get(env, _cursor_key(group, stream), 0))
    fresh = lines[cursor:]
    if not fresh:
        return 0
    appended = ingest(stores, env, group, stream, fresh)
    stores.logsctl.set(env, _cursor_key(group, stream), cursor + appended)
    return appended


def reset_cursor(stores: SynthStores, env: str, group: str, stream: str) -> None:
    """Forget how many lines of `stream` have been ingested -- called by
    whoever REPLACES the real container behind that stream (a Lambda
    redeploy), whose fresh output starts back at line 1. Without it,
    `ingest_tail` would skip the new container's first lines; with it, no line
    is lost and none is duplicated (the events already stored stay put -- this
    resets the READ position, never the log). A stream with no cursor yet (the
    first deploy of a function) is left completely alone rather than
    rewriting the whole sidecar for a key that isn't there."""
    if stores.logsctl.get(env, _cursor_key(group, stream)) is not None:
        stores.logsctl.delete(env, _cursor_key(group, stream))


def stored_events(stores: SynthStores, env: str, group: str, tail: int) -> list[dict]:
    """The last `tail` events of `group` in timestamp order -- the read
    `api/logs.py` serves for a `logs` node (and for `odin logs --group`),
    bypassing SigV4/IAM exactly like every other odin control-plane read."""
    return _ordered(_events(stores, env, group))[-tail:] if tail > 0 else []


def group_exists(stores: SynthStores, env: str, group: str) -> bool:
    return _group(stores, env, group) is not None


# --- dispatch --------------------------------------------------------------

_Handler = Callable[[dict, str, SynthStores, float], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateLogGroup": _create_log_group,
    "DeleteLogGroup": _delete_log_group,
    "DescribeLogGroups": _describe_log_groups,
    "PutRetentionPolicy": _put_retention_policy,
    "DeleteRetentionPolicy": _delete_retention_policy,
    "ListTagsForResource": _list_tags,
    "ListTagsLogGroup": _list_tags,
    "TagResource": _tag_resource,
    "UntagResource": _untag_resource,
    "CreateLogStream": _create_log_stream,
    "PutLogEvents": _put_log_events,
    "GetLogEvents": _get_log_events,
    "FilterLogEvents": _filter_log_events,
    "DescribeLogStreams": _describe_log_streams,
}


def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """The whole CloudWatch Logs answer -- same no-backing contract as
    ec2net/iamctl/ecr: an unmodeled action gets a protocol-correct error,
    never a 503 and never a silent forward."""
    op = action.removeprefix("logs:")
    handler = _HANDLERS.get(op)
    if handler is None:
        return errors.synth_error("logs", "InvalidAction", f"The action {op} is not valid.", 400)
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    return handler(payload, env, stores, now)
