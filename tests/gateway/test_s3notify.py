"""gateway/models/s3notify.py: the S3 bucket-notification control plane, the
refusal that keeps an undeliverable trigger out of the store, and the enqueue
hook that hands an object write to the dispatcher.

Same method as W2.1's logsctl and the EventBridge control plane: REAL
boto3-signed captures through the repo's own `CaptureSink` (a local socket, no
container), every request routed through the real `classify()` -> the real
`synth.pure_answer()`/`synth.postprocess()`, and every response round-tripped
through botocore's OWN `rest-xml` parser against the REAL `s3` service model.

That last part is not ceremony here, it is the whole point: S3's notification
XML element names are NOT its botocore member names
(`LambdaFunctionConfigurations` goes on the wire as `<CloudFunctionConfiguration>`
carrying `<CloudFunction>`), so a rendering written from the API shape would
look right and parse to nothing at all.
"""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path

import boto3
import botocore.session
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.gateway import synth
from odin.gateway.app import GatewayState, create_gateway_app, serve_in_thread, stop_in_thread
from odin.gateway.classify import classify
from odin.gateway.keys import KeyStore
from odin.gateway.models import s3notify
from odin.gateway.policy import Statement
from odin.gateway.stores import SynthStores

from .conftest import split_url

_SESSION = botocore.session.get_session()
ENV = "s3n-default"
BUCKET = "uploads"
LAMBDA_ARN = "arn:aws:lambda:us-east-1:000000000000:function:thumbnailer"
OTHER_LAMBDA_ARN = "arn:aws:lambda:us-east-1:000000000000:function:archiver"
QUEUE_ARN = "arn:aws:sqs:us-east-1:000000000000:jobs"
TOPIC_ARN = "arn:aws:sns:us-east-1:000000000000:fanout"

# The REAL `DeleteObjects` answer, captured from a scoped throwaway
# `rustfs/rustfs:latest` (since removed) while deleting one key that existed
# and one that never did. Two facts, and the second is the interesting one:
# the element shape is exactly `<DeleteResult><Deleted><Key>`, and a key that
# NEVER EXISTED comes back as `<Deleted>` with no `<Error>` at all -- correct
# S3 idempotency, and the reason the RESPONSE can never decide whether to fire.
# What decides is `app.py`'s pre-forward HEAD probe, which arrives here as the
# `absent` argument to `_post`.
MEASURED_DELETE_RESULT = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Deleted><Key>incoming/a.jpg</Key></Deleted>"
    b"<Deleted><Key>does/not/exist.jpg</Key></Deleted>"
    b"</DeleteResult>"
)

LAMBDA_CONFIG = {
    "LambdaFunctionConfigurations": [{
        "Id": "on-upload",
        "LambdaFunctionArn": LAMBDA_ARN,
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [
            {"Name": "prefix", "Value": "incoming/"},
            {"Name": "suffix", "Value": ".jpg"},
        ]}},
    }],
}


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


def _parse(operation: str, response: Response):
    model = _SESSION.get_service_model("s3")
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {"status_code": response.status_code, "headers": dict(response.headers), "body": bytes(response.body)}
    return parser.parse(raw, operation_model.output_shape)


def _classified(req):
    """The real classify() verdict for a real captured request. Every test goes
    through it rather than naming an action directly -- a handler wired to an
    action classify never produces is a handler that never runs."""
    path, query = split_url(req.url)
    classified = classify("s3", req.method, path, query, req.headers, req.body)
    assert classified is not None, f"unmappable: {req.method} {req.url}"
    action, resource = classified
    return action, resource, path, query


async def _pure(stores: SynthStores, req) -> Response:
    action, resource, _path, _query = _classified(req)
    response = await synth.pure_answer(action, resource, ENV, req.body, stores, 0.0)
    assert response is not None, f"{action} must be synth-owned, never forwarded to RustFS"
    return response


def _post(stores: SynthStores, req, response_body: bytes = b"", absent: frozenset[str] = frozenset()) -> bytes:
    action, resource, path, query = _classified(req)
    assert synth.is_postprocess_action(action), f"{action} is not registered for postprocess"
    return synth.postprocess(
        action, resource, ENV, req.body, response_body, stores, "127.0.0.1:4266", 0.0, path, query, absent,
    )


def _pending(stores: SynthStores) -> list[dict]:
    records = [v for k, v in stores.dispatch.items(ENV).items() if k.startswith("pending:")]
    return sorted(records, key=lambda record: (record["key"], record["target_arn"]))


async def _configure(stores: SynthStores, sink, s3, configuration: dict) -> Response:
    return await _pure(stores, sink.call(
        lambda: s3.put_bucket_notification_configuration(Bucket=BUCKET, NotificationConfiguration=configuration)
    ))


# --- the wire shape, measured -------------------------------------------------


def test_boto3_sends_the_element_names_this_module_parses(sink, s3):
    """The measurement the whole module rests on, pinned as a test so nobody
    re-derives the wire from the API member names. Captured from a real
    boto3-signed `put_bucket_notification_configuration`."""
    captured = sink.call(
        lambda: s3.put_bucket_notification_configuration(Bucket=BUCKET, NotificationConfiguration=LAMBDA_CONFIG)
    )

    assert captured.method == "PUT"
    assert captured.url.endswith("/uploads?notification")
    assert captured.body == (
        b'<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<CloudFunctionConfiguration>"
        b"<Id>on-upload</Id>"
        b"<CloudFunction>arn:aws:lambda:us-east-1:000000000000:function:thumbnailer</CloudFunction>"
        b"<Event>s3:ObjectCreated:*</Event>"
        b"<Filter><S3Key>"
        b"<FilterRule><Name>prefix</Name><Value>incoming/</Value></FilterRule>"
        b"<FilterRule><Name>suffix</Name><Value>.jpg</Value></FilterRule>"
        b"</S3Key></Filter>"
        b"</CloudFunctionConfiguration>"
        b"</NotificationConfiguration>"
    ), captured.body


def test_boto3_sends_the_other_two_families_under_their_own_element_names(sink, s3):
    """`QueueConfigurations` -> `<QueueConfiguration><Queue>` and
    `TopicConfigurations` -> `<TopicConfiguration><Topic>`. The refusal below
    has to recognise these to name what it cannot deliver."""
    captured = sink.call(lambda: s3.put_bucket_notification_configuration(
        Bucket=BUCKET,
        NotificationConfiguration={
            "QueueConfigurations": [{"Id": "q", "QueueArn": QUEUE_ARN, "Events": ["s3:ObjectRemoved:*"]}],
            "TopicConfigurations": [{"Id": "t", "TopicArn": TOPIC_ARN, "Events": ["s3:ObjectCreated:*"]}],
        },
    ))

    assert b"<QueueConfiguration><Id>q</Id><Queue>" in captured.body, captured.body
    assert b"<TopicConfiguration><Id>t</Id><Topic>" in captured.body, captured.body


# --- the two PURE actions -----------------------------------------------------


async def test_the_notification_actions_are_never_forwarded(stores, sink, s3):
    """RustFS rejects every ARN form with `InvalidArgument` and persists the
    configuration anyway, so a forward means apply-fails/plan-clean/never-fires.
    A non-None `pure_answer` is what stops app.py ever reaching the backing."""
    put = sink.call(lambda: s3.put_bucket_notification_configuration(
        Bucket=BUCKET, NotificationConfiguration=LAMBDA_CONFIG))
    get = sink.call(lambda: s3.get_bucket_notification_configuration(Bucket=BUCKET))

    assert await _pure(stores, put) is not None
    assert await _pure(stores, get) is not None


async def test_a_put_round_trips_through_botocores_own_parser(stores, sink, s3):
    """The GET must parse back into exactly what the PUT sent -- id, arn,
    events and BOTH filter rules -- using the same parser terraform's SDK
    equivalent uses, not this module's own reading of the XML."""
    await _configure(stores, sink, s3, LAMBDA_CONFIG)
    response = await _pure(stores, sink.call(lambda: s3.get_bucket_notification_configuration(Bucket=BUCKET)))

    parsed = _parse("GetBucketNotificationConfiguration", response)
    assert parsed["LambdaFunctionConfigurations"] == [{
        "Id": "on-upload",
        "LambdaFunctionArn": LAMBDA_ARN,
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [
            {"Name": "prefix", "Value": "incoming/"},
            {"Name": "suffix", "Value": ".jpg"},
        ]}},
    }], parsed


async def test_a_never_configured_bucket_gets_the_empty_configuration(stores, sink, s3):
    """What makes a `tofu` refresh CLEAN. A bucket nobody configured must read
    as no configurations at all, not as an error and not as a phantom entry."""
    response = await _pure(stores, sink.call(lambda: s3.get_bucket_notification_configuration(Bucket=BUCKET)))

    assert bytes(response.body) == (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></NotificationConfiguration>'
    ), response.body
    parsed = _parse("GetBucketNotificationConfiguration", response)
    assert "LambdaFunctionConfigurations" not in parsed, parsed


async def test_an_empty_put_clears_the_configuration(stores, sink, s3):
    await _configure(stores, sink, s3, LAMBDA_CONFIG)
    assert s3notify.configurations(stores, ENV, BUCKET) != []

    await _configure(stores, sink, s3, {})

    assert s3notify.configurations(stores, ENV, BUCKET) == []


async def test_a_delete_clears_the_configuration(stores, sink, s3):
    """`DELETE /{bucket}?notification` classifies to the same
    `s3:PutBucketNotification` action as the PUT and carries no body, so the
    empty parse is the whole mechanism -- this module never needs the HTTP
    method. boto3 has no operation that sends it (the SDK models the clear as
    an empty PUT), so the request is named at the classify boundary instead."""
    await _configure(stores, sink, s3, LAMBDA_CONFIG)

    classified = classify("s3", "DELETE", f"/{BUCKET}", {"notification": ""}, {}, b"")
    assert classified == ("s3:PutBucketNotification", BUCKET), classified
    action, resource = classified
    assert await synth.pure_answer(action, resource, ENV, b"", stores, 0.0) is not None

    assert s3notify.configurations(stores, ENV, BUCKET) == []


async def test_a_malformed_body_is_an_error_not_a_silent_clear(stores):
    """An empty parse CLEARS the bucket. So a body that is not XML must fail
    loudly: parsed as "no configurations" it would silently disable every
    trigger on the bucket behind a 200."""
    response = await synth.pure_answer("s3:PutBucketNotification", BUCKET, ENV, b"not xml at all", stores, 0.0)

    assert response.status_code == 400
    parsed = _parse("PutBucketNotificationConfiguration", response)
    assert parsed["Error"]["Code"] == "MalformedXML", parsed


async def test_special_characters_survive_the_round_trip(stores, sink, s3):
    """Ids and filter values are user data and go into XML text nodes."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [{
        "Id": "a&b<c>",
        "LambdaFunctionArn": LAMBDA_ARN,
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "a&b/"}]}},
    }]})
    response = await _pure(stores, sink.call(lambda: s3.get_bucket_notification_configuration(Bucket=BUCKET)))

    parsed = _parse("GetBucketNotificationConfiguration", response)
    entry = parsed["LambdaFunctionConfigurations"][0]
    assert entry["Id"] == "a&b<c>", parsed
    assert entry["Filter"]["Key"]["FilterRules"] == [{"Name": "prefix", "Value": "a&b/"}], parsed


# --- THE REFUSAL --------------------------------------------------------------


@pytest.mark.parametrize(
    ("configuration", "cannot_deliver", "names_the_reason"),
    [
        ({"QueueConfigurations": [{"Id": "q", "QueueArn": QUEUE_ARN, "Events": ["s3:ObjectCreated:*"]}]},
         QUEUE_ARN, "an SQS queue"),
        ({"TopicConfigurations": [{"Id": "t", "TopicArn": TOPIC_ARN, "Events": ["s3:ObjectCreated:*"]}]},
         TOPIC_ARN, "an SNS topic"),
        ({"LambdaFunctionConfigurations": [{"Id": "x", "LambdaFunctionArn": QUEUE_ARN, "Events": ["s3:ObjectCreated:*"]}]},
         QUEUE_ARN, "not a Lambda function ARN"),
        # The case that makes the FAMILY check load-bearing rather than
        # redundant with the ARN check: a queue configuration carrying a lambda
        # ARN passes `startswith("arn:aws:lambda:")` and is still not something
        # odin can deliver -- the user asked for a queue. Without this the
        # family branch could be deleted and no test would notice, which is
        # exactly what mutation M3 measured before it existed.
        ({"QueueConfigurations": [{"Id": "q", "QueueArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]}]},
         LAMBDA_ARN, "an SQS queue"),
    ],
    ids=["sqs-target", "sns-target", "non-lambda-arn-in-a-lambda-configuration", "queue-target-carrying-a-lambda-arn"],
)
async def test_an_undeliverable_target_is_refused_and_never_stored(
    stores, sink, s3, configuration, cannot_deliver, names_the_reason,
):
    """A Lambda invoke is the only sink odin has. Storing an SQS/SNS target
    would turn today's honest `tofu apply` FAILURE into a silent one -- apply
    green, plan clean, nothing fires -- which is strictly worse. So it fails,
    loudly, naming what odin can and cannot deliver."""
    response = await _configure(stores, sink, s3, configuration)

    assert response.status_code == 400
    parsed = _parse("PutBucketNotificationConfiguration", response)
    assert parsed["Error"]["Code"] == "InvalidArgument", parsed
    assert cannot_deliver in parsed["Error"]["Message"], parsed
    assert "Lambda" in parsed["Error"]["Message"], parsed
    assert names_the_reason in parsed["Error"]["Message"], parsed
    assert s3notify.configurations(stores, ENV, BUCKET) == [], "a refused configuration must not be stored"


async def test_a_refusal_leaves_an_existing_configuration_untouched(stores, sink, s3):
    """Half-applying a rejected PUT would leave the bucket in a state neither
    the caller nor terraform asked for."""
    await _configure(stores, sink, s3, LAMBDA_CONFIG)
    before = s3notify.configurations(stores, ENV, BUCKET)

    await _configure(stores, sink, s3, {
        "LambdaFunctionConfigurations": LAMBDA_CONFIG["LambdaFunctionConfigurations"],
        "QueueConfigurations": [{"Id": "q", "QueueArn": QUEUE_ARN, "Events": ["s3:ObjectCreated:*"]}],
    })

    assert s3notify.configurations(stores, ENV, BUCKET) == before


async def test_the_refusal_parses_as_an_s3_error(stores, sink, s3):
    """The wire shape matters as much as the decision: `errors.synth_error` had
    no `s3` branch at all until this feature found it, so a refusal came out as
    an AWS-JSON body `RestXMLParser` cannot read and the caller saw a parse
    failure instead of the reason. (The end-to-end form -- a real boto3 client
    over a real socket raising a real `ClientError` -- is
    `test_a_real_boto3_client_raises_the_refusal` below.)"""
    response = await _configure(stores, sink, s3, {"QueueConfigurations": [
        {"Id": "q", "QueueArn": QUEUE_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})

    assert response.media_type == "application/xml", response.media_type
    parsed = _parse("PutBucketNotificationConfiguration", response)
    assert parsed["Error"]["Code"] == "InvalidArgument", parsed
    assert "S3 -> SQS" in parsed["Error"]["Message"], parsed


async def test_only_lambda_configurations_can_ever_be_stored(stores, sink, s3):
    """The ratchet `_configuration_xml`'s docstring names: it always renders
    `<CloudFunctionConfiguration>`, and that is TOTAL only because the refusal
    is the sole write path. If a non-lambda target ever gets stored, the GET
    starts lying about what kind of target it is."""
    for configuration in (
        {"QueueConfigurations": [{"Id": "q", "QueueArn": QUEUE_ARN, "Events": ["s3:ObjectCreated:*"]}]},
        {"TopicConfigurations": [{"Id": "t", "TopicArn": TOPIC_ARN, "Events": ["s3:ObjectCreated:*"]}]},
        {"LambdaFunctionConfigurations": [{"Id": "x", "LambdaFunctionArn": TOPIC_ARN, "Events": ["s3:ObjectCreated:*"]}]},
        LAMBDA_CONFIG,
    ):
        await _configure(stores, sink, s3, configuration)

    stored = s3notify.configurations(stores, ENV, BUCKET)
    assert stored, "the one deliverable configuration should have landed"
    assert all(entry["target_arn"].startswith(s3notify.LAMBDA_ARN_PREFIX) for entry in stored), stored


# --- the ENQUEUE hook ---------------------------------------------------------


async def test_a_matching_write_enqueues_exactly_one_pending_record(stores, sink, s3):
    """The whole handoff to `reconcile/dispatch.py`, asserted field by field --
    the dispatcher reads every one of them."""
    await _configure(stores, sink, s3, LAMBDA_CONFIG)

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="incoming/a.jpg", Body=b"hello")))

    records = _pending(stores)
    assert len(records) == 1, records
    assert records[0] == {
        "bucket": BUCKET,
        "key": "incoming/a.jpg",
        "event_name": "s3:ObjectCreated:Put",
        "target_arn": LAMBDA_ARN,
        "at": records[0]["at"],
        "size": 5,
        "etag": hashlib.md5(b"hello").hexdigest(),
    }


async def test_at_is_wall_clock_not_the_monotonic_now_the_handler_is_given(stores, sink, s3):
    """`now` is `time.monotonic()` (app.py) and this record is PERSISTED, so a
    monotonic `at` from before a restart is not comparable with one from after
    and the drain order silently inverts. The handler is handed `now=0.0` here;
    a wall-clock `at` is the proof it did not use it."""
    await _configure(stores, sink, s3, LAMBDA_CONFIG)
    before = time.time()
    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="incoming/a.jpg", Body=b"x")))

    at = _pending(stores)[0]["at"]
    assert before <= at <= time.time(), at


async def test_the_response_body_is_returned_unchanged(stores, sink, s3):
    await _configure(stores, sink, s3, LAMBDA_CONFIG)
    body = b"<whatever/>"

    assert _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="incoming/a.jpg", Body=b"x")), body) is body


async def test_a_bucket_with_no_configuration_enqueues_nothing(stores, sink, s3):
    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="incoming/a.jpg", Body=b"x")))

    assert _pending(stores) == []


@pytest.mark.parametrize(
    "key",
    ["outgoing/a.jpg", "a.jpg", "incoming-other/a.jpg"],
    ids=["wrong-prefix", "no-prefix", "prefix-is-not-a-path-boundary"],
)
async def test_the_prefix_filter_excludes_a_key_that_does_not_match(stores, sink, s3, key):
    """THE mutation target. A prefix filter that silently matches everything is
    the decorative-trigger bug one layer down: the config renders, the apply is
    green, and the function fires for objects the user deliberately excluded."""
    await _configure(stores, sink, s3, LAMBDA_CONFIG)

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")))

    assert _pending(stores) == [], f"{key} must not match prefix 'incoming/'"


@pytest.mark.parametrize("key", ["incoming/a.png", "incoming/a.jpg.bak"], ids=["wrong-suffix", "suffix-not-at-end"])
async def test_the_suffix_filter_excludes_a_key_that_does_not_match(stores, sink, s3, key):
    await _configure(stores, sink, s3, LAMBDA_CONFIG)

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")))

    assert _pending(stores) == [], f"{key} must not match suffix '.jpg'"


async def test_a_configuration_with_no_filter_matches_every_key(stores, sink, s3):
    """An absent `Filter` is "no filter", which the empty-string prefix/suffix
    gives for free -- every string starts and ends with `""`."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "all", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="anything/at/all.txt", Body=b"x")))

    assert [record["key"] for record in _pending(stores)] == ["anything/at/all.txt"]


async def test_a_wildcard_event_matches_the_concrete_one(stores, sink, s3):
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "created", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="a.txt", Body=b"x")))

    assert [record["event_name"] for record in _pending(stores)] == ["s3:ObjectCreated:Put"]


async def test_an_exact_event_name_does_not_match_a_different_concrete_event(stores, sink, s3):
    """`s3:ObjectCreated:Post` is a real AWS event name and a plain PUT is not
    it. Matching on the family alone would fire for writes the user excluded by
    naming one specific event."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "posts-only", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:Post"]},
    ]})

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="a.txt", Body=b"x")))

    assert _pending(stores) == []


async def test_a_created_configuration_does_not_fire_for_a_delete(stores, sink, s3):
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "created", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})

    _post(stores, sink.call(lambda: s3.delete_object(Bucket=BUCKET, Key="a.txt")))

    assert _pending(stores) == []


async def test_a_delete_enqueues_object_removed(stores, sink, s3):
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})

    _post(stores, sink.call(lambda: s3.delete_object(Bucket=BUCKET, Key="incoming/a.jpg")))

    records = _pending(stores)
    assert len(records) == 1, records
    assert records[0]["event_name"] == "s3:ObjectRemoved:Delete"
    assert records[0]["key"] == "incoming/a.jpg"
    assert records[0]["size"] == 0 and records[0]["etag"] == ""


async def test_two_matching_configurations_each_get_their_own_record(stores, sink, s3):
    """At-least-once, per target. One write fanning out to two functions must
    not collapse into one pending record."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "a", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
        {"Id": "b", "LambdaFunctionArn": OTHER_LAMBDA_ARN, "Events": ["s3:ObjectCreated:Put"]},
    ]})

    _post(stores, sink.call(lambda: s3.put_object(Bucket=BUCKET, Key="a.txt", Body=b"x")))

    assert sorted(record["target_arn"] for record in _pending(stores)) == sorted([LAMBDA_ARN, OTHER_LAMBDA_ARN])


# --- the four wire shapes that all classify to s3:PutObject ------------------


async def test_creating_a_multipart_upload_enqueues_nothing(stores, sink, s3):
    """`POST /{b}/{k}?uploads` classifies to `s3:PutObject` and lands NO object.
    Firing here announces an object that does not exist yet."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "all", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})
    captured = sink.call(lambda: s3.create_multipart_upload(Bucket=BUCKET, Key="big.bin"))
    assert _classified(captured)[0] == "s3:PutObject", "the premise of this test"

    _post(stores, captured, b"<InitiateMultipartUploadResult/>")

    assert _pending(stores) == []


async def test_uploading_a_part_enqueues_nothing(stores, sink, s3):
    """`PUT /{b}/{k}?partNumber=N&uploadId=` classifies to `s3:PutObject` too.
    Without the query string this hook would fire once PER PART."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "all", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})
    captured = sink.call(lambda: s3.upload_part(Bucket=BUCKET, Key="big.bin", PartNumber=1, UploadId="u1", Body=b"x" * 8))
    assert _classified(captured)[0] == "s3:PutObject", "the premise of this test"

    _post(stores, captured)

    assert _pending(stores) == []


async def test_completing_a_multipart_upload_enqueues_the_multipart_event(stores, sink, s3):
    """This one DOES land an object, and real S3 names it
    `s3:ObjectCreated:CompleteMultipartUpload` -- so a configuration written as
    `s3:ObjectCreated:Put` must not fire for it, and the wildcard must."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "wildcard", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
        {"Id": "puts-only", "LambdaFunctionArn": OTHER_LAMBDA_ARN, "Events": ["s3:ObjectCreated:Put"]},
    ]})
    captured = sink.call(lambda: s3.complete_multipart_upload(
        Bucket=BUCKET, Key="big.bin", UploadId="u1",
        MultipartUpload={"Parts": [{"ETag": '"abc"', "PartNumber": 1}]},
    ))
    assert _classified(captured)[0] == "s3:PutObject", "the premise of this test"

    _post(stores, captured, b"<CompleteMultipartUploadResult/>")

    records = _pending(stores)
    assert [record["target_arn"] for record in records] == [LAMBDA_ARN], records
    assert records[0]["event_name"] == "s3:ObjectCreated:CompleteMultipartUpload"
    assert records[0]["key"] == "big.bin"


async def test_a_multi_object_delete_enqueues_per_key_the_backing_reported_deleted(stores, sink, s3):
    """`POST /{b}?delete` carries NO key in the path -- the keys are in the
    body, and which ones SUCCEEDED is only in the response.

    The response here is `MEASURED_DELETE_RESULT`, captured from a real
    `rustfs/rustfs:latest`, so the parse is proven against the bytes odin will
    actually receive rather than against a reading of the S3 reference. It also
    carries the divergence the probe found, asserted below."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": "incoming/a.jpg"}, {"Key": "does/not/exist.jpg"}]},
    ))
    assert _classified(captured)[0] == "s3:DeleteObject", "the premise of this test"

    _post(stores, captured, MEASURED_DELETE_RESULT)

    assert [record["key"] for record in _pending(stores)] == ["does/not/exist.jpg", "incoming/a.jpg"]


async def test_a_key_the_probe_found_ABSENT_fires_nothing(stores, sink, s3):
    """The over-fire this file used to pin as a documented divergence.

    RustFS reported BOTH keys as `<Deleted>` with zero `<Error>` entries --
    correct S3 behaviour, since DeleteObjects is idempotent, and the reason the
    RESPONSE can never separate them. What separates them is `app.py`'s
    pre-forward HEAD probe, whose answer arrives here as `absent`. Real AWS
    fires nothing for a key that was not there, and now neither does odin.

    The expected list is spelled out in full rather than derived from the
    input: a test that filtered the same tuple the source filters would pass
    for a `_writes` that had stopped filtering at all."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": "incoming/a.jpg"}, {"Key": "does/not/exist.jpg"}]},
    ))

    _post(stores, captured, MEASURED_DELETE_RESULT, absent=frozenset({"does/not/exist.jpg"}))

    assert [record["key"] for record in _pending(stores)] == ["incoming/a.jpg"]


async def test_a_single_object_delete_of_an_absent_key_fires_nothing(stores, sink, s3):
    """The same fix on the other delete shape. `DELETE /{b}/{k}` answers 204
    whether or not the key was there (MEASURED against rustfs/rustfs:latest,
    2026-08-03), so this one needs the probe just as much -- and it used to be
    covered by nothing at all, since only the multi-object shape had a test."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})

    _post(
        stores, sink.call(lambda: s3.delete_object(Bucket=BUCKET, Key="incoming/a.jpg")),
        absent=frozenset({"incoming/a.jpg"}),
    )

    assert _pending(stores) == []


async def test_an_EMPTY_absent_set_suppresses_nothing(stores, sink, s3):
    """The fail-open polarity, pinned. `_absent_keys` returns only keys the
    backing definitely 404'd, so a probe that timed out, was refused, or never
    ran hands back an empty set -- and an empty set must leave every key
    firing. Inverting the sense (an `existing` set that must CONTAIN a key for
    it to fire) would turn every one of those failures into silence."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": "incoming/a.jpg"}, {"Key": "does/not/exist.jpg"}]},
    ))

    _post(stores, captured, MEASURED_DELETE_RESULT, absent=frozenset())

    assert [record["key"] for record in _pending(stores)] == ["does/not/exist.jpg", "incoming/a.jpg"]


# --- what the pre-forward probe asks about, and what it costs ----------------


async def test_a_bucket_with_no_notification_is_never_probed(stores, sink, s3):
    """The whole cost argument. `probe_keys` returns nothing for a bucket that
    has no configuration, so `app.py` issues no HEAD at all -- which is every
    bucket until someone draws a notification onto one."""
    captured = sink.call(lambda: s3.delete_object(Bucket=BUCKET, Key="incoming/a.jpg"))
    _action, resource, path, query = _classified(captured)

    assert s3notify.probe_keys(stores, ENV, resource, path, query, captured.body) == ()


async def test_only_a_REMOVAL_configuration_makes_a_delete_worth_probing(stores, sink, s3):
    """A bucket whose only configuration is `ObjectCreated` fires nothing on a
    delete, so probing its keys would buy an answer nobody reads."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "created", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectCreated:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_object(Bucket=BUCKET, Key="incoming/a.jpg"))
    _action, resource, path, query = _classified(captured)

    assert s3notify.probe_keys(stores, ENV, resource, path, query, captured.body) == ()


async def test_the_probe_is_scoped_to_the_keys_the_filter_would_fire_for(stores, sink, s3):
    """A `prefix`/`suffix` filter narrows what is probed, not just what fires.
    Both expected tuples are literals: deriving them from `matches` would be
    the source grading its own homework."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "jpgs", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"], "Filter": {"Key": {
            "FilterRules": [{"Name": "prefix", "Value": "incoming/"}, {"Name": "suffix", "Value": ".jpg"}],
        }}},
    ]})
    captured = sink.call(lambda: s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [
        {"Key": "incoming/a.jpg"}, {"Key": "incoming/b.txt"}, {"Key": "elsewhere/c.jpg"},
    ]}))
    _action, resource, path, query = _classified(captured)

    assert s3notify.probe_keys(stores, ENV, resource, path, query, captured.body) == ("incoming/a.jpg",)


async def test_the_probe_reads_the_multi_object_keys_out_of_the_REQUEST(stores, sink, s3):
    """`POST /{b}?delete` carries its keys in the body and none in the path,
    and the probe runs BEFORE the response exists -- so this is the one list it
    can be built from. Spelled out in full, in wire order."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [
        {"Key": "incoming/a.jpg"}, {"Key": "does/not/exist.jpg"},
    ]}))
    _action, resource, path, query = _classified(captured)

    assert s3notify.probe_keys(stores, ENV, resource, path, query, captured.body) == (
        "incoming/a.jpg", "does/not/exist.jpg",
    )


async def test_an_unparseable_delete_body_probes_nothing_rather_than_raising(stores, sink, s3):
    """Fail-open at the other end of the same path: a body odin cannot read
    yields no probe keys, an empty `absent`, and therefore today's behaviour --
    not a 500 on a delete that would otherwise have succeeded."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": "a.txt"}]}))
    _action, resource, path, query = _classified(captured)

    assert s3notify.probe_keys(stores, ENV, resource, path, query, b"} not xml {") == ()


async def test_a_key_the_backing_reported_as_FAILED_does_not_enqueue(stores, sink, s3):
    """The `<Error>` skip in `_deleted_keys`, and the one body in this file odin
    has NOT measured: the RustFS probe produced zero `<Error>` entries, so this
    shape comes from the S3 API reference, not from the wire. Said out loud
    because a test that fabricates an upstream signal proves the parser, not the
    integration -- but the branch is real (a genuine AccessDenied or object-lock
    retention does come back this way) and deleting it would make a FAILED
    delete announce an object that is still standing."""
    await _configure(stores, sink, s3, {"LambdaFunctionConfigurations": [
        {"Id": "removed", "LambdaFunctionArn": LAMBDA_ARN, "Events": ["s3:ObjectRemoved:*"]},
    ]})
    captured = sink.call(lambda: s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": "a.txt"}, {"Key": "locked.txt"}]},
    ))
    spec_shaped_answer = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<Deleted><Key>a.txt</Key></Deleted>"
        b"<Error><Key>locked.txt</Key><Code>AccessDenied</Code></Error>"
        b"</DeleteResult>"
    )

    _post(stores, captured, spec_shaped_answer)

    assert [record["key"] for record in _pending(stores)] == ["a.txt"], "the failed key must not fire"


# --- through the REAL gateway app --------------------------------------------


class _Backing:
    """A stand-in for RustFS that answers 200 with a fixed body -- enough for
    `catch_all` to reach the postprocess hook, which is what this section
    measures. No container."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __call__(self, scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/xml"), (b"content-length", str(len(self._body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": self._body})


async def _swallow(*_args, **_kwargs) -> None:
    return None


def _gateway(tmp_path: Path, stores: SynthStores, backing_body: bytes):
    keystore = KeyStore(tmp_path / "keys")
    access_key, secret_key = keystore.issue(ENV, "api")
    state = GatewayState()
    state.update(ENV, {"api": [Statement(actions=("*",), resources=("*",))]}, {"s3": 9000})
    app = create_gateway_app(
        state, keystore, stores, _swallow,
        forward_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=_Backing(backing_body))),
    )
    return app, (access_key, secret_key)


@contextmanager
def _live(tmp_path: Path, stores: SynthStores, backing_body: bytes = b""):
    """A real boto3 client against the real gateway on a real bound port.

    Sync, and on `serve_in_thread` -- the TEST-ONLY helper CLAUDE.md sanctions
    for exactly this: boto3 blocks, so serving on the caller's loop would
    deadlock against the calls that block it. `TestClient` is not a substitute
    here, because boto3 dials a socket rather than an ASGI transport."""
    app, (access_key, secret_key) = _gateway(tmp_path, stores, backing_body)
    server, thread, port = serve_in_thread(app, host="127.0.0.1", port=0)
    try:
        yield boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
        )
    finally:
        stop_in_thread(server, thread)


def test_a_real_object_write_through_the_real_gateway_enqueues(tmp_path):
    """VERIFY THROUGH THE PRODUCT'S OWN PATH. Everything above calls
    `synth.postprocess` directly and would keep passing if `app.py` never
    passed `path`/`query` at all -- the hook would then see an empty key, match
    nothing, and fail SILENTLY. This drives a real boto3 client through the
    real `create_gateway_app`, so the wiring itself is what is measured."""
    stores = SynthStores(tmp_path / "synth")

    with _live(tmp_path, stores) as s3:
        s3.put_bucket_notification_configuration(Bucket=BUCKET, NotificationConfiguration=LAMBDA_CONFIG)
        s3.put_object(Bucket=BUCKET, Key="incoming/photo.jpg", Body=b"bytes")
        s3.put_object(Bucket=BUCKET, Key="elsewhere/photo.jpg", Body=b"bytes")

    records = [v for k, v in stores.dispatch.items(ENV).items() if k.startswith("pending:")]
    assert len(records) == 1, records
    assert records[0]["key"] == "incoming/photo.jpg"
    assert records[0]["target_arn"] == LAMBDA_ARN


def test_a_real_boto3_client_raises_the_refusal(tmp_path):
    """The end-to-end form of the refusal: over a real socket, does botocore
    turn it into a `ClientError` the caller can act on rather than a parse
    failure? `tofu apply` is a Go SDK doing the same thing."""
    stores = SynthStores(tmp_path / "synth")

    with _live(tmp_path, stores) as s3, pytest.raises(ClientError) as caught:
        s3.put_bucket_notification_configuration(
            Bucket=BUCKET,
            NotificationConfiguration={"QueueConfigurations": [
                {"Id": "q", "QueueArn": QUEUE_ARN, "Events": ["s3:ObjectCreated:*"]},
            ]},
        )

    error = caught.value.response["Error"]
    assert error["Code"] == "InvalidArgument", error
    assert "S3 -> SQS" in error["Message"], error


def test_a_stored_pending_record_survives_a_reload(tmp_path):
    """`records.py` validates on every store READ, so a shape it rejects makes
    the whole sidecar unreadable for the dispatcher. Proven by reloading from
    disk through a fresh `SynthStores`, not by re-reading the cache."""
    stores = SynthStores(tmp_path / "synth")

    with _live(tmp_path, stores) as s3:
        s3.put_bucket_notification_configuration(Bucket=BUCKET, NotificationConfiguration=LAMBDA_CONFIG)
        s3.put_object(Bucket=BUCKET, Key="incoming/photo.jpg", Body=b"bytes")

    reloaded = SynthStores(tmp_path / "synth")
    assert [v["key"] for v in reloaded.dispatch.items(ENV).values()] == ["incoming/photo.jpg"]
    assert reloaded.s3notify.get(ENV, f"notify:{BUCKET}")["configurations"][0]["target_arn"] == LAMBDA_ARN
