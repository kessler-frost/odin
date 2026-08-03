# odin — end-to-end scenarios

What you can draw, what really comes up when you apply it, and the test that
proves it. Every row names a file you can open; nothing here is a capability
claim without one.

The other docs answer different questions:
[internals.md](internals.md) is the architecture and how odin is verified,
[limits.md](limits.md) is the complete caveat list,
[architecture.md](architecture.md) is the diagram, and
[cli.md](cli.md) is the command surface. This file is the catalogue of
end-to-end journeys.

> **This file was rewritten on 2026-08-02 because it described an architecture
> that no longer exists.** It documented a Moto/OpenTofu **validate-only** flow
> (a `Validate` button, `tofu plan` against an ephemeral Moto server, "no
> apply") and a parked app-workload layer (`service`/`dep`/`batch`/`llm` nodes,
> a scheduler, a config-completion "Brain"). Both are gone, and the receipts
> are checkable: there is no `/validate` route anywhere in `src/odin/`, `moto`
> appears nowhere in `src/odin/` or `pyproject.toml`, `src/odin/resources.py`
> does not exist, and all four test files the old tables cited as evidence
> (`tests/aws/test_skeleton_e2e.py`, `test_multikind_e2e.py`,
> `test_aws_usable_e2e.py`, `tests/agent/test_brain.py`) are absent from the
> tree. The parked layer lives at git tag `app-layer-parked`; see
> [NORTHSTAR.md](../NORTHSTAR.md) for the direction that replaced it.
>
> The lesson is the one this repo keeps relearning: a stale doc is believed as
> readily as a current one, and a table of green ticks is believed faster than
> a paragraph. Every tick below says what it means.

## What a proof means here

Almost every scenario is pinned by an **integration-marked** test — one that
boots real containers, a real gateway and real `tofu`, not a mock. Those are
the rows worth trusting, and they are the ones cited. The handful of cited
proofs that are *unit* tests are labelled **(unit)** inline, so the strength of
each row is visible rather than assumed.

**These tests were not re-run to write this document.** The claims are read
from each test's *assertions* (not its prose — one docstring in this suite is
provably stale about its own test). So a row means "this is asserted, against a
real substrate, by a test that runs on demand", not "this passed today". To
check for yourself:

```bash
uv run pytest                  # unit — every commit
uv run pytest -m integration   # containers, tofu, Lima VMs — on demand, slow
```

## Driving a scenario yourself

No browser required, and this is the reproducible path. A canvas is a plain
JSON document (`{"nodes": [...], "edges": [...]}`) that the CLI and the UI
share — see [cli.md](cli.md#the-canvas-json-schema) for the field-by-field
schema.

```bash
odin start                                   # http://localhost:4200
odin canvas get --env demo \
  | jq '.nodes += [{"id":"x1","type":"s3","position":{"x":80,"y":80},"data":{"label":"backups"}}]' \
  | odin canvas set - --env demo
odin apply --env demo                        # canvas -> Terraform -> tofu apply -> real services
odin world --env demo                        # one line per resource, with live facts
odin destroy --env demo                      # tear it down, keep the environment
odin env rm demo                             # ...and remove the environment itself
```

The gate a script should read is `not_covered`, not the exit code — a node odin
has no model for does **not** fail an apply:

```bash
odin apply -o json | jq -e '.not_covered | length == 0'
```

For a browser pass, the driver is **`agent-browser`** (`agent-browser skills get
core --full`). It is the only browser driver in this project; `playwright-cli`
was removed machine-wide on 2026-07-27, and the sidebar-to-canvas drag path is
verified working with it.

## 1. The AWS core, provisioned for real

One apply, a mixed canvas, real backing containers behind every badge.

| Scenario | Draw | What really comes up | Proof |
|---|---|---|---|
| The whole Apply button | `s3` + `sqs` + `sns` (+ subscription edge) + `dynamodb` + `rds` | five healthy resources in RustFS / goaws / dynalite / Postgres; `tf.status ok`; re-apply is zero drift; an **empty canvas is a full teardown** | `tests/simulate/test_apply_full_e2e.py` |
| A node becomes a real resource | any one AWS kind | the resource exists in its real backing, checked with a host-side boto3 client | `tests/aws/test_provision_e2e.py` |
| Queues, topics and tables round-trip | `sqs`, `sns`→`sqs`, `dynamodb` | a message round-trips a queue; a subscription delivers with raw delivery; `put_item`/`get_item` round-trips; **a live edit adds a second queue to an already-healthy topic and both receive** | `tests/aws/test_backings_e2e.py` |
| Environments are isolated | the same label in envs `a` and `b` | two separate backing containers; destroying `a` collects only `a`'s | `tests/aws/test_backings_e2e.py` |
| A killed backing comes back | any AWS kind | killing the backing container demotes the node and the reconciler re-provisions it to healthy | `tests/aws/test_backings_e2e.py` |

Per-service Terraform slices, each a real `tofu apply` through the gateway with
a zero-drift `plan` afterwards: `test_rds_tf_e2e.py`, `test_elasticache_tf_e2e.py`,
`test_logs_tf_e2e.py`, `test_secrets_ssm_tf_e2e.py`, `test_iamctl_ecr_tf_e2e.py`,
`test_ec2net_tf_e2e.py` (all under `tests/simulate/`).

## 2. Compute

| Scenario | Draw | What really comes up | Proof |
|---|---|---|---|
| Lambda, end to end | `lambda` | `tofu apply` creates a real IAM role + function; a **real RIE container**; the provider's own `Pending`→`Active` waiter passes; a real `Invoke` through the gateway returns the echoed payload; `DeleteFunction` leaves nothing | `tests/simulate/test_lambda_tf_e2e.py` |
| ECS, end to end | `ecs` (count 2, `nginx:alpine`) | cluster + task definition + service; **two real Colima containers**; `runningCount` reaches 2; zero-drift plan; a re-apply at `desiredCount=1` leaves exactly one | `tests/simulate/test_ecs_tf_e2e.py` |
| EC2, end to end | `ec2` | a **real Lima VM** with a real, host-reachable vzNAT address — not a placeholder | `tests/simulate/test_ec2_tf_e2e.py` |
| A block device that really attaches | `ec2` + `ebs` | a real `limactl disk` volume that really joins the VM, verified by its mount point *inside the booted guest* | `tests/simulate/test_ebs_volume_e2e.py` |
| A workload gets its wiring | `rds` → `ecs` connection edge | `DATABASE_URL` resolved to the real endpoint and present in the running container's environment | `tests/simulate/test_ecs_env_wiring_e2e.py` |
| A task runs inside its instance | `ecs` drawn inside `ec2` | the task container runs *in that VM* via `nerdctl`, not on the host | `src/odin/gateway/models/ecsctl.py:1041` (`runtime_for_service`) |
| You can see why it crashed | `ecs` whose container exits 1 | `odin logs <node>` — the real CLI, over real HTTP against a real server — shows the crashing container's real stdout, and the node projects `crashed` in `/world` with a real verdict | `tests/simulate/test_ecs_crash_observability_e2e.py` |

## 3. Networking, security groups and the mesh

| Scenario | Draw | What really comes up | Proof |
|---|---|---|---|
| A VPC is a real network | `vpc` ⊃ `subnet` ⊃ `sg` (2 ingress rules) | a per-env Nebula network with the group's **compiled firewall**, readable at `GET /mesh?env=`; re-apply is zero drift; an empty canvas tears it all down | `tests/simulate/test_apply_full_ec2net_e2e.py` |
| A security group really gates a backing | `db-sg` allowing 5432 **from `web-sg`**; a `web` instance in `web-sg`, a `worker` in no group | one Apply, one canvas: `web` reaches the real Postgres and `worker` does not — an SG-to-SG rule enforced between a real Lima VM and a real container | `tests/simulate/test_sg_gates_backing_e2e.py`, `test_apply_full_ec2_sg_e2e.py` |
| Editing a group reaches running VMs | edit an applied `sg` | the change is pushed into already-booted VMs — it is not frozen at boot | `tests/simulate/test_sg_edit_propagation_e2e.py` |
| An instance's **assigned** group gates it | `sg` → one of two `ec2` nodes | two real Lima VMs on one canvas enforce *different* firewalls, from their own assigned groups rather than the VPC default | `tests/simulate/test_ec2_assigned_sg_e2e.py` |
| Revoking membership closes an **open** flow | remove a node from a group | a real TCP flow held open across the revoke stops working — not only the next connection. The certificate is what carries membership, so `test_sg_membership_revoke_e2e.py` uses rule-identical groups, leaving the cert as the only thing that could have done it | `tests/simulate/test_sg_revoke_drops_open_flow_e2e.py`, `test_sg_membership_revoke_e2e.py` |
| Egress is enforced, on a real VM | a restricted egress rule | the VM's overlay egress is blocked for real | `tests/simulate/test_sg_egress_gates_vm_e2e.py` |
| The mesh recovers | `rds` + `ec2` on a mesh | a killed database gets its mesh endpoint back after **one** apply — real VMs, real containers, real nebula | `tests/simulate/test_mesh_recovery_e2e.py`, `test_nebula_mesh_e2e.py` |

**A boundary this table does not overstate:** the mesh covers overlay traffic
only. A Lima VM keeps a second path off the box (vzNAT to the host), and a
security group does **not** gate it — `test_sg_egress_gates_vm_e2e.py` asserts
that path still works, deliberately. ECS tasks, the ALB proxy, Lambda and
ElastiCache are not mesh members at all.

## 4. Load balancing

| Scenario | Draw | What really comes up | Proof |
|---|---|---|---|
| ALB in front of a service | `alb` → `ecs` (2 tasks) | a real nginx proxy in front of a real two-task service — and **killing one task does not stop it serving 200s**, which is the entire point of a load balancer | `tests/simulate/test_alb_tf_e2e.py` |
| ALB in front of a **VM** | `alb` → `ec2` | the upstream is the VM's real address; `GET` on the load balancer's published port returns **200 with the VM's own bytes** | `tests/simulate/test_alb_ec2_target_e2e.py` |

The `alb → ec2` row is worth reading as a cautionary tale rather than a
feature. That path had **never once run** before the test was written: the
resolver read `private_ip_address`/`public_ip_address`, keys the EC2 model has
never written, so every real instance resolved to `None` and nginx was handed a
bare `i-…` id it cannot dial. Its one unit test had fabricated the record with
the key the reader wanted — honesty rule 1, living inside a test.

## 5. Event triggers

A drawn trigger really invokes a function. Each of these has a unit suite that
stubs the substrate; these are the versions where nothing is stubbed, because a
fake runtime cannot tell you whether the real one ever fires.

| Scenario | Draw | What really fires | Proof |
|---|---|---|---|
| A bucket write invokes a Lambda | `s3` → `lambda` | a real write to RustFS delivers an event whose ETag matches what the backing really reports, and the prefix/suffix **filter really filters** | `tests/simulate/test_dispatch_s3_e2e.py` |
| A queue message invokes a Lambda | `sqs` → `lambda` | a real message drives a real invoke | `tests/simulate/test_dispatch_sqs_e2e.py` |
| A schedule invokes a Lambda | schedule → `lambda` | the timer really fires the function | `tests/simulate/test_dispatch_schedule_e2e.py` |

## 6. IAM, enforced

| Scenario | What it proves | Proof |
|---|---|---|
| An ungranted caller is denied | a legitimate principal succeeds and an ungranted one gets `AccessDenied`, over **real SigV4** through the gateway | `tests/gateway/test_gateway_e2e.py::test_an_ungranted_principal_is_denied_while_a_legitimate_one_is_not` |
| Credentials do not cross environments | env `a`'s keys are refused in env `b` | `…::test_foreign_env_creds_denied` |
| A container reaches the gateway | a real workload container's boto3 traffic crosses into odin | `…::test_container_crosses_to_gateway` |
| The AWS surface answers for real | SQS/SNS/DynamoDB drive through the gateway, and an empty-queue long poll is **not** a 503 | `…::test_sqs_sns_dynamodb_through_gateway`, `…::test_sqs_long_poll_on_an_empty_queue_is_not_a_503` |

Authorization is from the **applied IAM**, not from canvas edges directly
(since v0.8.12) — so what is enforced is what was actually applied.

## 7. Import and round-trip

| Scenario | What it proves | Proof |
|---|---|---|
| Live-state import | a bucket created out-of-band with boto3 in RustFS is imported back as an `s3` node | `tests/simulate/test_import_tf_e2e.py` |

Round-trip is **not lossless**, and what it costs is listed rather than
discovered — see [internals.md](internals.md#architecture) and
[limits.md](limits.md). `kms` is emitted and not yet imported.

## 8. The honesty scenarios

These are the most valuable rows in the file. Each one is a way odin used to
report success it had not achieved, now pinned so it cannot come back.

| Scenario | The failure it prevents | Proof |
|---|---|---|
| **The false-green window** | after a container is killed, an apply must not answer `applied`/exit 0 over a resource that is down. Measured in the field before the fix: **four consecutive `applied`/exit-0 applies over ~8s with zero containers**, `/world` green the whole time | `tests/simulate/test_false_green_window_e2e.py` |
| **Recovery in one apply** | the apply that *discovers* a death must also *repair* it, and say what that cost. Before: `/world` said "re-Apply to recreate" and the re-Apply converged nothing — **302.8s of applies against a killed database that never came back**; after: **5.9s** | same file |
| **A typo must not delete your data** | changing `type: "s3"` to `"s3 "` — one character — used to destroy the bucket, its object and the backing behind `status: applied`, `tf: ok`, exit 0. Now the apply **refuses**, names the node, exits nonzero, and the bucket and object are still there | `tests/simulate/test_uncovered_destroy_guard_e2e.py` |
| **Destroy tells the truth** | `odin destroy` exited 0 after 300s claiming `status: destroyed` while its own nested payload said `tf: failed (exit code -9)` — the env still listed, six resources still in tofu state, containers still running. The suggested remedy produced another **300.31s and another exit 0**. It had returned a false 0 in three distinct forms across three releases | `tests/simulate/test_destroy_timeout_honesty_e2e.py`, `test_destroy_without_tofu_e2e.py` |
| **A bad image fails the apply** | an ECS task that can never start must fail the apply within a **bounded** time with an honest error, and destroy must still complete promptly — not silently succeed over a service that never runs, and not hang the pipeline | `tests/simulate/test_ecs_bad_image_e2e.py`, `test_ecs_bad_image_update_e2e.py` |
| **A failed update keeps serving** | a failed image update used to take the whole service down first: sampled at 2s, **three tasks serving 200 at 18:28:27, zero by 18:28:31**, 108 consecutive samples at zero, ~59s of CI reading "running" while the service was 100% down. Now the **outage window is zero seconds** — every previous-revision task keeps answering 200 across the whole failed apply, `/world` reads `error` *while it is still running*, and the apply still fails loudly | `tests/simulate/test_ecs_failed_update_keeps_serving_e2e.py` |
| **"re-Apply to recreate" is true** | a real Lima VM deleted out of band with `limactl delete --force` really does come back on the next apply — odin used to report the drift honestly and then fail to act on it | `tests/simulate/test_ec2_drift_e2e.py`, `test_ecs_drift_e2e.py` |
| **A no-op apply over an outage is not green** | tofu's `wait_for_steady_state` only runs when it actually *updates* a resource, so an apply that changes nothing sails past a service that is down. Applying an unchanged canvas over an outage must still refuse to report success | `test_ecs_noop_apply_outage_e2e.py`, `test_lambda_noop_apply_outage_e2e.py`, `test_rds_noop_apply_outage_e2e.py` |
| **A non-empty bucket still tears down** | `odin destroy` errored `BucketNotEmpty` because the generated `aws_s3_bucket` had no `force_destroy` — the backing prune reclaimed the data while tofu's own state was left inconsistent. An empty-canvas teardown now succeeds with an object still in the bucket | `tests/simulate/test_s3_force_destroy_e2e.py` |
| **An interrupted apply is reclaimed** | `kill -9` on tofu mid-apply — the everyday equivalents being Ctrl-C, an OOM, or a laptop sleeping — left tofu's state empty while **three Lima VMs kept Running**, with no supported way to reclaim them | `tests/simulate/test_interrupted_apply_reclaim_e2e.py` |
| **Secrets do not leak** | a `secret`/`ssm` node's plaintext must never reach a WebSocket frame or a line of `events.jsonl` | `tests/simulate/test_secret_no_leak.py` **(unit)**, `test_lighthouse_no_leak_e2e.py` |
| **Data survives a repair** | an RDS container replacement keeps the database: real rows written, container replaced, rows read back | `tests/aws/test_rds_postgres.py::test_real_rows_survive_the_container_replacement_odins_repair_performs` |

## Measured numbers

Quoted from the source and tests that recorded them, with where each came from.
They are historical measurements on one developer Mac, not guarantees.

| Number | What it measures | Where |
|---|---|---|
| 103s | an apply of three resources (S3 + DynamoDB + RDS), the README's own clip | `README.md` |
| 66s | a real Lima VM booting inside an `alb → ec2` apply | commit `0bb7b97` |
| `192.168.64.2` | the VM's real vzNAT address in that run — and the first evidence that a Colima **container** can reach a Lima VM's vzNAT address | commit `0bb7b97`, `docs/limits.md` |
| 74.6s | the two-VM Nebula mesh e2e, start to finish, on an idle machine | `src/odin/compute/instances.py:132` |
| ~8s / 4 applies | the false-green window, before it was closed | `tests/simulate/test_false_green_window_e2e.py` |
| 302.8s → 5.9s | recovery of a killed database, before and after the sweep moved ahead of the converges | same file |
| 0.74–0.76s | how fast the reconciler re-creates an `s3`/`sqs`/`sns`/`dynamodb` resource a failed destroy removed, at the default 1s poll | `docs/cli.md` |

## What this file does not cover

Stated rather than left to be discovered:

- **Nothing here was re-run to write this document.** Every row is read from a
  test's assertions. Treat `uv run pytest -m integration` as the current
  guarantee, not this page.
- **No multi-host scenario.** Nebula is single-host today: the mesh, firewall
  and per-VM daemons work, but a second machine joining an environment does
  not exist yet, so there is no scenario for it.
- **No scenario for a kind odin does not model.** An unmodelled node is
  reported in `not_covered` and acts on nothing; that is the coverage gate
  above, not a scenario.
- **`kms` does not round-trip** — emitted, not yet imported.
- **The kind catalogue is deliberately not duplicated here.** It moves; the
  current list lives in the README's "What you can do" table and in
  [cli.md](cli.md). Three kinds (`route53`, `efs`, `apigateway`) were in
  flight when this was written, so any list here would have been wrong within
  the day.
- **`odin start --dev` is not the path these scenarios use.** Drive them
  through `odin start` or the CLI. The dev-mode Vite proxy
  (`ui/vite.config.js`) has not been updated since 2026-06-21 and does not
  forward the routes the app uses today.
