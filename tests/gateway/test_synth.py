"""S1 -- gateway/synth.py: the synthesized control-plane (research
build-order items 2-5). Every response synth builds is round-tripped
through botocore's OWN protocol parser against the real service model
(`_parse`, below) -- proof the bytes are wire-correct, not just
string-matched -- and every request synth consumes is a REAL boto3-signed
capture via tests/gateway/harness.py's CaptureSink (same pattern G2/G3
established), never hand-built JSON/XML.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import botocore.session
import pytest
from botocore.parsers import create_parser
from starlette.responses import Response

from odin.aws.backings import ACCOUNT
from odin.gateway import synth
from odin.gateway.keys import Principal
from odin.gateway.stores import SynthStores

_SESSION = botocore.session.get_session()


def _parse(service: str, operation: str, response: Response, *, error: bool = False):
    """Parse a synth-built Response through botocore's real parser for
    `service.operation`, proving the wire shape actually round-trips
    through the SAME code boto3 uses to build a `ClientError`/return value."""
    model = _SESSION.get_service_model(service)
    operation_model = model.operation_model(operation)
    parser = create_parser(model.protocol)
    raw = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response.body,
    }
    parsed = parser.parse(raw, operation_model.output_shape)
    if error:
        assert response.status_code >= 300
    return parsed


@pytest.fixture
def stores(tmp_path: Path) -> SynthStores:
    return SynthStores(tmp_path)


# --- get_caller_identity: ONE account id, everywhere -------------------------
#
# v0.7.1, field test U6. There used to be a second, per-env account id here
# (`account_for_env`: sha256(env) % 10^12, e.g. `561031708110` for a env named
# `wa`) used for STS's `Account` field alone, while every ARN odin builds --
# QueueArn, TopicArn, secret ARN, log-group ARN, all of them -- used the fixed
# `backings.ACCOUNT`. A workload that did the ordinary thing (ask STS who it
# is, build an ARN from the answer) built an ARN odin would never match. The
# tests below pin the invariant that replaced it: there is exactly one account
# id in the system, and STS reports THAT one.


def test_get_caller_identity_parses_and_reports_the_one_account(sts):
    principal = Principal(env="staging", node_id="worker")
    response = synth.get_caller_identity("staging", principal)

    parsed = _parse("sts", "GetCallerIdentity", response)
    assert parsed["Account"] == ACCOUNT
    assert parsed["UserId"] == "worker"
    assert parsed["Arn"] == f"arn:aws:iam::{ACCOUNT}:user/worker"


def test_get_caller_identity_account_does_not_vary_by_env():
    accounts = {
        _parse("sts", "GetCallerIdentity", synth.get_caller_identity(env, Principal(env=env, node_id="api")))["Account"]
        for env in ("default", "staging", "a", "b", "wa")
    }
    assert accounts == {ACCOUNT}


def test_get_caller_identity_account_matches_the_account_inside_a_returned_arn(sink, sqs, stores):
    """The whole point of unifying the two: a client that builds an ARN out of
    its OWN caller identity -- a very common pattern -- must build one odin
    recognises. Asserted end-to-end in a NON-default env (the case that used
    to be broken), against an ARN synth really put on the wire rather than
    against the constant."""
    env = "staging"
    identity = _parse(
        "sts", "GetCallerIdentity",
        synth.get_caller_identity(env, Principal(env=env, node_id="worker")),
    )

    create_req = sink.call(lambda: sqs.create_queue(QueueName="jobs"))
    synth.postprocess(
        "sqs:CreateQueue", "jobs", env, create_req.body,
        b'{"QueueUrl": "http://us-east-1.goaws.com:4100/000000000000/jobs"}',
        stores, "127.0.0.1:4266", 0.0,
    )
    get_req = sink.call(
        lambda: sqs.get_queue_attributes(
            QueueUrl=f"{sink.endpoint}/000000000000/jobs", AttributeNames=["QueueArn"]
        )
    )
    queue_arn = _parse(
        "sqs", "GetQueueAttributes",
        synth.pure_answer("sqs:GetQueueAttributes", "jobs", env, get_req.body, stores, 0.0),
    )["Attributes"]["QueueArn"]

    # arn:aws:sqs:{region}:{account}:{name} -- field 4 is the account.
    assert queue_arn.split(":")[4] == identity["Account"]
    assert identity["Arn"].split(":")[4] == identity["Account"]


# --- SQS: tag CRUD -----------------------------------------------------------


def test_sqs_list_queue_tags_empty_by_default(sink, sqs, stores):
    req = sink.call(lambda: sqs.list_queue_tags(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    response = synth.pure_answer("sqs:ListQueueTags", "jobs", "default", req.body, stores, 0.0)
    parsed = _parse("sqs", "ListQueueTags", response)
    assert parsed["Tags"] == {}


def test_sqs_tag_queue_then_list_round_trips(sink, sqs, stores):
    tag_req = sink.call(
        lambda: sqs.tag_queue(QueueUrl=f"{sink.endpoint}/000000000000/jobs", Tags={"env": "prod"})
    )
    synth.pure_answer("sqs:TagQueue", "jobs", "default", tag_req.body, stores, 0.0)

    list_req = sink.call(lambda: sqs.list_queue_tags(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    response = synth.pure_answer("sqs:ListQueueTags", "jobs", "default", list_req.body, stores, 0.0)
    parsed = _parse("sqs", "ListQueueTags", response)
    assert parsed["Tags"] == {"env": "prod"}


def test_sqs_untag_queue_removes_key(sink, sqs, stores):
    stores.tags.set("default", "sqs:jobs", {"env": "prod", "team": "infra"})
    untag_req = sink.call(
        lambda: sqs.untag_queue(QueueUrl=f"{sink.endpoint}/000000000000/jobs", TagKeys=["env"])
    )
    synth.pure_answer("sqs:UntagQueue", "jobs", "default", untag_req.body, stores, 0.0)
    assert stores.tags.get("default", "sqs:jobs") == {"team": "infra"}


def test_sqs_tags_are_per_env(sink, sqs, stores):
    tag_req = sink.call(
        lambda: sqs.tag_queue(QueueUrl=f"{sink.endpoint}/000000000000/jobs", Tags={"env": "prod"})
    )
    synth.pure_answer("sqs:TagQueue", "jobs", "a", tag_req.body, stores, 0.0)

    list_req = sink.call(lambda: sqs.list_queue_tags(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    response = synth.pure_answer("sqs:ListQueueTags", "jobs", "b", list_req.body, stores, 0.0)
    assert _parse("sqs", "ListQueueTags", response)["Tags"] == {}


# --- SQS: GetQueueAttributes (echo + delete-confirmation shim) --------------


def test_sqs_get_queue_attributes_echoes_created_attributes(sink, sqs, stores):
    create_req = sink.call(
        lambda: sqs.create_queue(QueueName="jobs", Attributes={"DelaySeconds": "5"}, tags={"env": "prod"})
    )
    fake_goaws_response = b'{"QueueUrl": "http://us-east-1.goaws.com:4100/000000000000/jobs"}'
    synth.postprocess("sqs:CreateQueue", "jobs", "default", create_req.body, fake_goaws_response, stores, "127.0.0.1:4266", 0.0)

    get_req = sink.call(
        lambda: sqs.get_queue_attributes(QueueUrl=f"{sink.endpoint}/000000000000/jobs", AttributeNames=["All"])
    )
    response = synth.pure_answer("sqs:GetQueueAttributes", "jobs", "default", get_req.body, stores, 0.0)
    parsed = _parse("sqs", "GetQueueAttributes", response)

    attrs = parsed["Attributes"]
    assert attrs["DelaySeconds"] == "5"  # echoed from what CreateQueue set
    assert attrs["QueueArn"] == "arn:aws:sqs:us-east-1:000000000000:jobs"
    assert attrs["SqsManagedSseEnabled"] == "false"
    assert attrs["Policy"] == ""


def test_sqs_get_queue_attributes_filters_to_requested_names(sink, sqs, stores):
    stores.sqs_queues.set("default", "jobs", {
        "attributes": {"QueueArn": "arn:aws:sqs:us-east-1:000000000000:jobs", "DelaySeconds": "5", "Policy": ""},
        "deleted_at": None,
    })
    get_req = sink.call(
        lambda: sqs.get_queue_attributes(
            QueueUrl=f"{sink.endpoint}/000000000000/jobs", AttributeNames=["QueueArn"]
        )
    )
    response = synth.pure_answer("sqs:GetQueueAttributes", "jobs", "default", get_req.body, stores, 0.0)
    parsed = _parse("sqs", "GetQueueAttributes", response)
    assert parsed["Attributes"] == {"QueueArn": "arn:aws:sqs:us-east-1:000000000000:jobs"}


def test_sqs_get_queue_attributes_still_served_within_delete_grace_window(sink, sqs, stores):
    stores.sqs_queues.set("default", "jobs", {"attributes": {"QueueArn": "x"}, "deleted_at": 100.0})
    get_req = sink.call(
        lambda: sqs.get_queue_attributes(QueueUrl=f"{sink.endpoint}/000000000000/jobs", AttributeNames=["All"])
    )
    # now is just past delete, well inside QUEUE_DELETE_GRACE_SECONDS
    response = synth.pure_answer("sqs:GetQueueAttributes", "jobs", "default", get_req.body, stores, 100.1)
    parsed = _parse("sqs", "GetQueueAttributes", response)
    assert parsed["Attributes"] == {"QueueArn": "x"}


def test_sqs_get_queue_attributes_returns_queue_does_not_exist_past_grace_window(sink, sqs, stores):
    stores.sqs_queues.set("default", "jobs", {"attributes": {"QueueArn": "x"}, "deleted_at": 100.0})
    get_req = sink.call(
        lambda: sqs.get_queue_attributes(QueueUrl=f"{sink.endpoint}/000000000000/jobs", AttributeNames=["All"])
    )
    now = 100.0 + synth.QUEUE_DELETE_GRACE_SECONDS + 1.0
    response = synth.pure_answer("sqs:GetQueueAttributes", "jobs", "default", get_req.body, stores, now)
    assert response.status_code == 400
    parsed = _parse("sqs", "GetQueueAttributes", response, error=True)
    # The REAL wire code (S2, verified against terraform-provider-aws's own
    # source) -- NOT botocore's friendlier shape name "QueueDoesNotExist",
    # which the Go-SDK-based `tofu destroy` delete-waiter does not match.
    assert parsed["Error"]["Code"] == "AWS.SimpleQueueService.NonExistentQueue"


def test_sqs_delete_queue_then_recreate_clears_deleted_marker(sink, sqs, stores):
    create_req = sink.call(lambda: sqs.create_queue(QueueName="jobs"))
    fake_response = b'{"QueueUrl": "http://us-east-1.goaws.com:4100/000000000000/jobs"}'
    synth.postprocess("sqs:CreateQueue", "jobs", "default", create_req.body, fake_response, stores, "127.0.0.1:4266", 0.0)

    delete_req = sink.call(lambda: sqs.delete_queue(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    synth.postprocess("sqs:DeleteQueue", "jobs", "default", delete_req.body, b"{}", stores, "127.0.0.1:4266", 10.0)
    assert stores.sqs_queues.get("default", "jobs")["deleted_at"] == 10.0

    recreate_req = sink.call(lambda: sqs.create_queue(QueueName="jobs"))
    synth.postprocess("sqs:CreateQueue", "jobs", "default", recreate_req.body, fake_response, stores, "127.0.0.1:4266", 20.0)
    assert stores.sqs_queues.get("default", "jobs")["deleted_at"] is None


# --- SQS: CreateQueue host rewrite -------------------------------------------


def test_create_queue_rewrites_goaws_host_to_gateway_host(sink, sqs, stores):
    create_req = sink.call(lambda: sqs.create_queue(QueueName="jobs"))
    fake_goaws_response = b'{"QueueUrl": "http://us-east-1.goaws.com:4100/000000000000/jobs"}'

    rewritten = synth.postprocess(
        "sqs:CreateQueue", "jobs", "default", create_req.body, fake_goaws_response, stores, "127.0.0.1:4266", 0.0
    )

    payload = json.loads(rewritten)
    parts = urlsplit(payload["QueueUrl"])
    assert parts.netloc == "127.0.0.1:4266"
    assert parts.path == "/000000000000/jobs"  # path/account/name untouched


def test_create_queue_response_still_parses_after_rewrite(sink, sqs, stores):
    create_req = sink.call(lambda: sqs.create_queue(QueueName="jobs"))
    fake_goaws_response = b'{"QueueUrl": "http://us-east-1.goaws.com:4100/000000000000/jobs"}'
    rewritten = synth.postprocess(
        "sqs:CreateQueue", "jobs", "default", create_req.body, fake_goaws_response, stores, "127.0.0.1:4266", 0.0
    )
    parsed = _parse("sqs", "CreateQueue", Response(rewritten, media_type="application/x-amz-json-1.0"))
    assert parsed["QueueUrl"] == "http://127.0.0.1:4266/000000000000/jobs"


# --- SNS: topic attributes ----------------------------------------------------


def test_sns_get_topic_attributes_includes_defaults(sink, sns, stores):
    req = sink.call(lambda: sns.get_topic_attributes(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts"))
    response = synth.pure_answer("sns:GetTopicAttributes", "alerts", "default", req.body, stores, 0.0)
    parsed = _parse("sns", "GetTopicAttributes", response)
    attrs = parsed["Attributes"]
    assert attrs["TopicArn"] == "arn:aws:sns:us-east-1:000000000000:alerts"
    assert attrs["DisplayName"] == ""
    assert attrs["SubscriptionsConfirmed"] == "0"


def test_sns_set_topic_attributes_then_get_reflects_change(sink, sns, stores):
    set_req = sink.call(
        lambda: sns.set_topic_attributes(
            TopicArn="arn:aws:sns:us-east-1:000000000000:alerts",
            AttributeName="DisplayName",
            AttributeValue="Alerts!",
        )
    )
    synth.pure_answer("sns:SetTopicAttributes", "alerts", "default", set_req.body, stores, 0.0)

    get_req = sink.call(lambda: sns.get_topic_attributes(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts"))
    response = synth.pure_answer("sns:GetTopicAttributes", "alerts", "default", get_req.body, stores, 0.0)
    parsed = _parse("sns", "GetTopicAttributes", response)
    assert parsed["Attributes"]["DisplayName"] == "Alerts!"


def test_sns_create_topic_seeds_attributes_and_tags(sink, sns, stores):
    create_req = sink.call(
        lambda: sns.create_topic(
            Name="alerts", Attributes={"DisplayName": "Alerts"}, Tags=[{"Key": "env", "Value": "prod"}]
        )
    )
    fake_response = (
        b'<CreateTopicResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/">'
        b"<CreateTopicResult><TopicArn>arn:aws:sns:us-east-1:000000000000:alerts</TopicArn></CreateTopicResult>"
        b"<ResponseMetadata><RequestId>r</RequestId></ResponseMetadata></CreateTopicResponse>"
    )
    unchanged = synth.postprocess("sns:CreateTopic", "alerts", "default", create_req.body, fake_response, stores, "127.0.0.1:4266", 0.0)
    assert unchanged == fake_response  # CreateTopic doesn't need a host rewrite

    assert stores.sns_topics.get("default", "alerts") == {"DisplayName": "Alerts"}
    assert stores.tags.get("default", "sns:alerts") == {"env": "prod"}


# --- SNS: tag CRUD -------------------------------------------------------------


def test_sns_list_tags_for_resource_empty_by_default(sink, sns, stores):
    req = sink.call(lambda: sns.list_tags_for_resource(ResourceArn="arn:aws:sns:us-east-1:000000000000:alerts"))
    response = synth.pure_answer("sns:ListTagsForResource", "alerts", "default", req.body, stores, 0.0)
    parsed = _parse("sns", "ListTagsForResource", response)
    assert parsed["Tags"] == []


def test_sns_tag_resource_then_list_round_trips(sink, sns, stores):
    tag_req = sink.call(
        lambda: sns.tag_resource(
            ResourceArn="arn:aws:sns:us-east-1:000000000000:alerts", Tags=[{"Key": "env", "Value": "prod"}]
        )
    )
    synth.pure_answer("sns:TagResource", "alerts", "default", tag_req.body, stores, 0.0)

    list_req = sink.call(lambda: sns.list_tags_for_resource(ResourceArn="arn:aws:sns:us-east-1:000000000000:alerts"))
    response = synth.pure_answer("sns:ListTagsForResource", "alerts", "default", list_req.body, stores, 0.0)
    parsed = _parse("sns", "ListTagsForResource", response)
    assert parsed["Tags"] == [{"Key": "env", "Value": "prod"}]


def test_sns_untag_resource_removes_key(sink, sns, stores):
    stores.tags.set("default", "sns:alerts", {"env": "prod", "team": "infra"})
    untag_req = sink.call(
        lambda: sns.untag_resource(ResourceArn="arn:aws:sns:us-east-1:000000000000:alerts", TagKeys=["env"])
    )
    synth.pure_answer("sns:UntagResource", "alerts", "default", untag_req.body, stores, 0.0)
    assert stores.tags.get("default", "sns:alerts") == {"team": "infra"}


# --- SNS: subscription delete-confirmation fidelity --------------------------


def test_sns_get_subscription_attributes_live_falls_through(sink, sns, stores):
    req = sink.call(
        lambda: sns.get_subscription_attributes(
            SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-1"
        )
    )
    response = synth.pure_answer("sns:GetSubscriptionAttributes", "alerts", "default", req.body, stores, 0.0)
    assert response is None  # not synth-owned for a live subscription -- caller forwards normally


def test_sns_unsubscribe_then_get_subscription_attributes_returns_not_found(sink, sns, stores):
    unsub_req = sink.call(
        lambda: sns.unsubscribe(SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-1")
    )
    synth.postprocess("sns:Unsubscribe", "alerts", "default", unsub_req.body, b"", stores, "127.0.0.1:4266", 5.0)

    get_req = sink.call(
        lambda: sns.get_subscription_attributes(
            SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-1"
        )
    )
    response = synth.pure_answer("sns:GetSubscriptionAttributes", "alerts", "default", get_req.body, stores, 6.0)
    assert response is not None
    assert response.status_code == 404
    parsed = _parse("sns", "GetSubscriptionAttributes", response, error=True)
    assert parsed["Error"]["Code"] == "NotFound"


def test_sns_unsubscribe_marker_is_keyed_by_subscription_not_topic(sink, sns, stores):
    """Two subscriptions to the same topic must not share a delete marker."""
    unsub_req = sink.call(
        lambda: sns.unsubscribe(SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-1")
    )
    synth.postprocess("sns:Unsubscribe", "alerts", "default", unsub_req.body, b"", stores, "127.0.0.1:4266", 0.0)

    other_req = sink.call(
        lambda: sns.get_subscription_attributes(
            SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-2"
        )
    )
    response = synth.pure_answer("sns:GetSubscriptionAttributes", "alerts", "default", other_req.body, stores, 1.0)
    assert response is None  # sub-uuid-2 was never unsubscribed


# --- DynamoDB: tag CRUD --------------------------------------------------------


def test_dynamodb_list_tags_of_resource_empty_by_default(sink, dynamodb, stores):
    req = sink.call(
        lambda: dynamodb.list_tags_of_resource(ResourceArn="arn:aws:dynamodb:us-east-1:000000000000:table/orders")
    )
    response = synth.pure_answer("dynamodb:ListTagsOfResource", "orders", "default", req.body, stores, 0.0)
    parsed = _parse("dynamodb", "ListTagsOfResource", response)
    assert parsed["Tags"] == []


def test_dynamodb_tag_resource_then_list_round_trips(sink, dynamodb, stores):
    tag_req = sink.call(
        lambda: dynamodb.tag_resource(
            ResourceArn="arn:aws:dynamodb:us-east-1:000000000000:table/orders",
            Tags=[{"Key": "env", "Value": "prod"}],
        )
    )
    synth.pure_answer("dynamodb:TagResource", "orders", "default", tag_req.body, stores, 0.0)

    list_req = sink.call(
        lambda: dynamodb.list_tags_of_resource(ResourceArn="arn:aws:dynamodb:us-east-1:000000000000:table/orders")
    )
    response = synth.pure_answer("dynamodb:ListTagsOfResource", "orders", "default", list_req.body, stores, 0.0)
    parsed = _parse("dynamodb", "ListTagsOfResource", response)
    assert parsed["Tags"] == [{"Key": "env", "Value": "prod"}]


def test_dynamodb_untag_resource_removes_key(sink, dynamodb, stores):
    stores.tags.set("default", "dynamodb:orders", {"env": "prod", "team": "infra"})
    untag_req = sink.call(
        lambda: dynamodb.untag_resource(
            ResourceArn="arn:aws:dynamodb:us-east-1:000000000000:table/orders", TagKeys=["env"]
        )
    )
    synth.pure_answer("dynamodb:UntagResource", "orders", "default", untag_req.body, stores, 0.0)
    assert stores.tags.get("default", "dynamodb:orders") == {"team": "infra"}


def test_dynamodb_create_table_seeds_tags_closing_the_documented_drift(sink, dynamodb, stores):
    """research: dynalite 'accepts but drops' CreateTable's Tags param --
    the exact drift the gateway must close by observing the request itself."""
    create_req = sink.call(
        lambda: dynamodb.create_table(
            TableName="orders",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            Tags=[{"Key": "env", "Value": "prod"}],
        )
    )
    unchanged = synth.postprocess(
        "dynamodb:CreateTable", "orders", "default", create_req.body, b'{"TableDescription": {}}', stores, "127.0.0.1:4266", 0.0
    )
    assert unchanged == b'{"TableDescription": {}}'
    assert stores.tags.get("default", "dynamodb:orders") == {"env": "prod"}


# --- dispatch table sanity ----------------------------------------------------


def test_pure_answer_returns_none_for_non_synth_action(stores):
    assert synth.pure_answer("s3:GetObject", "uploads", "default", b"", stores, 0.0) is None


def test_is_postprocess_action():
    assert synth.is_postprocess_action("sqs:CreateQueue") is True
    assert synth.is_postprocess_action("sqs:SendMessage") is False
    assert synth.is_postprocess_action("sns:GetSubscriptionAttributes") is True


# --- SNS: live GetSubscriptionAttributes FilterPolicy fixup (S2) ------------


def test_postprocess_strips_the_null_placeholder_entries_goaws_sends():
    # The exact wire shape captured from a real goaws (harness.py's
    # CaptureSink can't emit this -- goaws is the one answering it, not
    # boto3): one populated attribute alongside one goaws leaves as the
    # literal string "null" for an optional field nobody set.
    goaws_body = (
        b"<GetSubscriptionAttributesResponse><GetSubscriptionAttributesResult>"
        b"<Attributes>"
        b"<entry><key>RawMessageDelivery</key><value>true</value></entry>"
        b"<entry><key>FilterPolicy</key><value>null</value></entry>"
        b"</Attributes>"
        b"</GetSubscriptionAttributesResult></GetSubscriptionAttributesResponse>"
    )
    fixed = synth.postprocess(
        "sns:GetSubscriptionAttributes", "alerts", "default", b"", goaws_body, None, "127.0.0.1:4266", 0.0,
    )
    assert b"<key>FilterPolicy</key>" not in fixed
    assert b"<key>RawMessageDelivery</key><value>true</value>" in fixed  # a real value is untouched


def test_postprocess_is_a_noop_when_no_null_placeholders_are_present():
    goaws_body = b"<Attributes><entry><key>RawMessageDelivery</key><value>true</value></entry></Attributes>"
    fixed = synth.postprocess(
        "sns:GetSubscriptionAttributes", "alerts", "default", b"", goaws_body, None, "127.0.0.1:4266", 0.0,
    )
    assert fixed == goaws_body
