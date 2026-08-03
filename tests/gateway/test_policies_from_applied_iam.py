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
guess.

The workload records come BOTH ways, and the split is deliberate:

  * the focused cases below seed a record directly, because what they are about
    is the compiler's chain (an instance with no node tag, a role with no
    policy) and a seed states that in three lines;
  * `test_a_real_*` (bottom of the file) run the REAL writers —
    `ec2:RunInstances`, `ecs:RegisterTaskDefinition` + `CreateService`,
    `lambda:CreateFunction` — into a fresh store and then run the real
    compiler. That is what stops the seeds drifting from the writers.

This file used to claim the second group was impossible, "because
`lambda:CreateFunction` and `ec2:RunInstances` boot a real RIE container and a
real Lima VM". That was wrong, and it cost the file its only real guard: all
three writers take an injectable substrate (`vm=`, `runtime=`, `substrate=`)
and the rest of the gateway suite has always driven them with fakes. What stood
in for the missing test was a substring grep over the writer modules' source
text, which was green in both of the ways it could be wrong — see the comment
above those tests.

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

from odin.gateway.classify import classify
from odin.gateway.models import ec2compute, ecsctl, iamctl, lambdactl
from odin.gateway.policy import compile_policies_from_iam
from odin.gateway.stores import SynthStores

from .conftest import split_url
# The substrate fakes the rest of the gateway suite already drives these
# writers with -- imported rather than re-declared so a fake that drifts from
# its real class drifts in exactly one place (the precedent is
# `test_serve_on_loop.py` importing `FakeRds`/`FakeRuntime`).
from .test_ec2compute import FakeInstanceVm
from .test_ecsctl import _CONTAINER_DEF, FakeTaskRuntime
from .test_lambdactl import FakeFunctionRuntime

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
#
# Until v0.8.18 this section was a SUBSTRING GREP: a list of ten string literals
# asserted to appear somewhere in the writer module's source text. It never
# built a record, never called a writer and never ran the compiler, and it was
# green in both of the ways that matter:
#
#   * a READ satisfies a grep meant to guard a WRITE. `"task_role_arn"` appears
#     twice in ecsctl.py -- the write at the RegisterTaskDefinition record and a
#     read at `_taskdef_wire`. Rename the write and the read still matches, so
#     the guard stayed green while every applied permission on every ecs
#     workload was denied.
#   * for ec2 there was no write occurrence to match AT ALL: `"odin:node"`
#     appears exactly once in ec2compute.py, in `tags.get("odin:node")` -- a
#     read. Delete the tag write and the guard could not notice. That is
#     precisely the bug the module docstring above says this file exists to
#     prevent.
#
# One of the ten (`"iam_instance_profile"`) had a single, write-only occurrence
# and did work. The replacement below runs the REAL writers instead, which is
# what the IAM half of this file already does and for the same reason.

WRITER_MUTATION_NOTE = """\
Mutation-tested (v0.8.18), both killed:
  * ecsctl.py's RegisterTaskDefinition record key `"task_role_arn"` -> renamed
  * ec2compute.py's `stores.tags.set(env, f"ec2:{instance_id}", tags)` -> deleted
Both left the OLD grep green; both fail here.
"""


async def _ec2_answer(stores: SynthStores, req, vm) -> None:
    path, query = split_url(req.url)
    action, resource = classify("ec2", req.method, path, query, req.headers, req.body)
    response = await ec2compute.pure_answer(action, resource, ENV, req.body, stores, NOW, vm)
    assert response.status_code == 200, response.body


async def _ecs_answer(stores: SynthStores, req, runtime) -> None:
    path, query = split_url(req.url)
    action, resource = classify("ecs", req.method, path, query, req.headers, req.body)
    response = await ecsctl.pure_answer(action, resource, ENV, req.body, stores, NOW, runtime)
    assert response.status_code == 200, response.body


async def _lambda_answer(stores: SynthStores, req, substrate) -> None:
    path, query = split_url(req.url)
    action, resource = classify("lambda", req.method, path, query, req.headers, req.body)
    response = await lambdactl.pure_answer(action, resource, ENV, req.body, stores, NOW, substrate, query)
    assert response.status_code in (200, 201, 202), response.body


async def test_a_real_run_instances_produces_a_record_the_compiler_can_authorize(
    tmp_path: Path, sink, ec2,
):
    """The REAL `ec2:RunInstances` writer, not a seed -- a fake `InstanceVm` is
    all it takes to run it, so the old "calling it would boot a VM" excuse for
    the grep never held.

    Asserts the PROPERTY, not the presence of a string: the grants reach the
    compiler under the node label a caller actually presents. Two separate
    writes have to be right for that -- the instance record's
    `iam_instance_profile` and the tag store's `odin:node` -- and the tag write
    is the one the old grep could not see at all.
    """
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "box-role")
    await _iam("iam:CreateInstanceProfile", {"InstanceProfileName": "box-profile"}, stores)
    await _iam("iam:AddRoleToInstanceProfile", {
        "InstanceProfileName": "box-profile", "RoleName": "box-role",
    }, stores)

    req = sink.call(lambda: ec2.run_instances(
        ImageId="ami-0abcdef1234567890", MinCount=1, MaxCount=1, InstanceType="t3.micro",
        IamInstanceProfile={"Name": "box-profile"},
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "odin:node", "Value": "box"}]}],
    ))
    await _ec2_answer(stores, req, FakeInstanceVm())

    compiled = compile_policies_from_iam(stores, ENV)
    assert [(s.actions, s.resources) for s in compiled.get("box", [])] == [
        (("s3:GetObject", "s3:ListBucket"), ("uploads",)),
    ], f"a really-launched instance's grants never reached the gateway. Got: {sorted(compiled)}"


async def test_a_real_register_task_definition_produces_a_record_the_compiler_can_authorize(
    tmp_path: Path, sink, ecs,
):
    """The REAL `ecs:RegisterTaskDefinition` + `ecs:CreateService` writers.
    `desiredCount=0` so nothing is placed -- the records are what this is about,
    and `FakeTaskRuntime` covers the convergence pass either way.

    This is the case the old grep provably could not catch: `"task_role_arn"`
    occurs twice in ecsctl.py, and only one of them is the write.
    """
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "api-role")
    runtime = FakeTaskRuntime()

    await _ecs_answer(stores, sink.call(lambda: ecs.create_cluster(clusterName="odin")), runtime)
    await _ecs_answer(stores, sink.call(lambda: ecs.register_task_definition(
        family="api", containerDefinitions=_CONTAINER_DEF,
        taskRoleArn="arn:aws:iam::000000000000:role/api-role",
    )), runtime)
    await _ecs_answer(stores, sink.call(lambda: ecs.create_service(
        cluster="odin", serviceName="api", taskDefinition="api", desiredCount=0,
    )), runtime)

    compiled = compile_policies_from_iam(stores, ENV)
    assert [(s.actions, s.resources) for s in compiled.get("api", [])] == [
        (("s3:GetObject", "s3:ListBucket"), ("uploads",)),
    ], f"a really-registered task definition's grants never reached the gateway. Got: {sorted(compiled)}"


async def test_a_real_create_function_produces_a_record_the_compiler_can_authorize(
    tmp_path: Path, sink, lambda_,
):
    """The REAL `lambda:CreateFunction` writer, with a fake `FunctionRuntime`
    standing in for the RIE container -- the same seam every other lambda unit
    test in this suite uses."""
    stores = SynthStores(tmp_path)
    await _role_with_grant(stores, "resizer-role")

    req = sink.call(lambda: lambda_.create_function(
        FunctionName="resizer", Role="arn:aws:iam::000000000000:role/resizer-role",
        Runtime="python3.12", Handler="lambda_function.lambda_handler",
        Code={"ZipFile": b"PK\x03\x04fake-zip-bytes"},
    ))
    await _lambda_answer(stores, req, FakeFunctionRuntime())

    compiled = compile_policies_from_iam(stores, ENV)
    assert [(s.actions, s.resources) for s in compiled.get("resizer", [])] == [
        (("s3:GetObject", "s3:ListBucket"), ("uploads",)),
    ], f"a really-created function's grants never reached the gateway. Got: {sorted(compiled)}"


SRC = Path(__file__).resolve().parents[2] / "src" / "odin" / "gateway"


def test_the_compiler_reads_the_tag_store_for_an_ec2_label():
    """The specific bug this file caught, pinned by shape rather than by prose.
    The instance record has no `tags` field — reading one found nothing, fell
    back to the instance id, and produced a principal no caller can present.

    A TEXT check, and that is now its whole job: it names the wrong SHAPE
    (`instance.get("tags")`) so a reviewer reading the diff sees why it is
    wrong. The guarantee that the label really arrives lives in
    `test_a_real_run_instances_produces_a_record_the_compiler_can_authorize`,
    which runs the writer.
    """
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
