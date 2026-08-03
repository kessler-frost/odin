# How odin is built, and how it is verified

## The path an apply takes

```

+-----------------------------------------------+  
|                                               |  
|         canvas (UI or odin canvas set)        |<+
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
|      spec/store.py: append-only revision      | |
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
|           iac/hcl.py: canvas to HCL           | |
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
|         simulate/runner.py: tofu apply        | |
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
|         gateway/: SigV4, IAM, AWS APIs        | |
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
| aws/, compute/: Postgres, RustFS, goaws, Lima | |
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
|       reconcile/drift.py: reality sweep       | |
|                                               | |
+-----------------------------------------------+ |
                        |                         |
                        |                         |
                        |                         |
                        |                         |
                        v                         |
+-----------------------------------------------+ |
|                                               | |
|           reconcile/: World + status          |-+
|                                               |  
+-----------------------------------------------+  
```

Each hop is a module you can read on its own:

| Stage | Module | What it owns |
|---|---|---|
| desired state | `spec/store.py` | append-only, content-addressed canvas revisions |
| compile | `iac/hcl.py` | canvas to HCL, deterministic and model-free |
| run | `simulate/runner.py` | `tofu apply`, bounded, with its output streamed |
| serve AWS | `gateway/` | SigV4, request classification, IAM, the AWS APIs |
| provision | `aws/`, `compute/` | Postgres, RustFS, goaws, dynalite, Lima VMs |
| observe | `reconcile/drift.py` | the reality sweep that catches out-of-band changes |
| project | `reconcile/` | World, node phases, live facts, crash verdicts |

The loop back to the canvas is what makes a badge trustworthy. Nothing marks a
node healthy because an apply returned zero; the reconciler asks docker and
limactl what exists and projects that.

To regenerate this diagram after editing `docs/diagrams/*.mmd`:

```bash
./scripts/render-diagrams.sh
```

## Architecture

- **UI:** React 19 + ReactFlow + Tailwind v4, served by Vite (`ui/`, `bun`).
  **Backend:** Python 3.12+ (`uv`), FastAPI + Server-Sent Events, Pydantic.
- **The gateway** (`src/odin/gateway/`) verifies SigV4, classifies each call into
  (service, action, resource), evaluates it against the edges you drew, then
  forwards to a backing or answers from its own per-service model store. EC2,
  VPC, SG, IAM, ECR, Lambda and ECS have no open-source AWS API to borrow, so odin
  owns the model and binds it to a substrate.
- **Translation** (`src/odin/iac/`) is deterministic in both directions and
  no longer covers quite the same node kinds in both: canvas → Terraform
  builds 23, and Terraform → canvas reads all 22 back across 38 resource types
  that it models. The gap is `kms`, added in v0.8.18 — emitted and not yet
  imported, so a project carrying an `aws_kms_key` does not round-trip through
  the canvas. `efs` and `route53` (both v0.8.19) did NOT widen that gap: each
  arrived in both directions at once. efs's two resource types and route53's two
  (`aws_route53_zone` plus the `aws_route53_record` companion an edge becomes)
  are why 32 became 36. Stated rather than rounded away, because "both directions" was
  true for eleven releases and is the sort of claim a reader keeps believing.
  36 became 38 in v0.8.21 with **no new node kind at all**, which is the
  interesting part: `aws_iam_instance_profile` and
  `aws_lb_target_group_attachment` were resources odin's generator had always
  written and its importer had never read, so odin's OWN output came back with
  two `unsupported` entries. Every kind was covered and the round trip was still
  broken — companion coverage is a separate count from kind coverage, which is
  why this sentence quotes both.

  These three numbers are pinned by
  `tests/agent/test_import_coverage_is_honest.py`, which derives all three from
  the live registries — so this sentence cannot go stale. **Nothing pins a
  COMMIT MESSAGE, and one is already wrong**: `a775d3d` says "corrected to a
  measured 19/19/28", which was true when written and was moved by the kms work
  a commit later. A commit message is immutable, unreachable by any ratchet, and
  reads as authoritative forever. So if you are here because a git log quoted
  these counts at you, re-derive them rather than believing the message —
  `len(hcl._BUILDERS)`, `len(set(import_tf._KIND.values()))`, and
  `len(set(import_tf._KIND) | companions)` are what the ratchet reads. Anything odin
  does not model is a LISTED unsupported entry rather than a silent omission.
  Equal node coverage is **not lossless**, and what it costs is listed rather
  than discovered: a security group's IPv6 rules and any port that is not a
  literal, a Lambda's body when only the HCL text is read, and — until the
  reader learns them — the `odin:ref:` tags and `egress` blocks the generator now
  writes. Port RANGES were on that list until v0.8.17 and are not any more: the
  grammar takes `tcp:8000-8100:0.0.0.0/0` and both bounds round-trip. Nor is
  `-1`, since v0.8.21: an ICMP rule (`from_port = -1`, which python-hcl2 renders
  as the interpolation `${-1}`, which is why it read as an expression) survives
  the round trip byte for byte. A drawn IAM
  permission is no longer among them: since v0.8.11 it is a real
  `aws_iam_role_policy`, and since v0.8.14 its `Resource` is a real ARN, so the
  generated file grants on Amazon what it grants here. See Known limits.
  **Runtime:** containers via Colima (default) or inside a Lima VM
  (`src/odin/runtime/`), and a Lima VM per EC2 node (`src/odin/compute/`).
- **Control loop:** a Spec Store (Stack = desired, World = observed) with a pure,
  idempotent `plan(Stack, World) → [Action]` reconciler. It drives the
  non-Terraform resources and projects Terraform-owned resources' live status back
  into World, so a badge reflects reality regardless of which path provisioned it.

## Verification

Every commit runs the unit suite, `ruff`, and a UI typecheck plus build. On demand,
because it needs a machine with Colima on it, the integration suite drives
containers, a gateway and `tofu` end to end
(`tests/gateway/test_gateway_e2e.py`, `tests/simulate/test_*_tf_e2e.py`,
`tests/test_file_modes.py`).

```bash
uv run pytest                  # unit
uv run pytest -m integration   # containers, slow
```

Beyond the suite, each release is exercised by hand against a live instance, one
subsystem at a time; the numbers under [Known limits](#known-limits) come from
those runs. The last single pass over the *whole* stack at once was 0.4.0's, so
treat the suite as the current guarantee.

