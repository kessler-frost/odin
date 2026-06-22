"""S0.1 — the embedded MiniStack AWS control plane answers boto3 calls."""
from __future__ import annotations

import pytest

from odin.aws.embed import (
    ACCOUNT_ID,
    ministack_boto_client,
    start_ministack,
    stop_ministack,
)


@pytest.fixture
def ministack():
    port = start_ministack()
    yield port
    stop_ministack()


def test_embed_answers_sts_get_caller_identity(ministack):
    sts = ministack_boto_client("sts")
    ident = sts.get_caller_identity()
    assert ident["Account"] == ACCOUNT_ID


def test_embed_answers_s3_list_buckets(ministack):
    s3 = ministack_boto_client("s3")
    result = s3.list_buckets()
    assert "Buckets" in result
