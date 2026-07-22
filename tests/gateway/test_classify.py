"""G2 -- classify: (action, resource) extraction against REAL boto3
requests captured by tests/gateway/harness.py. Ported from the research
prototype (.superpowers/sdd/research-iam-gateway.md §Q2): dynamodb/sqs via
X-Amz-Target, sns via the Action form param, s3 via a (method, has_key,
subresource) table incl. the full multipart lifecycle.

CONTRACT ADDENDUM (task-g2-brief.md, binding): resources are the bare
NODE LABEL the policy compiler emits (G1), never an ARN -- s3 bucket name,
sqs/sns/dynamodb resource NAME. One test per service below asserts that
form explicitly.

Two ops are deliberately unmappable in v1 and documented as such rather
than guessed at: SQS GetQueueUrl and SNS CreateTopic don't carry the
QueueUrl/TopicArn the addendum's extraction rule keys off of (they're
discovering/creating the identifier, not using it) -- odin's fabric layer
injects QueueUrl/TopicArn at Apply time instead, so workloads calling
these through the gateway get a clean deny (R6) rather than a mis-scoped
resource.
"""
from __future__ import annotations

from odin.gateway.classify import classify

from .conftest import split_url

# --- s3 ----------------------------------------------------------------


def test_s3_list_buckets(sink, s3):
    req = sink.call(lambda: s3.list_buckets())
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:ListAllMyBuckets", "*")


def test_s3_create_bucket(sink, s3):
    req = sink.call(lambda: s3.create_bucket(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:CreateBucket", "uploads")


def test_s3_delete_bucket(sink, s3):
    req = sink.call(lambda: s3.delete_bucket(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:DeleteBucket", "uploads")


def test_s3_head_bucket(sink, s3):
    req = sink.call(lambda: s3.head_bucket(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:ListBucket", "uploads")


def test_s3_list_objects_v2(sink, s3):
    req = sink.call(lambda: s3.list_objects_v2(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:ListBucket", "uploads")


def test_s3_get_bucket_location(sink, s3):
    req = sink.call(lambda: s3.get_bucket_location(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:GetBucketLocation", "uploads")


def test_s3_get_object(sink, s3):
    req = sink.call(lambda: s3.get_object(Bucket="uploads", Key="a.txt"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:GetObject", "uploads")


def test_s3_head_object(sink, s3):
    req = sink.call(lambda: s3.head_object(Bucket="uploads", Key="a.txt"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:GetObject", "uploads")


def test_s3_put_object(sink, s3):
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="a.txt", Body=b"hi"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:PutObject", "uploads")


def test_s3_delete_object(sink, s3):
    req = sink.call(lambda: s3.delete_object(Bucket="uploads", Key="a.txt"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:DeleteObject", "uploads")


def test_s3_delete_objects_batch(sink, s3):
    req = sink.call(lambda: s3.delete_objects(Bucket="uploads", Delete={"Objects": [{"Key": "a.txt"}]}))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:DeleteObject", "uploads")


def test_s3_create_multipart_upload(sink, s3):
    req = sink.call(lambda: s3.create_multipart_upload(Bucket="uploads", Key="big.bin"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:PutObject", "uploads")


def test_s3_upload_part(sink, s3):
    req = sink.call(
        lambda: s3.upload_part(Bucket="uploads", Key="big.bin", PartNumber=1, UploadId="fake-id", Body=b"x" * 16)
    )
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:PutObject", "uploads")


def test_s3_complete_multipart_upload(sink, s3):
    req = sink.call(
        lambda: s3.complete_multipart_upload(
            Bucket="uploads",
            Key="big.bin",
            UploadId="fake-id",
            MultipartUpload={"Parts": [{"ETag": "e", "PartNumber": 1}]},
        )
    )
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:PutObject", "uploads")


def test_s3_abort_multipart_upload(sink, s3):
    req = sink.call(lambda: s3.abort_multipart_upload(Bucket="uploads", Key="big.bin", UploadId="fake-id"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:AbortMultipartUpload", "uploads")


def test_s3_list_parts(sink, s3):
    req = sink.call(lambda: s3.list_parts(Bucket="uploads", Key="big.bin", UploadId="fake-id"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:ListMultipartUploadParts", "uploads")


def test_s3_list_multipart_uploads(sink, s3):
    req = sink.call(lambda: s3.list_multipart_uploads(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == (
        "s3:ListBucketMultipartUploads",
        "uploads",
    )


def test_s3_unknown_subresource_denies_cleanly(sink, s3):
    req = sink.call(lambda: s3.get_bucket_acl(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) is None


def test_s3_copy_object_deferred_denies_cleanly(sink, s3):
    req = sink.call(lambda: s3.copy_object(Bucket="uploads", Key="b.txt", CopySource="uploads/a.txt"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) is None


def test_s3_resource_is_bare_label_not_arn(sink, s3):
    req = sink.call(lambda: s3.put_object(Bucket="uploads", Key="dir/nested/a.txt", Body=b"hi"))
    path, query = split_url(req.url)
    action, resource = classify("s3", req.method, path, query, req.headers, req.body)
    assert resource == "uploads"
    assert "arn:" not in resource and "/" not in resource


# --- dynamodb ------------------------------------------------------------


def test_dynamodb_put_item(sink, dynamodb):
    req = sink.call(lambda: dynamodb.put_item(TableName="orders", Item={"id": {"S": "1"}}))
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == ("dynamodb:PutItem", "orders")


def test_dynamodb_get_item(sink, dynamodb):
    req = sink.call(lambda: dynamodb.get_item(TableName="orders", Key={"id": {"S": "1"}}))
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == ("dynamodb:GetItem", "orders")


def test_dynamodb_query(sink, dynamodb):
    req = sink.call(
        lambda: dynamodb.query(
            TableName="orders",
            KeyConditionExpression="id = :v",
            ExpressionAttributeValues={":v": {"S": "1"}},
        )
    )
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == ("dynamodb:Query", "orders")


def test_dynamodb_scan(sink, dynamodb):
    req = sink.call(lambda: dynamodb.scan(TableName="orders"))
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == ("dynamodb:Scan", "orders")


def test_dynamodb_delete_item(sink, dynamodb):
    req = sink.call(lambda: dynamodb.delete_item(TableName="orders", Key={"id": {"S": "1"}}))
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == ("dynamodb:DeleteItem", "orders")


def test_dynamodb_resource_is_bare_label_not_arn(sink, dynamodb):
    req = sink.call(lambda: dynamodb.put_item(TableName="orders", Item={"id": {"S": "1"}}))
    path, query = split_url(req.url)
    action, resource = classify("dynamodb", req.method, path, query, req.headers, req.body)
    assert resource == "orders"
    assert "arn:" not in resource


# --- sqs -----------------------------------------------------------------


def test_sqs_send_message(sink, sqs):
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:SendMessage", "jobs")


def test_sqs_receive_message(sink, sqs):
    req = sink.call(lambda: sqs.receive_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:ReceiveMessage", "jobs")


def test_sqs_delete_message(sink, sqs):
    req = sink.call(
        lambda: sqs.delete_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", ReceiptHandle="fake-handle")
    )
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:DeleteMessage", "jobs")


def test_sqs_get_queue_url_unmappable_in_v1(sink, sqs):
    # GetQueueUrl carries QueueName, not QueueUrl -- deferred (see module
    # docstring), must fail cleanly rather than guess.
    req = sink.call(lambda: sqs.get_queue_url(QueueName="jobs"))
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) is None


def test_sqs_resource_is_bare_label_not_url(sink, sqs):
    req = sink.call(lambda: sqs.send_message(QueueUrl=f"{sink.endpoint}/000000000000/jobs", MessageBody="hi"))
    path, query = split_url(req.url)
    action, resource = classify("sqs", req.method, path, query, req.headers, req.body)
    assert resource == "jobs"
    assert "/" not in resource and "http" not in resource


# --- sns -----------------------------------------------------------------


def test_sns_publish(sink, sns):
    req = sink.call(lambda: sns.publish(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts", Message="hi"))
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == ("sns:Publish", "alerts")


def test_sns_subscribe(sink, sns):
    req = sink.call(
        lambda: sns.subscribe(
            TopicArn="arn:aws:sns:us-east-1:000000000000:alerts",
            Protocol="sqs",
            Endpoint="arn:aws:sqs:us-east-1:000000000000:jobs",
        )
    )
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == ("sns:Subscribe", "alerts")


def test_sns_create_topic_unmappable_in_v1(sink, sns):
    # CreateTopic carries Name, not TopicArn (there's no ARN yet) --
    # deferred (see module docstring), must fail cleanly rather than guess.
    req = sink.call(lambda: sns.create_topic(Name="alerts"))
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) is None


def test_sns_resource_is_bare_label_not_arn(sink, sns):
    req = sink.call(lambda: sns.publish(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts", Message="hi"))
    path, query = split_url(req.url)
    action, resource = classify("sns", req.method, path, query, req.headers, req.body)
    assert resource == "alerts"
    assert "arn:" not in resource


# --- unknown service -------------------------------------------------------


def test_unknown_service_returns_none():
    assert classify("lambda", "POST", "/", {}, {}, b"") is None
