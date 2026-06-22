"""M5 — the catalog codegen produces canvas nodes for MiniStack's services."""
from __future__ import annotations

from odin.aws.catalog_gen import generate_catalog_ts


def test_generates_service_nodes_and_skips_internal():
    ts = generate_catalog_ts()
    assert "GENERATED_CATALOG: ServiceDef[]" in ts
    # long-tail AWS services get canvas nodes
    for svc in ("aws_elasticache", "aws_eks", "aws_cloudformation", "aws_stepfunctions"):
        assert f"type: '{svc}'" in ts
    # internal / already-curated ones are skipped
    for skipped in ("aws_sts", "aws_account", "aws_s3", "aws_rds"):
        assert f"type: '{skipped}'" not in ts
