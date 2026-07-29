"""The gateway's S3 bucket-notification model: `aws_s3_bucket_notification`'s
CONTROL plane, plus the ENQUEUE half of delivery.

**WHY THIS IS ODIN'S OWN STATE RATHER THAN A FORWARD.** `rustfs/rustfs:latest`
was probed directly (docs/limits.md and docs/event-dispatch-design.md §5): it
rejects EVERY `PutBucketNotificationConfiguration` ARN form with
`InvalidArgument` **and persists the configuration anyway**. Forwarded, that is
three answers that cannot all be true at once -- `tofu apply` FAILS, the next
`tofu plan` reads the config back through GET and reports NO DRIFT, and nothing
ever FIRES. So both bucket-notification actions are PURE here (`synth.py`'s
`_PURE_HANDLERS`): `app.py` returns a non-None `pure_answer` and the request
never reaches RustFS at all.

**THE WIRE SHAPE, MEASURED, NOT DERIVED FROM THE API MEMBER NAMES.** This is
the part nobody should ever re-guess: S3's notification XML element names are
NOT its botocore member names. Captured from a real boto3
`put_bucket_notification_configuration` through the repo's own `CaptureSink`
(tests/gateway/harness.py -- a local socket, no container):

    PUT /uploads?notification
    <NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CloudFunctionConfiguration>
        <Id>on-upload</Id>
        <CloudFunction>arn:aws:lambda:us-east-1:000000000000:function:thumbnailer</CloudFunction>
        <Event>s3:ObjectCreated:*</Event>
        <Filter><S3Key>
          <FilterRule><Name>prefix</Name><Value>incoming/</Value></FilterRule>
          <FilterRule><Name>suffix</Name><Value>.jpg</Value></FilterRule>
        </S3Key></Filter>
      </CloudFunctionConfiguration>
    </NotificationConfiguration>

so, member name -> wire element:

    LambdaFunctionConfigurations -> <CloudFunctionConfiguration>, arn in <CloudFunction>
    QueueConfigurations          -> <QueueConfiguration>,         arn in <Queue>
    TopicConfigurations          -> <TopicConfiguration>,         arn in <Topic>
    Events            (a list)   -> repeated <Event> (flattened, no wrapper)
    Filter.Key.FilterRules       -> <Filter><S3Key><FilterRule><Name>/<Value>

A DELETE of `?notification`, and a PUT of the empty
`<NotificationConfiguration/>` boto3 sends for `NotificationConfiguration={}`,
both mean "clear it" -- classify maps both methods to the same
`s3:PutBucketNotification` action, so this module needs no HTTP method to tell
them apart: an empty parse is a clear either way. A never-configured bucket
GETs back the empty document, which is what makes a refresh clean.

**THE REFUSAL.** The only sink odin can deliver to is a Lambda invoke. A
`QueueConfiguration`, a `TopicConfiguration`, or a `CloudFunctionConfiguration`
whose ARN is not a lambda ARN is therefore REJECTED with a real S3
`InvalidArgument` (the code real S3 itself sends for a bad notification ARN --
and, per the §5 probe, the code RustFS sends too, so odin's refusal reads on the
wire exactly like the upstream one it replaces). Nothing is stored. That is
deliberate and it is the whole point of the module: storing a configuration
nothing delivers would turn today's honest `tofu apply` FAILURE into a silent
one -- apply green, plan clean, nothing fires -- which is strictly worse than
the contradiction it replaces. `eventsctl._put_events` refuses `PutEvents` for
the identical reason.

**WHAT THIS MODULE DOES NOT DO: DELIVER.** `enqueue` writes a
`pending:{id}` record and returns; `reconcile/dispatch.py` drains it on the
reconciler tick. Until that drain exists, a stored lambda notification is a
trigger that does not fire, so this module must land in the SAME merge as the
dispatcher -- never ahead of it.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.gateway import errors
from odin.gateway.stores import SynthStores

SERVICE = "s3"
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"

# The three notification families, wire-element -> the child element carrying
# the target ARN. Measured (see the module docstring), not derived from the
# botocore member names, which differ from all six of these strings.
_FAMILIES = {
    "CloudFunctionConfiguration": "CloudFunction",
    "QueueConfiguration": "Queue",
    "TopicConfiguration": "Topic",
}

# The one sink odin can deliver to. Everything else is refused, loudly.
LAMBDA_ARN_PREFIX = "arn:aws:lambda:"

# AWS's own event vocabulary for the two object writes that reach this gateway.
# `s3:ObjectCreated:*` and `s3:ObjectRemoved:*` are the wildcard forms a user
# actually writes in terraform, and `_event_matches` below is what makes them
# match these concrete names.
OBJECT_CREATED_PUT = "s3:ObjectCreated:Put"
OBJECT_CREATED_MULTIPART = "s3:ObjectCreated:CompleteMultipartUpload"
OBJECT_REMOVED_DELETE = "s3:ObjectRemoved:Delete"

NOT_DELIVERABLE = (
    "odin's gateway delivers an S3 bucket notification by invoking a Lambda function, and a Lambda "
    "invoke is the ONLY sink it has: there is no S3 -> SQS and no S3 -> SNS delivery. {detail} "
    "Storing this configuration would make `tofu apply` succeed and every later `tofu plan` read "
    "clean while nothing ever fires, which is worse than this error. Point the notification at a "
    "Lambda function, or have the producer call SQS/SNS directly, until S3 -> SQS/SNS delivery is built."
)


def _key(bucket: str) -> str:
    return f"notify:{bucket}"


def _local(tag: str) -> str:
    """The element name without its namespace. boto3 always sends the
    `http://s3.amazonaws.com/doc/2006-03-01/` xmlns; a hand-rolled client may
    send none, and both mean the same document."""
    return tag.rsplit("}", 1)[-1]


def _invalid_argument(message: str) -> Response:
    """Real S3's own code for a bad notification ARN -- `InvalidArgument`, 400.

    Chosen because it is what the thing being replaced already says: the §5
    probe measured RustFS answering `InvalidArgument` to every ARN form, and
    real S3 answers `InvalidArgument` for an unreachable/unauthorized
    notification target. It is not a botocore-modelled exception SHAPE for S3
    (the s3 model declares almost none), which does not matter: `RestXMLParser`
    reads `Code` straight out of the `<Error>` document, so a caller gets
    `ClientError.response["Error"]["Code"] == "InvalidArgument"` either way --
    the same reason `errors.synth_error`'s docstring gives for SQS's legacy
    code."""
    return errors.synth_error(SERVICE, "InvalidArgument", message, 400)


# --- parsing the PUT body ----------------------------------------------------


def _filter_rules(element: ElementTree.Element) -> dict[str, str]:
    """`<Filter><S3Key><FilterRule><Name>prefix</Name><Value>x</Value>` ->
    `{"prefix": "x"}`. Real S3 lower-cases neither name nor value, and accepts
    at most one rule of each name."""
    rules: dict[str, str] = {}
    for rule in element.iter():
        if _local(rule.tag) != "FilterRule":
            continue
        names = {_local(child.tag): (child.text or "") for child in rule}
        rules[names.get("Name", "")] = names.get("Value", "")
    return rules


def _configuration(element: ElementTree.Element, arn_tag: str) -> dict[str, Any]:
    """One `<*Configuration>` element -> the normalized record shape every
    reader uses. Flat and stable on purpose: `reconcile/dispatch.py` matches an
    object write against exactly these five fields and builds the S3 event from
    them, so re-nesting it to mirror the wire would push the wire's three
    different ARN element names into the dispatcher."""
    children = {_local(child.tag): child for child in element}
    rules = _filter_rules(children["Filter"]) if "Filter" in children else {}
    return {
        "id": (children["Id"].text or "") if "Id" in children else "",
        "target_arn": (children[arn_tag].text or "") if arn_tag in children else "",
        "events": [child.text or "" for child in element if _local(child.tag) == "Event"],
        "prefix": rules.get("prefix", ""),
        "suffix": rules.get("suffix", ""),
    }


def _parse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """(wire family element name, normalized configuration) for every entry in
    the PUT body, in document order. An empty/absent body is an empty list,
    which is what makes DELETE and the empty PUT the same "clear" path.

    A body that is not XML at all raises `ElementTree.ParseError`, which
    `put_notification` turns into a `MalformedXML` -- never a silent empty
    parse, because an empty parse here CLEARS the bucket's configuration and a
    typo would then silently disable every trigger on it."""
    if not body.strip():
        return []
    root = ElementTree.fromstring(body)
    return [
        (_local(element.tag), _configuration(element, _FAMILIES[_local(element.tag)]))
        for element in root
        if _local(element.tag) in _FAMILIES
    ]


def _refusal(family: str, configuration: dict[str, Any]) -> str | None:
    """The reason this configuration cannot be delivered, or None."""
    named = f"Notification configuration {configuration['id']!r}" if configuration["id"] else "This notification configuration"
    if family != "CloudFunctionConfiguration":
        sink = "an SQS queue" if family == "QueueConfiguration" else "an SNS topic"
        return NOT_DELIVERABLE.format(detail=f"{named} targets {sink} ({configuration['target_arn']}).")
    if not configuration["target_arn"].startswith(LAMBDA_ARN_PREFIX):
        return NOT_DELIVERABLE.format(
            detail=f"{named} carries {configuration['target_arn']!r}, which is not a Lambda function ARN "
                   f"(odin expects one starting {LAMBDA_ARN_PREFIX!r})."
        )
    return None


# --- rendering the GET body --------------------------------------------------


def _filter_xml(configuration: dict[str, Any]) -> str:
    rules = "".join(
        f"<FilterRule><Name>{name}</Name><Value>{escape(configuration[name])}</Value></FilterRule>"
        for name in ("prefix", "suffix")
        if configuration[name]
    )
    return f"<Filter><S3Key>{rules}</S3Key></Filter>" if rules else ""


def _configuration_xml(configuration: dict[str, Any]) -> str:
    """Always `<CloudFunctionConfiguration>`, and that is TOTAL rather than an
    assumption: `_refusal` above is the only write path into this store and it
    rejects every non-lambda target, so a stored configuration is a lambda
    configuration by construction. `test_only_lambda_configurations_can_ever_be_stored`
    is the ratchet on that."""
    events = "".join(f"<Event>{escape(event)}</Event>" for event in configuration["events"])
    return (
        "<CloudFunctionConfiguration>"
        f"<Id>{escape(configuration['id'])}</Id>"
        f"<CloudFunction>{escape(configuration['target_arn'])}</CloudFunction>"
        f"{events}{_filter_xml(configuration)}"
        "</CloudFunctionConfiguration>"
    )


def _notification_xml(configurations: list[dict[str, Any]]) -> str:
    body = "".join(_configuration_xml(configuration) for configuration in configurations)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<NotificationConfiguration xmlns="{S3_NS}">{body}</NotificationConfiguration>'
    )


# --- the two PURE actions ----------------------------------------------------


def put_notification(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """`PUT|DELETE /{bucket}?notification`. Never forwarded -- see the module
    docstring for the measured reason RustFS cannot hold this state.

    Real S3 answers a bare 200 with no body, which is what the provider's
    create-then-read expects; botocore's `PutBucketNotificationConfiguration`
    output shape has no members at all."""
    try:
        parsed = _parse(body)
    except ElementTree.ParseError as exc:
        return errors.synth_error(SERVICE, "MalformedXML", f"The notification configuration is not well-formed XML ({exc}).", 400)
    refusals = [reason for family, configuration in parsed if (reason := _refusal(family, configuration))]
    if refusals:
        return _invalid_argument(" ".join(refusals))
    configurations = [configuration for _family, configuration in parsed]
    if configurations:
        stores.s3notify.set(env, _key(resource), {"configurations": configurations})
        return Response(b"", status_code=200)
    # An empty PUT and a DELETE both clear. Deleting the RECORD rather than
    # storing an empty list keeps "cleared" and "never configured" the same
    # state, so `get_notification` has one path instead of two.
    stores.s3notify.delete(env, _key(resource))
    return Response(b"", status_code=200)


def get_notification(resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response:
    """`GET /{bucket}?notification`. A never-configured (or cleared) bucket
    gets the empty document real S3 returns, which is what makes a `tofu`
    refresh clean rather than drifting on every plan."""
    record = stores.s3notify.get(env, _key(resource), {"configurations": []})
    return Response(_notification_xml(record["configurations"]), media_type="application/xml")


def configurations(stores: SynthStores, env: str, bucket: str) -> list[dict[str, Any]]:
    """This bucket's stored configurations -- the read `reconcile/dispatch.py`
    starts from. Deliberately in-process only and never on an HTTP route, the
    same boundary `eventsctl.rules` and `ssmctl.parameter_value` keep."""
    return stores.s3notify.get(env, _key(bucket), {"configurations": []})["configurations"]


# --- the ENQUEUE hook (a POSTPROCESS action, so it cannot await) -------------


def _object_key(path: str) -> str:
    """The object key out of a raw request path. `path` is the RAW
    percent-encoded target (`app.py::_raw_target`), so it needs the same
    `unquote` `classify._classify_s3` applies to the bucket."""
    segments = unquote(path).strip("/").split("/", 1)
    return segments[1] if len(segments) > 1 else ""


def _deleted_keys(response_body: bytes) -> list[str]:
    """The keys a multi-object `POST /{bucket}?delete` ACTUALLY removed, read
    out of its `<DeleteResult><Deleted><Key>` RESPONSE rather than the request.

    The request lists what was ASKED for; DeleteObjects reports per-object
    success and failure separately, and enqueuing off the request would fire
    `ObjectRemoved` for objects that are still there -- reporting a delete odin
    did not achieve.

    MEASURED against `rustfs/rustfs:latest` (scoped throwaway container, since
    removed), deleting one key that existed and one that never did:

        HTTP 200
        <?xml version="1.0" encoding="UTF-8"?><DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <Deleted><Key>incoming/a.jpg</Key></Deleted>
        <Deleted><Key>does/not/exist.jpg</Key></Deleted></DeleteResult>

    -- so the element shape is exactly what this parses, and there were ZERO
    `<Error>` entries. That second fact is the LIMIT recorded in
    `docs/limits.md`: DeleteObjects is idempotent, so S3 reports a key that
    never existed as `<Deleted>`, and odin therefore enqueues a removal
    notification real AWS would not send (real AWS fires nothing -- nothing was
    removed). The response carries no signal that distinguishes the two cases,
    and `postprocess` runs AFTER the forward, so odin cannot tell from here.
    The single-object `DELETE /{b}/{k}` has the identical divergence for the
    identical reason: S3 answers 204 whether or not the key was there.

    The `<Error>` skip below is still correct and still load-bearing -- a
    GENUINE per-object failure (AccessDenied, an object-lock retention) does
    come back as `<Error><Key>` and must not fire. RustFS was simply not
    provoked into emitting one, which is why the test for that branch says out
    loud that its body comes from the S3 API reference rather than a probe."""
    root = ElementTree.fromstring(response_body)
    return [
        (key.text or "")
        for deleted in root
        if _local(deleted.tag) == "Deleted"
        for key in deleted
        if _local(key.tag) == "Key"
    ]


def _writes(action: str, path: str, query: dict[str, str], request_body: bytes, response_body: bytes) -> list[tuple[str, str, int, str]]:
    """(key, event name, size, etag) for every object this request actually
    landed or removed -- EMPTY for the request shapes that land no object.

    This is the part `classify` cannot tell us and the part §5 of the design
    note missed. FOUR distinct wire shapes all classify to `s3:PutObject`:

      PUT  /{b}/{k}                        a single-part write   -> fires
      POST /{b}/{k}?uploads                CreateMultipartUpload -> NO object yet
      PUT  /{b}/{k}?partNumber=N&uploadId= UploadPart            -> NO object yet
      POST /{b}/{k}?uploadId=              CompleteMultipart     -> fires

    and two classify to `s3:DeleteObject`:

      DELETE /{b}/{k}                      one key, in the path
      POST   /{b}?delete                   many keys, in the BODY, no key in the path

    Without the query string this function would fire one notification per
    UPLOADED PART and one for the create-upload call, and would enqueue an
    empty key for every multi-object delete. That is why `postprocess` takes
    `path` and `query` even though `resource` already carries the bucket.

    `size`/`etag` are known only for the single-part write: `postprocess` never
    sees the backing's response HEADERS, so odin never observes an ETag at all.
    For that one shape S3's ETag is defined as the MD5 of the body odin just
    forwarded, so it is COMPUTED here rather than observed, and left empty for
    every shape where that identity does not hold. Deletes carry neither in a
    real S3 event either.

    ONE KNOWN OVER-FIRE, measured and documented in `docs/limits.md` rather
    than hidden: BOTH delete shapes enqueue a removal for a key that never
    existed. S3 is idempotent about deletes -- a single-object DELETE answers
    204 and DeleteObjects reports `<Deleted>`, in both cases whether or not the
    key was there (probed against RustFS; see `_deleted_keys`) -- so the
    response carries nothing that would let odin tell the difference, and
    `postprocess` runs after the forward, when the answer is unrecoverable
    either way. Real AWS fires no event there."""
    if action == "s3:DeleteObject":
        keys = _deleted_keys(response_body) if "delete" in query else [_object_key(path)]
        return [(key, OBJECT_REMOVED_DELETE, 0, "") for key in keys if key]
    key = _object_key(path)
    if not key or "uploads" in query or "partNumber" in query:
        return []
    if "uploadId" in query:
        return [(key, OBJECT_CREATED_MULTIPART, 0, "")]
    return [(key, OBJECT_CREATED_PUT, len(request_body), hashlib.md5(request_body).hexdigest())]


def _event_matches(stored: str, actual: str) -> bool:
    """AWS's real vocabulary: `s3:ObjectCreated:*` matches
    `s3:ObjectCreated:Put`, `s3:ObjectRemoved:*` matches
    `s3:ObjectRemoved:Delete`, and an exact name matches only itself -- so a
    configuration written as `s3:ObjectCreated:Put` does NOT fire for a
    multipart completion, exactly as on real S3."""
    return stored == actual or (stored.endswith(":*") and actual.startswith(stored[:-1]))


def matches(configuration: dict[str, Any], event_name: str, key: str) -> bool:
    """Whether this stored configuration fires for this write. The
    prefix/suffix half is `Filter.Key.FilterRules`, and it filters the object
    KEY -- an empty prefix/suffix is "no filter" because every string starts
    and ends with `""`, so absent rules need no separate branch."""
    return (
        any(_event_matches(stored, event_name) for stored in configuration["events"])
        and key.startswith(configuration["prefix"])
        and key.endswith(configuration["suffix"])
    )


def _enqueue(
    action: str, resource: str, env: str, request_body: bytes, response_body: bytes,
    stores: SynthStores, now: float, path: str, query: dict[str, str],
) -> bytes:
    """Write one `pending:{id}` record per MATCHING configuration and return
    the response body untouched.

    ENQUEUE, never invoke. `synth.postprocess` is SYNCHRONOUS and is called
    from `catch_all`'s async body, so it cannot await; and firing inline would
    block the object-write response for the whole handler duration, which real
    S3 notifications never do (they are asynchronous and at-least-once). The
    tick-driven `reconcile/dispatch.py` delivers.

    `at` is `time.time()`, NOT the `now` argument. `now` is `time.monotonic()`
    (app.py), whose epoch is arbitrary and RESETS when the process restarts --
    and this record is persisted to `.odin/{env}/gateway/dispatch.json`, so a
    pending write from before a restart and one from after would be ordered
    against incomparable clocks and drain out of order. `DispatchAnchor`
    alongside it in `records.py` is documented as wall-clock seconds for the
    same reason, and `eventsctl.pure_answer` mints its own `time.time()` on the
    identical trap."""
    at = time.time()
    stored = configurations(stores, env, resource)
    for key, event_name, size, etag in _writes(action, path, query, request_body, response_body):
        for configuration in stored:
            if matches(configuration, event_name, key):
                stores.dispatch.set(env, f"pending:{uuid.uuid4().hex}", {
                    "bucket": resource, "key": key, "event_name": event_name,
                    "target_arn": configuration["target_arn"], "at": at, "size": size, "etag": etag,
                })
    return response_body


# `synth._POSTPROCESS_HANDLERS` keys the table by action but does not pass the
# action to the handler, and `_writes` needs it (a PUT and a multi-object
# DELETE are told apart by action, not by method or path). Two thin bindings
# rather than a signature change to every other postprocess handler.
def enqueue_put_object(
    resource: str, env: str, request_body: bytes, response_body: bytes,
    stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str],
) -> bytes:
    return _enqueue("s3:PutObject", resource, env, request_body, response_body, stores, now, path, query)


def enqueue_delete_object(
    resource: str, env: str, request_body: bytes, response_body: bytes,
    stores: SynthStores, gateway_host: str, now: float, path: str, query: dict[str, str],
) -> bytes:
    return _enqueue("s3:DeleteObject", resource, env, request_body, response_body, stores, now, path, query)
