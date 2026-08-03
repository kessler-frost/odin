"""Shared fixtures for sigv4/classify tests: a fresh CaptureSink per test
plus one throwaway boto3 client per service, all pointed at the sink so
tests capture REAL boto3-signed requests instead of hand-building
fixtures (task-g2-brief.md step 1).
"""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qsl, urlsplit

import boto3
import pytest
from botocore.config import Config

from tests.gateway.harness import CaptureSink

ACCESS_KEY = "AKIDNODE00000001"
SECRET_KEY = "node-secret-vFvV8pQmZ3xR7Lk2Tn9y"


@pytest.fixture
def sink() -> Iterator[CaptureSink]:
    capture = CaptureSink()
    yield capture
    capture.close()


def _client(sink: CaptureSink, service: str, **extra: object):
    return boto3.client(
        service,
        endpoint_url=sink.endpoint,
        region_name="us-east-1",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        **extra,
    )


@pytest.fixture
def s3(sink: CaptureSink):
    return _client(sink, "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))


@pytest.fixture
def dynamodb(sink: CaptureSink):
    return _client(sink, "dynamodb")


@pytest.fixture
def sqs(sink: CaptureSink):
    return _client(sink, "sqs")


@pytest.fixture
def sns(sink: CaptureSink):
    return _client(sink, "sns")


@pytest.fixture
def sts(sink: CaptureSink):
    return _client(sink, "sts")


@pytest.fixture
def ec2(sink: CaptureSink):
    return _client(sink, "ec2")


@pytest.fixture
def iam(sink: CaptureSink):
    return _client(sink, "iam")


@pytest.fixture
def ecr(sink: CaptureSink):
    return _client(sink, "ecr")


@pytest.fixture
def lambda_(sink: CaptureSink):
    # Named `lambda_` (trailing underscore) since `lambda` is a keyword --
    # same reason every consumer below imports it as `lambda_`.
    return _client(sink, "lambda")


@pytest.fixture
def ecs(sink: CaptureSink):
    return _client(sink, "ecs")


@pytest.fixture
def logs(sink: CaptureSink):
    return _client(sink, "logs")


@pytest.fixture
def secretsmanager(sink: CaptureSink):
    return _client(sink, "secretsmanager")


@pytest.fixture
def ssm(sink: CaptureSink):
    return _client(sink, "ssm")


@pytest.fixture
def kms(sink: CaptureSink):
    # botocore names the service model `kms`, which is also its SigV4
    # credential scope (and so `classify()`'s `service`). Its `X-Amz-Target`
    # prefix is `TrentService`, which is a THIRD name and never reaches
    # classify's dispatch -- see gateway/models/kmsctl.py's wire-shape note.
    return _client(sink, "kms")


@pytest.fixture
def elasticache(sink: CaptureSink):
    return _client(sink, "elasticache")


@pytest.fixture
def rds(sink: CaptureSink):
    return _client(sink, "rds")


@pytest.fixture
def awslambda(sink: CaptureSink):
    # `lambda` is a Python keyword, so the fixture cannot be named after the
    # service the way every other one here is.
    return _client(sink, "lambda")


@pytest.fixture
def events(sink: CaptureSink):
    # EventBridge. botocore names the service model `events`, which is also its
    # SigV4 credential scope (and so `classify()`'s `service`) -- unlike elbv2
    # below, the two agree here.
    return _client(sink, "events")


@pytest.fixture
def route53(sink: CaptureSink):
    # Route 53 is a GLOBAL service, and its SigV4 credential scope carries
    # whatever region the CLIENT is configured with once `endpoint_url` is
    # overridden -- MEASURED as `.../us-west-2/route53/aws4_request` from a
    # client built with `region_name="us-west-2"`, i.e. NOT forced to
    # `us-east-1`. `_client` pins `us-east-1` like every other fixture here;
    # nothing in classify or the model reads the region, so the two agree.
    return _client(sink, "route53")


@pytest.fixture
def elbv2(sink: CaptureSink):
    # botocore names the service model `elbv2`; its SigV4 credential scope (and
    # so `classify()`'s `service`) is `elasticloadbalancing`.
    return _client(sink, "elbv2")


def split_url(url: str) -> tuple[str, dict[str, str]]:
    """path + query dict for classify(), preserving bare markers with no
    `=` (`?location`, `?uploads`, `?acl`, `?delete`) that `parse_qsl` drops
    unless told to keep blank values -- that flag is load-bearing for every
    S3 subresource check in classify.py.
    """
    parts = urlsplit(url)
    return parts.path, dict(parse_qsl(parts.query, keep_blank_values=True))
