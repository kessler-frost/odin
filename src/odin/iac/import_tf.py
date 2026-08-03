"""S4 — TF import: Terraform -> canvas, the reverse of S3b's `translate()`.

Two modes (research-verified, docs/superpowers/research/research-tofu-provider.md
§5 "Import direction"):

(a) **deterministic** (`parse_hcl*`): parse an existing project's HCL for every
    supported resource type (`_KIND` below, plus the two COMPANION types that
    fold into a node rather than becoming one: aws_sns_topic_subscription ->
    an edge, aws_secretsmanager_secret_version -> its secret node's value)
    into canvas nodes+edges. Unsupported types are LISTED, never dropped
    (northstar directive 5). W2.5 adds two more companions of the same shape:
    aws_lb_target_group + aws_lb_listener fold onto their aws_lb's `alb` node
    (one canvas node, three tf resources -- the inverse of hcl.py's own alb
    expansion, so generate -> import -> generate round-trips).

(b) **live-state import** (`import_live`): resources already exist in the
    env's backings (created out-of-band, or by a prior tofu apply) but were
    never authored as canvas nodes. Generates `import {}` blocks and runs
    `tofu plan -generate-config-out` in a throwaway scratch project against
    the SAME gateway + env-var injection every tofu invocation in Simulate
    uses, then parses the generated HCL with mode (a). The plan command's own
    exit code is not trusted as a success signal — research verified
    OpenTofu's generate-config-out reports a "Conflicting configuration
    arguments" error (a known quirk: it emits mutually-exclusive
    bucket/bucket_prefix and tags/tags_all) even though the file it wrote is
    complete and parses fine; since this file is only ever read back in here
    (never re-applied), that quirk doesn't matter for canvas hydration.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from odin.aws.backings import ACCOUNT, REGION
from odin.gateway.policy import arn_label
from odin.iac import hcl
from odin.iac.hcl import sanitize_name as sanitize
from odin.simulate import workspace as workspace_mod
from odin.simulate.runner import PLUGIN_CACHE_DIR
from odin.spec.translate import ALB_TARGET, DNS_RECORD, FILE_SYSTEM_MOUNT, VOLUME_ATTACHMENT
from odin.util import reap

_GRID_STEP = 220

# aws_* resource type -> canvas node kind. Anything else is unsupported.
_KIND = {
    "aws_s3_bucket": "s3",
    "aws_sqs_queue": "sqs",
    "aws_sns_topic": "sns",
    "aws_dynamodb_table": "dynamodb",
    "aws_iam_role": "iam_role",
    "aws_cloudwatch_log_group": "logs",
    "aws_secretsmanager_secret": "secret",
    "aws_ssm_parameter": "ssm",
    "aws_elasticache_cluster": "elasticache",
    "aws_db_instance": "rds",
    "aws_lb": "alb",
    # v0.7.1: the two CONTAINER kinds. They were reported unsupported, which
    # was honest but left the import asymmetric with generate -- and an
    # `aws_lb` needs a subnet AND a vpc on the canvas, so an imported load
    # balancer could never be applied (field test U2). Importing them is what
    # makes containment reconstructible from the source's own `vpc_id`/
    # `subnets` references.
    "aws_vpc": "vpc",
    "aws_subnet": "subnet",
    # v0.8.4: the two that made a NETWORK canvas un-round-trippable. An
    # `aws_security_group` is where the interesting half lives -- its rules are
    # what the Nebula firewall actually compiles from, so losing them on import
    # loses the security posture, not a label. `aws_ecr_repository` is here
    # because it is a one-argument resource that was being reported unsupported
    # for no reason other than nobody having written the line.
    "aws_security_group": "sg",
    "aws_ecr_repository": "ecr",
    # v0.8.4: an `aws_instance` is a real Lima VM to odin, and it has NO `name`
    # argument -- its label comes from the `odin:node` tag `_label` already falls
    # back to. Its optional SSH key lives on a companion `aws_key_pair`, folded
    # back on below rather than becoming a node of its own.
    "aws_instance": "ec2",
    # v0.8.4: one canvas `ecs` node is THREE tf resources (service + task
    # definition + the shared cluster), so only the SERVICE becomes a node --
    # the other two fold on, the same shape as the alb trio.
    "aws_ecs_service": "ecs",
    # v0.8.4, the last kind. A function's CONFIG is all in the HCL; its CODE is
    # in a zip beside `main.tf`, so `parse_hcl_dir` recovers it and
    # `parse_hcl_text` cannot -- see `_stamp_lambda`, which says so rather than
    # letting odin's default payload pass for the user's own function.
    "aws_lambda_function": "lambda",
    # v0.8.18: an `aws_ebs_volume` has NO `name` argument (exactly like
    # `aws_instance`), so its label comes from the `odin:node` tag `_label`
    # already falls back to -- which is why it has no `_NAME_ATTR` entry. Its
    # attachment to an instance is a companion `aws_volume_attachment`, folded
    # back into a canvas EDGE below rather than becoming a node.
    "aws_ebs_volume": "ebs",
    # v0.8.19: the DNS kind, and it needs a `_NAME_ATTR` entry where the two
    # kinds above it deliberately have none. `aws_instance`/`aws_ebs_volume`
    # have NO `name` argument at all, so their labels can only come from the
    # `odin:node` tag; an `aws_route53_zone` DOES have one, and the zone's name
    # IS its canvas label -- a route53 node is drawn as the domain itself
    # (`hcl.py::_route53` emits `quote(res.id)`). So the name argument is read
    # first and the tag stays the fallback, exactly as for every other named
    # kind. Its records are a companion `aws_route53_record`, folded back into a
    # canvas EDGE below rather than becoming a node.
    "aws_route53_zone": "route53",
    # v0.8.19: a shared file system. Its label comes from `creation_token` (a
    # `_NAME_ATTR` entry), NOT from the `odin:node` tag `ebs`/`ec2` fall back to,
    # and the difference is only visible on a project odin did not generate.
    #
    # `aws_efs_file_system` has no `name` argument at all -- checked against the
    # provider schema, the arguments are creation_token / encrypted / kms_key_id /
    # performance_mode / throughput_mode / lifecycle_policy / tags -- so
    # `creation_token` is the only name-shaped one, and it is the one `hcl.py`
    # writes the canvas label into. That much would work either way, because
    # odin's own output carries BOTH the token and the tag and they agree.
    #
    # The deciding reason is `_renamed_by_import`, which only fires for a type
    # that HAS a `_NAME_ATTR`: without the entry, a hand-authored
    # `creation_token = "${var.env}-data"` would fall through to the bare HCL
    # resource name and the regenerated file system would be created under a
    # DIFFERENT token, silently. With it, odin's own output round-trips without a
    # word (the literal equals the label) and a computed one is reported CHANGED.
    #
    # Its mounts come back from the CONSUMER side -- a task definition's
    # `volume`/`mountPoints` pair and a lambda's `file_system_config` -- as canvas
    # EDGES, below. The `aws_efs_access_point` those lambda mounts point through
    # is a COMPANION and never a node.
    "aws_efs_file_system": "efs",
    # v0.8.19: an HTTP API. Its stage, integrations and routes are COMPANIONS
    # (`_APIGW_COMPANION_TYPES` below) -- the stage and the routes fold away
    # entirely, and each integration becomes a canvas EDGE, which is what makes
    # one canvas node survive a round trip as one canvas node.
    "aws_apigatewayv2_api": "apigateway",
}
# Neither of these becomes a node. The task definition folds onto its service
# (image/port/memory/cpu live there, not on the service); the cluster is a
# singleton odin always emits exactly one of, named "odin", so importing it would
# invent a node the canvas has no kind for.
_ECS_COMPANION_TYPES = ("aws_ecs_task_definition", "aws_ecs_cluster")

# v0.8.11: a drawn IAM edge is emitted as an `aws_iam_role_policy` on the
# workload's role, so importing one reconstructs the EDGE rather than becoming a
# node. Before this, generate -> import dropped every permission and reported
# nothing, which is the round-trip loss the emission was added to fix; importing
# the policy back is the other half of that fix.
_IAM_POLICY_TYPE = "aws_iam_role_policy"
# W2.5: the two OTHER types an `alb` canvas node expands to. Neither becomes a
# node of its own -- they fold ONTO the alb node the same way
# aws_secretsmanager_secret_version folds onto its secret, which is what makes
# generate -> import -> generate round-trip instead of multiplying resources.
_ALB_COMPANION_TYPES = ("aws_lb_target_group", "aws_lb_listener")
# v0.8.19: an access point folds away exactly as an ecs CLUSTER does -- it never
# becomes a node and it is never reported unsupported when something claims it.
#
# It exists at all because `aws_lambda_function.file_system_config.arn` is
# documented as an ACCESS POINT arn (botocore's own `FileSystemArn` pattern ends
# `access-point/fsap-[a-f0-9]{17}$`), so a file-system arn there is a project
# real AWS rejects. On the way back it is the JOIN: a lambda's mount names the
# access point, the access point names the file system, and only then is there an
# efs node to draw the edge to. Keyed by HCL resource NAME, like `aws_key_pair`,
# because that is what the lambda's interpolation names.
_EFS_ACCESS_POINT_TYPE = "aws_efs_access_point"
# v0.8.19: the three OTHER types an `apigateway` canvas node expands to.
#
# THE EDGE IS RECOVERED FROM THE INTEGRATION, NOT FROM THE ROUTES, and that is
# the decision that makes this importable at all. odin emits TWO routes per
# target (`ANY /x` and `ANY /x/{proxy+}` -- see hcl.py) because that is what AWS
# needs to serve a whole path prefix, but both point at ONE integration, and the
# integration is the thing that names the target workload. So the route count
# never reaches the canvas and cannot double an edge.
#
# The stage folds away with nothing carried: odin emits exactly one, always
# `$default` with `auto_deploy = true`, so there is no information in it to
# lose. A stage with any OTHER name is reported, because serving it is something
# odin does not do.
_APIGW_COMPANION_TYPES = (
    "aws_apigatewayv2_stage", "aws_apigatewayv2_integration", "aws_apigatewayv2_route",
)
# The attribute each supported type's human-facing name lives in (mirrors
# hcl.py's builders: s3 uses `bucket`, elasticache uses `cluster_id`, rds uses
# `identifier`, everything else uses `name`).
_NAME_ATTR = {
    "aws_s3_bucket": "bucket", "aws_sqs_queue": "name", "aws_sns_topic": "name",
    "aws_dynamodb_table": "name", "aws_iam_role": "name",
    "aws_cloudwatch_log_group": "name",
    "aws_secretsmanager_secret": "name", "aws_ssm_parameter": "name",
    "aws_elasticache_cluster": "cluster_id",
    "aws_db_instance": "identifier",
    "aws_lb": "name",
    "aws_security_group": "name", "aws_ecr_repository": "name",
    "aws_ecs_service": "name",
    "aws_lambda_function": "function_name",
    "aws_route53_zone": "name",
    # v0.8.19. See the `_KIND` note: `creation_token` is the only name-shaped
    # argument the type has, it is where `hcl.py` writes the label, and being
    # HERE is what makes `_renamed_by_import` fire for one odin cannot read.
    "aws_efs_file_system": "creation_token",
    "aws_apigatewayv2_api": "name",
}
# canvas kind -> aws_* type, for mode (b) (the inverse of `_KIND`). iam_role,
# logs, secret and ssm have no backing to enumerate live resources from (all
# four are pure gateway models), so they stay out of the live path --
# elasticache likewise: its clusters exist only as gateway-model records plus a
# real container, and there's no `_import_id` shape to resolve one from outside
# a canvas Apply (mode (a), reading an existing HCL project, works fine).
# `rds` DOES stay in it: an `aws_db_instance`'s import id is its bare
# DBInstanceIdentifier (the `_import_id` default branch) and the gateway answers
# DescribeDBInstances for real -- the one thing `tofu plan
# -generate-config-out` cannot recover is the master `password` (no AWS API ever
# returns it), so a live-imported database comes back with hcl.py's default
# password rather than the original one. `alb` (W2.5) stays out of the live path
# too -- one canvas node is three aws_* resources, so there is no single live
# resource to import it from (mode (a), reading an existing HCL project, works).
# `sg` and `ecr` (v0.8.4) stay OUT of the live path deliberately, and for
# different reasons. A security group's live import id is its `sg-...` GroupId,
# which is minted by the gateway and appears nowhere on a canvas, so there is no
# id to resolve from outside an Apply. ECR has no `_import_id` shape either --
# its repositories exist as gateway-model records. Mode (a), reading an existing
# HCL project, works for both; claiming mode (b) without an id that resolves is
# how a live import would generate a bogus import block and fail at apply.
# `ec2` is out for the same reason as `sg`: an instance's live import id is its
# `i-...` InstanceId, minted at RunInstances and absent from any canvas.
# `ecs` is out for the alb reason rather than the id reason: one canvas node is
# three tf resources, so there is no single live resource to import it from.
# `lambda` is out of the live path because its CODE is not recoverable from any
# AWS API odin implements -- a live import would produce a function whose body is
# odin's default payload, which is the substitution this whole module refuses.
# `ebs` is out for the `sg`/`ec2` reason: a volume's live import id is its
# `vol-...` VolumeId, minted by the gateway at CreateVolume and appearing nowhere
# on a canvas, so there is no id to resolve one from outside an Apply. Mode (a),
# reading an existing HCL project, works.
# `route53` is out for the same reason, and it is the ID SHAPE rather than a
# missing model: a hosted zone's live import id is its `Z...` HostedZoneId,
# minted when the zone is created, while the canvas carries the DOMAIN
# (`example.com`) -- a different string, so there is nothing here to resolve one
# from. That holds whether or not a route53 gateway model exists to enumerate
# zones (there was none when this line was written, 2026-08-02), which is why
# the reason is stated as the id and not as the backing.
# `efs` is out for that same reason and it is worth stating rather than assuming:
# `terraform import aws_efs_file_system.x` takes the bare `fs-...` FileSystemId,
# which the gateway mints at CreateFileSystem and which appears NOWHERE on a
# canvas -- the label is the `creation_token`, not the id. There is no
# `_import_id` shape that resolves one from outside an Apply, and claiming mode
# (b) without one is how a live import generates a bogus `import {}` block and
# fails at apply. Mode (a) works.
# `apigateway` is out for the `alb`/`ecs` reason rather than the id reason: one
# canvas node is four tf resource types, so there is no single live resource to
# import it from, and the INTEGRATIONS are what carry the wiring. Mode (a),
# reading an existing HCL project, works.
_NO_LIVE_IMPORT = {
    "iam_role", "logs", "secret", "ssm", "elasticache", "alb", "sg", "ecr", "ec2", "ecs", "lambda",
    "ebs", "route53", "efs", "apigateway",
}
_TF_TYPE = {kind: rtype for rtype, kind in _KIND.items() if kind not in _NO_LIVE_IMPORT}

# The HCL arguments each kind CARRIES into the canvas -- so a round-trip through
# generate_tf reproduces them (finding #6). Any OTHER argument present on the
# resource is reported as a per-node warning rather than silently dropped
# (`__is_block__` is python-hcl2's internal block marker, never a real arg).
_IGNORED_ATTRS = {"__is_block__"}
# `tags` is carried by EVERY primary kind, so it is added centrally by
# `_carried` below rather than listed in each set -- see the note there.
_CARRIED_ATTRS = {
    # `force_destroy` is carried in the sense that odin always re-emits it --
    # as `true`, unconditionally (hcl.py's `_s3`). A source `force_destroy =
    # false` is therefore a CHANGED argument (`_FIXED_VALUES`), not a dropped
    # one: v0.7.5 reported it as "unmodeled", which reads as "odin ignored it"
    # when odin actually flips a bucket the user protected into one `tofu
    # destroy` will empty.
    "s3": {"bucket", "force_destroy"},
    "sqs": {"name"},
    "sns": {"name"},
    # `billing_mode` likewise: odin always emits PAY_PER_REQUEST, so a
    # PROVISIONED table with read/write capacity is a changed argument.
    "dynamodb": {"name", "billing_mode", "hash_key", "range_key", "attribute"},
    "iam_role": {"name"},  # assume_role_policy/inline policies are NOT carried -> warned
    "logs": {"name", "retention_in_days"},
    # W2.4: `recovery_window_in_days` is carried in the sense that odin always
    # emits its own value (0 -- see hcl.py's `_secret`). Until v0.7.6 that was
    # where the sentence stopped, and a source `recovery_window_in_days = 30`
    # became 0 in silence -- a 30-day undelete window turned into immediate,
    # irreversible deletion. It is a `_FIXED_VALUES` entry now, so odin's own
    # 0 still round-trips quietly while a DIFFERING one is reported.
    # The VALUE isn't here at all: it lives on the companion
    # aws_secretsmanager_secret_version resource, assembled separately below.
    "secret": {"name", "description", "recovery_window_in_days"},
    "ssm": {"name", "type", "value", "description"},
    # engine/num_cache_nodes are carried because hcl.py always re-emits them --
    # as `redis` and `1`, unconditionally. THIS COMMENT USED TO STOP THERE, and
    # that was the bug: neither value reaches the canvas node at all, so a
    # 3-node memcached cluster came back as a single-node redis one with no
    # warning of any kind (a different datastore, a different wire protocol,
    # a third of the nodes). Both are `_FIXED_VALUES` entries now.
    "elasticache": {"cluster_id", "engine", "node_type", "num_cache_nodes"},
    # `password` IS carried (unlike every other secret odin touches): dropping
    # it would make a round-trip through generate_tf silently substitute the
    # DEFAULT password, i.e. a real credential change on the next apply.
    "rds": {
        "identifier", "engine", "instance_class", "allocated_storage", "db_name",
        "username", "password", "skip_final_snapshot",
    },
    # W2.5: `internal`/`load_balancer_type` are values odin always emits itself
    # (hcl.py's `_alb`), so they are carried in the sense that odin re-emits
    # SOMETHING for them -- but a source value that DISAGREES with what odin
    # emits is a real semantic change and warns via `_FIXED_VALUES` below
    # (v0.7.0 dropped `internal = false` in silence, quietly turning an
    # internet-facing load balancer into an internal one). `subnets` is
    # CONTAINMENT on the canvas: carried as the `subnet`/`vpc` stamps when it
    # points at an imported subnet, warned about when it can't be resolved.
    "alb": {"name", "internal", "load_balancer_type", "subnets"},
    "vpc": {"cidr_block"},
    "subnet": {"cidr_block", "vpc_id"},
    # v0.8.4. `ingress` IS carried -- into the node's `ingressRules` text, one
    # `protocol:port:source` line per block, which is what `hcl.py::_sg` reads
    # back. v0.8.14: `egress` is carried the SAME real way now, into
    # `egressRules`, so the weaker "odin re-emits its own default" reading this
    # comment used to give is gone along with the warning that went with it.
    # `vpc_id` is CONTAINMENT, stamped by `_stamp_containment` exactly as a
    # subnet's is.
    "sg": {"name", "vpc_id", "ingress", "egress", "tags"},
    "ecr": {"name"},
    # `subnet_id` is containment; `vpc_security_group_ids` becomes the node's
    # `securityGroups` label list; `key_name` is a reference to the companion
    # aws_key_pair whose `public_key` is the real value.
    "ec2": {"ami", "instance_type", "subnet_id", "vpc_security_group_ids",
            "key_name", "user_data"},
    # `depends_on` is carried in the sense that odin RE-DERIVES it from the
    # node's own `${{...}}` refs, so it is never a dropped argument -- but see
    # `_stamp_ecs_taskdef`: the refs themselves are not in the HCL at all and
    # cannot come back, which is reported rather than left to be discovered.
    "ecs": {
        "name", "cluster", "task_definition", "desired_count", "launch_type",
        "wait_for_steady_state", "deployment_minimum_healthy_percent",
        "deployment_maximum_percent", "timeouts", "placement_constraints",
        "depends_on", "load_balancer",
    },
    # `filename`/`source_code_hash` are carried in the sense that odin re-derives
    # both from the code it materializes itself; `role` is either a reference to a
    # drawn iam_role node or odin's own auto-generated one (`_stamp_lambda`).
    # v0.8.19: `file_system_config` is carried as a canvas EDGE (plus the efs
    # node's `path`) by the mount pass below, not as a field on this node -- so
    # it belongs here for the same reason `depends_on` does. Leaving it out
    # reported "imported without unmodeled attribute(s): file_system_config" on
    # every mounted function INCLUDING odin's own output, which is exactly the
    # noise the central `tags` fix was written to stop.
    "lambda": {
        "function_name", "role", "handler", "runtime", "filename",
        "source_code_hash", "depends_on", "file_system_config",
    },
    # v0.8.18. `type` is carried in the sense that odin always re-emits it -- as
    # `gp3`, unconditionally (`hcl.py::_ebs`), because the canvas tile has no
    # volume-type field and the substrate (a `limactl disk`) has no volume type
    # at all. A source `type = "io2"` is therefore a CHANGED argument
    # (`_FIXED_VALUES`), never a dropped one: an io2 volume imported as gp3 in
    # silence is the elasticache bug in another costume.
    "ebs": {"availability_zone", "size", "type"},
    # v0.8.19. A hosted zone is a ONE-ARGUMENT resource here: `name`, which IS
    # the canvas label. Everything that makes a zone do anything lives in its
    # RECORDS, and those are companions -- so anything else written on the zone
    # itself (`comment`, `force_destroy`, a `vpc {}` block making it private,
    # `delegation_set_id`) is genuinely unmodeled and is reported by name rather
    # than looking carried because the zone imported cleanly.
    "route53": {"name"},
    # v0.8.19. `creation_token` only -- everything else `aws_efs_file_system`
    # accepts is genuinely UNMODELLED and reported dropped, which is the honest
    # answer rather than a `_FIXED_VALUES` entry pretending odin re-emits it.
    # `encrypted`/`kms_key_id` are the ones that matter: odin's substrate is a
    # host directory (agent C's `.odin/<env>/gateway/efs/<fs-id>/`) and it
    # encrypts NOTHING, so carrying them onto the canvas would claim a property
    # the substrate has not got -- the kms lesson. `performance_mode`,
    # `throughput_mode` and `lifecycle_policy` have no substrate meaning either.
    "efs": {"creation_token"},
    # v0.8.19. `protocol_type` is carried in the sense that odin always re-emits
    # it as `HTTP` -- a `WEBSOCKET` source is a CHANGED argument
    # (`_FIXED_VALUES`) rather than a silently dropped one, because odin's
    # substrate is an HTTP reverse proxy and a websocket API imported as an HTTP
    # one would be a green tile that never completes a handshake.
    "apigateway": {"name", "protocol_type"},
}
# EVERY primary kind's carried set, `tags` included.
#
# `tags` is unioned in HERE rather than listed 18 times, because listing it per
# kind is exactly how it went missing: `sqs`, `sns`, `dynamodb` and `iam_role`
# each had a set that stopped at `name`, so their user tags were dropped AND the
# drop was reported as an unmodeled attribute -- on every import of odin's own
# output, since `_tags_block` also stamps `odin:node`. Measured before this
# change: four warnings reading "imported without unmodeled attribute(s): tags"
# for four resources odin had just generated.
#
# The per-kind fix would have closed four instances and left the fifth kind
# somebody adds next year open. `hcl.py::generate_tf` appends `_tags_block(res)`
# to EVERY primary builder unconditionally, so "which kinds carry tags" was
# never a per-kind question -- the answer is all of them, and this is the one
# place that can now be wrong. `tests/agent/test_import_tags.py` pins it against
# `_KIND` so a new kind cannot regress it silently.
def _carried(kind: str) -> set[str]:
    return _CARRIED_ATTRS.get(kind, set()) | {"tags"}
# The companion resources' equivalent: which of THEIR arguments a round trip
# reproduces (hcl.py's alb companion pass emits exactly these). Until v0.7.1
# nothing computed dropped attributes for a companion at all, so a target
# group's `matcher`, `stickiness`, `deregistration_delay` -- and every
# health_check member except `path` -- vanished without a word. v0.7.6 adds the
# OTHER TWO companion types, which still had no honesty pass of any kind: an
# sns subscription's `filter_policy` (a routing rule -- without it the queue
# starts receiving every message on the topic) and `raw_message_delivery =
# false` (odin always emits true, which changes the envelope every consumer
# parses), plus a secret version's `version_stages`.
_CARRIED_COMPANION_ATTRS = {
    "aws_lb_target_group": {"name", "port", "protocol", "vpc_id", "target_type", "health_check"},
    "aws_lb_listener": {"load_balancer_arn", "port", "protocol", "default_action"},
    "aws_sns_topic_subscription": {"topic_arn", "protocol", "endpoint", "raw_message_delivery"},
    "aws_secretsmanager_secret_version": {"secret_id", "secret_string"},
    # v0.8.18. An attachment becomes an EDGE, and an edge carries no arguments,
    # so anything the source put on it has to be accounted for here or it
    # vanishes: `force_detach` and `skip_destroy` both change what a destroy
    # does to a disk that has data on it. `device_name` IS carried in the sense
    # that odin re-derives it positionally (`_assigned_devices`), so a source
    # that names a different device is reported CHANGED.
    "aws_volume_attachment": {"device_name", "instance_id", "volume_id"},
    # v0.8.19, the attachment's shape exactly: a record becomes an EDGE, so
    # everything the source wrote on it has to be accounted for here or it
    # vanishes. `zone_id`/`records` are the two references the edge is rebuilt
    # from; `type`/`ttl` are carried in the sense that odin re-emits its own
    # (`_FIXED_VALUES` names a source that disagrees); `name` is RE-DERIVED as
    # `<ec2 label>.<zone label>`, so a record the user named anything else is
    # reported CHANGED, the same way an attachment's `device_name` is. What is
    # NOT here is every routing feature a hosts file cannot have --
    # `set_identifier`, `weighted_routing_policy`, `failover_routing_policy`,
    # `health_check_id`, `alias` -- and each of those changes WHICH address the
    # name answers with, so a silent drop would be a different record wearing
    # the same name.
    "aws_route53_record": {"zone_id", "name", "type", "ttl", "records"},
    # v0.8.19. The access point folds ONTO its file system, so anything the
    # source put on it has to be accounted for here or it vanishes. `posix_user`
    # is the one that bites: it forces every file the mount creates to one
    # uid/gid, and odin re-emits nothing for it. `root_directory` IS carried in
    # the sense that odin always re-emits it -- as `path = "/"` -- so a source
    # rooted at a subdirectory is a CHANGED argument, reported by
    # `_root_directory_change` below rather than by `_FIXED_VALUES`, which cannot
    # reach inside a block.
    _EFS_ACCESS_POINT_TYPE: {"file_system_id", "root_directory"},
    # v0.8.19. The integration becomes an EDGE, and an edge carries no
    # arguments, so anything the source put on one has to be accounted for here
    # or it vanishes silently. `request_parameters` (a path/header rewrite),
    # `credentials_arn`, `tls_config` and `timeout_milliseconds` all change what
    # the caller gets, and odin models none of them.
    "aws_apigatewayv2_integration": {
        "api_id", "integration_type", "integration_uri", "integration_method",
        "payload_format_version",
    },
    "aws_apigatewayv2_route": {"api_id", "route_key", "target"},
    "aws_apigatewayv2_stage": {"api_id", "name", "auto_deploy"},
}
_CARRIED_HEALTH_CHECK_ATTRS = {"path"}
# v0.8.19: the arguments of the ECS mount, which lives in nested blocks on a
# TASK DEFINITION rather than on a resource of its own.
#
# It is NOT in `_CARRIED_COMPANION_ATTRS`, deliberately, and not only because
# `aws_ecs_task_definition` is not the owner of these keys: that table is read by
# `tests/agent/test_import_coverage_is_honest.py` as the set of aws_* resource
# TYPES import understands, and a key that is not a type would inflate the
# published count. `_CARRIED_HEALTH_CHECK_ATTRS` sits outside it for the same
# reason.
#
# The task definition has NO general attribute-honesty pass (it is folded on by
# `_stamp_ecs_taskdef`, which reports what it cannot carry in prose), and adding
# one here was rejected on scope: it would immediately warn about `family`,
# `network_mode` and `requires_compatibilities` on every ecs import that exists
# today. So the pass below is targeted at the mount blocks, which are the part
# this change introduces and the part that would otherwise vanish.
_CARRIED_EFS_VOLUME_ATTRS = {"name", "efs_volume_configuration"}
_CARRIED_EFS_VOLUME_CONFIG_ATTRS = {"file_system_id", "root_directory"}
# hcl.py roots BOTH the access point and the ECS volume configuration at the top
# of the file system. A source that roots either one at a subdirectory is telling
# the consumer it may see only that subtree, and regenerating with `/` hands it
# the WHOLE file system -- a widening, which is the direction worth a warning.
_ODIN_EFS_ROOT = "/"
# (owner, attribute) -> the value odin ALWAYS emits, lowercased. An imported
# value that differs is reported by name: the argument survives, its MEANING
# does not. Owner is the canvas kind for a primary resource, the aws_* type for
# a companion.
#
# EVERY ENTRY HERE IS A SUBSTITUTION ODIN MAKES BECAUSE THE LOCAL SUBSTRATE
# CANNOT DO THE OTHER THING (a real redis container is not memcached; odin's
# DeleteSecret has no recovery window; there is no snapshot surface to take a
# final snapshot into). The substitution is a legitimate limit -- the SILENCE
# was the bug, and this table is the whole cure: an entry is compared against
# the source's own value, so odin's own generated HCL round-trips without a
# word while anything else is named on the import itself.
_FIXED_VALUES = {
    ("s3", "force_destroy"): "true",
    ("dynamodb", "billing_mode"): "pay_per_request",
    ("secret", "recovery_window_in_days"): "0",
    ("elasticache", "engine"): "redis",
    ("elasticache", "num_cache_nodes"): "1",
    ("rds", "skip_final_snapshot"): "true",
    ("alb", "internal"): "true",
    ("alb", "load_balancer_type"): "application",
    ("aws_lb_target_group", "protocol"): "http",
    ("aws_lb_target_group", "target_type"): "instance",
    ("aws_lb_listener", "protocol"): "http",
    ("aws_sns_topic_subscription", "protocol"): "sqs",
    ("aws_sns_topic_subscription", "raw_message_delivery"): "true",
    # odin emits all four of these unconditionally (`hcl.py::_ecs`), and the
    # rolling-update pair is load-bearing: `_ECS_MIN_HEALTHY_PERCENT = 100` is
    # what keeps the previous revision serving while a new one comes up, so a
    # source that lowered it comes back with different rollout behaviour.
    ("ecs", "launch_type"): "ec2",
    ("ecs", "wait_for_steady_state"): "true",
    ("ecs", "deployment_minimum_healthy_percent"): "100",
    ("ecs", "deployment_maximum_percent"): "200",
    ("ebs", "type"): "gp3",
    # v0.8.19, and the substitution is the substrate's, not a shortcut: odin
    # resolves a name with a HOSTS ENTRY (`--add-host` on containers,
    # `/etc/hosts` on VMs), and a hosts entry is `<ip> <name>`. It has no record
    # type and no TTL to express, so `hcl.py` emits `A`/`60` unconditionally
    # (`_DNS_RECORD_TYPE`/`_DNS_RECORD_TTL`). A source `type = "CNAME"` really
    # does come back as an A record answering with an IP -- a different kind of
    # answer, not a detail -- and a `ttl = 300` really does come back as 60.
    #
    # Lowercase because this table is compared against `_literal`, which
    # lowercases: an entry spelled `"A"` could never equal the source's own `"a"`
    # and would fire on every import of odin's OWN output, which is the
    # every-time warning `_same_literal` exists to have stopped once already.
    ("aws_route53_record", "type"): "a",
    ("aws_route53_record", "ttl"): "60",
}
# The default odin's `_node_data` falls back to when a NUMERIC argument's source
# value isn't a literal number odin can read (`allocated_storage = var.size`,
# `retention_in_days = local.days`) -> {kind: {attribute: what that costs the
# user}}. A quoted `"500"` IS readable (see `_int_text`); a computed one is not,
# and substituting odin's default for it in silence is the elasticache bug in
# another costume -- a 500 GiB database imported as 20 GiB, a 30-day log
# retention imported as never-expire.
_RDS_DEFAULT_STORAGE = "20"  # hcl.py::_DEFAULT_ALLOCATED_STORAGE
_UNREADABLE_NUMBERS = {
    "logs": {
        "retention_in_days": "the canvas gets no retention at all -- AWS's never-expire default",
    },
    "rds": {
        "allocated_storage": f"the canvas gets odin's default {_RDS_DEFAULT_STORAGE} GiB",
    },
    "ebs": {
        "size": f"the canvas gets odin's default {hcl._DEFAULT_EBS_SIZE} GiB",
    },
}
_CONTAINER_KINDS = ("vpc", "subnet")

# Canvas geometry for the layout pass. These ARE the UI's own container sizes
# (`defaultStyleForType` in ui/src/components/Canvas.tsx) and its 20px grid: an
# imported node has to be geometrically inside its container box, because the
# browser re-derives the `vpc`/`subnet` stamps from geometry whenever nodes are
# measured or dragged (ui/src/lib/containment.ts) and would strip a stamp whose
# node visually sits outside.
_LEAF_SIZE = (220, 120)
_MIN_VPC_SIZE = (560, 380)
_MIN_SUBNET_SIZE = (520, 280)
_PAD = 20
_HEADER = 40


class Unsupported(BaseModel):
    model_config = {"frozen": True}
    type: str
    name: str
    reason: str


class ImportResult(BaseModel):
    model_config = {"frozen": True}
    nodes: list[dict] = []
    edges: list[dict] = []
    unsupported: list[Unsupported] = []
    # A per-node note that a resource WAS imported but some of its in-resource
    # arguments couldn't be carried (finding #6) -- honest at attribute
    # granularity, not just resource-type granularity.
    warnings: list[str] = []
    # Set ONLY when the input itself failed to PARSE (finding #7) -- distinct
    # from a well-formed file that merely contains unsupported resources (which
    # stays a success with an `unsupported` list). The CLI treats a non-None
    # value as a hard error and exits non-zero, so a CI job's exit-code check
    # catches a broken import.
    parse_error: str | None = None


@dataclass(frozen=True)
class LiveResource:
    """A resource the caller asserts already exists in the env's backings —
    mode (b)'s input. `type` is the canvas kind (s3/sqs/sns/dynamodb), `id`
    the resource's AWS-facing name (bucket/queue/topic/table name)."""

    type: str
    id: str


def _plain_literal(value: object) -> str | None:
    """`value` as a plain string literal, or None when it is anything computed.

    Only a plain literal (no leftover `${...}`, whether it was a bare reference
    or a literal with an embedded interpolation) is a value odin can carry onto
    the canvas."""
    unquoted = hcl.unquote(value) if isinstance(value, str) else None
    return unquoted if isinstance(unquoted, str) and "${" not in unquoted else None


def _label(rtype: str, rname: str, attrs: dict) -> str:
    """The canvas label -- which for every named kind IS the name odin will
    create the resource under, so a fallback here RENAMES a real resource
    (reported by `_renamed_by_import`, never silent).

    A computed name falls back to odin's own management tag (which IS the canvas
    label, so odin's generated HCL round-trips even for the name-less kinds),
    then to the resource's own HCL name."""
    name_attr = _NAME_ATTR.get(rtype)
    literal = _plain_literal(attrs.get(name_attr)) if name_attr else None
    return literal or _all_tags(attrs).get("odin:node") or rname


def _ref_target(value: object) -> str | None:
    """The resource NAME an interpolation like `${aws_sns_topic.alerts.arn}`
    points at (`alerts`), or None if `value` isn't an interpolation."""
    if not isinstance(value, str) or not (value.startswith("${") and value.endswith("}")):
        return None
    parts = value[2:-1].split(".")
    return parts[1] if len(parts) >= 2 else None


def _attribute_types(attrs: dict) -> dict[str, str]:
    """{attribute name -> type} from a dynamodb table's `attribute {}` blocks
    (python-hcl2 parses repeated blocks into a list of dicts)."""
    types: dict[str, str] = {}
    for block in attrs.get("attribute") or []:
        name = hcl.unquote(block.get("name"))
        atype = hcl.unquote(block.get("type"))
        if isinstance(name, str) and isinstance(atype, str):
            types[name] = atype
    return types


def _all_tags(attrs: dict) -> dict[str, str]:
    """Every tag on the resource, odin's own machinery included."""
    raw = attrs.get("tags")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = hcl.unquote(key) or key
        val = hcl.unquote(value)
        if isinstance(name, str) and isinstance(val, str):
            out[name] = val
    return out


# The `odin:` tag namespace is MACHINERY, not user data, and it is reserved as a
# namespace rather than key by key. It started as one key (`odin:node`, which
# `reconcile/tf_status.py` and `gateway/keys.py` both match on) and the exclusion
# was written as `name != "odin:node"`. v0.8.14 adds a whole family
# (`odin:ref:<VAR>`, the canvas wiring), and an exact-match exclusion would have
# surfaced every one of them in the config panel as an editable user tag AND
# re-emitted them as literal tags beside the ones the generator writes itself.
# Reserving the prefix means the next member of the family needs no change here
# -- the same reason AWS reserves `aws:`.
_ODIN_TAG_PREFIX = "odin:"


def _tags(attrs: dict) -> dict[str, str]:
    """The USER `tags` map: everything outside odin's reserved namespace, so a
    round trip surfaces exactly what the user wrote and nothing odin added."""
    return {
        name: value for name, value in _all_tags(attrs).items()
        if not name.startswith(_ODIN_TAG_PREFIX)
    }


def _int_attr(value: object, default: int) -> int:
    """python-hcl2 parses an unquoted `port = 80` as a real int and a quoted
    `"80"` as a 4-character string (verified empirically -- see `unquote`), so
    both spellings have to reduce to the same number."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    unquoted = hcl.unquote(value)
    return int(unquoted) if isinstance(unquoted, str) and unquoted.isdigit() else default


def _forward_target_group(listener_attrs: dict) -> str | None:
    """`aws_lb_target_group.<name>` from a listener's `default_action {}` block
    (python-hcl2 parses a repeated block into a list of dicts). v1 reads the
    FIRST action carrying a `target_group_arn` -- the only shape hcl.py emits
    and the only one elbv2ctl models."""
    for block in listener_attrs.get("default_action") or []:
        target = _ref_target(block.get("target_group_arn"))
        if target:
            return f"aws_lb_target_group.{target}"
    return None


def _health_check_path(tg_attrs: dict) -> str:
    for block in tg_attrs.get("health_check") or []:
        path = hcl.unquote(block.get("path"))
        if isinstance(path, str) and path:
            return path
    return "/"


def _literal(value: object) -> str:
    """An HCL scalar reduced to a comparable lowercase string: python-hcl2 gives
    back a real bool for `internal = false`, an int for `port = 80`, and a
    quote-wrapped string for `"HTTP"`."""
    unquoted = hcl.unquote(value) if isinstance(value, str) else value
    return str(unquoted).lower()


def _int_text(value: object) -> str | None:
    """An HCL integer argument as digits, or None when the source value isn't a
    literal number at all. python-hcl2 parses `20` as a real int and a quoted
    `"20"` as a 4-character string (both are valid HCL for a number argument and
    both must survive), while `var.size` arrives as the string `${var.size}`,
    which nothing can turn into a number here -- and `isinstance(True, int)` is
    True in Python, so a bool has to be excluded explicitly."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    unquoted = hcl.unquote(value)
    return unquoted if isinstance(unquoted, str) and unquoted.isdigit() else None


def _derived_changes(triples: Iterable[tuple[str, object, str]]) -> dict[str, str]:
    """{attribute: why} for each `(attribute, source value, what odin emits)`
    whose source value disagrees with what odin emits -- the `_FIXED_VALUES`
    check for the values odin COMPUTES per resource instead of hardcoding: a
    node's own name, a target group's `<alb label>-tg`."""
    return {
        attr: f"odin always emits {expected}"
        for attr, value, expected in triples
        if value is not None and _literal(value) != _literal(expected)
    }


def _renamed_by_import(rtype: str, attrs: dict, label: str) -> dict[str, str]:
    """The name argument odin could not read (`name = "${var.env}-jobs"`).

    The canvas label falls back to the HCL resource name, and `generate_tf` then
    emits THAT as the real bucket/queue/table/cluster name -- so importing a
    project whose names are built from variables silently renames every resource
    in it. Compared against the label rather than tested for `${`, so a name odin
    CAN read (the round-trip case) never fires."""
    return _derived_changes((attr, attrs.get(attr), label) for attr in (_NAME_ATTR.get(rtype),) if attr)


def _unreadable_numbers(kind: str, attrs: dict) -> dict[str, str]:
    """The numeric arguments whose source value odin cannot read as a number, so
    `_node_data`'s own default lands on the canvas instead (`_UNREADABLE_NUMBERS`)."""
    return {
        attr: f"not a literal number, so {cost}"
        for attr, cost in _UNREADABLE_NUMBERS.get(kind, {}).items()
        if attr in attrs and _int_text(attrs[attr]) is None
    }


def _uncarried_attribute_blocks(attrs: dict, data: dict) -> list[str]:
    """A dynamodb `attribute {}` block for something that is neither the hash nor
    the range key. `generate_tf` emits an attribute block for exactly those two,
    so a secondary index's key attribute does not survive -- the index itself is
    already reported as an unmodeled argument, its attribute was not."""
    keys = {data.get("hashKey"), data.get("rangeKey")}
    return [f"attribute.{name}" for name in _attribute_types(attrs) if name not in keys]


def _attribute_notes(
    owner: str, attrs: dict, carried: set[str],
    also_dropped: Iterable[str], also_changed: dict[str, str],
) -> tuple[list[str], list[str]]:
    """`(dropped, changed)` -- the two ways an argument fails to survive a round
    trip through `generate_tf`, reported by name, never dropped in silence (the
    v0.5.4 attribute-honesty rule, which is meant to have no exceptions).

    Either odin doesn't model the argument at all (`dropped`), or odin emits its
    own value for it and the source's value differs (`changed`). The second kind
    is the sneakier one -- the argument is still THERE in the regenerated HCL,
    saying something else -- and it is returned SEPARATELY because through
    v0.7.5 both shared one line reading "imported without unmodeled
    attribute(s)", which says odin IGNORED the argument when odin in fact
    changed the resource. `also_changed` carries the cases a static table can't
    express: a value odin computes per resource, and a number odin can't read.
    """
    dropped = sorted({k for k in attrs if k not in carried and k not in _IGNORED_ATTRS} | set(also_dropped))
    fixed = {
        key: f"odin always emits {want}"
        for (fixed_owner, key), want in _FIXED_VALUES.items()
        if fixed_owner == owner and key in attrs and _literal(attrs[key]) != want
    }
    changed = sorted(
        f"{key}={_literal(attrs[key])} ({why})"
        for key, why in {**fixed, **also_changed}.items() if key in attrs
    )
    return dropped, changed


def _attribute_warnings(subject: str, what: str, dropped: list[str], changed: list[str]) -> list[str]:
    """The per-resource honesty lines a caller surfaces (`cli/translate.py`
    prints each as `warning: ...` on stderr, and they ride in the JSON body's
    `warnings` array for programmatic callers).

    Two lines, not one, and the second one says CHANGED: an argument odin
    substitutes its own value for is not a missing argument, and a user reading
    "imported without unmodeled attribute(s): engine=memcached" would reasonably
    conclude their cluster was imported unchanged minus a detail."""
    return [
        *([f"{subject}: imported without unmodeled {what}attribute(s): {', '.join(dropped)}"]
          if dropped else []),
        *([f"{subject}: imported with CHANGED {what}argument(s) -- odin substitutes its own "
           f"value: {', '.join(changed)}"] if changed else []),
    ]


def _dropped_health_check_attrs(tg_attrs: dict) -> list[str]:
    """Health-check members other than `path`: odin emits only the canvas-
    authored `path` and leaves the rest for the provider to read back, so a
    source `matcher`/`interval`/`healthy_threshold` does not survive."""
    return sorted(
        f"health_check.{key}"
        for block in (tg_attrs.get("health_check") or [])
        for key in block
        if key not in _CARRIED_HEALTH_CHECK_ATTRS and key not in _IGNORED_ATTRS
    )


def _node_data(kind: str, label: str, attrs: dict) -> dict:
    data: dict = {"label": label}
    if kind == "dynamodb":
        hash_key = hcl.unquote(attrs.get("hash_key"))
        data["hashKey"] = hash_key if isinstance(hash_key, str) else "id"
        types = _attribute_types(attrs)
        if data["hashKey"] in types:
            data["hashKeyType"] = types[data["hashKey"]]
        range_key = hcl.unquote(attrs.get("range_key"))
        if isinstance(range_key, str):
            data["rangeKey"] = range_key
            if range_key in types:
                data["rangeKeyType"] = types[range_key]
    if kind == "logs":
        # `_int_text` because a quoted `retention_in_days = "30"` is valid HCL for
        # a number argument and used to fall straight through an `isinstance(int)`
        # test into "no retention" -- i.e. never expire, for a group the user
        # asked to expire. A genuinely computed value still can't be carried, and
        # `_unreadable_numbers` reports THAT rather than substituting in silence.
        retention = _int_text(attrs.get("retention_in_days"))
        if retention is not None:
            data["retentionInDays"] = retention
    if kind == "ssm":
        # W2.4: the parameter's VALUE comes across as canvas data -- the same
        # trust model as any other import (SECURITY.md: treat an imported .tf
        # like a shell script), and it's the only way a round trip through
        # `generate_tf` can reproduce the parameter at all.
        for attr, field in (("type", "paramType"), ("value", "paramValue")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind in ("secret", "ssm"):
        description = hcl.unquote(attrs.get("description"))
        if isinstance(description, str):
            data["description"] = description
    if kind == "rds":
        # Same reading as logs' retention above, and the same reason: a quoted
        # `allocated_storage = "500"` silently became odin's default 20 GiB.
        storage = _int_text(attrs.get("allocated_storage"))
        data["allocatedStorage"] = storage or _RDS_DEFAULT_STORAGE
        for attr, field in (("engine", "engine"), ("instance_class", "instanceClass"),
                            ("db_name", "dbName"), ("username", "username"), ("password", "password")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind in _CONTAINER_KINDS:
        cidr = hcl.unquote(attrs.get("cidr_block"))
        if isinstance(cidr, str):
            data["cidr"] = cidr
    tags = _tags(attrs)
    if tags:
        data["tags"] = tags
    if kind == "elasticache":
        node_type = hcl.unquote(attrs.get("node_type"))
        if isinstance(node_type, str):
            data["nodeType"] = node_type
    if kind == "lambda":
        for attr, field in (("runtime", "runtime"), ("handler", "handler")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind == "ecs":
        count = _int_text(attrs.get("desired_count"))
        if count is not None:
            data["count"] = count
    if kind == "ec2":
        # `userData` is carried verbatim, and that is a deliberate trust
        # decision, not an oversight: it is a shell script odin will run on a
        # real VM. SECURITY.md's rule for the whole import path applies -- treat
        # an imported .tf like a shell script, because for this field it IS one.
        for attr, field in (("ami", "ami"), ("instance_type", "instanceType"),
                            ("user_data", "userData")):
            value = hcl.unquote(attrs.get(attr))
            if isinstance(value, str):
                data[field] = value
    if kind == "ebs":
        # `size` is TEXT on the canvas -- the tile's own `defaultData` is
        # `{size: '10'}` and the config panel writes a string -- so a quoted
        # `size = "100"` and a bare `size = 100` both land as digits, exactly as
        # rds's `allocatedStorage` does. A computed one can't be read at all and
        # `_unreadable_numbers` reports THAT rather than substituting in silence.
        data["size"] = _int_text(attrs.get("size")) or hcl._DEFAULT_EBS_SIZE
        az = hcl.unquote(attrs.get("availability_zone"))
        if isinstance(az, str):
            data["az"] = az
    return data


def _apigw_target_label(uri: object, by_hcl_name: dict[str, str]) -> str | None:
    """The canvas label an integration's `integration_uri` names, or None.

    Two shapes, one per integration type, and both are odin's OWN output read
    back rather than a guess about what a hand-written project might contain:

      AWS_PROXY   `aws_lambda_function.<hcl name>.invoke_arn`
      HTTP_PROXY  `"http://${aws_ecs_service.<hcl name>.name}.odin.internal"`

    A regex over the whole string, NOT `_referenced_label`, and that difference
    is a real bug this caught rather than a style choice. `_ref_target` requires
    the value to be an interpolation END TO END (`value.startswith("${") and
    value.endswith("}")`), which the lambda form is and the ecs form is NOT --
    its interpolation is EMBEDDED in a URL. Written the first way, every
    `apigateway -> ecs` edge came back `unsupported` with
    `integration_uri='"http://${aws_ecs_service.checkout.name}.odin.internal"'`
    and the route was silently lost on the round trip.

    A URI naming anything else (a real Lambda ARN typed by hand, an external
    URL) resolves to None and the caller reports it as unsupported rather than
    dropping the wiring in silence."""
    text = uri if isinstance(uri, str) else ""
    for rtype in ("aws_lambda_function", "aws_ecs_service"):
        name = _referenced_hcl_name(text, rtype)
        label = by_hcl_name.get(f"{rtype}.{name}") if name else None
        if label:
            return label
    return None


def _apigw_companions(
    companions: list[tuple[str, str, dict]], by_hcl_name: dict[str, str],
) -> tuple[list[dict], list[str], list[Unsupported]]:
    """An API's stage/integrations/routes -> canvas edges + honesty warnings.

    ONE EDGE PER INTEGRATION, never per route. odin emits two routes for every
    target, so counting routes would produce two identical edges for one drawn
    line -- and the canvas would then generate four routes on the next Apply,
    then eight. Recovering from the integration makes the round trip a fixed
    point by construction rather than by a de-duplication step someone could
    remove.

    The routes are still READ, for what they can say that the integration
    cannot: a route key that is not one of the two odin would emit means the
    source serves a path odin will not, and that is reported CHANGED. Silence
    there would let `POST /checkout` import as a node whose next Apply serves
    `/checkout` on a completely different path.
    """
    edges: list[dict] = []
    warnings: list[str] = []
    unsupported: list[Unsupported] = []
    integration_labels: dict[str, tuple[str, str]] = {}

    for rtype, rname, attrs in companions:
        if rtype != "aws_apigatewayv2_integration":
            continue
        api_label = _referenced_label(attrs.get("api_id"), "aws_apigatewayv2_api", by_hcl_name)
        target_label = _apigw_target_label(attrs.get("integration_uri"), by_hcl_name)
        if not (api_label and target_label):
            missing = ", ".join(
                f"{arg}={attrs.get(arg)!r}" for arg, found in
                (("api_id", api_label), ("integration_uri", target_label)) if not found
            )
            unsupported.append(Unsupported(
                type="aws_apigatewayv2_integration", name=rname,
                reason=f"integration references a resource outside the supported set ({missing}) "
                       "-- the edge is dropped, so a regenerated project would NOT route to this target",
            ))
            continue
        integration_labels[rname] = (api_label, target_label)
        edges.append({
            "source": api_label, "target": target_label, "data": {"edgeType": ALB_TARGET},
        })
        dropped, changed = _attribute_notes(
            "aws_apigatewayv2_integration", attrs,
            _CARRIED_COMPANION_ATTRS["aws_apigatewayv2_integration"], (), {},
        )
        warnings += _attribute_warnings(
            f"{api_label} -> {target_label} (api route)", "", dropped, changed,
        )

    warnings += _apigw_route_warnings(companions, integration_labels, by_hcl_name)
    warnings += _apigw_stage_warnings(companions)
    return edges, warnings, unsupported


def _apigw_route_warnings(
    companions: list[tuple[str, str, dict]],
    integration_labels: dict[str, tuple[str, str]],
    by_hcl_name: dict[str, str],
) -> list[str]:
    """A route whose key is not one odin would generate, named.

    The canvas cannot hold a route key -- the path comes from the TARGET's label
    (`hcl.py::_apigw_route_keys`) -- so a source that routes `POST /checkout` to
    a function labelled `orders` loses the `/checkout` path on the next Apply and
    serves `/orders` instead. That is a real behaviour change and it is exactly
    the class `_FIXED_VALUES` exists for, reported the same way."""
    warnings: list[str] = []
    for rtype, rname, attrs in companions:
        if rtype != "aws_apigatewayv2_route":
            continue
        target = attrs.get("target")
        integration = _referenced_hcl_name(target, "aws_apigatewayv2_integration")
        labels = integration_labels.get(integration or "")
        if labels is None:
            continue
        api_label, target_label = labels
        expected = hcl._apigw_route_keys(target_label)
        actual = attrs.get("route_key")
        # `_literal` on BOTH sides, never a raw `in`: python-hcl2 hands back the
        # value quote-wrapped (`'"ANY /orders"'`), so a raw membership test says
        # "changed" for every route odin itself just generated -- which it did,
        # on the first run, for the `{proxy+}` half of every pair.
        matched = _literal(actual) in {_literal(key) for key in expected}
        dropped, changed = _attribute_notes(
            "aws_apigatewayv2_route", attrs, _CARRIED_COMPANION_ATTRS["aws_apigatewayv2_route"], (),
            _derived_changes([] if matched else [("route_key", actual, " or ".join(expected))]),
        )
        warnings += _attribute_warnings(
            f"{api_label} -> {target_label} (api route {rname})", "", dropped, changed,
        )
    return warnings


def _apigw_stage_warnings(companions: list[tuple[str, str, dict]]) -> list[str]:
    """A stage odin will not serve, named. odin emits exactly one stage per API,
    always `$default` -- the stage whose invoke path carries no stage segment,
    which is what lets the nginx prefix and the route key mean the same thing.
    A source naming any other stage would have its paths served one segment
    higher than it wrote them."""
    warnings: list[str] = []
    for rtype, rname, attrs in companions:
        if rtype != "aws_apigatewayv2_stage":
            continue
        dropped, changed = _attribute_notes(
            "aws_apigatewayv2_stage", attrs, _CARRIED_COMPANION_ATTRS["aws_apigatewayv2_stage"], (),
            _derived_changes([("name", attrs.get("name"), hcl._APIGW_STAGE)]),
        )
        warnings += _attribute_warnings(f"{rname} (api stage)", "", dropped, changed)
    return warnings


def _referenced_hcl_name(value: object, rtype: str) -> str | None:
    """The HCL RESOURCE NAME a `${aws_x.<name>.attr}` interpolation points at.

    `_referenced_label` answers with the canvas LABEL, which is what an edge
    needs; this answers with the name, which is what joining two companions to
    each other needs (a route names its integration, and the integration is not
    a node)."""
    text = value if isinstance(value, str) else ""
    match = re.search(rf"{re.escape(rtype)}\.([A-Za-z0-9_]+)\.", text)
    return match.group(1) if match else None


def _assigned_devices(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """`{(volume label, instance label): the device odin will assign}`.

    RE-DERIVED with `hcl.py`'s own positional rule rather than read from the
    source, because that is what a regenerate does: `hcl.py`'s attachment pass
    indexes `_EBS_DEVICE_NAMES` by the volume's place in the sorted list of that
    instance's volumes. A source `device_name = "/dev/xvdb"` therefore comes back
    as something else, which is a CHANGED argument and is reported as one.

    That the two rules agree is not asserted here in prose -- the byte-stable
    generate -> import -> generate test is what proves it, the same way the alb
    target group's `<label>-tg` name is proved."""
    by_instance: dict[str, list[str]] = {}
    for volume, instance in pairs:
        by_instance.setdefault(instance, []).append(volume)
    return {
        (volume, instance): hcl._EBS_DEVICE_NAMES[slot]
        for instance, volumes in by_instance.items()
        for slot, volume in enumerate(sorted(set(volumes)))
        if slot < len(hcl._EBS_DEVICE_NAMES)
    }


def _referenced_label(value: object, rtype: str, by_hcl_name: dict[str, str]) -> str | None:
    """The canvas label of the imported `rtype` resource an interpolation points
    at (`vpc_id = aws_vpc.net.id` -> the vpc node's label), or None."""
    target = _ref_target(value)
    return by_hcl_name.get(f"{rtype}.{target}") if target else None


# What odin emits for a record's `records`, spelled once so the warning and the
# comment cannot drift apart.
_ONE_RECORD_VALUE = (
    "odin emits ONE address per record -- one `aws_route53_record` per drawn "
    "route53 -> ec2 edge, holding that instance's `private_ip`"
)


def _record_reference(value: object) -> object:
    """The single interpolation out of an `aws_route53_record`'s `records`.

    PRINTED FROM python-hcl2 BEFORE CODING AGAINST IT (honesty rule 1), because
    this is the one reference in this file that is not a bare string.
    `records = [aws_instance.api_server.private_ip]` parses to the LIST
    `['${aws_instance.api_server.private_ip}']` -- the interpolation is INSIDE
    it, and `_ref_target` reads only a `str`, so handing the list straight to
    `_referenced_label` returns None and EVERY record, odin's own included,
    would be reported as naming a resource outside the supported set.

    The FIRST entry, because odin emits exactly one. A source round-robin record
    listing several addresses keeps only that one, and the loss is reported as a
    CHANGED `records` argument (`_ONE_RECORD_VALUE`) rather than left for the
    next apply to reveal.
    """
    values = value if isinstance(value, list) else []
    return values[0] if values else None


# hcl.py::_DEFAULT_EGRESS, as the parsed block it becomes.
#
# v0.8.14 changes what this is FOR. Through v0.8.13 odin had no canvas field for
# outbound rules at all, so a group whose egress differed from this was a CHANGED
# argument and all the import could do was say "a restricted egress comes back
# UNRESTRICTED". `hcl-generate` added an `egressRules` field and real `egress`
# emission, so the rules are now genuinely carried and that warning would be a
# caveat outliving its fix (honesty rule 3).
#
# It still matters, for the reason `hcl-generate` confirmed: an EMPTY
# `egressRules` field keeps this exact default block, byte-identical. So when an
# imported group's egress IS the default, the honest canvas is one with the field
# EMPTY -- which regenerates the identical file and looks like every hand-drawn
# canvas -- rather than one carrying a synthesized `-1:0:0.0.0.0/0` line that
# happens to generate the same bytes.
_ODIN_EGRESS = {"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}


def _same_literal(value: object, want: object) -> bool:
    """`_literal` equality that also works for a LIST argument.

    `_literal` only unquotes a `str`, so a list falls through to `str(...)` and
    `['"0.0.0.0/0"']` (what python-hcl2 hands back -- quotes retained on the
    MEMBERS) never equals `['0.0.0.0/0']`. Measured: that mismatch made odin's
    own generated egress report itself as a changed argument, i.e. a warning on
    every single security-group import, which is exactly how a real warning gets
    trained out of people.
    """
    if isinstance(want, list):
        return isinstance(value, list) and [_literal(v) for v in value] == [_literal(w) for w in want]
    return _literal(value) == _literal(want)


def _is_odin_default_egress(blocks: list) -> bool:
    """Is this group's egress exactly the wide-open block hcl.py emits for an
    EMPTY `egressRules` field? Then the field stays empty and the round trip is
    byte-identical -- see `_ODIN_EGRESS`."""
    return len(blocks) == 1 and all(
        _same_literal(blocks[0].get(key), want) for key, want in _ODIN_EGRESS.items()
    )


def _readable_rule(line: str) -> bool:
    """Can `hcl.py` read this line BACK?

    This asserts the inverse instead of describing it, and it is here because
    describing it was wrong. It used to be a hand-kept COPY of the generator's
    parse (`line.split(":")` with `len(p) == 3`) while the writer below is
    `":".join(...)` with no check on the parts. So any source containing a colon
    produced a line odin emits happily and then cannot parse -- **an IPv6 CIDR is
    the everyday case**, and a canvas label with a colon in it is the other one.

    Measured before this guard, on a group whose only rule was
    `cidr_blocks = ["2001:db8::/32"]`: the import produced
    `ingressRules = 'tcp:443:2001:db8::/32'` and **zero warnings**, and
    re-generating then dropped the ENTIRE aws_security_group
    (`unsupported: ['web (sg): ingressRules: expected one "protocol:port:source"
    rule per line']`) -- taking every OTHER rule in the group with it, since one
    unreadable line fails the whole field. A clean-looking import that deletes a
    security group on the next Apply is strictly worse than one that says it
    could not carry a rule.

    v0.8.17 stops copying the parse and CALLS it. A copy is only ever as true as
    the last person who remembered to edit both, and the port-range grammar is
    the proof: extending `hcl.parse_sg_rule` to accept `8000-8100` while this
    still read `parts[1].isdigit()` would have made every imported range fail its
    own round-trip check and be dropped -- a new feature that silently narrows a
    firewall, which is the exact defect the feature exists to remove.

    THE IPv6 TERM IS NOT DECORATION, and the first version of that conversion
    left it out and broke four tests. Parsing is only half of "will the generator
    accept this?": `hcl.parse_sg_rule` bounds its split at 2 fields ON PURPOSE, so
    `tcp:443:2001:db8::/32` parses perfectly well and is then declined by
    `_sg_rule_blocks` for being IPv6 -- taking the whole group with it. The old
    bare `split(":")` happened to reject IPv6 by counting colons, which is why
    dropping it looked safe. Asking the real predicate is both narrower (a canvas
    label that CONTAINS a colon now round-trips, where colon-counting refused it)
    and honest about the reason.
    """
    rule = hcl.parse_sg_rule(line)
    return rule is not None and not hcl.is_ipv6_cidr(rule[3])


def _ingress_rule_line(block: dict, by_hcl_name: dict[str, str]) -> str | None:
    """One `protocol:port:source` line from an `ingress {}`/`egress {}` block, or
    None when the block cannot be expressed as one.

    The exact inverse of `hcl.py::_sg_peer`: `cidr_blocks` is a literal
    CIDR, and `security_groups` is another SG NODE'S LABEL -- the
    identity-based "only the web tier may reach me" rule, which is the form the
    Nebula firewall compiles to a `group:` rule. Reading it back as the referenced
    group's label (not its `sg-` id) is what lets the rule survive a round trip
    at all, since the canvas has no ids in it.

    A port RANGE IS CARRIED, since v0.8.17 (`hcl.sg_rule_port` writes the
    `8000-8100` spelling and `hcl.parse_sg_rule` reads it back). This used to
    return None for `from_port != to_port`, which was the honest thing while the
    canvas had no way to say it -- the rule went to the dropped list with a count
    rather than being narrowed to its lower bound in silence. Now BOTH BOUNDS
    survive, and `tests/agent/test_sg_port_ranges.py` asserts both of them: a
    round trip that returned `8000-8000` would satisfy "the rule survived" and
    still have closed a hundred ports.

    What still cannot be expressed is a block with no readable port at all
    (`var.port`, a computed value) -- `_int_text` returns None and the block goes
    to the dropped list. `_readable_rule` is the same refusal for a rule that
    would SERIALIZE fine and not parse back.
    """
    from_port, to_port = _int_text(block.get("from_port")), _int_text(block.get("to_port"))
    protocol = hcl.unquote(block.get("protocol"))
    if from_port is None or to_port is None or not isinstance(protocol, str):
        return None
    cidrs = block.get("cidr_blocks") or []
    groups = block.get("security_groups") or []
    source = next(
        (text for value in cidrs if isinstance(text := hcl.unquote(value), str) and "/" in text),
        None,
    ) or next(
        (label for value in groups
         if (label := _referenced_label(value, "aws_security_group", by_hcl_name))),
        None,
    )
    line = f"{protocol}:{hcl.sg_rule_port(from_port, to_port)}:{source}"
    return line if source and _readable_rule(line) else None


# v0.8.14, CANVAS WIRING, the half that closes limits.md's "an imported ECS
# service loses its canvas wiring entirely".
#
# `hcl-generate` emits one tag per `${{producer.ATTR}}` env reference:
#
#     tags = {
#       "odin:ref:DATABASE_URL" = "db.DATABASE_URL"
#       "odin:node"             = "api"
#     }
#
# THE WRAPPER IS NOT IN THE FILE, and that is the part neither of us could have
# guessed alone. I proposed writing the canvas text `${{db.DATABASE_URL}}`
# verbatim; `hcl-generate` probed OpenTofu 1.12.3 and found it is a PARSE error
# ("Missing key/value separator ... Expected an equals sign"), which fails the
# whole project rather than one resource. The escaped form `$${{...}}` parses,
# but `$`/`{`/`}` are outside AWS's documented tag-value character set, so it
# would not survive being taken to Amazon -- the exact portability failure the
# emitted-policy work exists to fix. Hence the bare `producer.ATTR`, which
# python-hcl2 hands back as an ordinary quoted literal with no `${` in it, so
# `_plain_literal` accepts it and `hcl.unquote` is a real inverse of `quote`.
#
# Reading it back re-authors `data.env`, which `spec/translate.py::_resource`
# lifts straight into `Ref`s -- so `depends_on` re-derives exactly as it does for
# a canvas somebody drew, and this importer does not have to reconstruct it.
_ODIN_REF_PREFIX = "odin:ref:"
_WIRED_KINDS = ("ecs", "lambda")  # mirrors hcl.py::_WIRED_KINDS


def _canvas_refs(attrs: dict) -> dict[str, str]:
    """`{VAR: "${{producer.ATTR}}"}` from a workload's `odin:ref:<VAR>` tags.

    Sorted by variable name, matching the order `hcl-generate` emits them in, so
    generate -> import -> generate is byte-stable. Ordering is only a
    byte-stability concern and not a correctness one: `hcl.py::_depends_on_block`
    is `sorted(set(...))` over `_ref_dependencies`, which itself sorts the ref
    target ids -- so the emitted `depends_on` is a function of the ref target SET
    and cannot be changed by the order of this dict. `limits.md` claimed the
    ordering was lost independently of the references; it is not, it re-derives
    for free once the references come back.
    """
    return {
        name[len(_ODIN_REF_PREFIX):]: f"${{{{{value}}}}}"
        for name, value in sorted(_all_tags(attrs).items())
        if name.startswith(_ODIN_REF_PREFIX) and name[len(_ODIN_REF_PREFIX):] and "." in value
    }


def _depends_on_producers(attrs: dict, by_hcl_name: dict[str, str]) -> list[str]:
    """The canvas labels a workload's `depends_on` names.

    A `depends_on` entry parses as `'${aws_db_instance.app_db}'` -- an
    interpolation wrapper around the `<type>.<name>` key `by_hcl_name` is keyed
    by, and with NO attribute suffix, unlike every other reference in this file.
    So neither `_ref_target` (which pulls the NAME out of `${aws_x.y.arn}`) nor
    the raw string resolves it: an earlier draft used each in turn and the
    warning named "resources this file does not define" for a database sitting
    right there in the same file. Printed the real parsed value rather than
    reasoning about it a third time.
    """
    return [
        found for value in (attrs.get("depends_on") or [])
        if (found := by_hcl_name.get(str(value).strip().removeprefix("${").removesuffix("}")))
    ]


def _stamp_canvas_refs(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str]
) -> list[str]:
    """Re-author every workload's `env` map from its own ref tags, and report a
    workload whose wiring genuinely could not be recovered.

    Applies to ecs AND lambda -- `hcl.py::_WIRED_KINDS` is both, and fixing only
    the kind limits.md named would leave the sibling open, which is this repo's
    most-repeated bug shape.

    The warning is the part that had to change rather than merely be deleted.
    Before v0.8.14 it fired for any workload with a `depends_on`, because no
    wiring could EVER be recovered. Now that most can, a warning that still fired
    would be worse than none: this module's whole value is that its warnings are
    worth reading. So it fires only when the file names producers and carries no
    `odin:ref:` tags to rebuild them from -- an HCL project odin did not generate,
    or one generated by a version that predates the tags.

    A producer that is only the PLACEMENT HOST is excluded, and that exclusion is
    load-bearing: `hcl.py` builds `depends_on` from TWO sources, the node's env
    refs AND `_placement_dependency` (the instance a placed service must not
    start before). Measured end to end through the real CLI before it existed, a
    service drawn inside an ec2 box with no env refs at all was told to "re-add
    the env references it consumed" when it had never had any.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] not in _WIRED_KINDS:
            continue
        attrs = attrs_by_label[label]
        refs = _canvas_refs(attrs)
        node["data"].update({"env": refs} if refs else {})
        host = _placement_host(attrs)
        producers = [name for name in _depends_on_producers(attrs, by_hcl_name) if name != host]
        warnings += [] if refs or not producers else [
            f"{label} ({node['type']}): its canvas wiring could not be imported -- this file "
            "carries no `odin:ref:` tags, which is how odin records a `${{producer.ATTR}}` env "
            "reference without writing the RESOLVED value (that would put a database password into "
            "tfstate). Only the ordering is left, in `depends_on`, and odin re-derives that FROM "
            "the references, so a re-generated project loses the ordering too. This workload "
            f"depended on {', '.join(producers)}; re-add the env references it consumed."
        ]
    return warnings


_ODIN_PLACEMENT_PREFIX = "attribute:odin.instance == "


def _placement_host(attrs: dict) -> str | None:
    """The EC2 node a service's tasks were pinned to, out of its real
    `placement_constraints { type = "memberOf" }`.

    This is the owner's "an ecs box inside an ec2 box means ecs ON ec2" gesture
    as it survives Terraform, so losing it on import would silently move a
    workload back onto the shared host -- a different machine, with different
    memory, reported as a clean import.
    """
    for block in attrs.get("placement_constraints") or []:
        expression = hcl.unquote(block.get("expression"))
        if isinstance(expression, str) and expression.startswith(_ODIN_PLACEMENT_PREFIX):
            return expression[len(_ODIN_PLACEMENT_PREFIX):].strip() or None
    return None


def _container_definition(taskdef: dict) -> dict:
    """The single container out of a task definition's `container_definitions`.

    It is a JSON STRING literal in the HCL, so this needs `unquote` to be a REAL
    inverse of `quote` -- which it only became in this same release. With the old
    quote-stripping version the escaped inner quotes survived and `json.loads`
    could not read it at all.
    """
    raw = hcl.unquote(taskdef.get("container_definitions"))
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed[0] if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) else {}


def _member_text(archive: zipfile.ZipFile, name: str) -> str | None:
    """One zip member as text, or None when it isn't text at all (a vendored
    `.so`, a compiled asset) -- the canvas carries a package as TEXT, so that
    distinction has to survive up to the warning that reports it."""
    with suppress(UnicodeDecodeError):
        return archive.read(name).decode()
    return None


def _lambda_members(archives: dict[str, bytes], filename: str) -> tuple[dict[str, str], list[str]] | None:
    """`({member name: text}, [members that are not text])` out of a function's
    deployment zip, or None when there is no such archive to read.

    odin materializes the zip beside `main.tf` and references it by filename
    (`hcl.py::_lambda`), so the code is recoverable in DIRECTORY mode and simply
    absent in text mode. `_stamp_lambda` reports the difference rather than
    letting odin's `_DEFAULT_LAMBDA_CODE` pass for the user's own function.

    EVERY member, not `namelist()[0]`. Until v0.8.14 a package was one file by
    construction, so reading the first entry was the same thing as reading the
    function -- once `sourceDir` can package a whole tree it stops being: the
    first entry in a multi-file archive is whichever name sorted first, which
    for a function whose handler is in `lambda_function.py` beside a `helpers.py`
    is `helpers.py`. That would have put a helper module on the canvas as the
    function's whole body, silently, and re-applied it as the function.
    """
    raw = archives.get(filename)
    if raw is None:
        return None
    with suppress(zipfile.BadZipFile):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            decoded = {
                name: _member_text(archive, name)
                for name in sorted(archive.namelist()) if not name.endswith("/")
            }
        return (
            {name: text for name, text in decoded.items() if text is not None},
            [name for name, text in decoded.items() if text is None],
        )
    return None


def _carry_lambda_code(node: dict, label: str, filename: str, recovered) -> list[str]:
    """Put the recovered package on the node, and warn about whatever of it
    could not come along. Three outcomes, deliberately separate:

    * ONE text member and nothing else -- the v1 shape. It lands in `code`, the
      field the config panel's textarea edits, exactly as before.
    * MORE than one -- it lands in `files`, the inline `{path: text}` map
      `hcl.py::_lambda_package` re-packages verbatim, so the archive this import
      read and the archive the next Apply writes are byte-identical.
    * NOTHING readable -- the node keeps odin's default placeholder body, and
      that is stated rather than left to be discovered.
    """
    members, binary = recovered or ({}, [])
    if len(members) == 1 and not binary:
        node["data"]["code"] = next(iter(members.values()))
        return []
    if members:
        node["data"]["files"] = members
        return [] if not binary else [
            f"{label} (lambda): {len(binary)} file(s) in {filename} are not text and are NOT on the "
            f"canvas ({', '.join(binary[:4])}) -- a canvas carries a package as text, so a function "
            "with a compiled dependency needs its `sourceDir` set to the real directory instead"
        ]
    return [
        f"{label} (lambda): its CODE could not be imported -- a function's body lives in "
        f"{filename or 'a zip'} beside main.tf, not in the HCL. Reading a directory "
        "(`odin translate import <dir>`) recovers it; from HCL text alone the node comes back "
        "with odin's DEFAULT placeholder payload, which is NOT your function."
    ]


def _statement_resources(statement: dict) -> list[str]:
    """A statement's `Resource` reduced to canvas node LABELS, de-duplicated.

    IAM allows `Resource` to be a bare string OR a list, and odin's generator is
    about to move from the first to the second (`hcl-generate`, v0.8.14: real
    ARNs, always a list, because s3 needs `arn:aws:s3:::b` AND `arn:aws:s3:::b/*`
    to express bucket-plus-objects). Normalizing BOTH is not future-proofing for
    its own sake -- a hand-authored project being imported can legitimately carry
    either, and `dict.__contains__` on a list raises `TypeError: unhashable
    type`, so the un-normalized read crashes the whole import on a perfectly
    valid policy.

    De-duplicated because two ARNs reduce to ONE canvas node (the s3 pair above,
    and logs' `log-group:<n>` + `log-group:<n>:*`): without it, one drawn
    permission comes back as two identical edges and the round trip is not
    stable. Measured -- the s3 pair produced exactly that.

    `gateway/policy.py::arn_label` does the reduction, imported rather than
    reimplemented: it is the same function the gateway's own evaluator uses to
    match a policy against a classified request, so an edge this importer
    reconstructs is by construction one the evaluator would enforce. A second
    reducer here could drift from it and the drift would show up as a permission
    that looks drawn and is not honored.

    It needs the ACTION because the match is service-keyed -- the ARN's service
    field must equal the action's prefix, which is what stops `arn:aws:s3:::*`
    reducing to a bare `*` that would match every resource of every service. It
    returns None for anything that is not an ARN, so a bare LABEL (what odin
    emitted before v0.8.14, and what a hand-authored policy may well carry) falls
    through unchanged and both shapes work with no branch.
    """
    raw = statement.get("Resource")
    values = raw if isinstance(raw, list) else [raw]
    actions = statement.get("Action") or []
    action = str(actions[0]) if isinstance(actions, list) and actions else str(actions)
    return list(dict.fromkeys(
        arn_label(value, action) or value for value in values if isinstance(value, str)
    ))


def _edges_from_role_policies(
    policies: list[dict], node_by_label: dict[str, dict], attrs_by_label: dict[str, dict],
) -> tuple[list[dict], list[str]]:
    """Turn each `aws_iam_role_policy` back into the canvas edges that produced it.

    The policy names a ROLE; the canvas edge starts at the WORKLOAD that role
    belongs to, so the two are matched by walking the workloads and asking which
    role each one carries. That is the same direction `hcl.py` writes it in, and
    it avoids depending on the `<node>-role` naming convention for anything the
    file already states outright.

    ## A statement naming nothing on this canvas is REPORTED, not skipped

    It used to be skipped in silence, on the reasoning that an edge to a
    non-existent resource would be dropped by `canvas_to_stack` anyway. That
    reasoning is sound about the EDGE and wrong about the USER: a dropped IAM
    edge is a dropped permission, and this module's contract is that a round trip
    never loses something without saying so.

    It stopped being hypothetical with `hcl-generate`'s ARN change: `Resource`
    became `arn:aws:s3:::uploads` instead of `uploads`, which matches no canvas
    label, so every drawn permission would have silently vanished from an
    imported canvas -- the whole security posture, reported as a clean import.
    `_statement_resources` reduces the ARN back to a label through the gateway's
    own `arn_label`, and this warning is what remains for the cases it cannot
    reduce (a hand-written policy whose Action is `*`, or a Resource naming
    something that genuinely is not on this canvas).
    """
    role_to_workload: dict[str, str] = {}
    for label, node in node_by_label.items():
        attrs = attrs_by_label.get(label) or {}
        target = _ref_target(attrs.get("role")) or _ref_target(attrs.get("task_role_arn"))
        if target:
            role_to_workload[target] = label
        # An auto-role is named for its workload and referenced by nothing else,
        # which is the only handle an ec2/ecs node gives us.
        role_to_workload.setdefault(f"{sanitize(label)}_role", label)

    out: list[dict] = []
    warnings: list[str] = []
    for policy in policies:
        source = role_to_workload.get(_ref_target(policy.get("role")) or "")
        document = hcl.unquote(policy.get("policy"))
        if source is None or not isinstance(document, str):
            continue
        try:
            parsed = json.loads(document)
        except json.JSONDecodeError:
            continue
        for statement in parsed.get("Statement") or []:
            actions = statement.get("Action") or []
            resources = _statement_resources(statement)
            targets = [r for r in resources if r in node_by_label]
            unresolved = [r for r in resources if r not in node_by_label]
            # Endpoints are LABELS, matching the subscription edges above --
            # `canvas_to_stack` resolves an id through `labels` and falls back to
            # the raw value, so a label works and reads better in a saved canvas.
            out += [
                {"source": source, "target": target,
                 "data": {"edgeType": "iam", "permissions": list(actions)}}
                for target in targets if actions
            ]
            warnings += [] if not (unresolved and actions) else [
                f"{source} (iam): a granted permission could not be imported as an edge -- its "
                f"policy allows {', '.join(str(a) for a in actions)} on "
                f"{', '.join(unresolved)}, which names no node on this canvas. The PERMISSION IS "
                "LOST: the imported canvas grants less than the source did."
            ]
    return out, warnings


def _stamp_lambda(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str],
    role_names: dict[str, str], archives: dict[str, bytes],
) -> tuple[list[str], set[str]]:
    """Resolve each function's role and code. Returns (warnings, labels to DROP).

    ## The phantom role, which was a defect before lambda import existed

    A lambda drawn with no `role` gets an AUTO-GENERATED `aws_iam_role`
    (`hcl.py` pass 3, `name = "<function>-role"`). Because `_KIND` maps
    `aws_iam_role` to a real canvas kind, importing odin's own generated project
    produced an `iam_role` NODE THE USER NEVER DREW -- measured before this
    function existed: a one-lambda canvas round-tripped into a canvas containing
    one `iam_role` called `thumbnailer-role` and no function at all. So the role
    is dropped here when it is odin's own, and the node's `role` field is left
    empty, which is exactly what the canvas said in the first place.

    Detecting it reconstructs hcl.py's `<function>-role` NAMING CONVENTION, and
    that is a compromise worth naming: the generated HCL carries no marker
    distinguishing an auto-role from a drawn one (both get the same
    `assume_role_policy` -- `_iam_role` and the auto pass emit the identical
    default Lambda trust policy), so the name is the only signal available. A
    user who draws a role and happens to call it `<function>-role` gets it folded
    in; the effect is that their `role` field comes back empty and odin
    regenerates the same role, so the Terraform is unchanged.
    """
    warnings: list[str] = []
    drop: set[str] = set()
    for label, node in node_by_label.items():
        if node["type"] != "lambda":
            continue
        attrs = attrs_by_label[label]

        role_label = _referenced_label(attrs.get("role"), "aws_iam_role", by_hcl_name)
        auto = role_label is not None and role_names.get(role_label) == f"{label}-role"
        if role_label and not auto:
            node["data"]["role"] = role_label
        if auto:
            drop.add(role_label)
        warnings += [] if role_label or attrs.get("role") is None else [
            f"{label} (lambda): its `role` names no imported aws_iam_role, so odin will "
            "auto-generate an execution role for it on the next Apply"
        ]

        filename = hcl.unquote(attrs.get("filename"))
        recovered = _lambda_members(archives, filename) if isinstance(filename, str) else None
        warnings += _carry_lambda_code(node, label, filename, recovered)
    return warnings, drop


def _stamp_ecs_taskdef(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str],
    taskdefs: dict[str, dict],
) -> list[str]:
    """Fold each service's task definition back onto its node, and say what a
    round trip cannot bring with it.

    `image`, `port`, `memory` and `cpu` are all on the TASK DEFINITION, not the
    service, so without this the node comes back with a default image and no
    resources -- an nginx placeholder where the user's own container was.

    THE WIRING IS THE HONEST GAP, and it is not this importer's doing: a
    workload's `${{db.DATABASE_URL}}` refs are deliberately NEVER interpolated
    into the HCL (`hcl.py`'s `_WIRED_KINDS` note -- a resolved DATABASE_URL
    carries the database password, so writing it into the generated config would
    put a credential in `terraform.tfstate` in plaintext AND drift on every
    plan). Only `depends_on` survives, which names WHICH producers the service
    consumed but not which variable or attribute. So the ordering round-trips and
    the values cannot, and an imported service will start with no configuration
    it did not have on the canvas. `depends_on` is used to name the producers in
    the warning, which is the most an import can honestly offer here.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] != "ecs":
            continue
        attrs = attrs_by_label[label]

        target = _ref_target(attrs.get("task_definition"))
        taskdef = taskdefs.get(target or "", {})
        if not taskdef:
            warnings.append(
                f"{label} (ecs): its `task_definition` names no imported aws_ecs_task_definition, so "
                "the service comes back with odin's DEFAULT image and no port -- not the container "
                "it was running"
            )
        container = _container_definition(taskdef)
        image = container.get("image")
        if isinstance(image, str):
            node["data"]["image"] = image
        ports = container.get("portMappings") or []
        port = ports[0].get("containerPort") if ports and isinstance(ports[0], dict) else None
        if port is not None:
            node["data"]["port"] = str(port)
        for attr, field in (("memory", "memory"), ("cpu", "cpu")):
            value = _int_text(taskdef.get(attr))
            if value is not None:
                node["data"][field] = value

        host = _placement_host(attrs)
        if host:
            node["data"]["host"] = host

    return warnings


def _root_directory_path(value: object) -> str | None:
    """The directory a `root_directory` roots a mount at, or None when the source
    does not state it as a plain literal.

    TWO SPELLINGS, ONE MEANING, which is why this takes a value rather than a
    key: on an `aws_efs_access_point` it is a BLOCK (`root_directory { path =
    "/data" }`, which python-hcl2 hands back as a list of dicts), and inside a
    task definition's `efs_volume_configuration` it is a plain string ARGUMENT.
    """
    block = value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else None
    return _plain_literal(block.get("path") if block is not None else value)


def _root_directory_lines(attrs: dict, key: str) -> list[str]:
    """The CHANGED line for a mount rooted somewhere other than the top of the
    file system.

    A `_FIXED_VALUES` entry cannot express this -- that table compares
    `attrs[key]` directly, and a block never equals a scalar -- and routing it
    through `_attribute_notes`'s `also_changed` would print the parsed block
    verbatim as `[{'path': '"/data"'}]`. So the line is rendered here, with the
    real path in it.

    The direction is what makes it worth a warning: odin re-roots the mount at
    `/`, so the consumer comes back seeing the WHOLE file system where the source
    had confined it to one subtree. A widening, reported by name.
    """
    path = _root_directory_path(attrs.get(key)) if key in attrs else _ODIN_EFS_ROOT
    return [] if path == _ODIN_EFS_ROOT else [
        f"{key}={path or _literal(attrs[key])} (odin always roots this mount at "
        f"{_ODIN_EFS_ROOT}, so the consumer sees the WHOLE file system, not that subtree)"
    ]


def _efs_volume_notes(block: dict) -> tuple[list[str], list[str]]:
    """`(dropped, changed)` for one task-definition `volume {}` block holding an
    EFS mount -- the arguments an EDGE cannot carry.

    `transit_encryption` and `authorization_config` are the two that matter, and
    neither survives: odin's substrate is a host directory bind-mounted into the
    container, so there is no TLS session to encrypt and no access point to
    authorize against. A mount the source encrypted coming back plain, with no
    word said, is this module's worst available failure.

    The owner passed to `_attribute_notes` is the resource these blocks live on
    rather than a name invented for them, so a `_FIXED_VALUES` entry added for
    `aws_ecs_task_definition` later would be honoured here for free.
    """
    config = (block.get("efs_volume_configuration") or [{}])[0]
    dropped, _ = _attribute_notes(
        "aws_ecs_task_definition", block, _CARRIED_EFS_VOLUME_ATTRS, (), {},
    )
    inner, _ = _attribute_notes(
        "aws_ecs_task_definition", config, _CARRIED_EFS_VOLUME_CONFIG_ATTRS, (), {},
    )
    return (
        sorted(dropped + [f"efs_volume_configuration.{name}" for name in inner]),
        [f"efs_volume_configuration.{line}" for line in _root_directory_lines(config, "root_directory")],
    )


def _ecs_efs_mounts(
    label: str, attrs: dict, taskdefs: dict[str, dict], by_hcl_name: dict[str, str],
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """`([(efs label, container path, access point name)], warnings)` for one ecs
    service. The access point is always None on this side -- ECS reaches the file
    system directly.

    THE MOUNT IS A JOIN, not a lookup, and that is the whole difficulty: the task
    definition's `volume {}` block names the FILE SYSTEM
    (`efs_volume_configuration.file_system_id`) and the container definition's
    `mountPoints[]` names the PATH, and the only thing tying the two together is
    the volume's own `name` matching a mountPoint's `sourceVolume`. Either half
    missing is a mount that cannot be reconstructed, and each is reported
    separately because they cost different things.

    A `host {}` or `docker_volume_configuration {}` volume is NOT an efs mount and
    is skipped rather than reported: calling it a lost efs mount would be a lie
    about its type, and a mountPoint pointing at one is perfectly ordinary.
    """
    taskdef = taskdefs.get(_ref_target(attrs.get("task_definition")) or "", {})
    container = _container_definition(taskdef)
    paths = {
        point.get("sourceVolume"): point.get("containerPath")
        for point in container.get("mountPoints") or [] if isinstance(point, dict)
    }
    mounts: list[tuple[str, str, str | None]] = []
    warnings: list[str] = []
    for block in taskdef.get("volume") or []:
        configs = block.get("efs_volume_configuration") or []
        if not configs:
            continue
        name = hcl.unquote(block.get("name"))
        efs = _referenced_label(configs[0].get("file_system_id"), "aws_efs_file_system", by_hcl_name)
        path = paths.get(name)
        if efs is None:
            warnings.append(
                f"{label} (ecs): its task definition mounts a volume named {name!r} whose "
                "`file_system_id` names no imported aws_efs_file_system. THE MOUNT IS LOST: the "
                "regenerated task definition carries no volume at all, so the container starts with "
                "an empty directory where its shared data was"
            )
            continue
        if path is None:
            warnings.append(
                f"{efs} -> {label} (efs mount): the task definition DECLARES this volume and no "
                "container mounts it (no `mountPoints` entry has "
                f"`sourceVolume = {name!r}`), so it does nothing today. odin's canvas cannot express "
                "a declared-but-unmounted volume -- an edge means mounted -- so the regenerated task "
                "definition omits the volume block"
            )
            continue
        mounts.append((efs, path, None))
        dropped, changed = _efs_volume_notes(block)
        warnings += _attribute_warnings(f"{efs} -> {label} (efs mount)", "", dropped, changed)
    return mounts, warnings


def _lambda_efs_mounts(
    label: str, attrs: dict, access_points: dict[str, dict], by_hcl_name: dict[str, str],
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """`([(efs label, local mount path, access point resource name)], warnings)`
    for one function.

    THE ACCESS POINT IS IN THE MIDDLE and it is not optional:
    `file_system_config.arn` is an access-point arn, so the function names the
    access point and the access point names the file system. Two hops, two ways
    to lose the mount, reported separately.
    """
    mounts: list[tuple[str, str, str | None]] = []
    warnings: list[str] = []
    for block in attrs.get("file_system_config") or []:
        ap_name = _ref_target(block.get("arn"))
        ap_attrs = access_points.get(ap_name or "")
        efs = _referenced_label(
            (ap_attrs or {}).get("file_system_id"), "aws_efs_file_system", by_hcl_name,
        )
        if efs is None:
            warnings.append(
                f"{label} (lambda): its `file_system_config.arn` does not resolve to an imported "
                "aws_efs_file_system -- " + (
                    "it names no imported aws_efs_access_point" if ap_attrs is None else
                    "the aws_efs_access_point it names has a `file_system_id` that does"
                    " not either"
                ) + ". THE MOUNT IS LOST: the regenerated function has no file_system_config, so it "
                "runs with nothing at that path"
            )
            continue
        # An unreadable path is carried as "" rather than skipped: the MOUNT is
        # real and its edge must come back, and `_stamp_efs_paths` reports the
        # path separately rather than letting odin's default pass for the
        # source's own.
        mounts.append((efs, _plain_literal(block.get("local_mount_path")) or "", ap_name))
    return mounts, warnings


def _stamp_efs_paths(
    node_by_label: dict[str, dict], mounts: list[tuple[str, str, str, str]],
) -> list[str]:
    """Put ONE mount path on each efs node and report every consumer that used a
    different one. `mounts` is `[(efs label, consumer label, consumer kind, path)]`.

    AWS lets each consumer mount a file system wherever it likes; odin's tile has
    a single `path` field and `hcl.py` re-emits it to every consumer, so a source
    that disagrees cannot round-trip. Substituting in silence is the elasticache
    bug in another costume -- a function told to read `/mnt/config` comes back
    reading `/mnt/efs`, finds an empty directory, and nothing said so.

    WHICH path wins is not arbitrary, and the asymmetry is measured from
    botocore's own models rather than assumed. Lambda's `LocalMountPath` carries
    `pattern: /mnt/[a-zA-Z0-9-_.]+` -- ONE segment under /mnt, and the provider
    enforces it client-side -- while ECS's `containerPath` has no pattern at all.
    So a lambda's path is always legal for an ecs task and an ecs task's `/data`
    is NOT legal for a lambda: preferring the lambda's can never produce a project
    odin then declines by name, and preferring the other one can. Ties inside a
    kind break on the consumer label, so the answer never depends on file order.
    """
    warnings: list[str] = []
    by_efs: dict[str, list[tuple[str, str, str]]] = {}
    for efs, consumer, kind, path in sorted(mounts):
        by_efs.setdefault(efs, []).append((kind, consumer, path))
    for efs, entries in sorted(by_efs.items()):
        ranked = sorted(entries, key=lambda entry: (entry[0] != "lambda", entry[1]))
        paths = {consumer: path for _kind, consumer, path in ranked if path}
        unreadable = sorted(consumer for _kind, consumer, path in ranked if not path)
        warnings += [] if not unreadable else [
            f"{efs} (efs): {', '.join(unreadable)} mount(s) it at a `local_mount_path` odin cannot "
            "read as a literal, so the canvas gets odin's own default mount path -- not the one the "
            "source asked for"
        ]
        if not paths:
            continue
        chosen = next(path for _kind, consumer, path in ranked if path)
        node_by_label[efs]["data"]["path"] = chosen
        # A path a LAMBDA cannot mount, reported here at import time rather than
        # left to surface much later as an Apply that silently drops a function
        # -- `_stamp_containment`'s rule, and field test U2's.
        #
        # It ASKS THE REAL PREDICATE (`hcl._MOUNT_PATH`, compiled from the
        # pattern `hcl.py` reads out of botocore) instead of restating it, for
        # `_readable_rule`'s reason: a copy is only ever as true as the last
        # person who edited both.
        #
        # GATED ON A LAMBDA CONSUMER EXISTING, and that gate is the whole
        # correctness of this guard. AWS constrains only Lambda's
        # `LocalMountPath`; ECS's `containerPath` has no pattern at all, so
        # `/data` is a legal ecs-only mount that odin's bind-mount substrate
        # serves happily and `hcl.py` emits without complaint. An earlier version
        # fired on the node's path alone and told the user their file system
        # would be dropped -- true against a generator that applied Lambda's
        # pattern to every efs node, false the moment that was narrowed to
        # decline only the offending FUNCTION. The pattern it reads was real
        # throughout; the conclusion it drew from it went stale.
        #
        # Reachable despite `ranked` preferring a lambda's path: a lambda whose
        # own `local_mount_path` is computed contributes no readable path, so an
        # ecs consumer's `/data` wins and lands on a node that function mounts.
        mounts_on_lambda = any(kind == "lambda" for kind, _consumer, _path in ranked)
        warnings += [] if hcl._MOUNT_PATH.fullmatch(chosen) or not mounts_on_lambda else [
            f"{efs} (efs): the canvas gets `path = {chosen}`, which a Lambda function cannot mount "
            f"(AWS's pattern is {hcl._LOCAL_MOUNT_PATH_PATTERN}, one segment under /mnt). The file "
            "system and its ecs mounts are fine; odin declines the FUNCTION by name on the next "
            "Apply, so give it a path under /mnt or mount it on a service instead"
        ]
        changes = _derived_changes(
            (consumer, path, chosen) for consumer, path in sorted(paths.items())
        )
        warnings += _attribute_warnings(
            f"{efs} (efs)", "per-consumer mount ", [],
            [f"{consumer} mounts it at {paths[consumer]} ({why} to EVERY consumer, because the "
             "canvas tile carries ONE path field)" for consumer, why in sorted(changes.items())],
        )
    return warnings


def _stamp_ec2_wiring(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str],
    key_pairs: dict[str, dict],
) -> list[str]:
    """Rebuild an instance's containment, security groups and SSH key.

    Three references, each of which decides whether the node can be applied at
    all or what it is protected by, so each has its own warning rather than one
    vague line:

    * `subnet_id` -> the `subnet`/`vpc` stamps. `hcl.py::_ec2` REFUSES to build
      an instance that is not inside a subnet, so a missing stamp is a node Apply
      silently skips.
    * `vpc_security_group_ids` -> the `securityGroups` field, one LABEL per line
      (`_security_group_refs` reads exactly that). An id odin cannot resolve to
      an imported group is dropped and counted -- the regenerated instance is
      then in FEWER groups than the source, which for an inbound-deny default
      means less reachable, not more exposed, but is still a posture change.
    * `key_name` -> the companion `aws_key_pair`'s `public_key`, followed by
      REFERENCE rather than by reconstructing the `<name>_key` naming convention:
      the convention is hcl.py's private business, and a hand-authored project
      names its key pairs however it likes.

    A post-pass for `_stamp_sg_rules`' reason -- a group or key pair may be
    defined after the instance that points at it.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] != "ec2":
            continue
        attrs = attrs_by_label[label]

        subnet = _referenced_label(attrs.get("subnet_id"), "aws_subnet", by_hcl_name)
        vpc = node_by_label[subnet]["data"].get("vpc") if subnet in node_by_label else None
        node["data"].update({"subnet": subnet, "vpc": vpc} if subnet and vpc else {})
        warnings += [] if subnet and vpc else [
            f"{label} (ec2): imported without containment -- its `subnet_id` names no imported "
            "aws_subnet inside an imported aws_vpc, so Apply will skip it (\"not contained inside "
            "a Subnet on the canvas\") until you draw a VPC + Subnet and drop it inside"
        ]

        wanted = attrs.get("vpc_security_group_ids") or []
        groups = [
            found for value in wanted
            if (found := _referenced_label(value, "aws_security_group", by_hcl_name))
        ]
        if groups:
            node["data"]["securityGroups"] = "\n".join(groups)
        lost = len(wanted) - len(groups)
        warnings += [] if not lost else [
            f"{label} (ec2): {lost} of {len(wanted)} security group(s) could not be imported -- "
            "the id names no imported aws_security_group, so the regenerated instance is in FEWER "
            "groups than the source and the rules those groups carried do not apply to it"
        ]

        target = _ref_target(attrs.get("key_name"))
        public_key = hcl.unquote((key_pairs.get(target) or {}).get("public_key")) if target else None
        if isinstance(public_key, str):
            node["data"]["key"] = public_key
        warnings += [] if target is None or isinstance(public_key, str) else [
            f"{label} (ec2): its `key_name` names no imported aws_key_pair, so the instance comes "
            "back with NO SSH key and you will not be able to log in to it"
        ]
    return warnings


def _stamp_sg_rules(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str]
) -> list[str]:
    """Rebuild each security group's `ingressRules` AND `egressRules` text.

    A POST-pass, like `_stamp_containment`, and for the same reason: a rule may
    reference a group defined LATER in the file, which is not yet in
    `by_hcl_name` while the nodes are still being built.

    An unexpressible block is named rather than dropped in silence. Losing a
    rule quietly is the worst import defect available here, and the two
    directions fail in OPPOSITE directions, which is why they do not share a
    warning:

    * a dropped INGRESS rule makes the regenerated group MORE restrictive than
      the source -- traffic that used to be allowed stops.
    * a dropped EGRESS rule can make it WIDE OPEN, because an empty
      `egressRules` field is what tells hcl.py to emit its allow-everything
      default (`_ODIN_EGRESS`). So a group whose only egress rule odin cannot
      express does not come back with no egress; it comes back with all of it.
      That is the dangerous direction and it gets its own sentence.
    """
    warnings: list[str] = []
    for label, node in node_by_label.items():
        if node["type"] != "sg":
            continue
        attrs = attrs_by_label[label]

        blocks = attrs.get("ingress") or []
        lines = [_ingress_rule_line(block, by_hcl_name) for block in blocks]
        kept = [line for line in lines if line]
        if kept:
            node["data"]["ingressRules"] = "\n".join(kept)
        lost = len(lines) - len(kept)
        warnings += [] if not lost else [
            f"{label} (sg): {lost} of {len(lines)} ingress rule(s) could not be imported -- odin's "
            "rule is one `protocol:port:source` (the port may be a `8000-8100` range) with a CIDR "
            "or an imported security group, so an IPv6 CIDR (it contains the `:` the rule format "
            "separates on), a port that is not a literal number, or a source odin cannot resolve "
            "is left out. The regenerated group allows LESS inbound than the source did."
        ]

        # The default block is left as an EMPTY field on purpose: it regenerates
        # byte-identically and an imported canvas then looks like a hand-drawn
        # one, which is what `hcl-generate` recommended when we agreed the format.
        egress_blocks = attrs.get("egress") or []
        default = _is_odin_default_egress(egress_blocks)
        egress_lines = [] if default else [
            _ingress_rule_line(block, by_hcl_name) for block in egress_blocks
        ]
        egress_kept = [line for line in egress_lines if line]
        if egress_kept:
            node["data"]["egressRules"] = "\n".join(egress_kept)
        egress_lost = len(egress_lines) - len(egress_kept)
        warnings += [] if not egress_lost else [
            f"{label} (sg): {egress_lost} of {len(egress_lines)} egress rule(s) could not be "
            "imported -- odin's rule is one `protocol:port:destination` (the port may be a "
            "`8000-8100` range) with a CIDR or an imported security group, so an IPv6 CIDR, a "
            "port that is not a literal number, or an unresolvable destination is left out."
            + (
                " NONE of them survived, so the field is empty and odin re-emits its WIDE-OPEN "
                "default: this group's outbound traffic comes back UNRESTRICTED."
                if not egress_kept else
                " The regenerated group allows LESS outbound than the source did."
            )
        ]
    return warnings


def _stamp_containment(
    node_by_label: dict[str, dict], attrs_by_label: dict[str, dict], by_hcl_name: dict[str, str]
) -> list[str]:
    """Rebuild the canvas's containment stamps from the source's own references.

    Containment on the canvas is not geometry to the backend: it is the
    `data.vpc`/`data.subnet` fields the UI stamps onto a node drawn inside a
    container box (`ui/src/lib/containment.ts`), which `spec/translate.py`
    carries through like any other field and `iac/hcl.py` turns into
    `vpc_id`/`subnets`. So the inverse is exact: a subnet's `vpc_id` and a load
    balancer's `subnets` name the containers it belongs to.

    Returns a warning for any node that needed containment and couldn't get it
    -- reported HERE, at import time, instead of surfacing much later as
    Apply's "not contained inside a Subnet on the canvas" for a defect created
    at import (field test U2).
    """
    warnings: list[str] = []
    # v0.8.4: an sg's `vpc_id` is containment too, and EXACTLY as load-bearing --
    # `hcl.py::_sg` refuses to build a group that is not inside a VPC, so an
    # imported group without this stamp is a node Apply will skip.
    for label, node in node_by_label.items():
        if node["type"] != "sg":
            continue
        vpc = _referenced_label(attrs_by_label[label].get("vpc_id"), "aws_vpc", by_hcl_name)
        node["data"].update({"vpc": vpc} if vpc else {})
        warnings += [] if vpc else [
            f"{label} (sg): imported without containment -- its `vpc_id` names no imported "
            "aws_vpc, so Apply will skip it until you draw a VPC on the canvas and drop it inside"
        ]
    subnets = [(label, node) for label, node in node_by_label.items() if node["type"] == "subnet"]
    for label, node in subnets:
        vpc = _referenced_label(attrs_by_label[label].get("vpc_id"), "aws_vpc", by_hcl_name)
        node["data"].update({"vpc": vpc} if vpc else {})
        warnings += [] if vpc else [
            f"{label} (subnet): imported without containment -- its `vpc_id` names no imported "
            "aws_vpc, so Apply will skip it until you draw a VPC on the canvas and drop it inside"
        ]
    for label, node in node_by_label.items():
        if node["type"] != "alb":
            continue
        wanted = attrs_by_label[label].get("subnets") or []
        subnet = next(
            (found for value in wanted
             if (found := _referenced_label(value, "aws_subnet", by_hcl_name))),
            None,
        )
        vpc = node_by_label[subnet]["data"].get("vpc") if subnet in node_by_label else None
        node["data"].update({"subnet": subnet, "vpc": vpc} if subnet and vpc else {})
        warnings += [] if subnet and vpc else [
            f"{label} (alb): imported without containment -- its `subnets` name no imported "
            "aws_subnet inside an imported aws_vpc, so Apply will skip it (\"not contained inside "
            "a Subnet on the canvas\") until you draw a VPC + Subnet and drop it inside"
        ]
        # A canvas node sits in exactly ONE Subnet box, so only the first
        # resolvable subnet becomes containment. Real load balancers are
        # multi-AZ by requirement (the API rejects a single subnet for an ALB),
        # so this is the common case, not the exotic one -- and it changed the
        # regenerated `subnets` list in silence through v0.7.5.
        warnings += [
            f"{label} (alb): imported into ONE subnet ({subnet}) of the {len(wanted)} its `subnets` "
            "names -- a canvas node lives inside a single Subnet box, so the rest are dropped and "
            "the regenerated aws_lb spans one subnet"
        ] if subnet and vpc and len(wanted) > 1 else []
    return warnings


def _place(node: dict, x: int, y: int, size: tuple[int, int] | None = None) -> None:
    node["position"] = {"x": x, "y": y}
    node.update({"size": {"width": size[0], "height": size[1]}} if size else {})


def _subnet_size(children: list[dict]) -> tuple[int, int]:
    width = max(_MIN_SUBNET_SIZE[0], 2 * _PAD + len(children) * (_LEAF_SIZE[0] + _PAD))
    return width, max(_MIN_SUBNET_SIZE[1], _HEADER + _LEAF_SIZE[1] + _PAD)


def _layout(nodes: list[dict]) -> None:
    """Nest imported nodes GEOMETRICALLY, so the canvas agrees with the stamps.

    The stamps alone aren't enough: the browser re-derives containment from
    geometry every time nodes are measured or dragged, and strips a stamp whose
    node isn't visually inside its box -- so an imported load balancer parked on
    a flat row at y=0 would lose its containment on the first render. Sizes and
    the 20px grid come from the UI's own defaults.

    A project with no containers keeps the flat 220px row exactly as before.
    """
    vpcs = [n for n in nodes if n["type"] == "vpc"]
    subnets = [n for n in nodes if n["type"] == "subnet"]
    if not (vpcs or subnets):
        return
    leaves = [n for n in nodes if n["type"] not in _CONTAINER_KINDS]
    nested: list[int] = []
    x = bottom = 0
    for vpc in vpcs:
        own_subnets = [s for s in subnets if s["data"].get("vpc") == vpc["data"]["label"]]
        children = [[c for c in leaves if c["data"].get("subnet") == s["data"]["label"]]
                    for s in own_subnets]
        sizes = [_subnet_size(kids) for kids in children]
        width = max(_MIN_VPC_SIZE[0], 2 * _PAD + max((w for w, _ in sizes), default=0))
        height = max(_MIN_VPC_SIZE[1], _HEADER + sum(h + _PAD for _, h in sizes) + _PAD)
        _place(vpc, x, 0, (width, height))
        y = _HEADER
        for subnet, kids, (sub_w, sub_h) in zip(own_subnets, children, sizes, strict=True):
            _place(subnet, x + _PAD, y, (sub_w, sub_h))
            for index, child in enumerate(kids):
                _place(child, x + 2 * _PAD + index * (_LEAF_SIZE[0] + _PAD), y + _HEADER)
            nested += [id(subnet), *(id(kid) for kid in kids)]
            y += sub_h + _PAD
        x += width + 2 * _PAD
        bottom = max(bottom, height)
    loose = [n for n in nodes if n["type"] != "vpc" and id(n) not in nested]
    for index, node in enumerate(loose):
        _place(node, index * _GRID_STEP, bottom + 3 * _PAD,
               _MIN_SUBNET_SIZE if node["type"] == "subnet" else None)


def parse_hcl(files: dict[str, str], archives: dict[str, bytes] | None = None) -> ImportResult:
    """Mode (a) core: `files` maps filename -> HCL text (a single-string
    caller passes `{"main.tf": text}`)."""
    try:
        triples = hcl.parse_tf(files)
    except Exception as exc:
        # A genuine PARSE failure -- a hard error (finding #7), distinct from a
        # well-formed file with only unsupported resources.
        return ImportResult(parse_error=f"HCL failed to parse: {exc}")

    by_hcl_name: dict[str, str] = {}  # "aws_sns_topic.alerts" -> canvas label
    nodes: list[dict] = []
    unsupported: list[Unsupported] = []
    warnings: list[str] = []
    subscriptions: list[tuple[str, dict]] = []
    secret_versions: list[tuple[str, dict]] = []
    alb_companions: list[tuple[str, str, dict]] = []
    volume_attachments: list[tuple[str, dict]] = []
    dns_records: list[tuple[str, dict]] = []
    access_points: dict[str, dict] = {}
    apigw_companions: list[tuple[str, str, dict]] = []  # v0.8.19
    key_pairs: dict[str, dict] = {}
    taskdefs: dict[str, dict] = {}
    role_policies: list[dict] = []
    node_by_label: dict[str, dict] = {}
    attrs_by_label: dict[str, dict] = {}
    index = 0

    for rtype, rname, attrs in triples:
        if rtype == "aws_sns_topic_subscription":
            subscriptions.append((rname, attrs))
            continue
        if rtype == "aws_secretsmanager_secret_version":
            secret_versions.append((rname, attrs))
            continue
        if rtype in _ALB_COMPANION_TYPES:
            alb_companions.append((rtype, rname, attrs))
            continue
        if rtype == "aws_volume_attachment":
            # v0.8.18: a COMPANION that becomes an EDGE, like an sns
            # subscription -- it is how the canvas says which instance a volume
            # is attached to, and it never becomes a node.
            volume_attachments.append((rname, attrs))
            continue
        if rtype == "aws_route53_record":
            # v0.8.19: a COMPANION that becomes an EDGE, exactly like an
            # attachment -- it is how the canvas says which instance a name
            # resolves to, and it never becomes a node of its own.
            dns_records.append((rname, attrs))
            continue
        if rtype in _APIGW_COMPANION_TYPES:
            # v0.8.19. The INTEGRATION becomes an edge; the routes and the stage
            # are checked against what odin would emit and then folded away.
            apigw_companions.append((rtype, rname, attrs))
            continue
        if rtype == _IAM_POLICY_TYPE:
            role_policies.append(attrs)
            continue
        if rtype in _ECS_COMPANION_TYPES:
            # The task definition folds onto its service; the cluster is a
            # singleton odin always emits and the canvas has no kind for.
            if rtype == "aws_ecs_task_definition":
                taskdefs[rname] = attrs
            continue
        if rtype == _EFS_ACCESS_POINT_TYPE:
            # v0.8.19: a COMPANION, like an `aws_key_pair` -- it folds onto the
            # file system a lambda mounts through it and never becomes a node.
            # Keyed by HCL resource name because that is what the function's
            # `file_system_config.arn` interpolation names.
            access_points[rname] = attrs
            continue
        if rtype == "aws_key_pair":
            # A COMPANION, like a secret version: it folds onto the instance that
            # references it and never becomes a node. Keyed by HCL resource name
            # because that is what the instance's `key_name` interpolation names.
            key_pairs[rname] = attrs
            continue
        kind = _KIND.get(rtype)
        if kind is None:
            unsupported.append(Unsupported(type=rtype, name=rname, reason=f"{rtype} -- not supported by odin's import (yet)"))
            continue
        label = _label(rtype, rname, attrs)
        by_hcl_name[f"{rtype}.{rname}"] = label
        node = {
            "id": label, "type": kind,
            "position": {"x": index * _GRID_STEP, "y": 0},
            "data": _node_data(kind, label, attrs),
        }
        nodes.append(node)
        node_by_label[label] = node
        attrs_by_label[label] = attrs
        dropped, changed = _attribute_notes(
            kind, attrs, _carried(kind),
            _uncarried_attribute_blocks(attrs, node["data"]),
            {**_renamed_by_import(rtype, attrs, label), **_unreadable_numbers(kind, attrs)},
        )
        warnings += _attribute_warnings(f"{label} ({kind})", "", dropped, changed)
        index += 1

    # W2.4: a companion `aws_secretsmanager_secret_version` carries the VALUE,
    # which on the canvas is a field of the secret node itself -- so it's
    # assembled ONTO that node rather than imported as a node of its own (the
    # exact inverse of hcl.py's own companion pass, so generate -> import ->
    # generate round-trips). Mirrors the subscription pass below: an
    # unresolvable reference is reported, never silently dropped.
    for rname, attrs in secret_versions:
        target = _ref_target(attrs.get("secret_id"))
        label = by_hcl_name.get(f"aws_secretsmanager_secret.{target}") if target else None
        node = node_by_label.get(label) if label else None
        value = hcl.unquote(attrs.get("secret_string"))
        # Only a plain literal is a real value; a computed one ("${...}", e.g.
        # `secret_string = jsonencode(...)` or a var reference) can't be carried.
        if node is not None and isinstance(value, str) and "${" not in value:
            node["data"]["secretString"] = value
            version_type = "aws_secretsmanager_secret_version"
            dropped, changed = _attribute_notes(
                version_type, attrs, _CARRIED_COMPANION_ATTRS[version_type], (), {},
            )
            warnings += _attribute_warnings(f"{label} (secret)", f"{version_type} ", dropped, changed)
            continue
        unsupported.append(Unsupported(
            type="aws_secretsmanager_secret_version", name=rname,
            reason="secret value not carried -- it references a secret outside the supported set, or isn't a literal",
        ))

    # W2.5: fold the alb's two companion resources onto its node. A LISTENER
    # names its load balancer directly (`load_balancer_arn`) and, through its
    # forward action's `target_group_arn`, the target group -- so the listener
    # is what ties the trio together and is walked first. A target group with no
    # listener pointing at it can't be attributed to any load balancer, so it's
    # reported rather than guessed at (the subscription pass's rule).
    target_groups = {f"aws_lb_target_group.{rname}": attrs for rtype, rname, attrs in alb_companions if rtype == "aws_lb_target_group"}
    claimed_target_groups: set[str] = set()
    for rtype, rname, attrs in alb_companions:
        if rtype != "aws_lb_listener":
            continue
        alb_target = _ref_target(attrs.get("load_balancer_arn"))
        node = node_by_label.get(by_hcl_name.get(f"aws_lb.{alb_target}", "")) if alb_target else None
        if node is None:
            unsupported.append(Unsupported(
                type=rtype, name=rname,
                reason="listener references a load balancer outside the supported set",
            ))
            continue
        node["data"]["listenerPort"] = str(_int_attr(attrs.get("port"), 80))
        tg_key = _forward_target_group(attrs)
        tg_attrs = target_groups.get(tg_key) if tg_key else None
        if tg_attrs is None:
            unsupported.append(Unsupported(
                type=rtype, name=rname,
                reason="listener's forward action names no importable target group -- port/health check not carried",
            ))
            continue
        claimed_target_groups.add(tg_key)
        node["data"]["port"] = str(_int_attr(tg_attrs.get("port"), 80))
        node["data"]["healthCheckPath"] = _health_check_path(tg_attrs)
        # The companions' own arguments are held to the same honesty rule as a
        # primary resource's: v0.7.0 folded them on and said nothing about what
        # it left behind (a target group's `matcher = "200-299"`, every
        # health_check member but `path`).
        label = node["data"]["label"]
        for companion_type, companion_attrs in (
            ("aws_lb_listener", attrs), ("aws_lb_target_group", tg_attrs),
        ):
            dropped, changed = _attribute_notes(
                companion_type, companion_attrs, _CARRIED_COMPANION_ATTRS[companion_type],
                # A listener carries no health_check block and no `name`, so both
                # of these are no-ops for it -- no per-type branch needed.
                _dropped_health_check_attrs(companion_attrs),
                # hcl.py names the target group `<the alb node's label>-tg`, so a
                # target group the user named anything else is renamed by the
                # round trip (silent through v0.7.5).
                _derived_changes([("name", companion_attrs.get("name"), f"{label}-tg")]),
            )
            warnings += _attribute_warnings(f"{label} (alb)", f"{companion_type} ", dropped, changed)
    for rtype, rname, attrs in alb_companions:
        if rtype == "aws_lb_target_group" and f"aws_lb_target_group.{rname}" not in claimed_target_groups:
            unsupported.append(Unsupported(
                type=rtype, name=rname,
                reason="target group is not the forward target of any imported listener -- not folded onto a load balancer",
            ))

    warnings += _stamp_containment(node_by_label, attrs_by_label, by_hcl_name)
    warnings += _stamp_sg_rules(node_by_label, attrs_by_label, by_hcl_name)
    warnings += _stamp_ec2_wiring(node_by_label, attrs_by_label, by_hcl_name, key_pairs)
    warnings += _stamp_ecs_taskdef(node_by_label, attrs_by_label, by_hcl_name, taskdefs)
    warnings += _stamp_canvas_refs(node_by_label, attrs_by_label, by_hcl_name)
    role_names = {
        label: str(hcl.unquote(attrs_by_label[label].get("name")) or "")
        for label, node in node_by_label.items() if node["type"] == "iam_role"
    }
    lambda_warnings, folded_roles = _stamp_lambda(
        node_by_label, attrs_by_label, by_hcl_name, role_names, archives or {},
    )
    warnings += lambda_warnings
    # The ec2/ecs auto-role gets the same treatment a lambda's does, for the same
    # reason and by the same signal (`<workload>-role`). Without it, generate ->
    # import -> generate produced a SECOND role: the emitted `web_role` came back
    # as an `iam_role` NODE, and re-generating added a fresh auto-role beside it
    # (`web_role_2`). Caught by the round-trip assertion, which is the only thing
    # that would have.
    #
    # `claimed` is the half the first version left out, and the omission was only
    # ever visible END TO END. `_stamp_lambda`'s own docstring states the rule --
    # "an auto-role is named for its workload and REFERENCED BY NOTHING ELSE" --
    # but this pass tested the name and not the reference. Measured through the
    # real `odin import-tf` on a project with an ecs node `api` and a lambda whose
    # role is `api-role`: the role matched `<ecs label>-role`, was folded away as
    # the SERVICE's auto-role, and the lambda that actually used it then failed to
    # regenerate at all -- `unsupported: worker (lambda): role names something
    # that isn't an IAM Role on the canvas`, which also silently dropped its IAM
    # policy. A role somebody points at explicitly is by definition not
    # auto-generated, so it is excluded by that fact rather than by its name.
    workload_labels = {
        label for label, node in node_by_label.items() if node["type"] in ("ec2", "ecs")
    }
    claimed = {
        found for attrs in attrs_by_label.values()
        if (found := _referenced_label(attrs.get("role"), "aws_iam_role", by_hcl_name))
    }
    auto = {
        label for label, node in node_by_label.items()
        if node["type"] == "iam_role" and label.endswith("-role")
        and label[: -len("-role")] in workload_labels and label not in claimed
    }
    folded_roles |= auto
    # A folded auto-role must leave BOTH lists: the node list the canvas is built
    # from, and `node_by_label`, which later passes read.
    nodes = [node for node in nodes if node["id"] not in folded_roles]
    for label in folded_roles:
        node_by_label.pop(label, None)
    # ...and its WARNINGS go with it. The per-resource honesty pass ran while the
    # role was still a node, so a folded auto-role left behind
    # "thumbnailer-role (iam_role): imported without unmodeled attribute(s):
    # assume_role_policy" -- about a node that no longer exists, on every single
    # lambda import. Warning noise is not harmless here: this module's whole value
    # is that its warnings are worth reading.
    warnings = [
        warning for warning in warnings
        if not any(warning.startswith(f"{label} (iam_role)") for label in folded_roles)
    ]
    _layout(nodes)

    edges: list[dict] = []
    for rname, attrs in subscriptions:
        topic_target = _ref_target(attrs.get("topic_arn"))
        queue_target = _ref_target(attrs.get("endpoint"))
        topic_label = by_hcl_name.get(f"aws_sns_topic.{topic_target}") if topic_target else None
        queue_label = by_hcl_name.get(f"aws_sqs_queue.{queue_target}") if queue_target else None
        if topic_label and queue_label:
            edges.append({"source": topic_label, "target": queue_label})
            # The subscription becomes an EDGE, and an edge carries no arguments
            # -- so everything the source put ON the subscription has to be
            # accounted for here or it vanishes. `filter_policy` is the one that
            # matters most: drop it and the queue starts receiving every message
            # published to the topic, which no warning ever mentioned.
            sub_type = "aws_sns_topic_subscription"
            dropped, changed = _attribute_notes(
                sub_type, attrs, _CARRIED_COMPANION_ATTRS[sub_type], (), {},
            )
            warnings += _attribute_warnings(
                f"{topic_label} -> {queue_label} (sns subscription)", "", dropped, changed,
            )
        else:
            unsupported.append(Unsupported(
                type="aws_sns_topic_subscription", name=rname,
                reason="subscription references a resource outside the supported set -- edge dropped",
            ))

    # v0.8.18: each `aws_volume_attachment` back into the canvas edge that
    # produced it -- the exact inverse of hcl.py's attachment pass, which is what
    # makes generate -> import -> generate stable instead of losing the
    # attachment (and so DETACHING a live disk on the next apply).
    #
    # `edgeType` is stamped even though hcl.py keys on the two node KINDS: the
    # canvas needs it to draw and label the line, and `spec/translate.py`'s
    # `VOLUME_ATTACHMENT` is what it must spell.
    attached = [
        (rname, attrs,
         _referenced_label(attrs.get("volume_id"), "aws_ebs_volume", by_hcl_name),
         _referenced_label(attrs.get("instance_id"), "aws_instance", by_hcl_name))
        for rname, attrs in volume_attachments
    ]
    devices = _assigned_devices([(v, i) for _, _, v, i in attached if v and i])
    for rname, attrs, volume_label, instance_label in attached:
        if not (volume_label and instance_label):
            # The subscription pass's rule, and it matters more here: a dropped
            # attachment is a disk that the next apply detaches.
            missing = ", ".join(
                f"{arg}={attrs.get(arg)!r}" for arg, found in
                (("volume_id", volume_label), ("instance_id", instance_label)) if not found
            )
            unsupported.append(Unsupported(
                type="aws_volume_attachment", name=rname,
                reason=f"attachment references a resource outside the supported set ({missing}) "
                       "-- the edge is dropped, so a regenerated project would NOT attach this volume",
            ))
            continue
        edges.append({
            "source": volume_label, "target": instance_label,
            "data": {"edgeType": VOLUME_ATTACHMENT},
        })
        expected = devices.get((volume_label, instance_label))
        dropped, changed = _attribute_notes(
            "aws_volume_attachment", attrs, _CARRIED_COMPANION_ATTRS["aws_volume_attachment"], (),
            _derived_changes([("device_name", attrs.get("device_name"), expected)] if expected else []),
        )
        warnings += _attribute_warnings(
            f"{volume_label} -> {instance_label} (volume attachment)", "", dropped, changed,
        )

    # v0.8.19: each `aws_route53_record` back into the canvas edge that produced
    # it -- the inverse of hcl.py's record pass, and the same stake as the
    # attachment pass above. Lose the edge and the second generate emits no
    # record at all, so the next apply REMOVES the hosts entry and a name that
    # resolved stops resolving.
    #
    # ONLY an `aws_instance` is ever looked for, and the scope is not widened
    # here. hcl.py declines every other target BY NAME (`_DNS_TARGET_KINDS`,
    # measured against the real fact shapes) because a hosts entry is
    # `<ip> <name>` and carries no port -- so an edge invented to an alb or an
    # rds would be one the generator refuses to re-emit, i.e. an import that
    # cannot round-trip and says nothing about it.
    for rname, attrs in dns_records:
        zone_label = _referenced_label(attrs.get("zone_id"), "aws_route53_zone", by_hcl_name)
        instance_label = _referenced_label(
            _record_reference(attrs.get("records")), "aws_instance", by_hcl_name,
        )
        if not (zone_label and instance_label):
            missing = ", ".join(
                f"{arg}={attrs.get(arg)!r}" for arg, found in
                (("zone_id", zone_label), ("records", instance_label)) if not found
            )
            unsupported.append(Unsupported(
                type="aws_route53_record", name=rname,
                reason=f"record references a resource outside the supported set ({missing}) -- the "
                       "edge is dropped, so a regenerated project would NOT resolve this name",
            ))
            continue
        edges.append({
            "source": zone_label, "target": instance_label,
            "data": {"edgeType": DNS_RECORD},
        })
        values = attrs.get("records")
        dropped, changed = _attribute_notes(
            "aws_route53_record", attrs, _CARRIED_COMPANION_ATTRS["aws_route53_record"], (),
            {
                # hcl.py names the record `<ec2 label>.<zone label>`, so a record
                # the source called anything else is renamed by the round trip --
                # and a renamed DNS record is a name that stops answering.
                **_derived_changes([("name", attrs.get("name"), f"{instance_label}.{zone_label}")]),
                **({"records": _ONE_RECORD_VALUE}
                   if isinstance(values, list) and len(values) > 1 else {}),
            },
        )
        warnings += _attribute_warnings(
            f"{zone_label} -> {instance_label} (dns record)", "", dropped, changed,
        )
    # v0.8.19: EFS MOUNTS, recovered from the CONSUMER side.
    #
    # This is where the shape differs from every other companion in this file. An
    # `aws_volume_attachment` is a RESOURCE of its own naming both ends, so its
    # inverse is a lookup. An EFS mount is a nested block on the CONSUMER naming
    # only the file system, so the edge has to be reassembled -- from two places
    # on an ecs task definition (`volume {}` for the file system, `mountPoints[]`
    # for the path) and through an access point in the middle on a lambda.
    #
    # `edgeType` is stamped even though hcl.py's mount pass keys on the two node
    # KINDS: the canvas needs it to draw and label the line, and
    # `spec/translate.py`'s `FILE_SYSTEM_MOUNT` is what it must spell.
    finders = {
        "ecs": lambda label: _ecs_efs_mounts(label, attrs_by_label[label], taskdefs, by_hcl_name),
        "lambda": lambda label: _lambda_efs_mounts(
            label, attrs_by_label[label], access_points, by_hcl_name,
        ),
    }
    mounts: list[tuple[str, str, str, str]] = []
    claimed_access_points: set[str] = set()
    for label, node in sorted(node_by_label.items()):
        find = finders.get(node["type"])
        if find is None:
            continue
        found, mount_warnings = find(label)
        warnings += mount_warnings
        mounts += [(efs, label, node["type"], path) for efs, path, _ap in found]
        claimed_access_points |= {ap for _efs, _path, ap in found if ap}
    # ONE edge per (file system, consumer) pair even when a task definition
    # mounts the same file system twice: two edges between the same two nodes is
    # a canvas the UI cannot draw and a round trip that is not stable.
    edges += [
        {"source": efs, "target": consumer, "data": {"edgeType": FILE_SYSTEM_MOUNT}}
        for efs, consumer in sorted({(efs, consumer) for efs, consumer, _kind, _path in mounts})
    ]
    warnings += _stamp_efs_paths(node_by_label, mounts)
    # The claimed access points' own arguments, held to the same rule as any
    # other companion's, and attributed to the file system they fold onto --
    # once each, not once per function, since two lambdas may mount through one.
    for rname in sorted(claimed_access_points):
        ap_attrs = access_points[rname]
        efs = _referenced_label(
            ap_attrs.get("file_system_id"), "aws_efs_file_system", by_hcl_name,
        )
        dropped, changed = _attribute_notes(
            _EFS_ACCESS_POINT_TYPE, ap_attrs,
            _CARRIED_COMPANION_ATTRS[_EFS_ACCESS_POINT_TYPE], (), {},
        )
        warnings += _attribute_warnings(
            f"{efs} (efs)", f"{_EFS_ACCESS_POINT_TYPE} ", dropped,
            sorted(changed + _root_directory_lines(ap_attrs, "root_directory")),
        )
    # An access point no imported function mounts through is the target group
    # case: it folds onto nothing, so it is REPORTED rather than dropped in
    # silence. odin emits one only for a file system a lambda mounts, so its own
    # output never lands here.
    for rname in sorted(set(access_points) - claimed_access_points):
        unsupported.append(Unsupported(
            type=_EFS_ACCESS_POINT_TYPE, name=rname,
            reason="access point is not the mount target of any imported aws_lambda_function -- "
                   "odin emits one only for a file system a function mounts, so a regenerated "
                   "project would NOT contain it",
        ))
    apigw_edges, apigw_warnings, apigw_unsupported = _apigw_companions(apigw_companions, by_hcl_name)
    edges += apigw_edges
    warnings += apigw_warnings
    unsupported += apigw_unsupported

    policy_edges, policy_warnings = _edges_from_role_policies(
        role_policies, node_by_label, attrs_by_label,
    )
    edges += policy_edges
    warnings += policy_warnings

    return ImportResult(nodes=nodes, edges=edges, unsupported=unsupported, warnings=warnings)


def parse_hcl_text(text: str, archives: dict[str, bytes] | None = None) -> ImportResult:
    return parse_hcl({"main.tf": text}, archives)


def parse_hcl_dir(directory: Path) -> ImportResult:
    """Directory mode reads the ZIPS as well as the `.tf` files -- a lambda's body
    lives in one, so text mode can only ever report it missing (`_stamp_lambda`).
    Synchronous on purpose: `.read_bytes()` on a page-cached few-KB archive is
    sub-millisecond, and an async file API in Python is a thread pool in a costume
    (the concurrency note in .claude/CLAUDE.md)."""
    files = {p.name: p.read_text() for p in sorted(directory.glob("*.tf"))}
    archives = {p.name: p.read_bytes() for p in sorted(directory.glob("*.zip"))}
    return parse_hcl(files, archives)


def _import_id(resource: LiveResource, gateway_port: int) -> str:
    """Terraform's import ID format differs per resource type — mirrors the
    identifiers `aws/backings.py` itself hands back to workloads."""
    if resource.type == "sqs":
        return f"http://127.0.0.1:{gateway_port}/{ACCOUNT}/{resource.id}"
    if resource.type == "sns":
        return f"arn:aws:sns:{REGION}:{ACCOUNT}:{resource.id}"
    return resource.id  # s3 bucket name / dynamodb table name


def _import_blocks(resources: list[LiveResource], gateway_port: int) -> str:
    used: dict[str, set[str]] = {}
    blocks = []
    for resource in resources:
        tf_type = _TF_TYPE[resource.type]
        name = hcl.unique_name(hcl.sanitize_name(resource.id), used.setdefault(tf_type, set()))
        blocks.append(
            f'import {{\n  to = {tf_type}.{name}\n  id = {hcl.quote(_import_id(resource, gateway_port))}\n}}'
        )
    return "\n\n".join(blocks) + "\n"


def _tf_env(gateway_port: int, access_key: str, secret_key: str) -> dict[str, str]:
    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{gateway_port}",
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": REGION,
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE_DIR),
    }


async def _run(tofu: str, args: tuple[str, ...], cwd: Path, env: dict[str, str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        tofu, *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await proc.communicate()  # best-effort: `generated.tf`'s existence is the real success signal (module docstring)
    finally:
        await reap(proc)  # a cancelled call must not leave tofu (or its transport) standing


async def import_live(
    resources: list[LiveResource], gateway_port: int, access_key: str, secret_key: str,
) -> ImportResult:
    """Mode (b): generate `import {}` blocks for `resources`, resolve them
    against the real backings through the gateway, and parse whatever HCL
    `tofu plan -generate-config-out` produces with mode (a)."""
    supported = [r for r in resources if r.type in _TF_TYPE]
    unsupported = [
        Unsupported(type=r.type, name=r.id, reason=f"{r.type} -- not supported by odin's import (yet)")
        for r in resources if r.type not in _TF_TYPE
    ]
    if not supported:
        return ImportResult(unsupported=unsupported)

    tofu = shutil.which("tofu")
    if tofu is None:
        return ImportResult(unsupported=[
            *unsupported, Unsupported(type="*", name="*", reason="tofu not on PATH -- cannot live-import"),
        ])

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        main_tf = f"{hcl.HEADER}\n\n{hcl.provider_block(REGION)}\n\n{_import_blocks(supported, gateway_port)}"
        (scratch / "main.tf").write_text(main_tf)
        (scratch / "override.tf").write_text(workspace_mod.OVERRIDE_TF)
        env = _tf_env(gateway_port, access_key, secret_key)
        await _run(tofu, ("init", "-input=false"), scratch, env)
        await _run(tofu, ("plan", "-input=false", "-no-color", "-generate-config-out=generated.tf"), scratch, env)
        generated = scratch / "generated.tf"
        if not generated.exists():
            return ImportResult(unsupported=[
                *unsupported,
                Unsupported(type="*", name="*", reason="tofu could not generate config for the requested resources"),
            ])
        result = parse_hcl({"generated.tf": generated.read_text()})

    return ImportResult(
        nodes=result.nodes, edges=result.edges,
        unsupported=[*unsupported, *result.unsupported], warnings=result.warnings,
        parse_error=result.parse_error,
    )
