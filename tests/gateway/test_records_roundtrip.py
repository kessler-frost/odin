"""v0.7.7 -- the OTHER half of gateway/records.py: what the real writers
actually produce must still load.

The strictness tests in `test_records.py` prove the guard fires. This file
proves it does not fire on odin's own output, and it does that the only way
that is worth anything -- by calling the REAL `pure_answer` handlers, letting
them write real records, and then reading those records back through a store
that has never seen them. A model checked against a hand-typed fixture proves
only that I typed the fixture to match the model.

The AWS-shaped modules whose create path needs a substrate (rdsctl, ec2compute,
lambdactl, ecsctl, elbv2ctl, cachectl) are NOT driven here -- they need their
own container/VM fakes, which live in their own test modules. Their models
were derived from the creation dict literals in source rather than from
observed output, and that is a weaker footing; it is stated here rather than
implied away. What protects them meanwhile is that every field they declare is
either written unconditionally at creation or optional with a default.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from odin.gateway import synth
from odin.gateway.models import ec2net, ecr, iamctl, logsctl, secretsctl, ssmctl
from odin.gateway.stores import SynthStores

ENV = "v77a-rt"
NOW = 1785130167.0


def reload(root: Path) -> SynthStores:
    """A store that has never read this env -- so touching it is a real load
    from disk, through `records.validate`."""
    return SynthStores(root)


def touch_everything(stores: SynthStores) -> dict[str, int]:
    """Force a load of every store and report how many records each holds, so
    a test that seeded nothing cannot pass by validating nothing."""
    names = [
        "tags", "sqs_queues", "sns_topics", "sns_subscriptions", "ec2net", "iamctl",
        "ecr", "ec2compute", "lambdactl", "ecsctl", "logsctl", "secretsctl",
        "ssmctl", "cachectl", "rdsctl", "elbv2ctl",
    ]
    return {name: len(getattr(stores, name).items(ENV)) for name in names}


def json_call(module, action: str, payload: dict, stores: SynthStores, resource: str = ""):
    response = module.pure_answer(action, resource, ENV, json.dumps(payload).encode(), stores, NOW)
    assert response.status_code == 200, response.body
    return response


def query_call(module, action: str, params: dict, stores: SynthStores, resource: str = ""):
    response = module.pure_answer(action, resource, ENV, urlencode(params).encode(), stores, NOW)
    assert response.status_code == 200, response.body
    return response


def test_logsctl_real_records_reload(tmp_path: Path):
    """group + stream + the events ring buffer + a barrier, all written by the
    real CloudWatch Logs handlers."""
    stores = SynthStores(tmp_path)
    group, stream = "/odin/app", "s1"
    json_call(logsctl, "logs:CreateLogGroup", {"logGroupName": group, "tags": {"odin:node": "app"}}, stores)
    json_call(logsctl, "logs:CreateLogStream", {"logGroupName": group, "logStreamName": stream}, stores)
    json_call(logsctl, "logs:PutLogEvents", {
        "logGroupName": group, "logStreamName": stream,
        "logEvents": [{"timestamp": 1785130167000, "message": "hello"}],
    }, stores)
    json_call(logsctl, "logs:PutRetentionPolicy", {"logGroupName": group, "retentionInDays": 7}, stores)
    logsctl.reset_cursor(stores, ENV, group, stream)  # writes a real `barrier:` int

    fresh = reload(tmp_path)
    assert fresh.logsctl.get(ENV, f"events:{group}")[0]["message"] == "hello"
    assert fresh.logsctl.get(ENV, f"barrier:{group}:{stream}") == 1
    assert fresh.tags.get(ENV, f"logs:{logsctl.group_arn(group)}") == {"odin:node": "app"}


def test_logsctl_substrate_shipping_reloads(tmp_path: Path):
    """`ingest_tail` is the OTHER writer into the ring buffer -- the substrate
    log-shipping path, which is where the character-splat bug lived."""
    stores = SynthStores(tmp_path)
    group, stream = "/aws/lambda/hello", "s1"
    logsctl.ensure_group(stores, ENV, group)
    logsctl.ingest_tail(stores, ENV, group, stream, "line one\nline two\n")

    fresh = reload(tmp_path)
    assert [e["message"] for e in fresh.logsctl.get(ENV, f"events:{group}")] == ["line one", "line two"]


def test_ecr_real_record_reloads(tmp_path: Path):
    stores = SynthStores(tmp_path)
    json_call(ecr, "ecr:CreateRepository", {
        "repositoryName": "app", "tags": [{"Key": "odin:node", "Value": "app"}],
    }, stores)

    fresh = reload(tmp_path)
    assert fresh.ecr.get(ENV, "repo:app")["repository_name"] == "app"


def test_iamctl_real_records_reload(tmp_path: Path):
    """A role, a managed policy, an instance profile, and the attachment that
    turns `attached_policy_arns` from `[]` into a real list."""
    stores = SynthStores(tmp_path)
    query_call(iamctl, "iam:CreateRole", {
        "RoleName": "app", "AssumeRolePolicyDocument": '{"Version":"2012-10-17"}',
    }, stores)
    query_call(iamctl, "iam:CreatePolicy", {
        "PolicyName": "readonly", "PolicyDocument": '{"Version":"2012-10-17"}',
    }, stores)
    arn = "arn:aws:iam::000000000000:policy/readonly"
    query_call(iamctl, "iam:AttachRolePolicy", {"RoleName": "app", "PolicyArn": arn}, stores)
    query_call(iamctl, "iam:PutRolePolicy", {
        "RoleName": "app", "PolicyName": "inline", "PolicyDocument": '{"Version":"2012-10-17"}',
    }, stores)
    query_call(iamctl, "iam:CreateInstanceProfile", {"InstanceProfileName": "app"}, stores)
    query_call(iamctl, "iam:AddRoleToInstanceProfile", {
        "InstanceProfileName": "app", "RoleName": "app",
    }, stores)

    fresh = reload(tmp_path)
    role = fresh.iamctl.get(ENV, "role:app")
    assert role["attached_policy_arns"] == [arn]
    assert "inline" in role["inline_policies"]
    assert fresh.iamctl.get(ENV, "instance-profile:app")["roles"] == ["app"]


def test_ssmctl_real_record_reloads(tmp_path: Path):
    stores = SynthStores(tmp_path)
    json_call(ssmctl, "ssm:PutParameter", {
        "Name": "/odin/db/url", "Value": "postgres://x", "Type": "String",
    }, stores)

    fresh = reload(tmp_path)
    assert fresh.ssmctl.get(ENV, "param:/odin/db/url")["version"] == 1


def test_secretsctl_real_records_reload(tmp_path: Path):
    """The secret AND its version -- `version_stages` is the list whose
    membership test decides which cleartext GetSecretValue returns."""
    stores = SynthStores(tmp_path)
    json_call(secretsctl, "secretsmanager:CreateSecret", {
        "Name": "db-password", "SecretString": "hunter2",
    }, stores)

    fresh = reload(tmp_path)
    versions = [v for k, v in fresh.secretsctl.items(ENV).items() if k.startswith("version:")]
    assert versions and "AWSCURRENT" in versions[0]["version_stages"]


def test_ec2net_real_records_reload(tmp_path: Path):
    """vpc + subnet + the DEFAULT security group the VPC mints, whose `rules`
    map is compiled into the Nebula firewall."""
    stores = SynthStores(tmp_path)
    query_call(ec2net, "ec2:CreateVpc", {"CidrBlock": "10.0.0.0/16"}, stores)
    vpcs = [v for k, v in stores.ec2net.items(ENV).items() if k.startswith("vpc:")]
    assert vpcs
    query_call(ec2net, "ec2:CreateSubnet", {
        "VpcId": vpcs[0]["vpc_id"], "CidrBlock": "10.0.1.0/24",
    }, stores)
    query_call(ec2net, "ec2:CreateSecurityGroup", {
        "VpcId": vpcs[0]["vpc_id"], "GroupName": "web", "GroupDescription": "web tier",
    }, stores)

    fresh = reload(tmp_path)
    counts = touch_everything(fresh)
    assert counts["ec2net"] >= 4  # vpc + subnet + default sg + web sg
    sgs = [v for k, v in fresh.ec2net.items(ENV).items() if k.startswith("sg:")]
    assert all(isinstance(sg["rules"], dict) for sg in sgs)


def _synth_env(root: Path) -> SynthStores:
    """All four synth stores populated by synth.py's OWN postprocess writers --
    the real `sqs:CreateQueue` / `sqs:DeleteQueue` / `sns:CreateTopic` /
    `sns:Unsubscribe` handlers, fed the request+response bodies they see in
    production."""
    stores = SynthStores(root)
    synth.postprocess(
        "sqs:CreateQueue", "jobs", ENV,
        json.dumps({"Attributes": {"VisibilityTimeout": "30"}, "tags": {"odin:node": "jobs"}}).encode(),
        json.dumps({"QueueUrl": "http://127.0.0.1:4100/queue/jobs"}).encode(),
        stores, "127.0.0.1:5186", NOW,
    )
    synth.postprocess(
        "sqs:DeleteQueue", "old-jobs", ENV, b"{}", b"{}", stores, "127.0.0.1:5186", NOW,
    )
    synth.postprocess(
        "sns:CreateTopic", "events", ENV,
        b"Attributes.entry.1.key=DisplayName&Attributes.entry.1.value=events"
        b"&Tags.member.1.Key=odin%3Anode&Tags.member.1.Value=events",
        b"<CreateTopicResponse/>", stores, "127.0.0.1:5186", NOW,
    )
    synth.postprocess(
        "sns:Unsubscribe", "events", ENV,
        b"SubscriptionArn=arn%3Aaws%3Asns%3Aus-east-1%3A000000000000%3Aevents%3Asub",
        b"<UnsubscribeResponse/>", stores, "127.0.0.1:5186", NOW,
    )
    return stores


@pytest.mark.parametrize("store", ["tags", "sqs_queues", "sns_topics", "sns_subscriptions"])
def test_synth_stores_reload_after_real_writes(tmp_path: Path, store: str):
    _synth_env(tmp_path)
    assert getattr(reload(tmp_path), store).items(ENV)


def test_a_deleted_queues_grace_marker_reloads(tmp_path: Path):
    """`deleted_at` is the field the delete-confirmation shim compares against
    `now`; the real DeleteQueue writer is what puts a float there."""
    _synth_env(tmp_path)
    assert isinstance(reload(tmp_path).sqs_queues.get(ENV, "old-jobs")["deleted_at"], float)


def test_an_env_written_by_many_models_reloads_whole(tmp_path: Path):
    """The integration the individual tests do not give: several stores
    populated in one env, then EVERY store in that env loaded. A model that is
    too strict for one writer fails here even if its own test forgot the
    field."""
    stores = SynthStores(tmp_path)
    json_call(logsctl, "logs:CreateLogGroup", {"logGroupName": "/odin/app"}, stores)
    json_call(ecr, "ecr:CreateRepository", {"repositoryName": "app"}, stores)
    json_call(ssmctl, "ssm:PutParameter", {"Name": "/a", "Value": "b", "Type": "String"}, stores)
    query_call(iamctl, "iam:CreateRole", {"RoleName": "app", "AssumeRolePolicyDocument": "{}"}, stores)
    query_call(ec2net, "ec2:CreateVpc", {"CidrBlock": "10.0.0.0/16"}, stores)

    counts = touch_everything(reload(tmp_path))
    assert counts["logsctl"] and counts["ecr"] and counts["ssmctl"]
    assert counts["iamctl"] and counts["ec2net"]
