"""G2 -- classify: (action, resource) extraction against REAL boto3
requests captured by tests/gateway/harness.py. Ported from the research
prototype (.superpowers/sdd/research-iam-gateway.md §Q2): dynamodb/sqs via
X-Amz-Target, sns via the Action form param, s3 via a (method, has_key,
subresource) table incl. the full multipart lifecycle.

CONTRACT ADDENDUM (task-g2-brief.md, binding): resources are the bare
NODE LABEL the policy compiler emits (G1), never an ARN -- s3 bucket name,
sqs/sns/dynamodb resource NAME. One test per service below asserts that
form explicitly.

S1 (gateway/synth.py) extended this surface for create/name-carrying and
tag-CRUD paths: SQS CreateQueue/GetQueueUrl and SNS CreateTopic resolve via
QueueName/Name (no QueueUrl/TopicArn exists yet at that point); SNS/DynamoDB
tag ops resolve via `ResourceArn` instead of Topic/TableName; SNS
subscription-scoped ops (GetSubscriptionAttributes/Unsubscribe) resolve via
`SubscriptionArn`'s embedded topic name. None of these change what a
workload principal can reach in practice -- no edge grants create/tag verbs
to workers in v1 (see test_proxy.py's default-deny coverage for these same
two ops) -- classify() just stops manufacturing a distinct
`unmappable-action` reason for them.
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


def test_s3_get_bucket_acl_resolves_to_its_real_action(sink, s3):
    # S2: mapped so evaluate() gets to run (the operator's full-allow needs a
    # chance) -- a workload still denies via default-deny, not unmappable.
    req = sink.call(lambda: s3.get_bucket_acl(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:GetBucketAcl", "uploads")


def test_s3_get_bucket_policy_resolves_to_its_real_action(sink, s3):
    # The exact probe a real `tofu apply` of aws_s3_bucket makes right after
    # CreateBucket -- the gap that surfaced this whole mapping (S2).
    req = sink.call(lambda: s3.get_bucket_policy(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:GetBucketPolicy", "uploads")


def test_s3_get_bucket_tagging_resolves_to_its_real_action(sink, s3):
    req = sink.call(lambda: s3.get_bucket_tagging(Bucket="uploads"))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) == ("s3:GetBucketTagging", "uploads")


def test_s3_put_bucket_config_subresource_still_denies_cleanly(sink, s3):
    # v1 has no write path for any bucket-config subresource -- only the GET
    # side is mapped; a PUT still denies (unmappable, not merely unpermitted).
    req = sink.call(lambda: s3.put_bucket_tagging(Bucket="uploads", Tagging={"TagSet": [{"Key": "k", "Value": "v"}]}))
    path, query = split_url(req.url)
    assert classify("s3", req.method, path, query, req.headers, req.body) is None


def test_s3_object_level_subresource_still_denies_cleanly(sink, s3):
    # Genuinely unsupported (v1 has no create path for a legal hold at all)
    # -- unlike bucket-config reads, this stays unmappable regardless of
    # method (research §Q2: "unknown subresources ... -> explicit deny").
    req = sink.call(
        lambda: s3.get_object_legal_hold(Bucket="uploads", Key="a.txt")
    )
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


def test_sqs_get_queue_url_resolves_via_queue_name(sink, sqs):
    req = sink.call(lambda: sqs.get_queue_url(QueueName="jobs"))
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:GetQueueUrl", "jobs")


def test_sqs_create_queue_resolves_via_queue_name(sink, sqs):
    req = sink.call(lambda: sqs.create_queue(QueueName="jobs"))
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:CreateQueue", "jobs")


def test_sqs_list_queue_tags(sink, sqs):
    req = sink.call(lambda: sqs.list_queue_tags(QueueUrl=f"{sink.endpoint}/000000000000/jobs"))
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:ListQueueTags", "jobs")


def test_sqs_get_queue_attributes(sink, sqs):
    req = sink.call(
        lambda: sqs.get_queue_attributes(QueueUrl=f"{sink.endpoint}/000000000000/jobs", AttributeNames=["All"])
    )
    path, query = split_url(req.url)
    assert classify("sqs", req.method, path, query, req.headers, req.body) == ("sqs:GetQueueAttributes", "jobs")


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


def test_sns_create_topic_resolves_via_name(sink, sns):
    req = sink.call(lambda: sns.create_topic(Name="alerts"))
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == ("sns:CreateTopic", "alerts")


def test_sns_list_tags_for_resource_resolves_via_resource_arn(sink, sns):
    req = sink.call(
        lambda: sns.list_tags_for_resource(ResourceArn="arn:aws:sns:us-east-1:000000000000:alerts")
    )
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == ("sns:ListTagsForResource", "alerts")


def test_sns_tag_resource_resolves_via_resource_arn(sink, sns):
    req = sink.call(
        lambda: sns.tag_resource(
            ResourceArn="arn:aws:sns:us-east-1:000000000000:alerts", Tags=[{"Key": "env", "Value": "prod"}]
        )
    )
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == ("sns:TagResource", "alerts")


def test_sns_get_subscription_attributes_resolves_topic_from_subscription_arn(sink, sns):
    req = sink.call(
        lambda: sns.get_subscription_attributes(
            SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-1"
        )
    )
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == (
        "sns:GetSubscriptionAttributes",
        "alerts",
    )


def test_sns_unsubscribe_resolves_topic_from_subscription_arn(sink, sns):
    req = sink.call(
        lambda: sns.unsubscribe(SubscriptionArn="arn:aws:sns:us-east-1:000000000000:alerts:sub-uuid-1")
    )
    path, query = split_url(req.url)
    assert classify("sns", req.method, path, query, req.headers, req.body) == ("sns:Unsubscribe", "alerts")


def test_sns_resource_is_bare_label_not_arn(sink, sns):
    req = sink.call(lambda: sns.publish(TopicArn="arn:aws:sns:us-east-1:000000000000:alerts", Message="hi"))
    path, query = split_url(req.url)
    action, resource = classify("sns", req.method, path, query, req.headers, req.body)
    assert resource == "alerts"
    assert "arn:" not in resource


# --- dynamodb tag CRUD (via ResourceArn, not TableName) ---------------------


def test_dynamodb_list_tags_of_resource_resolves_via_resource_arn(sink, dynamodb):
    req = sink.call(
        lambda: dynamodb.list_tags_of_resource(
            ResourceArn="arn:aws:dynamodb:us-east-1:000000000000:table/orders"
        )
    )
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == (
        "dynamodb:ListTagsOfResource",
        "orders",
    )


def test_dynamodb_tag_resource_resolves_via_resource_arn(sink, dynamodb):
    req = sink.call(
        lambda: dynamodb.tag_resource(
            ResourceArn="arn:aws:dynamodb:us-east-1:000000000000:table/orders",
            Tags=[{"Key": "env", "Value": "prod"}],
        )
    )
    path, query = split_url(req.url)
    assert classify("dynamodb", req.method, path, query, req.headers, req.body) == (
        "dynamodb:TagResource",
        "orders",
    )


# --- unknown service -------------------------------------------------------


def test_unknown_service_returns_none():
    assert classify("lambda", "POST", "/", {}, {}, b"") is None
