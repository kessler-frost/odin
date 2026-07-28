"""Edge-compiled IAM policies for the odin gateway.

compile_policies turns a Stack's `kind == "iam"` edges (workload -> resource
node, carrying AWS verbs) into per-node Allow statements. evaluate is the
general matcher productionized from the research prototype
(.superpowers/sdd/research-iam-gateway.md §Q3): `*` wildcards (matching any
sequence, including across `/`) with every other character taken literally,
case-sensitive on both sides (odin controls both the compiler's casing and
the classifier's), explicit-deny-wins, default-deny. The compiler itself
never emits Deny in v1 -- Deny support is kept in the evaluator for future
edge-level deny authoring and is exercised by tests.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel

from odin.spec.models import Stack

Effect = Literal["Allow", "Deny"]


class Statement(BaseModel):
    model_config = {"frozen": True}
    effect: Effect = "Allow"
    actions: tuple[str, ...]
    resources: tuple[str, ...]


def _pattern(spec: str) -> re.Pattern[str]:
    """Compile an IAM-style wildcard spec: '*' matches any sequence
    (including across '/'); every other character is matched literally."""
    parts = spec.split("*")
    return re.compile("^" + ".*".join(re.escape(part) for part in parts) + "$")


def _matches_any(specs: tuple[str, ...], value: str) -> bool:
    return any(_pattern(spec).fullmatch(value) for spec in specs)


def compile_policies(stack: Stack) -> dict[str, list[Statement]]:
    """Compile each workload's `kind == "iam"` edges into Allow statements."""
    policies: dict[str, list[Statement]] = {}
    for edge in stack.edges:
        if edge.kind != "iam":
            continue
        statement = Statement(actions=edge.perms, resources=(edge.dst,))
        policies.setdefault(edge.src, []).append(statement)
    return policies


def evaluate(statements: list[Statement], action: str, resource: str) -> bool:
    """default-deny; an explicit Deny beats any Allow regardless of order."""
    allowed = False
    for statement in statements:
        if not (_matches_any(statement.actions, action) and _matches_any(statement.resources, resource)):
            continue
        if statement.effect == "Deny":
            return False
        allowed = True
    return allowed


# --- compiling from the APPLIED IaC ------------------------------------------
#
# Owner decision, 2026-07-28: a permission takes effect when it is APPLIED, and
# the applied Terraform is what says so. `compile_policies` above reads the
# Stack's edges; this reads the IAM records tofu created through the gateway.
#
# Two things made the switch worth doing. `iamctl.py` described itself as "a
# DOCUMENT STORE for Terraform compatibility ... nothing here ever compiles into
# a Statement" -- state that nothing consults, which is the decorative thing this
# project keeps removing. And a hand-written `aws_iam_role_policy` in an imported
# project was ignored outright: only canvas edges counted, so applying real IAM
# through odin granted nothing.
#
# The behaviour a user sees is UNCHANGED, which is worth stating plainly because
# it would be easy to claim otherwise: the reconciler already compiled from
# `store.get_stack(env)`, the APPLIED Stack, so an edge drawn and left unapplied
# never granted anything. Measured before this change: canvas with 1 edge,
# applied stack with 0, `evaluate` False.


def _role_name(arn_or_name: str) -> str:
    """`arn:aws:iam::…:role/web-role` or `web-role` -> `web-role`."""
    return (arn_or_name or "").rpartition("/")[2] or (arn_or_name or "")


def _statements_for_role(stores, env: str, role_name: str) -> list[Statement]:
    """Every Allow a role carries: its inline policies, plus the documents of any
    managed policies attached to it."""
    role = stores.iamctl.get(env, f"role:{role_name}")
    if role is None:
        return []
    documents = list((role.get("inline_policies") or {}).values())
    for arn in role.get("attached_policy_arns") or []:
        policy = stores.iamctl.get(env, f"policy:{arn}")
        if policy is not None and policy.get("document"):
            documents.append(policy["document"])

    out: list[Statement] = []
    for raw in documents:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        for statement in _as_list(parsed.get("Statement")):
            actions = tuple(_as_list(statement.get("Action")))
            resources = tuple(_as_list(statement.get("Resource")))
            if actions and resources:
                out.append(Statement(
                    effect="Deny" if statement.get("Effect") == "Deny" else "Allow",
                    actions=actions, resources=resources,
                ))
    return out


def _as_list(value) -> list:
    """IAM lets `Action`/`Resource`/`Statement` be a single value or a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _role_by_node(stores, env: str) -> dict[str, str]:
    """`{node label: role name}` for every workload that carries one.

    Read from each service's OWN record rather than from a naming convention,
    because `hcl.py` now states the link in the file: a lambda's `role`, a task
    definition's `task_role_arn`, and an instance's `iam_instance_profile`
    resolved through the profile that owns it.
    """
    roles: dict[str, str] = {}
    for key, fn in stores.lambdactl.items(env).items():
        if key.startswith("fn:") and fn.get("role"):
            roles[fn["function_name"]] = _role_name(fn["role"])

    # A service names its task definition by ARN (`.../task-definition/api:1`),
    # so the lookup is keyed on the `family:revision` that ARN ends with. Reading
    # a `task_definition` field instead matched nothing and denied every applied
    # permission on every ecs workload -- the same shape as the ec2 bug below.
    taskdefs = {
        f'{td.get("family")}:{td.get("revision")}': td
        for key, td in stores.ecsctl.items(env).items() if key.startswith("taskdef:")
    }
    for key, service in stores.ecsctl.items(env).items():
        if not key.startswith("service:"):
            continue
        taskdef = taskdefs.get(str(service.get("task_definition_arn", "")).rpartition("/")[2])
        arn = (taskdef or {}).get("task_role_arn")
        if arn:
            roles[service["service_name"]] = _role_name(arn)

    profiles = {
        key.partition(":")[2]: profile
        for key, profile in stores.iamctl.items(env).items()
        if key.startswith("instance-profile:")
    }
    for key, instance in stores.ec2compute.items(env).items():
        if not key.startswith("instance:"):
            continue
        profile = profiles.get(str(instance.get("iam_instance_profile") or ""))
        role_names = (profile or {}).get("roles") or []  # iamctl's own key
        # The node label lives in the SHARED tag store under `ec2:{id}`, not on
        # the instance record -- `_run_instances` writes it there and
        # `ec2compute` itself reads it back the same way. Reading a `tags` field
        # off the record instead found nothing and fell back to the instance id,
        # which is a principal name no caller can ever present: every applied
        # permission on an ec2 workload would have been denied.
        tags = stores.tags.get(env, f"ec2:{instance.get('instance_id')}", {})
        label = tags.get("odin:node")
        if role_names and label:
            roles[label] = _role_name(role_names[0])
    return roles


def compile_policies_from_iam(stores, env: str) -> dict[str, list[Statement]]:
    """`{workload: [Statement]}` from the IAM tofu actually applied.

    A workload with no role, or a role with no policy, gets nothing — which is
    default-deny, the same answer an unapplied canvas gives.
    """
    return {
        node: statements
        for node, role in _role_by_node(stores, env).items()
        if (statements := _statements_for_role(stores, env, role))
    }
