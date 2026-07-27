"""v0.7.7 -- gateway/records.py: a malformed sidecar record is a NAMED failure
instead of a confidently wrong answer.

Two halves, and both are load-bearing:

  * **The value.** Every `test_wrong_answer_*` below starts from a shape that
    was MEASURED answering confidently and incorrectly on v0.7.6 -- the
    measurement is quoted in each docstring -- and asserts it now raises a
    `StoreUnreadable` naming the file AND the key. Mutation-tested: drop the
    field from the model in `records.py` and the matching test fails.
  * **The cost.** Every `test_upgrade_*` asserts the guard does NOT reject a
    record a released odin could have written. A guard that refuses a
    pre-upgrade store is worse than the bug it prevents, so the permissive
    cases are tested as deliberately as the strict ones.

These tests write the store FILE directly and then read it through a real
`SynthStores`, because that is the only path the validation is on -- a store
that never re-reads from disk (one process writing and reading its own cache)
never validates anything, and a test that only called `set()` would prove
nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from odin.gateway.models import logsctl
from odin.gateway.stores import SynthStores
from odin.reconcile import drift, tf_status
from odin.spec.store import CONTROL, StoreUnreadable

ENV = "v77a"


def seed(root: Path, name: str, payload) -> SynthStores:
    """Write one store file verbatim and hand back a store that has never read
    it -- so the next access is a real load from disk."""
    gateway = root / ENV / "gateway"
    gateway.mkdir(parents=True, exist_ok=True)
    (gateway / f"{name}.json").write_text(json.dumps(payload))
    return SynthStores(root)


def unreadable(store, name: str) -> StoreUnreadable:
    with pytest.raises(StoreUnreadable) as caught:
        getattr(store, name).items(ENV)
    return caught.value


# --- the file itself ---------------------------------------------------------


def test_top_level_list_is_named_rather_than_a_dict_update_error(tmp_path: Path):
    """v0.7.6 gap: `json.loads` accepted a top-level LIST, `JsonStore` stored
    it, and the failure surfaced from `items()` as

        ValueError: dictionary update sequence element #0 has length 9; 2 is required

    -- which is not a `StoreUnreadable`, so it never reached the CONTROL
    recovery advice server.py already had written for this exact file."""
    stores = seed(tmp_path, "ecsctl", ["cluster:a"])
    exc = unreadable(stores, "ecsctl")
    assert exc.role == CONTROL
    assert exc.path.name == "ecsctl.json"
    assert "dictionary update sequence" not in str(exc)


def test_top_level_string_is_named_rather_than_an_attribute_error(tmp_path: Path):
    """The `get()` half of the same gap: `AttributeError: 'str' object has no
    attribute 'get'`, from whichever model module touched it first."""
    stores = seed(tmp_path, "ecsctl", "cluster:a")
    exc = unreadable(stores, "ecsctl")
    assert exc.path.name == "ecsctl.json"


def test_the_failure_names_the_offending_key(tmp_path: Path):
    """A file has hundreds of records; naming the file alone leaves the
    operator to find the bad one by eye."""
    stores = seed(tmp_path, "rdsctl", {"db:orders": {}})
    assert "'db:orders'" in str(unreadable(stores, "rdsctl"))


# --- wrong answers, one per store that had one -------------------------------


def test_wrong_answer_log_events_string_is_no_longer_splat_into_characters(tmp_path: Path):
    """THE keys.json shape, one store over. Measured on v0.7.6 with
    `events:{group}` holding `"boom"`: `_append_events` returned sequence
    token 2, `PutLogEvents` answered 200, and the file came back

        "events:/aws/lambda/hello": ["b", "o", "o", "m", {...}]

    -- the string splatted by `[*(current or []), *records]` exactly the way
    `pair[0], pair[1]` indexed `"AKIAodin1234"`, and PERSISTED. Every later
    read then died with `TypeError: string indices must be integers`."""
    group = "/aws/lambda/hello"
    stores = seed(tmp_path, "logsctl", {
        f"group:{group}": {"log_group_name": group, "auto": False, "retention_in_days": None},
        f"events:{group}": "boom",
    })
    exc = unreadable(stores, "logsctl")
    assert f"'events:{group}'" in str(exc)


def test_wrong_answer_log_events_list_of_strings(tmp_path: Path):
    """The same store one step less corrupt: a list of bare lines instead of
    event dicts. `_ordered` sorts on `e["timestamp"]` and dies with no file
    named."""
    group = "/aws/lambda/hello"
    stores = seed(tmp_path, "logsctl", {f"events:{group}": ["line one", "line two"]})
    assert f"'events:{group}'" in str(unreadable(stores, "logsctl"))


def test_wrong_answer_log_events_is_the_write_path_too(tmp_path: Path):
    """The splat happened on the WRITE, so the guard has to fire there as well
    -- `_append_events` reads the buffer before appending to it, and that read
    is now validated. Before: token 2 and a corrupt file. Now: a named
    failure and nothing written."""
    group = "/aws/lambda/hello"
    stores = seed(tmp_path, "logsctl", {f"events:{group}": "boom"})
    with pytest.raises(StoreUnreadable):
        logsctl._append_events(stores, ENV, group, "s1", [{"timestamp": 1, "message": "hi"}])


def test_wrong_answer_barrier_bool_no_longer_reads_as_one(tmp_path: Path):
    """`int(stores.logsctl.get(env, barrier_key, 0))` turns `true` into 1, so
    the log-shipping dedup treats one stored line as already-seen that isn't.
    Measured on v0.7.6 against a 4-message stream: barrier `true` -> anchor
    `['m1','m2','m3']` (m0 silently dropped), barrier `2.9` -> `['m2','m3']`.
    Duplicated or skipped log lines, no error. `strict` int rejects both --
    `bool` is not an `int` to Pydantic in strict mode."""
    group, stream = "/aws/lambda/hello", "s1"
    for bad in (True, 2.9, "1"):
        stores = seed(tmp_path, "logsctl", {f"barrier:{group}:{stream}": bad})
        assert f"'barrier:{group}:{stream}'" in str(unreadable(stores, "logsctl"))


def test_wrong_answer_rds_missing_endpoint_port_no_longer_reports_healthy(tmp_path: Path):
    """The worst one measured, because it turns off FOUR guards with one
    absent field. `drift._db_records` skips a record whose `endpoint_port` is
    falsy, and `_dead` / `live_verdicts` / `sweep_compute` all read through
    it. On v0.7.6, with a runtime reporting the Postgres container GONE:

        well-formed record   -> /world: rds CRASHED, real verdict
        endpoint_port absent -> /world: rds HEALTHY, no verdict, no facts

    So the record with one field missing was reported healthy while nothing
    was running, and no probe would ever contradict it."""
    record = {
        "db_instance_identifier": "mydb", "status": "available",
        "master_username": "odin", "master_password": "pw", "db_name": "app",
    }
    stores = seed(tmp_path, "rdsctl", {"db:mydb": record})
    assert "endpoint_port" in str(unreadable(stores, "rdsctl"))


def test_wrong_answer_rds_null_endpoint_port_is_rejected(tmp_path: Path):
    """`endpoint_port: null` reaches the identical falsy skip. `int | None`
    would have accepted it, which is why the field is a bare `int`: rdsctl
    writes `0` at creation and a real port on `available`, never null."""
    stores = seed(tmp_path, "rdsctl", {"db:mydb": {
        "db_instance_identifier": "mydb", "status": "available", "endpoint_port": None,
        "master_username": "odin", "master_password": "pw", "db_name": "app",
    }})
    assert "endpoint_port" in str(unreadable(stores, "rdsctl"))


def test_wrong_answer_rds_false_green_is_gone_end_to_end(tmp_path: Path):
    """The projection itself, not just the store: `tf_status.project` used to
    answer `('rds', 'healthy', {}, None)` for this record. It must now refuse
    to answer at all rather than answer wrongly -- an unreadable store is a
    500 that names the file, which is the outcome `server.py` already has
    recovery advice for."""
    stores = seed(tmp_path, "rdsctl", {"db:mydb": {
        "db_instance_identifier": "mydb", "status": "available", "endpoint_port": None,
        "master_username": "odin", "master_password": "pw", "db_name": "app",
    }})
    with pytest.raises(StoreUnreadable):
        tf_status.project(stores, ENV)
    with pytest.raises(StoreUnreadable):
        drift._db_records(stores, ENV)


def test_wrong_answer_load_balancer_security_groups_string(tmp_path: Path):
    """elbv2's own instance of the keys.json bug, measured: with
    `security_groups` holding the bare string `"sg-0abc123"`, `_lb_xml`'s
    `[escape(sg) for sg in record["security_groups"]]` emitted TEN
    single-character `<member>` entries, botocore parsed all ten, and
    terraform-provider-aws would write `['s','g','-','0',...]` into
    `aws_lb.security_groups` state -- every later plan dirty against
    fiction."""
    stores = seed(tmp_path, "elbv2ctl", {"lb:web": {
        "name": "web", "arn": "arn:lb", "state": "active", "lb_id": "abc",
        "scheme": "internet-facing", "type": "application", "ip_address_type": "ipv4",
        "vpc_id": "vpc-1", "created_time": "2026-01-01", "availability_zones": [],
        "attributes": {}, "security_groups": "sg-0abc123",
    }})
    assert "security_groups" in str(unreadable(stores, "elbv2ctl"))


def test_wrong_answer_target_list_string(tmp_path: Path):
    """`targets:{tg}` is a bare list; a string is iterated as characters and
    `"h"["id"]` is the same TypeError the ring buffer gave."""
    stores = seed(tmp_path, "elbv2ctl", {"targets:api": "host.docker.internal"})
    assert "'targets:api'" in str(unreadable(stores, "elbv2ctl"))


def test_wrong_answer_sqs_attributes_string_is_no_longer_echoed_as_a_map(tmp_path: Path):
    """Measured on v0.7.6: `{"attributes": "VisibilityTimeout=30"}` made
    GetQueueAttributes answer

        {"Attributes": "VisibilityTimeout=30"}

    with a 200 -- a string where the AWS wire shape is a map, handed to
    botocore as though odin had checked it."""
    stores = seed(tmp_path, "sqs_queues", {"q1": {"attributes": "VisibilityTimeout=30", "deleted_at": None}})
    assert "attributes" in str(unreadable(stores, "sqs_queues"))


def test_wrong_answer_sqs_deleted_at_string(tmp_path: Path):
    """`now - deleted_at` against a string raised `TypeError: unsupported
    operand type(s) for -: 'float' and 'str'` from inside GetQueueAttributes
    -- and the same field on an ecs service record 500s every
    DescribeServices, which breaks plan, apply and destroy at once."""
    stores = seed(tmp_path, "sqs_queues", {"q1": {"attributes": {}, "deleted_at": "yesterday"}})
    assert "deleted_at" in str(unreadable(stores, "sqs_queues"))


def test_wrong_answer_tags_record_string(tmp_path: Path):
    """A tags record is `{tag: value}`. As a string, `tags.get("odin:node")`
    is an unnamed `AttributeError` that takes down the WHOLE World projection
    -- `tf_status` reads tags for every kind it projects."""
    stores = seed(tmp_path, "tags", {"lambda:arn:x": "odin:node=hello"})
    assert "'lambda:arn:x'" in str(unreadable(stores, "tags"))


def test_wrong_answer_tags_record_wire_shaped_list(tmp_path: Path):
    """ECS's own WIRE shape for tags is a list of `{Key, Value}` structs, so
    this is the plausible mistake rather than a contrived one -- and
    `tags.items()` on a list is the same unnamed AttributeError."""
    stores = seed(tmp_path, "tags", {"ecs:arn:x": [{"Key": "odin:node", "Value": "api"}]})
    assert "'ecs:arn:x'" in str(unreadable(stores, "tags"))


def test_wrong_answer_ecs_desired_count_string(tmp_path: Path):
    """`running == record["desired_count"]` never holds for `"2"`, so the
    service reads `starting` forever; elsewhere the same value raises
    (`int >= str`) mid-apply."""
    stores = seed(tmp_path, "ecsctl", {"service:c1:web": {
        "service_name": "web", "cluster_name": "c1", "status": "ACTIVE",
        "desired_count": "2", "task_definition_arn": "arn:td", "created_at": 1.0,
        "launch_type": "FARGATE",
    }})
    assert "desired_count" in str(unreadable(stores, "ecsctl"))


def test_wrong_answer_taskdef_revision_counter_string(tmp_path: Path):
    """`taskdef-rev:{family}` is a bare int the register path does `+ 1` on.
    Measured consequence of it being wrong (absent, in the survey's probe):
    `_latest_active_taskdef` returned None for a family with three live
    revisions, and the next RegisterTaskDefinition minted revision 1 and
    OVERWROTE the existing `taskdef:api:1`."""
    stores = seed(tmp_path, "ecsctl", {"taskdef-rev:api": "3"})
    assert "'taskdef-rev:api'" in str(unreadable(stores, "ecsctl"))


def test_wrong_answer_iam_attached_policy_arns_string(tmp_path: Path):
    """`if arn not in role["attached_policy_arns"]` becomes a SUBSTRING test
    when the value is a string, so AttachRolePolicy can decide a policy is
    already attached because its text appears somewhere -- and DeleteRole's
    in-use guard reads any non-empty string as in-use."""
    stores = seed(tmp_path, "iamctl", {"role:app": {
        "role_name": "app", "arn": "arn:role/app",
        "attached_policy_arns": "arn:aws:iam::aws:policy/ReadOnlyAccess",
        "inline_policies": {},
    }})
    assert "attached_policy_arns" in str(unreadable(stores, "iamctl"))


def test_wrong_answer_secret_version_stages_string(tmp_path: Path):
    """`version_stages` is membership-tested to find AWSCURRENT. As a string
    that is a substring match, and GetSecretValue would hand back the wrong
    version's CLEARTEXT."""
    stores = seed(tmp_path, "secretsctl", {"version:db:v1": {
        "version_id": "v1", "version_stages": "AWSCURRENT", "secret_string": "hunter2",
    }})
    assert "version_stages" in str(unreadable(stores, "secretsctl"))


def test_wrong_answer_ec2_instance_record_string(tmp_path: Path):
    """The record itself as a bare string -- `record["state_name"]` gives
    `TypeError: string indices must be integers`, naming nothing."""
    stores = seed(tmp_path, "ec2compute", {"instance:i-1": "running"})
    assert "'instance:i-1'" in str(unreadable(stores, "ec2compute"))


def test_wrong_answer_lambda_record_missing_state(tmp_path: Path):
    """`_LAMBDA_PHASE.get(record["state"], ...)` -- a `KeyError: 'state'`
    with no file attached, out of the World projection."""
    stores = seed(tmp_path, "lambdactl", {"fn:hello": {
        "function_name": "hello", "function_arn": "arn:fn",
    }})
    assert "state" in str(unreadable(stores, "lambdactl"))


def test_wrong_answer_security_group_rules_string(tmp_path: Path):
    """`sg.rules` is compiled into the Nebula firewall that really gates an
    rds container's overlay port -- the one store value in odin a network
    reachability decision is made from."""
    stores = seed(tmp_path, "ec2net", {"sg:sg-1": {
        "group_id": "sg-1", "group_name": "web", "vpc_id": "vpc-1", "rules": "allow-all",
    }})
    assert "rules" in str(unreadable(stores, "ec2net"))


# --- the cost: what must still load ------------------------------------------


def test_upgrade_unknown_key_prefix_is_left_alone(tmp_path: Path):
    """logsctl's pre-v0.7.1 `cursor:` residue is real, on real disks, and
    nothing reads it (stores.py's own note). Refusing to load a file because
    of dead weight would strand the env for no gain."""
    group = "/aws/lambda/hello"
    stores = seed(tmp_path, "logsctl", {
        f"cursor:{group}:s1": 41,
        f"group:{group}": {"log_group_name": group},
    })
    assert stores.logsctl.get(ENV, f"cursor:{group}:s1") == 41


def test_upgrade_unknown_store_name_is_not_validated(tmp_path: Path):
    """A store `records.py` has never heard of keeps working exactly as
    before -- the registry is opt-in, so a model added tomorrow needs no edit
    here to keep running."""
    gateway = tmp_path / ENV / "gateway"
    gateway.mkdir(parents=True)
    (gateway / "widgets.json").write_text(json.dumps({"anything": "at all"}))
    from odin.gateway.stores import JsonStore
    assert JsonStore(tmp_path, "widgets").get(ENV, "anything") == "at all"


def test_upgrade_a_record_carrying_only_load_bearing_fields_loads(tmp_path: Path):
    """The strictness rule stated as a test: nothing is required except what a
    reader actually depends on. rdsctl writes ~20 fields; a record holding
    only the six that decide anything is still readable, so a field dropped
    between releases cannot brick an env."""
    stores = seed(tmp_path, "rdsctl", {"db:mydb": {
        "db_instance_identifier": "mydb", "status": "available", "endpoint_port": 5432,
        "master_username": "odin", "master_password": "pw", "db_name": "app",
    }})
    assert stores.rdsctl.get(ENV, "db:mydb")["status"] == "available"


def test_upgrade_unknown_extra_fields_survive_the_read(tmp_path: Path):
    """A NEWER odin's added field must not make the record unreadable, and it
    must still be THERE afterwards -- validation checks and throws the model
    away; the store keeps the plain dict it always did."""
    stores = seed(tmp_path, "lambdactl", {"fn:hello": {
        "function_name": "hello", "function_arn": "arn:fn", "state": "Active",
        "a_field_from_a_later_release": {"nested": [1, 2]},
    }})
    record = stores.lambdactl.get(ENV, "fn:hello")
    assert record["a_field_from_a_later_release"] == {"nested": [1, 2]}


def test_upgrade_records_stay_plain_mutable_dicts(tmp_path: Path):
    """Model modules mutate the dicts the store hands back and the store
    re-`json.dumps` them, so validation must not substitute Pydantic models."""
    stores = seed(tmp_path, "lambdactl", {"fn:hello": {
        "function_name": "hello", "function_arn": "arn:fn", "state": "Active",
    }})
    record = stores.lambdactl.get(ENV, "fn:hello")
    assert type(record) is dict
    record["state"] = "Failed"
    stores.lambdactl.set(ENV, "fn:hello", record)
    assert json.loads((tmp_path / ENV / "gateway" / "lambdactl.json").read_text())["fn:hello"]["state"] == "Failed"


def test_upgrade_nullable_fields_may_really_be_null(tmp_path: Path):
    """Every one of these is written as `None` by the real creation path, so
    rejecting null would refuse a brand-new record."""
    stores = seed(tmp_path, "ec2compute", {"instance:i-1": {
        "instance_id": "i-1", "state_name": "pending", "private_ip": None,
        "state_reason": None, "terminated_at": None,
    }})
    assert stores.ec2compute.get(ENV, "instance:i-1")["private_ip"] is None


def test_upgrade_a_zero_endpoint_port_is_accepted(tmp_path: Path):
    """rdsctl writes `endpoint_port: 0` at creation and while a reboot is in
    flight. It is a value problem, not a shape problem: rejecting 0 here
    would refuse a record the writer produces on the happy path -- which is
    the failure this module exists to prevent, not cause."""
    stores = seed(tmp_path, "rdsctl", {"db:mydb": {
        "db_instance_identifier": "mydb", "status": "creating", "endpoint_port": 0,
        "master_username": "odin", "master_password": "pw", "db_name": "app",
    }})
    assert stores.rdsctl.get(ENV, "db:mydb")["endpoint_port"] == 0


def test_upgrade_an_empty_container_definitions_list_is_accepted(tmp_path: Path):
    """`RegisterTaskDefinition` accepts an empty list, so the store really
    holds one; the `[0]` that then raises is a reader bug, not a shape the
    parser may refuse."""
    stores = seed(tmp_path, "ecsctl", {"taskdef:api:1": {
        "family": "api", "revision": 1, "status": "ACTIVE", "network_mode": "bridge",
        "container_definitions": [], "requires_compatibilities": [], "volumes": [],
        "registered_at": 1.0,
    }})
    assert stores.ecsctl.get(ENV, "taskdef:api:1")["container_definitions"] == []


def test_upgrade_a_non_string_tag_value_is_accepted(tmp_path: Path):
    """`ecr.py` and `synth.py` build tags straight out of a client's JSON
    (`{t["Key"]: t.get("Value", "")}`), so a caller sending a non-string
    `Value` makes odin WRITE one. Requiring `str` would let a legal API call
    brick the env on the next read."""
    stores = seed(tmp_path, "tags", {"ecr:arn:x": {"count": 7}})
    assert stores.tags.get(ENV, "ecr:arn:x") == {"count": 7}


def test_upgrade_an_integer_timestamp_is_accepted_where_a_float_is_declared(tmp_path: Path):
    """JSON `1` decodes to a Python int; strict Pydantic accepts an int for a
    `float` field (probed on 2.13.4), so a whole-second timestamp is fine."""
    stores = seed(tmp_path, "sqs_queues", {"q1": {"attributes": {}, "deleted_at": 1785130167}})
    assert stores.sqs_queues.get(ENV, "q1")["deleted_at"] == 1785130167


def test_upgrade_an_absent_store_file_is_still_an_empty_dict(tmp_path: Path):
    stores = SynthStores(tmp_path)
    assert stores.ecsctl.items(ENV) == {}



# --- the one field where a wrong TYPE is a security hole ----------------------


def _sg_record(is_egress):
    return {"sg:sg-1": {"group_id": "sg-1", "group_name": "web", "vpc_id": "vpc-1",
                        "rules": {"r1": {"is_egress": is_egress, "ip_protocol": "-1"}}}}


@pytest.mark.parametrize("bad", [0, "", None, "false", 1], ids=repr)
def test_a_security_group_rule_with_a_non_bool_direction_is_refused(tmp_path: Path, bad):
    """`is_egress` decides whether a rule means "allow all OUTBOUND" (the seeded
    AWS default) or "allow all INBOUND" (the widest firewall there is), and
    `_compiled_firewall` used to select on it with a TRUTHINESS test.

    Measured on the seeded egress rule — proto `-1`, no ports, `0.0.0.0/0` — a
    falsy `is_egress` compiled to
    `inbound=[{'port':'any','proto':'any','cidr':'0.0.0.0/0'}]` where correct is
    `inbound=[]`. Asymmetric, which is what made it dangerous: the string
    `"false"` is truthy so it failed CLOSED, while `0`/`""`/`None` failed OPEN.
    So this is refused on read rather than tolerated."""
    exc = unreadable(seed(tmp_path, "ec2net", _sg_record(bad)), "ec2net")
    assert exc.role == CONTROL
    assert exc.path.name == "ec2net.json"


def test_a_well_formed_security_group_still_loads(tmp_path: Path):
    """The guard must not refuse what the writers really produce — the four
    route handlers pass a literal `True`/`False`, and have since this store's
    birth commit."""
    stores = seed(tmp_path, "ec2net", _sg_record(False))
    assert stores.ec2net.get(ENV, "sg:sg-1")["rules"]["r1"]["is_egress"] is False
