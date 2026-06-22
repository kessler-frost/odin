"""Provision control-plane AWS resources (S3/SQS/SNS/DynamoDB) in the embed.

A canvas node like an S3 bucket named "uploads" becomes a real bucket in the
embedded MiniStack on apply, so app containers (which reach the embed) can use
it. Control-plane only — no container; the resource lives in MiniStack's
in-memory state. Idempotent create + existence check + best-effort teardown.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from odin.aws.embed import ministack_boto_client

PROVISIONED = ("s3", "sqs", "sns", "dynamodb")


class MiniStackAws:
    def provision(self, service: str, name: str) -> None:
        client = ministack_boto_client(service)
        try:
            if service == "s3":
                client.create_bucket(Bucket=name)
            elif service == "sqs":
                client.create_queue(QueueName=name)
            elif service == "sns":
                client.create_topic(Name=name)
            elif service == "dynamodb":
                client.create_table(
                    TableName=name, BillingMode="PAY_PER_REQUEST",
                    AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                    KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                )
        except ClientError as exc:
            if not any(w in str(exc) for w in ("Exist", "Conflict", "InUse")):
                raise

    def exists(self, service: str, name: str) -> bool:
        client = ministack_boto_client(service)
        try:
            if service == "s3":
                client.head_bucket(Bucket=name)
            elif service == "sqs":
                client.get_queue_url(QueueName=name)
            elif service == "sns":
                arns = [t["TopicArn"] for t in client.list_topics().get("Topics", [])]
                return any(a.endswith(f":{name}") for a in arns)
            elif service == "dynamodb":
                client.describe_table(TableName=name)
            return True
        except ClientError:
            return False

    def deprovision(self, service: str, name: str) -> None:
        client = ministack_boto_client(service)
        try:
            if service == "s3":
                client.delete_bucket(Bucket=name)
            elif service == "sqs":
                client.delete_queue(QueueUrl=client.get_queue_url(QueueName=name)["QueueUrl"])
            elif service == "dynamodb":
                client.delete_table(TableName=name)
            elif service == "sns":
                for topic in client.list_topics().get("Topics", []):
                    if topic["TopicArn"].endswith(f":{name}"):
                        client.delete_topic(TopicArn=topic["TopicArn"])
        except ClientError:
            pass
