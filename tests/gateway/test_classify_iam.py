"""V2a -- classify's iam branch, in test_classify_ec2.py's exact style: one
test per action asserting the exact (action, resource) tuple against a REAL
captured boto3 request. IAM shares EC2's resource convention (classify.py's
module docstring): the id param when the request carries one, else "*".
"""
from __future__ import annotations

from odin.gateway.classify import classify

from .conftest import split_url


def _classified(req):
    path, query = split_url(req.url)
    return classify("iam", req.method, path, query, req.headers, req.body)


def test_create_role_resolves_its_own_name(sink, iam):
    """Unlike EC2's server-generated ids, IAM resources are name-keyed by
    the CALLER -- CreateRole's request already carries RoleName, so (unlike
    ec2:CreateVpc) this resolves to a real resource, not "*"."""
    req = sink.call(lambda: iam.create_role(RoleName="lambda-role", AssumeRolePolicyDocument="{}"))
    assert _classified(req) == ("iam:CreateRole", "lambda-role")


def test_get_role_resolves_role_name(sink, iam):
    req = sink.call(lambda: iam.get_role(RoleName="lambda-role"))
    assert _classified(req) == ("iam:GetRole", "lambda-role")


def test_delete_role(sink, iam):
    req = sink.call(lambda: iam.delete_role(RoleName="lambda-role"))
    assert _classified(req) == ("iam:DeleteRole", "lambda-role")


def test_list_roles_has_no_id(sink, iam):
    req = sink.call(lambda: iam.list_roles())
    assert _classified(req) == ("iam:ListRoles", "*")


def test_put_role_policy_resolves_role_name(sink, iam):
    req = sink.call(lambda: iam.put_role_policy(RoleName="lambda-role", PolicyName="inline", PolicyDocument="{}"))
    assert _classified(req) == ("iam:PutRolePolicy", "lambda-role")


def test_attach_role_policy_resolves_role_name_over_policy_arn(sink, iam):
    """A request carrying BOTH a RoleName and a PolicyArn resolves to the
    role (the ordered candidate list in classify.py puts RoleName first) --
    the only principal driving iam calls in v1 is the OPERATOR, so which id
    wins is a fidelity choice, not an authorization one."""
    req = sink.call(lambda: iam.attach_role_policy(
        RoleName="lambda-role", PolicyArn="arn:aws:iam::000000000000:policy/basic",
    ))
    assert _classified(req) == ("iam:AttachRolePolicy", "lambda-role")


def test_create_policy_resolves_by_arn_absence_to_wildcard(sink, iam):
    req = sink.call(lambda: iam.create_policy(PolicyName="basic", PolicyDocument="{}"))
    assert _classified(req) == ("iam:CreatePolicy", "*")


def test_get_policy_resolves_policy_arn(sink, iam):
    req = sink.call(lambda: iam.get_policy(PolicyArn="arn:aws:iam::000000000000:policy/basic"))
    assert _classified(req) == ("iam:GetPolicy", "arn:aws:iam::000000000000:policy/basic")


def test_create_instance_profile_resolves_its_own_name(sink, iam):
    req = sink.call(lambda: iam.create_instance_profile(InstanceProfileName="lambda-profile"))
    assert _classified(req) == ("iam:CreateInstanceProfile", "lambda-profile")


def test_add_role_to_instance_profile_resolves_role_name(sink, iam):
    req = sink.call(lambda: iam.add_role_to_instance_profile(InstanceProfileName="lambda-profile", RoleName="lambda-role"))
    assert _classified(req) == ("iam:AddRoleToInstanceProfile", "lambda-role")
