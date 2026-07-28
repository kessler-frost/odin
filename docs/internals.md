# How odin is built, and how it is verified

## Architecture

- **UI:** React 19 + ReactFlow + Tailwind v4, served by Vite (`ui/`, `bun`).
  **Backend:** Python 3.12+ (`uv`), FastAPI + Server-Sent Events, Pydantic.
- **The gateway** (`src/odin/gateway/`) verifies SigV4, classifies each call into
  (service, action, resource), evaluates it against the edges you drew, then
  forwards to a real backing or answers from its own per-service model store. EC2,
  VPC, SG, IAM, ECR, Lambda and ECS have no open-source AWS API to borrow, so odin
  owns the model and binds it to a real substrate.
- **Translation** (`src/odin/agent/`) is deterministic in both directions and
  covers the same NODE KINDS in both: canvas → Terraform builds 18, and
  Terraform → canvas reads all 18 back across 24 resource types. Anything odin
  does not model is a LISTED unsupported entry rather than a silent omission.
  Equal node coverage is **not lossless**, and the sharpest gap is edges:
  **a drawn IAM permission never reaches the Terraform at all.** It is enforced —
  by odin's gateway, from the canvas — but `odin translate` says so on stderr
  rather than letting you discover it, because that file handed to real AWS
  grants nothing. See Known limits.
  **Runtime:** real containers via Colima (default) or inside a Lima VM
  (`src/odin/runtime/`), and a real Lima VM per EC2 node (`src/odin/compute/`).
- **Control loop:** a Spec Store (Stack = desired, World = observed) with a pure,
  idempotent `plan(Stack, World) → [Action]` reconciler. It drives the
  non-Terraform resources and projects Terraform-owned resources' live status back
  into World, so a badge reflects reality regardless of which path provisioned it.

## Verification

Every commit runs the unit suite, `ruff`, and a UI typecheck plus build. On demand,
because it needs a machine with Colima on it, the integration suite drives real
containers, a real gateway and a real `tofu` rather than mocks
(`tests/gateway/test_gateway_e2e.py`, `tests/simulate/test_*_tf_e2e.py`,
`tests/test_file_modes.py`).

```bash
uv run pytest                  # unit
uv run pytest -m integration   # real containers, slow
```

Beyond the suite, each release is exercised by hand against a live instance, one
subsystem at a time; the numbers under [Known limits](#known-limits) come from
those runs. The last single pass over the *whole* stack at once was 0.4.0's, so
treat the suite as the current guarantee.

