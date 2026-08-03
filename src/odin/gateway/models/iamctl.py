"""The gateway's IAM control-plane model (task V2a): roles, inline + managed
policies, attachments, and instance profiles, built to the captured Lambda+
IAM call surface in docs/superpowers/research/research-coverage.md §2d and
MiniStack's own IAM taxonomy (§2.6: "pure CRUD, no evaluation" -- role =
{trust doc, inline docs by name, attached ARNs}; policy = ARN-keyed
versioned documents; instance-profile = named role container) -- adopted as
a design, never as a dependency (NORTHSTAR directive 5).

THIS STORE IS THE AUTHORIZATION SOURCE (changed in v0.8.12). It used to be a
document store for Terraform compatibility only, while `evaluate()` read a
policy map compiled straight from the canvas edges -- which meant a permission
drawn on the canvas took effect without an apply, and the Terraform odin
generated described an IAM posture the gateway was not using.

Now a drawn permission is compiled into a real `aws_iam_role_policy` by
`iac/hcl.py`, applied by tofu through the gateway like any other resource,
and landed HERE. `gateway/policy.py::compile_policies_from_iam` reads these
records back -- each workload's role via its own service record (a lambda's
`role`, a task definition's `task_role_arn`, an instance's
`iam_instance_profile`), then that role's inline and attached documents -- and
hands the result to `evaluate()`. So a permission takes effect when it is
applied, not when it is drawn, and an unapplied edge grants nothing.

Like ec2net.py, IAM has NO backing container: this module is the WHOLE
answer for every `iam:*` action the gateway classifies. IAM shares EC2's
"OPERATOR is the only caller" reasoning (classify.py's `_classify_iam`):
extraction only needs to never return None.

Model decisions, each traced to the research:
- CreateRole/CreatePolicy mint AWS-shaped ids (`AROA`/`ANPA`/`AIPA` +
  17 uppercase hex chars, no hyphen -- IAM ids, unlike EC2's, never use one)
  and store the role/policy by NAME (not id): IAM's own API is name-keyed
  (GetRole/PutRolePolicy/etc. all take RoleName), so the id is carried on
  the record for display only.
- AssumeRolePolicyDocument/PolicyDocument round-trip AS RAW JSON internally
  (parse_qsl already URL-decodes the form body once) and are re-URL-encoded
  only when serialized onto the wire (`policyDocumentType` shapes) --
  verified against botocore's own IAM model: every request/response member
  of that shape carries no `serialization` override, so the query-protocol
  default (the PascalCase member name as the wire tag) applies throughout;
  IAM does NOT lowerCamelCase its tags the way EC2 does.
- DeleteRole/DeletePolicy/DeleteInstanceProfile enforce real AWS's
  DeleteConflict semantics: a role can't be deleted while it still has
  inline policies, attached managed policies, or instance-profile
  membership; a policy can't be deleted while any role still has it
  attached; an instance profile can't be deleted while it still contains a
  role. CreateRole/CreatePolicy on an existing name is EntityAlreadyExists
  (unlike EC2's randomly-minted ids, IAM's name IS the identity, so this
  collision is real).
- AttachRolePolicy/DetachRolePolicy are idempotent by content (attaching an
  already-attached arn is a no-op, not a duplicate) -- mirrors ec2net's
  Authorize/Revoke idempotence philosophy for the same wire-level reason
  (a `tofu apply` retry must never double-count).
- Every wire tag was verified against botocore's own IAM service model
  (query protocol, the SAME generic envelope shape as SNS) and every
  response round-trips through botocore's `create_parser("query")` in
  tests/gateway/test_iamctl.py, exactly S1/V1a's test method.

Persistence: one `JsonStore` at `.odin/{env}/gateway/iamctl.json`
(`stores.iamctl`), flat keys `"role:{name}"` / `"policy:{arn}"` /
`"instance-profile:{name}"`. Tags share the SAME shared `stores.tags` store
ec2net/synth use, keyed `"iam:{arn}"` (a role's or policy's own arn --
distinct namespace from ec2's `"ec2:{resource_id}"` and sqs/sns/dynamodb's
`"{service}:{resource}"`, so no collision is even possible).
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, unquote
from xml.sax.saxutils import escape

from starlette.responses import Response

from odin.aws.backings import ACCOUNT
from odin.gateway import errors
from odin.gateway.stores import SynthStores

_IAM_NS = "https://iam.amazonaws.com/doc/2010-05-08/"
_REQUEST_ID = "00000000-0000-0000-0000-000000000000"
_DEFAULT_MAX_SESSION_DURATION = "3600"
_POLICY_VERSION_ID = "v1"  # v1 never models CreatePolicyVersion -- one version, always the default


def _mint(prefix: str) -> str:
    """An AWS-shaped IAM id: `PREFIX` + 17 uppercase hex chars, NO hyphen
    (IAM ids, unlike EC2's `vpc-xxx`, are one unbroken alnum run)."""
    return f"{prefix}{secrets.token_hex(9)[:17].upper()}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- request parsing: IAM's query-protocol serialization ---------------------
# IAM is genuine "query" protocol (the SAME shape SNS uses -- `prefix.member.
# N.Field`), unlike EC2's own distinct `Prefix.N.Rest` convention. Kept
# self-contained (not imported from synth.py) to avoid a circular import --
# synth.py itself imports this module.


def _params(body: bytes) -> dict[str, str]:
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


def _parse_tags(params: dict[str, str]) -> dict[str, str]:
    """`Tags.member.N.Key` / `Tags.member.N.Value` -> a flat tag dict."""
    indexed: dict[int, dict[str, str]] = {}
    for key, value in params.items():
        if not key.startswith("Tags.member."):
            continue
        _tags, _member, index, field = key.split(".", 3)
        indexed.setdefault(int(index), {})[field] = value
    return {item["Key"]: item.get("Value", "") for item in indexed.values() if "Key" in item}


def _parse_tag_keys(params: dict[str, str]) -> list[str]:
    """`TagKeys.member.N` -> the list of keys to remove."""
    indexed: dict[int, str] = {}
    for key, value in params.items():
        if key.startswith("TagKeys.member."):
            indexed[int(key.rsplit(".", 1)[-1])] = value
    return [indexed[i] for i in sorted(indexed)]


def _tags_xml(tags: dict[str, str]) -> str:
    members = "".join(f"<member><Key>{escape(k)}</Key><Value>{escape(v)}</Value></member>" for k, v in tags.items())
    return f"<Tags>{members}</Tags>"


# --- store access --------------------------------------------------------------


def _key(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


def _role(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.iamctl.get(env, _key("role", name))


def _policy(stores: SynthStores, env: str, arn: str) -> dict | None:
    return stores.iamctl.get(env, _key("policy", arn))


def _instance_profile(stores: SynthStores, env: str, name: str) -> dict | None:
    return stores.iamctl.get(env, _key("instance-profile", name))


def _roles(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.iamctl.items(env).items() if k.startswith("role:")]


def _instance_profiles(stores: SynthStores, env: str) -> list[dict]:
    return [v for k, v in stores.iamctl.items(env).items() if k.startswith("instance-profile:")]


def _tags_for(stores: SynthStores, env: str, arn: str) -> dict[str, str]:
    return stores.tags.get(env, f"iam:{arn}", {})


def _set_tags(stores: SynthStores, env: str, arn: str, tags: dict[str, str]) -> None:
    stores.tags.set(env, f"iam:{arn}", tags)


# --- errors ----------------------------------------------------------------


def _no_such_entity(message: str) -> Response:
    return errors.synth_error("iam", "NoSuchEntity", message, 404)


def _entity_already_exists(message: str) -> Response:
    return errors.synth_error("iam", "EntityAlreadyExists", message, 409)


def _delete_conflict(message: str) -> Response:
    return errors.synth_error("iam", "DeleteConflict", message, 409)


# --- wire building: IAM-protocol XML ------------------------------------------


def _response(op: str, inner: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{op}Response xmlns="{_IAM_NS}"><{op}Result>{inner}</{op}Result>'
        f"<ResponseMetadata><RequestId>{_REQUEST_ID}</RequestId></ResponseMetadata>"
        f"</{op}Response>"
    )
    return Response(xml, media_type="text/xml")


def _empty_response(op: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<{op}Response xmlns="{_IAM_NS}">'
        f"<ResponseMetadata><RequestId>{_REQUEST_ID}</RequestId></ResponseMetadata>"
        f"</{op}Response>"
    )
    return Response(xml, media_type="text/xml")


def _role_xml(role: dict, tags: dict[str, str]) -> str:
    doc = quote(role["assume_role_policy_document"], safe="")
    parts = [
        f"<Path>{escape(role['path'])}</Path>",
        f"<RoleName>{escape(role['role_name'])}</RoleName>",
        f"<RoleId>{role['role_id']}</RoleId>",
        f"<Arn>{role['arn']}</Arn>",
        f"<CreateDate>{role['create_date']}</CreateDate>",
        f"<AssumeRolePolicyDocument>{doc}</AssumeRolePolicyDocument>",
        f"<MaxSessionDuration>{role['max_session_duration']}</MaxSessionDuration>",
    ]
    if role.get("description"):
        parts.append(f"<Description>{escape(role['description'])}</Description>")
    parts.append(_tags_xml(tags))
    return "".join(parts)


def _policy_xml(policy: dict) -> str:
    parts = [
        f"<PolicyName>{escape(policy['policy_name'])}</PolicyName>",
        f"<PolicyId>{policy['policy_id']}</PolicyId>",
        f"<Arn>{policy['arn']}</Arn>",
        f"<Path>{escape(policy['path'])}</Path>",
        f"<DefaultVersionId>{policy['default_version_id']}</DefaultVersionId>",
        f"<AttachmentCount>{policy['attachment_count']}</AttachmentCount>",
        "<PermissionsBoundaryUsageCount>0</PermissionsBoundaryUsageCount>",
        "<IsAttachable>true</IsAttachable>",
        f"<CreateDate>{policy['create_date']}</CreateDate>",
        f"<UpdateDate>{policy['create_date']}</UpdateDate>",
    ]
    if policy.get("description"):
        parts.append(f"<Description>{escape(policy['description'])}</Description>")
    return "".join(parts)


def _instance_profile_xml(profile: dict, stores: SynthStores, env: str) -> str:
    roles_xml = "".join(
        f"<member>{_role_xml(role, _tags_for(stores, env, role['arn']))}</member>"
        for name in profile["roles"] if (role := _role(stores, env, name)) is not None
    )
    parts = [
        f"<Path>{escape(profile['path'])}</Path>",
        f"<InstanceProfileName>{escape(profile['instance_profile_name'])}</InstanceProfileName>",
        f"<InstanceProfileId>{profile['instance_profile_id']}</InstanceProfileId>",
        f"<Arn>{profile['arn']}</Arn>",
        f"<CreateDate>{profile['create_date']}</CreateDate>",
        f"<Roles>{roles_xml}</Roles>",
        _tags_xml(_tags_for(stores, env, profile["arn"])),
    ]
    return "".join(parts)


# --- Role: create / get / delete / list ---------------------------------------


def _create_role(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("RoleName", "")
    if _role(stores, env, name) is not None:
        return _entity_already_exists(f"Role with name {name} already exists.")
    path = params.get("Path") or "/"
    role = {
        "role_name": name,
        "path": path,
        "role_id": _mint("AROA"),
        "arn": f"arn:aws:iam::{ACCOUNT}:role{path}{name}",
        "create_date": _now(),
        # Already single-URL-decoded by parse_qsl; stored raw, re-encoded on the wire.
        "assume_role_policy_document": unquote(params.get("AssumeRolePolicyDocument", "")),
        "description": params.get("Description", ""),
        "max_session_duration": int(params.get("MaxSessionDuration", _DEFAULT_MAX_SESSION_DURATION)),
        "inline_policies": {},
        "attached_policy_arns": [],
    }
    stores.iamctl.set(env, _key("role", name), role)
    tags = _parse_tags(params)
    _set_tags(stores, env, role["arn"], tags)
    return _response("CreateRole", f"<Role>{_role_xml(role, tags)}</Role>")


def _get_role(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("RoleName", "")
    role = _role(stores, env, name)
    if role is None:
        return _no_such_entity(f"The role with name {name} cannot be found.")
    return _response("GetRole", f"<Role>{_role_xml(role, _tags_for(stores, env, role['arn']))}</Role>")


def _role_in_any_instance_profile(stores: SynthStores, env: str, name: str) -> bool:
    return any(name in p["roles"] for p in _instance_profiles(stores, env))


def _delete_role(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("RoleName", "")
    role = _role(stores, env, name)
    if role is None:
        return _no_such_entity(f"The role with name {name} cannot be found.")
    if role["inline_policies"] or role["attached_policy_arns"] or _role_in_any_instance_profile(stores, env, name):
        return _delete_conflict(f"Cannot delete entity, must detach all policies/instance profiles first: {name}")
    stores.iamctl.delete(env, _key("role", name))
    _set_tags(stores, env, role["arn"], {})
    return _empty_response("DeleteRole")


def _list_roles(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    prefix = params.get("PathPrefix") or "/"
    roles = [r for r in _roles(stores, env) if r["path"].startswith(prefix)]
    items = "".join(f"<member>{_role_xml(r, _tags_for(stores, env, r['arn']))}</member>" for r in roles)
    return _response("ListRoles", f"<Roles>{items}</Roles><IsTruncated>false</IsTruncated>")


# --- Inline role policies -------------------------------------------------------


def _put_role_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name = params.get("RoleName", "")
    role = _role(stores, env, role_name)
    if role is None:
        return _no_such_entity(f"The role with name {role_name} cannot be found.")
    policy_name = params.get("PolicyName", "")
    role["inline_policies"][policy_name] = unquote(params.get("PolicyDocument", ""))
    stores.iamctl.set(env, _key("role", role_name), role)
    return _empty_response("PutRolePolicy")


def _get_role_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name, policy_name = params.get("RoleName", ""), params.get("PolicyName", "")
    role = _role(stores, env, role_name)
    document = role["inline_policies"].get(policy_name) if role else None
    if document is None:
        return _no_such_entity(f"The role policy {policy_name} on role {role_name} cannot be found.")
    doc = quote(document, safe="")
    inner = f"<RoleName>{escape(role_name)}</RoleName><PolicyName>{escape(policy_name)}</PolicyName><PolicyDocument>{doc}</PolicyDocument>"
    return _response("GetRolePolicy", inner)


def _delete_role_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name, policy_name = params.get("RoleName", ""), params.get("PolicyName", "")
    role = _role(stores, env, role_name)
    if role is None or policy_name not in role["inline_policies"]:
        return _no_such_entity(f"The role policy {policy_name} on role {role_name} cannot be found.")
    del role["inline_policies"][policy_name]
    stores.iamctl.set(env, _key("role", role_name), role)
    return _empty_response("DeleteRolePolicy")


def _list_role_policies(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name = params.get("RoleName", "")
    role = _role(stores, env, role_name)
    if role is None:
        return _no_such_entity(f"The role with name {role_name} cannot be found.")
    items = "".join(f"<member>{escape(n)}</member>" for n in role["inline_policies"])
    return _response("ListRolePolicies", f"<PolicyNames>{items}</PolicyNames><IsTruncated>false</IsTruncated>")


# --- Managed (customer) policies ------------------------------------------------


def _create_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("PolicyName", "")
    path = params.get("Path") or "/"
    arn = f"arn:aws:iam::{ACCOUNT}:policy{path}{name}"
    if _policy(stores, env, arn) is not None:
        return _entity_already_exists(f"A policy called {name} already exists.")
    now = _now()
    policy = {
        "policy_name": name,
        "path": path,
        "policy_id": _mint("ANPA"),
        "arn": arn,
        "default_version_id": _POLICY_VERSION_ID,
        "document": unquote(params.get("PolicyDocument", "")),
        "attachment_count": 0,
        "create_date": now,
        "description": params.get("Description", ""),
    }
    stores.iamctl.set(env, _key("policy", arn), policy)
    _set_tags(stores, env, arn, _parse_tags(params))
    return _response("CreatePolicy", f"<Policy>{_policy_xml(policy)}</Policy>")


def _get_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn = params.get("PolicyArn", "")
    policy = _policy(stores, env, arn)
    if policy is None:
        return _no_such_entity(f"Policy {arn} does not exist.")
    return _response("GetPolicy", f"<Policy>{_policy_xml(policy)}</Policy>")


def _get_policy_version(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn, version_id = params.get("PolicyArn", ""), params.get("VersionId", "")
    policy = _policy(stores, env, arn)
    if policy is None or version_id != policy["default_version_id"]:
        return _no_such_entity(f"Policy {arn} version {version_id} does not exist.")
    doc = quote(policy["document"], safe="")
    inner = (
        f"<Document>{doc}</Document><VersionId>{policy['default_version_id']}</VersionId>"
        f"<IsDefaultVersion>true</IsDefaultVersion><CreateDate>{policy['create_date']}</CreateDate>"
    )
    return _response("GetPolicyVersion", f"<PolicyVersion>{inner}</PolicyVersion>")


def _list_policy_versions(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn = params.get("PolicyArn", "")
    policy = _policy(stores, env, arn)
    if policy is None:
        return _no_such_entity(f"Policy {arn} does not exist.")
    inner = (
        f"<VersionId>{policy['default_version_id']}</VersionId><IsDefaultVersion>true</IsDefaultVersion>"
        f"<CreateDate>{policy['create_date']}</CreateDate>"
    )
    return _response("ListPolicyVersions", f"<Versions><member>{inner}</member></Versions><IsTruncated>false</IsTruncated>")


def _delete_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn = params.get("PolicyArn", "")
    policy = _policy(stores, env, arn)
    if policy is None:
        return _no_such_entity(f"Policy {arn} does not exist.")
    if policy["attachment_count"] > 0:
        return _delete_conflict(f"Cannot delete a policy attached to a role: {arn}")
    stores.iamctl.delete(env, _key("policy", arn))
    _set_tags(stores, env, arn, {})
    return _empty_response("DeletePolicy")


# --- Attachments -----------------------------------------------------------------


def _attach_role_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name, arn = params.get("RoleName", ""), params.get("PolicyArn", "")
    role = _role(stores, env, role_name)
    if role is None:
        return _no_such_entity(f"The role with name {role_name} cannot be found.")
    if arn not in role["attached_policy_arns"]:
        role["attached_policy_arns"].append(arn)
        stores.iamctl.set(env, _key("role", role_name), role)
        policy = _policy(stores, env, arn)
        if policy is not None:  # AWS-managed arns (not in our store) are tolerated but not counted
            policy["attachment_count"] += 1
            stores.iamctl.set(env, _key("policy", arn), policy)
    return _empty_response("AttachRolePolicy")


def _detach_role_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name, arn = params.get("RoleName", ""), params.get("PolicyArn", "")
    role = _role(stores, env, role_name)
    if role is None:
        return _no_such_entity(f"The role with name {role_name} cannot be found.")
    if arn in role["attached_policy_arns"]:
        role["attached_policy_arns"].remove(arn)
        stores.iamctl.set(env, _key("role", role_name), role)
        policy = _policy(stores, env, arn)
        if policy is not None:
            policy["attachment_count"] = max(0, policy["attachment_count"] - 1)
            stores.iamctl.set(env, _key("policy", arn), policy)
    return _empty_response("DetachRolePolicy")


def _list_attached_role_policies(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name = params.get("RoleName", "")
    role = _role(stores, env, role_name)
    if role is None:
        return _no_such_entity(f"The role with name {role_name} cannot be found.")
    items = []
    for arn in role["attached_policy_arns"]:
        policy = _policy(stores, env, arn)
        policy_name = policy["policy_name"] if policy else arn.rsplit("/", 1)[-1]
        items.append(f"<member><PolicyName>{escape(policy_name)}</PolicyName><PolicyArn>{arn}</PolicyArn></member>")
    return _response("ListAttachedRolePolicies", f"<AttachedPolicies>{''.join(items)}</AttachedPolicies><IsTruncated>false</IsTruncated>")


# --- Instance profiles -------------------------------------------------------------


def _create_instance_profile(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("InstanceProfileName", "")
    if _instance_profile(stores, env, name) is not None:
        return _entity_already_exists(f"Instance Profile {name} already exists.")
    path = params.get("Path") or "/"
    profile = {
        "instance_profile_name": name,
        "path": path,
        "instance_profile_id": _mint("AIPA"),
        "arn": f"arn:aws:iam::{ACCOUNT}:instance-profile{path}{name}",
        "create_date": _now(),
        "roles": [],
    }
    stores.iamctl.set(env, _key("instance-profile", name), profile)
    _set_tags(stores, env, profile["arn"], _parse_tags(params))
    return _response("CreateInstanceProfile", f"<InstanceProfile>{_instance_profile_xml(profile, stores, env)}</InstanceProfile>")


def _get_instance_profile(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("InstanceProfileName", "")
    profile = _instance_profile(stores, env, name)
    if profile is None:
        return _no_such_entity(f"Instance Profile {name} cannot be found.")
    return _response("GetInstanceProfile", f"<InstanceProfile>{_instance_profile_xml(profile, stores, env)}</InstanceProfile>")


def _delete_instance_profile(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    name = params.get("InstanceProfileName", "")
    profile = _instance_profile(stores, env, name)
    if profile is None:
        return _no_such_entity(f"Instance Profile {name} cannot be found.")
    if profile["roles"]:
        return _delete_conflict(f"Cannot delete entity, must remove roles from instance profile first: {name}")
    stores.iamctl.delete(env, _key("instance-profile", name))
    _set_tags(stores, env, profile["arn"], {})
    return _empty_response("DeleteInstanceProfile")


def _add_role_to_instance_profile(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    profile_name, role_name = params.get("InstanceProfileName", ""), params.get("RoleName", "")
    profile = _instance_profile(stores, env, profile_name)
    if profile is None:
        return _no_such_entity(f"Instance Profile {profile_name} cannot be found.")
    if _role(stores, env, role_name) is None:
        return _no_such_entity(f"The role with name {role_name} cannot be found.")
    if role_name not in profile["roles"]:
        profile["roles"].append(role_name)
        stores.iamctl.set(env, _key("instance-profile", profile_name), profile)
    return _empty_response("AddRoleToInstanceProfile")


def _remove_role_from_instance_profile(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    profile_name, role_name = params.get("InstanceProfileName", ""), params.get("RoleName", "")
    profile = _instance_profile(stores, env, profile_name)
    if profile is None:
        return _no_such_entity(f"Instance Profile {profile_name} cannot be found.")
    if role_name in profile["roles"]:
        profile["roles"].remove(role_name)
        stores.iamctl.set(env, _key("instance-profile", profile_name), profile)
    return _empty_response("RemoveRoleFromInstanceProfile")


def _list_instance_profiles_for_role(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role_name = params.get("RoleName", "")
    profiles = [p for p in _instance_profiles(stores, env) if role_name in p["roles"]]
    items = "".join(f"<member>{_instance_profile_xml(p, stores, env)}</member>" for p in profiles)
    return _response("ListInstanceProfilesForRole", f"<InstanceProfiles>{items}</InstanceProfiles><IsTruncated>false</IsTruncated>")


# --- Tags (per-resource Tag/Untag/List, shared stores.tags) ------------------------


def _tag_role(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role = _role(stores, env, params.get("RoleName", ""))
    if role is None:
        return _no_such_entity(f"The role with name {params.get('RoleName', '')} cannot be found.")
    tags = {**_tags_for(stores, env, role["arn"]), **_parse_tags(params)}
    _set_tags(stores, env, role["arn"], tags)
    return _empty_response("TagRole")


def _untag_role(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role = _role(stores, env, params.get("RoleName", ""))
    if role is None:
        return _no_such_entity(f"The role with name {params.get('RoleName', '')} cannot be found.")
    tags = {k: v for k, v in _tags_for(stores, env, role["arn"]).items() if k not in _parse_tag_keys(params)}
    _set_tags(stores, env, role["arn"], tags)
    return _empty_response("UntagRole")


def _list_role_tags(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    role = _role(stores, env, params.get("RoleName", ""))
    if role is None:
        return _no_such_entity(f"The role with name {params.get('RoleName', '')} cannot be found.")
    tags = _tags_for(stores, env, role["arn"])
    return _response("ListRoleTags", f"{_tags_xml(tags)}<IsTruncated>false</IsTruncated>")


def _tag_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn = params.get("PolicyArn", "")
    if _policy(stores, env, arn) is None:
        return _no_such_entity(f"Policy {arn} does not exist.")
    tags = {**_tags_for(stores, env, arn), **_parse_tags(params)}
    _set_tags(stores, env, arn, tags)
    return _empty_response("TagPolicy")


def _untag_policy(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn = params.get("PolicyArn", "")
    if _policy(stores, env, arn) is None:
        return _no_such_entity(f"Policy {arn} does not exist.")
    tags = {k: v for k, v in _tags_for(stores, env, arn).items() if k not in _parse_tag_keys(params)}
    _set_tags(stores, env, arn, tags)
    return _empty_response("UntagPolicy")


def _list_policy_tags(params: dict[str, str], env: str, stores: SynthStores) -> Response:
    arn = params.get("PolicyArn", "")
    if _policy(stores, env, arn) is None:
        return _no_such_entity(f"Policy {arn} does not exist.")
    return _response("ListPolicyTags", f"{_tags_xml(_tags_for(stores, env, arn))}<IsTruncated>false</IsTruncated>")


# --- dispatch --------------------------------------------------------------------


_Handler = Callable[[dict[str, str], str, SynthStores], Response]

_HANDLERS: dict[str, _Handler] = {
    "CreateRole": _create_role,
    "GetRole": _get_role,
    "DeleteRole": _delete_role,
    "ListRoles": _list_roles,
    "PutRolePolicy": _put_role_policy,
    "GetRolePolicy": _get_role_policy,
    "DeleteRolePolicy": _delete_role_policy,
    "ListRolePolicies": _list_role_policies,
    "CreatePolicy": _create_policy,
    "GetPolicy": _get_policy,
    "GetPolicyVersion": _get_policy_version,
    "ListPolicyVersions": _list_policy_versions,
    "DeletePolicy": _delete_policy,
    "AttachRolePolicy": _attach_role_policy,
    "DetachRolePolicy": _detach_role_policy,
    "ListAttachedRolePolicies": _list_attached_role_policies,
    "CreateInstanceProfile": _create_instance_profile,
    "GetInstanceProfile": _get_instance_profile,
    "DeleteInstanceProfile": _delete_instance_profile,
    "AddRoleToInstanceProfile": _add_role_to_instance_profile,
    "RemoveRoleFromInstanceProfile": _remove_role_from_instance_profile,
    "ListInstanceProfilesForRole": _list_instance_profiles_for_role,
    "TagRole": _tag_role,
    "UntagRole": _untag_role,
    "ListRoleTags": _list_role_tags,
    "TagPolicy": _tag_policy,
    "UntagPolicy": _untag_policy,
    "ListPolicyTags": _list_policy_tags,
}


async def pure_answer(action: str, resource: str, env: str, body: bytes, stores: SynthStores, now: float) -> Response | None:
    """The whole IAM answer -- same signature/contract as ec2net.pure_answer:
    IAM has no backing to fall through to, so an unmodeled action still gets
    a protocol-correct error rather than a 503 (real AWS's own
    `NoSuchEntity`-adjacent code doesn't fit an unknown ACTION, so this uses
    the same status IAM would for a malformed request)."""
    # A coroutine with no `await` inside, deliberately (v0.7.7): this model's
    # substrate is odin's own JSON sidecar, so nothing here blocks and nothing
    # here needs to yield. It is `async def` only so `synth.pure_answer` has
    # ONE contract to dispatch against -- `await <model>.pure_answer(...)` for
    # every model, with no per-service branch that could return an un-awaited
    # coroutine as if it were a Response.
    handler = _HANDLERS.get(action.removeprefix("iam:"))
    if handler is None:
        return errors.synth_error("iam", "InvalidAction", f"The action {action} is not valid.", 400)
    return handler(_params(body), env, stores)
