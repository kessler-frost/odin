from __future__ import annotations

from odin.terraform.provider import SERVICES, render_provider
from odin.terraform.runner import _parse_ndjson


def test_render_provider_points_every_service_at_the_endpoint():
    tf = render_provider("http://127.0.0.1:4202")
    for svc in SERVICES:
        assert svc in tf
    assert tf.count("http://127.0.0.1:4202") == len(SERVICES)


def test_render_provider_skips_real_aws_checks():
    tf = render_provider("http://127.0.0.1:4202", region="eu-west-1")
    assert "skip_credentials_validation = true" in tf
    assert "skip_requesting_account_id  = true" in tf
    assert "skip_metadata_api_check     = true" in tf
    assert 'region                      = "eu-west-1"' in tf
    assert 'source = "hashicorp/aws"' in tf


def test_parse_ndjson_collects_diagnostics_and_changes():
    stream = "\n".join([
        '{"type":"version","terraform":"1.12.1"}',
        '{"type":"diagnostic","diagnostic":{"severity":"error","summary":"bad cidr"}}',
        '{"type":"planned_change","change":{"resource":{"addr":"aws_vpc.v"},"action":"create"}}',
        "not json — ignored",
        "",
    ])
    diagnostics, changes = _parse_ndjson(stream)
    assert len(diagnostics) == 1
    assert diagnostics[0]["summary"] == "bad cidr"
    assert len(changes) == 1
    assert changes[0]["resource"]["addr"] == "aws_vpc.v"
