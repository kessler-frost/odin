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

from odin.gateway import records, synth
from odin.gateway.models import ec2net, ecr, eventsctl, iamctl, logsctl, secretsctl, ssmctl
from odin.gateway.stores import JsonStore, SynthStores

ENV = "v77a-rt"
NOW = 1785130167.0


def reload(root: Path) -> SynthStores:
    """A store that has never read this env -- so touching it is a real load
    from disk, through `records.validate`."""
    return SynthStores(root)


def store_names(stores: SynthStores) -> list[str]:
    """Every `JsonStore` on `SynthStores`, DERIVED rather than listed.

    This used to be a hand-written list of 17 names, and the store count has
    grown one service at a time from four -- so an 18th store was invisible to
    both readers below until someone remembered to type it in. Same reasoning
    `SynthStores.forget_env` already writes down for deriving from `vars`."""
    return sorted(name for name, store in vars(stores).items() if isinstance(store, JsonStore))


def touch_everything(stores: SynthStores) -> dict[str, int]:
    """Force a load of every store and report how many records each holds, so
    a test that seeded nothing cannot pass by validating nothing."""
    return {name: len(getattr(stores, name).items(ENV)) for name in store_names(stores)}


def test_every_store_has_a_records_schema(tmp_path: Path):
    """The ratchet that was missing: a new `JsonStore` with no `SCHEMAS` entry
    loads WITHOUT validation and says nothing about it (`_adapter_for` returns
    None for an unknown store name and every record is skipped).

    That is a guard silently not firing -- honesty rule 1 -- and it is exactly
    how the file-shape bugs `records.py`'s docstring lists got in. Nothing
    enforced the pairing before: `touch_everything`'s list happened to match
    `SCHEMAS` at 17 entries each, by hand, twice."""
    missing = sorted(set(store_names(SynthStores(tmp_path))) - set(records.SCHEMAS))
    assert not missing, f"these stores load unvalidated -- add a records.SCHEMAS entry: {missing}"


def test_records_schemas_name_no_store_that_does_not_exist(tmp_path: Path):
    """The other direction: a `SCHEMAS` key that matches no store validates
    nothing at all, so a typo there is a schema that silently never runs."""
    unknown = sorted(set(records.SCHEMAS) - set(store_names(SynthStores(tmp_path))))
    assert not unknown, f"these SCHEMAS entries match no JsonStore: {unknown}"


async def json_call(module, action: str, payload: dict, stores: SynthStores, resource: str = ""):
    response = await module.pure_answer(action, resource, ENV, json.dumps(payload).encode(), stores, NOW)
    assert response.status_code == 200, response.body
    return response


async def query_call(module, action: str, params: dict, stores: SynthStores, resource: str = ""):
    response = await module.pure_answer(action, resource, ENV, urlencode(params).encode(), stores, NOW)
    assert response.status_code == 200, response.body
    return response


async def test_logsctl_real_records_reload(tmp_path: Path):
    """group + stream + the events ring buffer + a barrier, all written by the
    real CloudWatch Logs handlers."""
    stores = SynthStores(tmp_path)
    group, stream = "/odin/app", "s1"
    await json_call(logsctl, "logs:CreateLogGroup", {"logGroupName": group, "tags": {"odin:node": "app"}}, stores)
    await json_call(logsctl, "logs:CreateLogStream", {"logGroupName": group, "logStreamName": stream}, stores)
    await json_call(logsctl, "logs:PutLogEvents", {
        "logGroupName": group, "logStreamName": stream,
        "logEvents": [{"timestamp": 1785130167000, "message": "hello"}],
    }, stores)
    await json_call(logsctl, "logs:PutRetentionPolicy", {"logGroupName": group, "retentionInDays": 7}, stores)
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


async def test_ecr_real_record_reloads(tmp_path: Path):
    stores = SynthStores(tmp_path)
    await json_call(ecr, "ecr:CreateRepository", {
        "repositoryName": "app", "tags": [{"Key": "odin:node", "Value": "app"}],
    }, stores)

    fresh = reload(tmp_path)
    assert fresh.ecr.get(ENV, "repo:app")["repository_name"] == "app"


async def test_iamctl_real_records_reload(tmp_path: Path):
    """A role, a managed policy, an instance profile, and the attachment that
    turns `attached_policy_arns` from `[]` into a real list."""
    stores = SynthStores(tmp_path)
    await query_call(iamctl, "iam:CreateRole", {
        "RoleName": "app", "AssumeRolePolicyDocument": '{"Version":"2012-10-17"}',
    }, stores)
    await query_call(iamctl, "iam:CreatePolicy", {
        "PolicyName": "readonly", "PolicyDocument": '{"Version":"2012-10-17"}',
    }, stores)
    arn = "arn:aws:iam::000000000000:policy/readonly"
    await query_call(iamctl, "iam:AttachRolePolicy", {"RoleName": "app", "PolicyArn": arn}, stores)
    await query_call(iamctl, "iam:PutRolePolicy", {
        "RoleName": "app", "PolicyName": "inline", "PolicyDocument": '{"Version":"2012-10-17"}',
    }, stores)
    await query_call(iamctl, "iam:CreateInstanceProfile", {"InstanceProfileName": "app"}, stores)
    await query_call(iamctl, "iam:AddRoleToInstanceProfile", {
        "InstanceProfileName": "app", "RoleName": "app",
    }, stores)

    fresh = reload(tmp_path)
    role = fresh.iamctl.get(ENV, "role:app")
    assert role["attached_policy_arns"] == [arn]
    assert "inline" in role["inline_policies"]
    assert fresh.iamctl.get(ENV, "instance-profile:app")["roles"] == ["app"]


async def test_ssmctl_real_record_reloads(tmp_path: Path):
    stores = SynthStores(tmp_path)
    await json_call(ssmctl, "ssm:PutParameter", {
        "Name": "/odin/db/url", "Value": "postgres://x", "Type": "String",
    }, stores)

    fresh = reload(tmp_path)
    assert fresh.ssmctl.get(ENV, "param:/odin/db/url")["version"] == 1


async def test_secretsctl_real_records_reload(tmp_path: Path):
    """The secret AND its version -- `version_stages` is the list whose
    membership test decides which cleartext GetSecretValue returns."""
    stores = SynthStores(tmp_path)
    await json_call(secretsctl, "secretsmanager:CreateSecret", {
        "Name": "db-password", "SecretString": "hunter2",
    }, stores)

    fresh = reload(tmp_path)
    versions = [v for k, v in fresh.secretsctl.items(ENV).items() if k.startswith("version:")]
    assert versions and "AWSCURRENT" in versions[0]["version_stages"]


async def test_eventsctl_real_records_reload(tmp_path: Path):
    """A custom bus, a rule on it, and the target LIST -- the shape that made
    `events:` splat into characters one store over."""
    stores = SynthStores(tmp_path)
    await json_call(eventsctl, "events:CreateEventBus", {"Name": "orders"}, stores)
    await json_call(eventsctl, "events:PutRule", {
        "Name": "nightly", "EventBusName": "orders", "ScheduleExpression": "rate(1 day)",
        "Tags": [{"Key": "odin:node", "Value": "nightly"}],
    }, stores)
    await json_call(eventsctl, "events:PutTargets", {
        "Name": "nightly", "Rule": "nightly", "EventBusName": "orders",
        "Targets": [{"Id": "t1", "Arn": "arn:aws:lambda:us-east-1:000000000000:function:f"}],
    }, stores)

    fresh = reload(tmp_path)
    assert fresh.eventsctl.get(ENV, "rule:orders:nightly")["event_bus_name"] == "orders"
    assert [t["Id"] for t in fresh.eventsctl.get(ENV, "targets:orders:nightly")] == ["t1"]
    assert fresh.tags.get(ENV, f"events:{eventsctl.rule_arn('orders', 'nightly')}") == {"odin:node": "nightly"}


async def test_ec2net_real_records_reload(tmp_path: Path):
    """vpc + subnet + the DEFAULT security group the VPC mints, whose `rules`
    map is compiled into the Nebula firewall."""
    stores = SynthStores(tmp_path)
    await query_call(ec2net, "ec2:CreateVpc", {"CidrBlock": "10.0.0.0/16"}, stores)
    vpcs = [v for k, v in stores.ec2net.items(ENV).items() if k.startswith("vpc:")]
    assert vpcs
    await query_call(ec2net, "ec2:CreateSubnet", {
        "VpcId": vpcs[0]["vpc_id"], "CidrBlock": "10.0.1.0/24",
    }, stores)
    await query_call(ec2net, "ec2:CreateSecurityGroup", {
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


async def test_an_env_written_by_many_models_reloads_whole(tmp_path: Path):
    """The integration the individual tests do not give: several stores
    populated in one env, then EVERY store in that env loaded. A model that is
    too strict for one writer fails here even if its own test forgot the
    field."""
    stores = SynthStores(tmp_path)
    await json_call(logsctl, "logs:CreateLogGroup", {"logGroupName": "/odin/app"}, stores)
    await json_call(ecr, "ecr:CreateRepository", {"repositoryName": "app"}, stores)
    await json_call(ssmctl, "ssm:PutParameter", {"Name": "/a", "Value": "b", "Type": "String"}, stores)
    await query_call(iamctl, "iam:CreateRole", {"RoleName": "app", "AssumeRolePolicyDocument": "{}"}, stores)
    await query_call(ec2net, "ec2:CreateVpc", {"CidrBlock": "10.0.0.0/16"}, stores)

    counts = touch_everything(reload(tmp_path))
    assert counts["logsctl"] and counts["ecr"] and counts["ssmctl"]
    assert counts["iamctl"] and counts["ec2net"]
