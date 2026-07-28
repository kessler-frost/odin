"""`compile_policies_from_iam` — the whole enforcement path since v0.8.12.

The gateway authorizes from the IAM that was APPLIED, not from the canvas edges,
and every workload kind reaches its role a different way:

    lambda -> the function record's own `role` ARN
    ecs    -> the SERVICE's task definition, and its `task_role_arn`
    ec2    -> the INSTANCE's `iam_instance_profile`, then that profile's `roles`,
              with the principal's NAME coming from the shared tag store

The IAM half is driven by the real handlers (`iam:CreateRole`,
`iam:PutRolePolicy`, `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`)
with the parameters the AWS provider really sends, because that half is what the
new code reads and a hand-written dict would only prove the compiler parses my
guess. The workload records are seeded, deliberately: `lambda:CreateFunction`
and `ec2:RunInstances` boot a real RIE container and a real Lima VM, so calling
them here would make a unit test start substrate. `test_the_seeded_workload
_records_still_match_what_the_real_writers_produce` is the guard on that
shortcut — it fails if a writer renames a field this compiler depends on.

That guard is not theoretical. Writing this file is what caught the ec2 label
bug: the compiler read `instance["tags"]`, which does not exist — the tags live
in the shared store under `ec2:{id}` — so an ec2 workload's grants were keyed by
instance id, a principal name no caller can present, and every applied
permission on an ec2 node would have been denied.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlencode

from odin.gateway.models import iamctl
from odin.gateway.policy import compile_policies_from_iam
from odin.gateway.stores import SynthStores

ENV = "applied-iam"
NOW = 1785130167.0
GRANT = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": "uploads"}],
})
TRUST = '{"Version":"2012-10-17","Statement":[]}'


async def _iam(action: str, params: dict, stores: SynthStores):
    """The real IAM control-plane handler, called the way the provider calls it."""
    response = await iamctl.pure_answer(action, "", ENV, urlencode(params).encode(), stores, NOW)
    assert response.status_code == 200, response.body
    return response


async def _role_with_grant(stores: SynthStores, role: str) -> None:
    """What `aws_iam_role` + `aws_iam_role_policy` really leave in the store."""
    await _iam("iam:CreateRole", {"RoleName": role, "AssumeRolePolicyDocument": TRUST}, stores)
    await _iam("iam:PutRolePolicy", {
        "RoleName": role, "PolicyName": f"{role}-grants", "PolicyDocument": GRANT,
    }, stores)


async def test_a_lambda_reaches_its_role_through_its_own_role_arn(tmp_path: Path):
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "resizer-role")
    stores.lambdactl.set(ENV, "fn:resizer", {
        "function_name": "resizer", "state": "Active",
        "role": "arn:aws:iam::000000000000:role/resizer-role",
    })

    compiled = compile_policies_from_iam(stores, ENV)
    assert [(s.actions, s.resources) for s in compiled["resizer"]] == [
        (("s3:GetObject", "s3:ListBucket"), ("uploads",)),
    ]


async def test_an_ecs_service_reaches_its_role_through_its_task_definition(tmp_path: Path):
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "api-role")
    stores.ecsctl.set(ENV, "taskdef:api:1", {
        "family": "api", "revision": 1, "task_role_arn": "arn:aws:iam::000000000000:role/api-role",
    })
    stores.ecsctl.set(ENV, "service:odin:api", {
        "service_name": "api", "cluster_name": "odin",
        "task_definition_arn": "arn:aws:ecs:us-east-1:000000000000:task-definition/api:1",
    })

    assert "api" in compile_policies_from_iam(stores, ENV)


async def test_an_ec2_instance_reaches_its_role_through_its_instance_profile(tmp_path: Path):
    """The longest chain, and the only one that reads a second record — plus the
    only one whose principal NAME comes from somewhere other than the record
    itself. `RunInstances` carries the profile, never the role."""
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "box-role")
    await _iam("iam:CreateInstanceProfile", {"InstanceProfileName": "box-profile"}, stores)
    await _iam("iam:AddRoleToInstanceProfile", {
        "InstanceProfileName": "box-profile", "RoleName": "box-role",
    }, stores)
    stores.ec2compute.set(ENV, "instance:i-abc", {
        "instance_id": "i-abc", "iam_instance_profile": "box-profile", "state_name": "running",
    })
    stores.tags.set(ENV, "ec2:i-abc", {"odin:node": "box"})

    compiled = compile_policies_from_iam(stores, ENV)
    assert "box" in compiled, (
        "an ec2 instance's grants never reached the gateway under the name a "
        "caller presents — the chain is instance -> profile -> roles -> policy, "
        f"and the node label comes from the tag store. Got: {sorted(compiled)}"
    )


async def test_an_instance_with_no_node_tag_is_not_a_principal_at_all(tmp_path: Path):
    """An instance odin did not create carries no `odin:node` tag, so there is no
    name to authorize under. Inventing one (the instance id) would put an entry
    in the map that no caller can ever match, which reads as a grant and behaves
    as a deny."""
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "box-role")
    await _iam("iam:CreateInstanceProfile", {"InstanceProfileName": "box-profile"}, stores)
    await _iam("iam:AddRoleToInstanceProfile", {
        "InstanceProfileName": "box-profile", "RoleName": "box-role",
    }, stores)
    stores.ec2compute.set(ENV, "instance:i-orphan", {
        "instance_id": "i-orphan", "iam_instance_profile": "box-profile",
    })

    assert compile_policies_from_iam(stores, ENV) == {}


async def test_a_workload_with_no_role_gets_nothing(tmp_path: Path):
    """Default-deny — the same answer an unapplied canvas gives."""
    stores = SynthStores(tmp_path)
    stores.lambdactl.set(ENV, "fn:lonely", {"function_name": "lonely", "state": "Active", "role": ""})

    assert compile_policies_from_iam(stores, ENV) == {}


async def test_a_role_with_no_policy_gets_nothing(tmp_path: Path):
    """A workload odin gave a role but nobody granted anything to. The role is
    real; the authorization is empty, and an empty entry would be a lie in the
    shape of a grant."""
    stores = SynthStores(tmp_path)
    await _iam("iam:CreateRole", {"RoleName": "bare-role", "AssumeRolePolicyDocument": TRUST}, stores)
    stores.lambdactl.set(ENV, "fn:bare", {
        "function_name": "bare", "state": "Active", "role": "arn:aws:iam::000000000000:role/bare-role",
    })

    assert compile_policies_from_iam(stores, ENV) == {}


async def test_an_attached_managed_policy_counts_too(tmp_path: Path):
    """`aws_iam_role_policy_attachment` is as valid a way to grant as an inline
    policy, and a real canvas imported from someone's Terraform will use it."""
    stores = SynthStores(tmp_path)
    await _iam("iam:CreateRole", {"RoleName": "att-role", "AssumeRolePolicyDocument": TRUST}, stores)
    await _iam("iam:CreatePolicy", {"PolicyName": "reader", "PolicyDocument": GRANT}, stores)
    await _iam("iam:AttachRolePolicy", {
        "RoleName": "att-role", "PolicyArn": "arn:aws:iam::000000000000:policy/reader",
    }, stores)
    stores.lambdactl.set(ENV, "fn:att", {
        "function_name": "att", "state": "Active", "role": "arn:aws:iam::000000000000:role/att-role",
    })

    assert "att" in compile_policies_from_iam(stores, ENV)


# --- the guard on the seeded records ------------------------------------------

SRC = Path(__file__).resolve().parents[2] / "src" / "odin" / "gateway"
# Every field the compiler reads off a record it did not write here, and the
# module whose writer must still produce it.
DEPENDENCIES = [
    ("models/lambdactl.py", '"function_name"'),
    ("models/lambdactl.py", '"role"'),
    ("models/ecsctl.py", '"task_role_arn"'),
    ("models/ecsctl.py", '"revision"'),
    ("models/ecsctl.py", '"service_name"'),
    ("models/ecsctl.py", '"task_definition_arn"'),
    ("models/ec2compute.py", '"iam_instance_profile"'),
    ("models/ec2compute.py", '"instance_id"'),
    ("models/ec2compute.py", 'f"ec2:{instance_id}"'),
    ("models/ec2compute.py", '"odin:node"'),
]


def test_the_seeded_workload_records_still_match_what_the_real_writers_produce():
    """The price of not calling `CreateFunction`/`RunInstances` here (they boot a
    real container and a real VM) is that the seeds could drift from the writers.
    This is what stops that being silent: rename a field the compiler depends on
    and this fails, naming it, instead of the gateway quietly denying every
    permission on that kind."""
    missing = [
        f"{module}: {field}" for module, field in DEPENDENCIES
        if field not in (SRC / module).read_text()
    ]
    assert missing == [], (
        "`compile_policies_from_iam` reads these off records written elsewhere, "
        f"and the writer no longer mentions them: {missing}"
    )


def test_the_compiler_reads_the_tag_store_for_an_ec2_label():
    """The specific bug this file caught, pinned by shape rather than by prose.
    The instance record has no `tags` field — reading one found nothing, fell
    back to the instance id, and produced a principal no caller can present."""
    source = (SRC / "policy.py").read_text()
    ec2_half = source.partition("instance-profile:")[2]
    assert "stores.tags.get(" in ec2_half, (
        "the ec2 label must come from the shared tag store (`ec2:{id}` ->"
        " `odin:node`), which is where `_run_instances` writes it"
    )
    assert not re.search(r'instance\.get\(\s*["\']tags["\']', ec2_half), (
        "an instance record carries no `tags` field — reading one silently "
        "denies every applied permission on every ec2 workload"
    )
