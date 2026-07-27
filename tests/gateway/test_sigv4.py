"""G2 -- sigv4: verify()/scope()/resign() against REAL boto3-signed
requests captured by tests/gateway/harness.py, no hand-built fixtures.
Ported from the research prototype
(.superpowers/sdd/research-iam-gateway.md §Q1): decomposed canonical
recompute using the request's ORIGINAL X-Amz-Date (never add_auth on the
inbound request), STREAMING-* rejection, UNSIGNED-PAYLOAD support.

One correctness gap this suite pins down (found via the harness, not in
the research writeup): for S3, botocore's canonical_request trusts the
X-Amz-Content-SHA256 HEADER verbatim rather than re-hashing the body, so a
valid signature alone doesn't prove the body wasn't swapped -- verify()
independently cross-checks that header against the real bytes.
"""
from __future__ import annotations

from botocore.config import Config

from odin.gateway import sigv4

from .conftest import ACCESS_KEY, SECRET_KEY, _client


def _secret_for(key: str) -> str | None:
    return SECRET_KEY if key == ACCESS_KEY else None


def _wrong_secret(key: str) -> str | None:
    return "not-the-real-secret" if key == ACCESS_KEY else None


# --- valid signatures across all four services -----------------------------


def test_s3_get_object_verifies(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_s3_head_object_verifies(sink, s3):
    req = sink.call(lambda: s3.head_object(Bucket="uploads", Key="a.txt"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_s3_put_object_verifies(sink, s3):
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_s3_list_objects_v2_verifies(sink, s3):
    req = sink.call(lambda: s3.list_objects_v2(Bucket="uploads"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_s3_multipart_upload_part_verifies(sink, s3):
    req = sink.call(
        lambda: s3.upload_part(Bucket="uploads", Key="big.bin", PartNumber=1, UploadId="fake-id", Body=b"x" * 32)
    )
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_dynamodb_put_item_verifies(sink, dynamodb):
    req = sink.call(lambda: dynamodb.put_item(TableName="orders", Item={"id": {"S": "1"}}))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_dynamodb_query_verifies(sink, dynamodb):
    req = sink.call(
        lambda: dynamodb.query(
            TableName="orders",
            KeyConditionExpression="id = :v",
            ExpressionAttributeValues={":v": {"S": "1"}},
        )
    )
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_sqs_send_message_verifies(sink, sqs):
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_sqs_receive_message_verifies(sink, sqs):
    req = sink.call(lambda: sqs.receive_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_sns_publish_verifies(sink, sns):
    req = sink.call(lambda: sns.publish(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts", Message="hi"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


def test_sns_subscribe_verifies(sink, sns):
    req = sink.call(
        lambda: sns.subscribe(
            TopicArn="arn:aws:sns:us-east-1:000000000000:alerts",
            Protocol="sqs",
            Endpoint="arn:aws:sqs:us-east-1:000000000000:jobs",
        )
    )
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


async def test_unsigned_payload_verifies(sink):
    # A REAL UNSIGNED-PAYLOAD capture, not a hand-edited header: botocore's
    # payload_signing_enabled=False config is what actually produces one.
    client = await _client(
        sink, "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path", "payload_signing_enabled": False})
    )
    req = sink.call(lambda: client.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes"))
    assert req.headers["X-Amz-Content-SHA256"] == "UNSIGNED-PAYLOAD"
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _secret_for) == ACCESS_KEY


# --- negatives ---------------------------------------------------------


def test_wrong_secret_rejects(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, _wrong_secret) is None


def test_unknown_access_key_rejects(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body, lambda k: None) is None


def test_tampered_s3_body_rejects(sink, s3):
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes"))
    assert sigv4.verify(req.method, req.url, req.headers, b"tampered-bytes-here!!", _secret_for) is None


def test_tampered_dynamodb_body_rejects(sink, dynamodb):
    req = sink.call(lambda: dynamodb.put_item(TableName="orders", Item={"id": {"S": "1"}}))
    assert sigv4.verify(req.method, req.url, req.headers, req.body + b"x", _secret_for) is None


def test_tampered_sqs_body_rejects(sink, sqs):
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))
    assert sigv4.verify(req.method, req.url, req.headers, req.body + b"x", _secret_for) is None


def test_missing_authorization_header_rejects(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    headers = {k: v for k, v in req.headers.items() if k.lower() != "authorization"}
    assert sigv4.verify(req.method, req.url, headers, req.body, _secret_for) is None


def test_malformed_authorization_header_rejects(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    headers = dict(req.headers)
    headers["Authorization"] = "Basic dXNlcjpwYXNz"
    assert sigv4.verify(req.method, req.url, headers, req.body, _secret_for) is None


def test_streaming_payload_rejects(sink, s3):
    # Real boto3 (seekable bodies) never sends STREAMING-* -- verified in
    # the research and by every other capture in this file -- so this
    # exercises the rejection branch directly on an otherwise-real capture
    # rather than fabricating a whole signed request by hand.
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes"))
    headers = dict(req.headers)
    headers["X-Amz-Content-SHA256"] = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
    assert sigv4.verify(req.method, req.url, headers, req.body, _secret_for) is None


# --- scope() -----------------------------------------------------------


def test_scope_reads_service_and_region_s3(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    assert sigv4.scope(req.headers) == ("s3", "us-east-1")


def test_scope_reads_service_and_region_dynamodb(sink, dynamodb):
    req = sink.call(lambda: dynamodb.put_item(TableName="orders", Item={"id": {"S": "1"}}))
    assert sigv4.scope(req.headers) == ("dynamodb", "us-east-1")


def test_scope_reads_service_and_region_sqs(sink, sqs):
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))
    assert sigv4.scope(req.headers) == ("sqs", "us-east-1")


def test_scope_reads_service_and_region_sns(sink, sns):
    req = sink.call(lambda: sns.publish(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts", Message="hi"))
    assert sigv4.scope(req.headers) == ("sns", "us-east-1")


def test_scope_returns_none_without_authorization_header():
    assert sigv4.scope({"Host": "example.com"}) is None


# --- resign() ------------------------------------------------------------


def test_resign_produces_headers_that_verify_under_new_identity(sink, s3):
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes"))
    resigned = sigv4.resign(req.method, req.url, req.headers, req.body, "BACKINGKEY", "backing-secret", "s3", "us-east-1")
    assert resigned["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=BACKINGKEY/")
    ak = sigv4.verify(req.method, req.url, resigned, req.body, lambda k: "backing-secret" if k == "BACKINGKEY" else None)
    assert ak == "BACKINGKEY"


def test_resign_does_not_mutate_the_original_headers(sink, s3):
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"payload-bytes"))
    original_auth = req.headers["Authorization"]
    sigv4.resign(req.method, req.url, req.headers, req.body, "BACKINGKEY", "backing-secret", "s3", "us-east-1")
    assert req.headers["Authorization"] == original_auth
