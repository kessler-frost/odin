# C1 — Real Backings (MiniStack removal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove MiniStack; back `rds`/`s3`/`sqs`/`sns`/`dynamodb` nodes with real OSS containers run by allfather's own reconciler.

**Architecture:** The Reconciler already talks to AWS-ish machinery only via three injected objects (`_rds`, `_aws`, `_aws_env`). We build direct-container replacements (`PostgresRds`, `BackingAws`), then flip `server.py` wiring, rename the action, extend supervision, and delete the emulator. Spec: `docs/superpowers/specs/2026-07-22-v030-real-backings-design.md`.

**Tech Stack:** Python 3.12+/uv, FastAPI, boto3 (provisioning client), Docker via Colima behind `RuntimeDriver`.

## Global Constraints

- `uv` never pip; `bun` never npm/npx; `python` not python3.
- Pathlib for paths; imports at top of file; keep if/else + try/except minimal.
- Permissive licenses only for backings (Apache-2.0/MIT/BSD/MPL — NO AGPL, no proprietary dev-only licenses).
- Unit suite must stay green after every task: `uv run pytest -q` (integration excluded by default via `addopts`).
- Integration tests marked `@pytest.mark.integration` / `pytestmark = pytest.mark.integration`; run explicitly `uv run pytest -m integration -q <file>`.
- Every container carries the `allfather=1` label (ColimaRuntime adds it) and is torn down by the test that made it; `docker ps -aq --filter label=allfather=1 | xargs -r docker rm -f` must show nothing left after a test session.
- Container naming: `allfather-rds-{env}-{id}` for rds, `allfather-aws-{svc}-{env}` for shared backings.
- Containers reach the host at `host.docker.internal` (the runtime adds the host-gateway mapping); published facts that containers consume must use it, host-side probes use `127.0.0.1`.
- Commit after every task (conventional commits); do NOT push until the controller says so.

---

### Task 1: PostgresRds — rds nodes as direct Postgres containers

**Files:**
- Modify: `src/odin/aws/rds.py` (append the new class; leave `MiniStackRds` untouched — it dies in the switchover task)
- Test: `tests/aws/test_rds_postgres.py` (new)

**Interfaces:**
- Consumes: `odin.runtime.colima.ContainerSpec`, the `RuntimeDriver` protocol (`run_container`, `stop`, `status`, `host_port`).
- Produces (the switchover task wires this into `server.py`): class `PostgresRds` with
  `__init__(self, runtime, env: str = "default")`,
  `create_db(self, db_id: str, user: str, password: str) -> None`,
  `delete_db(self, db_id: str) -> None`,
  `endpoint(self, db_id: str) -> tuple[str, int] | None`,
  `container_name(self, db_id: str) -> str` (= `f"allfather-rds-{self._env}-{db_id}"`).
  Exact drop-in for how the Reconciler already calls `_rds` (see `reconciler.py:139-161,197-209`).

- [ ] **Step 1: Read the two consumer/pattern files**

Read `src/odin/reconcile/reconciler.py` (how `_rds` is called: `create_db`, `delete_db`, `endpoint`, `container_name`; `pg_ready` gates healthy so `endpoint` may return as soon as a host port exists) and `tests/reconcile/test_reconciler.py` (the `FakeRuntime` test style to mirror).

- [ ] **Step 2: Write the failing unit tests**

`tests/aws/test_rds_postgres.py`:

```python
"""PostgresRds: rds nodes as direct Postgres containers (no emulator)."""
from __future__ import annotations

from dataclasses import dataclass, field

from odin.aws.rds import PostgresRds
from odin.runtime.colima import ContainerSpec


@dataclass
class FakeRuntime:
    runs: list[ContainerSpec] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)

    def run_container(self, spec: ContainerSpec):
        self.runs.append(spec)
        self.statuses[spec.name] = "running"
        self.ports[spec.name] = 55432

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.statuses.pop(name, None)
        self.ports.pop(name, None)

    def status(self, name: str) -> str:
        return self.statuses.get(name, "absent")

    def host_port(self, name: str, container_port: int) -> int:
        return self.ports.get(name, 0)


def test_container_name_is_env_scoped():
    rds = PostgresRds(FakeRuntime(), env="staging")
    assert rds.container_name("db") == "allfather-rds-staging-db"


def test_create_db_runs_postgres_with_creds_and_dynamic_port():
    rt = FakeRuntime()
    PostgresRds(rt, env="default").create_db("db", "app", "s3cret")
    spec = rt.runs[0]
    assert spec.name == "allfather-rds-default-db"
    assert spec.image.startswith("postgres:16")
    assert spec.env["POSTGRES_USER"] == "app"
    assert spec.env["POSTGRES_PASSWORD"] == "s3cret"
    assert spec.ports == {5432: 0}


def test_create_db_is_idempotent_while_running():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    rds.create_db("db", "app", "pw")
    rds.create_db("db", "app", "pw")
    assert len(rt.runs) == 1


def test_endpoint_none_until_running_then_host_port():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    assert rds.endpoint("db") is None
    rds.create_db("db", "app", "pw")
    assert rds.endpoint("db") == ("127.0.0.1", 55432)


def test_delete_db_stops_container():
    rt = FakeRuntime()
    rds = PostgresRds(rt)
    rds.create_db("db", "app", "pw")
    rds.delete_db("db")
    assert rt.stopped == ["allfather-rds-default-db"]
    assert rds.endpoint("db") is None
```

- [ ] **Step 3: Run tests, confirm they fail**

Run: `uv run pytest tests/aws/test_rds_postgres.py -q`
Expected: ImportError — `PostgresRds` not defined.

- [ ] **Step 4: Implement PostgresRds**

Append to `src/odin/aws/rds.py`:

```python
POSTGRES_IMAGE = "postgres:16-alpine"


class PostgresRds:
    """rds nodes as direct Postgres containers via the RuntimeDriver.

    Drop-in for the Reconciler's `_rds` seam. `endpoint` returns as soon as a
    host port is published — the Reconciler's `pg_ready` probe gates healthy.
    """

    def __init__(self, runtime, env: str = "default") -> None:
        self._rt = runtime
        self._env = env

    def container_name(self, db_id: str) -> str:
        return f"allfather-rds-{self._env}-{db_id}"

    def create_db(self, db_id: str, user: str, password: str) -> None:
        name = self.container_name(db_id)
        if self._rt.status(name) == "running":
            return  # idempotent: already up
        self._rt.stop(name)  # clear any exited remnant so the boot is fresh
        self._rt.run_container(ContainerSpec(
            name=name,
            image=POSTGRES_IMAGE,
            env={"POSTGRES_USER": user, "POSTGRES_PASSWORD": password},
            ports={5432: 0},
            labels={"allfather-env": self._env},
        ))

    def delete_db(self, db_id: str) -> None:
        self._rt.stop(self.container_name(db_id))

    def endpoint(self, db_id: str) -> tuple[str, int] | None:
        port = self._rt.host_port(self.container_name(db_id), 5432)
        return ("127.0.0.1", port) if port else None
```

(Import `ContainerSpec` at top of the file: `from odin.runtime.colima import ContainerSpec`.)

NOTE: the Fake in the tests treats `stop` on an absent name as fine — verify `ColimaRuntime.stop` is likewise idempotent (it is: `rm -f`, check=False); do not add defensive branches. But DO update the FakeRuntime `stop` in the test to tolerate absent names if the code calls `stop` before first run.

- [ ] **Step 5: Run unit tests, confirm green; run the whole unit suite**

Run: `uv run pytest tests/aws/test_rds_postgres.py -q` → all pass.
Run: `uv run pytest -q` → everything else still green (102 tests were green before this task).

- [ ] **Step 6: Add the make-or-break integration test**

Append to `tests/aws/test_rds_postgres.py`:

```python
import time

import psycopg2
import pytest

from odin.runtime.colima import ColimaRuntime


@pytest.mark.integration
def test_create_db_boots_real_postgres_select_1():
    rt = ColimaRuntime()
    rds = PostgresRds(rt, env="itest")
    rds.create_db("db", "app", "apppass123")
    try:
        deadline = time.monotonic() + 120  # first run may pull the image
        last = None
        while time.monotonic() < deadline:
            ep = rds.endpoint("db")
            if ep is not None:
                try:
                    conn = psycopg2.connect(
                        host=ep[0], port=ep[1], user="app",
                        password="apppass123", dbname="postgres",
                        connect_timeout=2,
                    )
                    with conn, conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        assert cur.fetchone()[0] == 1
                    conn.close()
                    return
                except psycopg2.OperationalError as exc:
                    last = exc
            time.sleep(2)
        raise AssertionError(f"postgres never became ready: {last}")
    finally:
        rds.delete_db("db")
        assert rt.status(rds.container_name("db")) in ("absent", "unknown", "")
```

Adjust the final status assertion to whatever `ColimaRuntime.status` actually returns for a removed container (read `colima.py` first; do not guess).

- [ ] **Step 7: Run the integration test for real (Colima is running)**

Run: `uv run pytest -m integration tests/aws/test_rds_postgres.py -q`
Expected: 1 passed (~10-60s). Then verify no leftovers: `docker ps -a --filter label=allfather=1 --format '{{.Names}}'` prints nothing from this test.

- [ ] **Step 8: Commit**

```bash
git add src/odin/aws/rds.py tests/aws/test_rds_postgres.py
git commit -m "feat(aws): PostgresRds — rds nodes as direct Postgres containers"
```

---

### Task 2: BackingAws — shared per-env backing containers + provisioning

**Files:**
- Create: `src/odin/aws/backings.py`
- Modify: `src/odin/runtime/colima.py` (`ContainerSpec` gains `volumes: dict[str, str] = field(default_factory=dict)` — host path → container path; `run_container` adds `"-v", f"{host}:{container}"` per entry)
- Test: `tests/aws/test_backings.py` (new; unit-only — real containers run in Task 4)

**Interfaces:**
- Consumes: `RuntimeDriver` (`run_container`, `stop`, `status`, `host_port`), `ContainerSpec`.
- Produces (Task 3 wires these; keep signatures exact):
  `BackingAws(runtime, env: str = "default", root: Path = Path(".odin"), client_factory=None)` with
  `provision(service: str, name: str, subscriptions: tuple[str, ...] = ()) -> None`,
  `exists(service: str, name: str) -> bool`,
  `deprovision(service: str, name: str) -> None`,
  `facts(service: str, name: str) -> dict`,
  `aws_env() -> dict[str, str]`,
  `gc(active_kinds: set[str]) -> None`,
  `client(service: str)` (host-side boto3 client for tests/e2e),
  `ensure_backing(service: str) -> None`,
  plus module constants `PROVISIONED = ("s3", "sqs", "sns", "dynamodb")`, `ACCESS_KEY = "allfather"`, `SECRET_KEY = "allfather-secret-key"`, `REGION = "us-east-1"`, `ACCOUNT = "000000000000"`.

**The registry (verified values — do not substitute images or versions):**

```python
@dataclass(frozen=True)
class BackingDef:
    name: str                  # container name suffix: allfather-aws-{name}-{env}
    image: str
    port: int                  # container port of the wire API
    env: dict[str, str]
    command: tuple[str, ...]
    kinds: tuple[str, ...]     # node kinds this backing serves

BACKINGS: tuple[BackingDef, ...] = (
    BackingDef(name="rustfs", image="rustfs/rustfs:latest", port=9000,
               env={"RUSTFS_ACCESS_KEY": ACCESS_KEY, "RUSTFS_SECRET_KEY": SECRET_KEY},
               command=(), kinds=("s3",)),
    BackingDef(name="goaws", image="admiralpiett/goaws:v0.5.4", port=4100,
               env={}, command=("-config", "/conf/goaws.yaml", "Local"),
               kinds=("sqs", "sns")),   # ONE container serves both; SNS→SQS delivery is in-process
    BackingDef(name="dynalite", image="node:alpine", port=4567, env={},
               command=("npx", "-y", "dynalite", "--port", "4567"),
               kinds=("dynamodb",)),    # ~20s cold start (npx fetch); no TTL/Streams — accepted
)
```

**Behaviors:**
- `_backing_for(service)`: the def whose `kinds` contains the service. Container name `f"allfather-aws-{d.name}-{env}"`, labels `{"allfather-env": env}`.
- `ensure_backing(service)`: if `status == "running"` → return. Else `stop(name)` (clear remnant), for goaws first write the config file `root/{env}/goaws.yaml` (mkdir parents) and mount `volumes={str((root/env).resolve()): "/conf"}` — `.odin/` is under the repo CWD which is under `$HOME`, the only tree Colima shares into the VM (macOS `/tmp` is NOT mounted — a `/tmp` mount silently yields a missing file and goaws then emits junk ARNs). Then `run_container` with `ports={d.port: 0}`, then poll `_probe(service)` (a cheap client call: s3 `list_buckets`, sqs `list_queues`, sns `list_topics`, dynamodb `list_tables`) every 1s until success, deadline 120s (dynalite's npx fetch + image pulls). Raise RuntimeError with the container's `logs` tail on timeout.
- goaws.yaml content (write exactly this — the key casing and quoting were verified live; the `Local` top-level key matches the positional `Local` in `command`):
  ```yaml
  Local:
    Host: "localhost"
    Port: "4100"
    Region: "us-east-1"
    AccountId: "000000000000"
    LogToFile: false
  ```
  (`AccountId` casing verified against a live run that produced `arn:aws:sns:us-east-1:<account>:...`. Host stays "localhost" — goaws only uses it to build returned URL hosts, which boto3's endpoint override ignores; our published QUEUE_URL facts are constructed canonically, not read back.) The Task 4 integration test MUST assert a created topic's ARN contains `:000000000000:` — the deterministic canary for config-schema drift.
- `client(service)`: `boto3.client(service, endpoint_url=f"http://127.0.0.1:{self._rt.host_port(cname, d.port)}", aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY, region_name=REGION, config=...)` — for s3 pass `botocore.client.Config(signature_version="s3v4", s3={"addressing_style": "path"})`; others need no Config. `client_factory` (test seam): when provided, called as `client_factory(service, endpoint_url)` instead of boto3.
- `provision`: `ensure_backing(service)` then: s3 `create_bucket(Bucket=name)`; sqs `create_queue(QueueName=name)`; dynamodb `create_table(TableName=name, BillingMode="PAY_PER_REQUEST", AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}], KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])`; sns `create_topic(Name=name)` (its response carries `TopicArn` — use the RETURNED value, don't construct) then for each queue name in `subscriptions`: `create_queue(QueueName=q)` (idempotent — the sqs node's own provision may not have run yet), `qarn = sqs_client.get_queue_attributes(QueueUrl=<returned QueueUrl>, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]` (authoritative — verified pattern; do NOT construct the ARN), `subscribe(TopicArn=<returned>, Protocol="sqs", Endpoint=qarn, Attributes={"RawMessageDelivery": "true"})` (the value is the STRING "true"). Idempotency: same ClientError tolerance as the old MiniStackAws (`"Exist" | "Conflict" | "InUse"` in str → pass, else raise).
- `exists`: first `self._rt.status(cname) == "running"` else False (cheap liveness — a dead backing must demote nodes without HTTP timeouts); then the per-service check (s3 `head_bucket`, sqs `get_queue_url`, sns `list_topics` any ARN endswith `:{name}`, dynamodb `describe_table`) catching `(ClientError, BotoCoreError)` → False (`BotoCoreError` covers EndpointConnectionError when the backing just died).
- `facts(service, name)`: `endpoint = f"http://host.docker.internal:{host_port}"` (import `CONTAINER_HOST` once Task 3 moves it; until then define the literal locally with a comment) — s3 `{"BUCKET": name, "endpoint": endpoint}`; sqs `{"QUEUE_URL": f"{endpoint}/{ACCOUNT}/{name}", "endpoint": endpoint}` (constructed canonically — goaws's own returned URL host doesn't resolve; boto3 consumers dial the endpoint override anyway); sns `{"TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}", "endpoint": endpoint}`; dynamodb `{"TABLE": name, "endpoint": endpoint}`.
- `aws_env()`: for each def whose container is running: `AWS_ENDPOINT_URL_{kind.upper()}` = `f"http://host.docker.internal:{host_port}"` for EVERY kind the def serves (goaws yields both `_SQS` and `_SNS` from one container); plus, always: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`.
- `deprovision`: best-effort per service (s3 `delete_bucket`; sqs `delete_queue(QueueUrl=get_queue_url(...))`; sns `delete_topic(TopicArn=constructed arn)`; dynamodb `delete_table`), swallow `ClientError`/`BotoCoreError` exactly like the old code.
- `gc(active_kinds)`: for each def with `set(d.kinds).isdisjoint(active_kinds)` → `self._rt.stop(cname)` (stop is idempotent on absent names).

- [ ] **Step 1: Failing unit tests** (`tests/aws/test_backings.py`) with a `FakeRuntime` (same shape as `tests/aws/test_rds_postgres.py`'s) and a fake client factory recording calls: (a) `ensure_backing("s3")` runs `rustfs/rustfs:latest` with creds env, dynamic port `{9000: 0}`, name `allfather-aws-rustfs-default`; second call while running → one run; (b) `ensure_backing("sqs")` and `("sns")` both target the SAME container name `allfather-aws-goaws-<env>` and write `goaws.yaml` under `root/env/` containing the exact line `AccountId: "000000000000"` + mount `{abs path: "/conf"}` (use `tmp_path` as root); (c) `provision("sns", "alerts", subscriptions=("jobs",))` — the fake sns client returns `{"TopicArn": "arn:fake:alerts"}` from `create_topic` and the fake sqs client returns a QueueUrl from `create_queue` and `{"Attributes": {"QueueArn": "arn:fake:jobs"}}` from `get_queue_attributes`; assert `subscribe` was called with `TopicArn="arn:fake:alerts"`, `Protocol="sqs"`, `Endpoint="arn:fake:jobs"`, `Attributes={"RawMessageDelivery": "true"}` (i.e. RETURNED values are used, never constructed ones); (d) `exists` False when container not running (no client call made — assert factory not invoked); (e) `facts` shapes for all four kinds exactly as specified; (f) `aws_env` lists `_SQS` and `_SNS` when only goaws runs + always the three cred/region vars; (g) `gc({"s3"})` stops goaws + dynalite but not rustfs; `gc(set())` stops everything; (h) `ContainerSpec.volumes` → `run_container` emits `-v host:container` (extend the existing colima unit tests where run_container args are asserted — find them with `grep -rn "run_container" tests/runtime/`).
- [ ] **Step 2:** `uv run pytest tests/aws/test_backings.py -q` → fails (no module).
- [ ] **Step 3:** Implement `backings.py` + the `ContainerSpec.volumes` extension.
- [ ] **Step 4:** `uv run pytest tests/aws/test_backings.py tests/runtime/ -q` green; `uv run pytest -q` green; `uv run ruff check .` clean.
- [ ] **Step 5: Commit** — `feat(aws): BackingAws — per-env RustFS/goaws/dynalite backing containers`

---

### Task 4: Integration reality pass — e2e rewires run green + the new-kind coverage

**Files:**
- Modify (already compile-correct from Task 3, now made TRUE): `tests/aws/test_provision_e2e.py`, `tests/aws/test_skeleton_e2e.py`, `tests/aws/test_multikind_e2e.py`, `tests/aws/test_aws_usable_e2e.py`
- Create: `tests/aws/test_backings_e2e.py`
- Modify if reality disagrees: `src/odin/aws/backings.py` (e.g. goaws config key casing, probe timing) — every such fix gets its own assertion

**What must pass (run each; fix code until green):**
1. `test_provision_e2e.py`: s3 node "uploads" → healthy; `BackingAws(runtime, "default").client("s3").list_buckets()` shows `uploads`.
2. `test_skeleton_e2e.py` + `test_multikind_e2e.py`: unchanged intent (rds+service gating, multi-kind) on the new wiring.
3. `test_aws_usable_e2e.py`: batch node on `amazon/aws-cli` runs `s3 mb s3://allfather-test` with ONLY the injected env (now `AWS_ENDPOINT_URL_S3`) → done; host-side client sees the bucket.
4. New `test_backings_e2e.py` (all `pytestmark = pytest.mark.integration`, each test tears down via `/destroy` and asserts `runtime.list_allfather() == []`):
   - `test_sqs_roundtrip`: sqs node "jobs" → healthy with `QUEUE_URL` fact; host client `send_message`/`receive_message` roundtrips a body.
   - `test_sns_to_sqs_delivery`: canvas has sns "alerts", sqs "jobs", edge alerts→jobs; both healthy; **assert the topic ARN fact contains `:000000000000:`** (the goaws-config canary); publish "ping" to the TOPIC_ARN via host client → receive from jobs queue → body == "ping" (raw delivery).
   - `test_dynamodb_put_get`: dynamodb node "sessions" → healthy; put_item/get_item roundtrip via host client.
   - `test_env_isolation`: same s3 node label "uploads" applied in env `a` and env `b` → `docker ps` shows `allfather-aws-rustfs-a` AND `allfather-aws-rustfs-b`; bucket exists in each env's client; `/destroy?env=a` kills only `a`'s backings (gc) while `b` stays healthy; then destroy `b`.
   - `test_backing_crash_recovers`: s3 node healthy → `docker rm -f allfather-aws-rustfs-default` → node phase leaves `healthy` (crashed) within ~10s → returns to healthy (reconciler re-provisions; ensure_backing reboots RustFS) → bucket exists again.
5. The Task 1 integration test still green (`tests/aws/test_rds_postgres.py`).

**Sequencing note:** run files one at a time (`uv run pytest -m integration <file> -q`); these boot real containers — after EACH file assert `docker ps -aq --filter label=allfather=1` is empty before the next.

- [ ] **Step 1:** Run e2e files 1-3, fix reality gaps (expect goaws yaml casing and probe timing to be the likely culprits; the canary assertion tells you fast).
- [ ] **Step 2:** Write + run the five new tests in `test_backings_e2e.py` one by one.
- [ ] **Step 3:** Full sweep: `uv run pytest -m integration tests/aws/ -q` → all green, zero leftover containers.
- [ ] **Step 4:** `uv run pytest -q` still green.
- [ ] **Step 5: Commit** — `test(aws): real-backing integration suite — sqs/sns/dynamodb coverage + isolation + crash recovery`

---

### Task 3: The switchover — reconciler/server on real backings, MiniStack deleted

**Files:**
- Modify: `src/odin/reconcile/actions.py` (rename `CreateMiniStackResource` → `ProvisionResource`; docstring drops MiniStack)
- Modify: `src/odin/reconcile/plan.py` (import rename only — logic unchanged)
- Modify: `src/odin/reconcile/reconciler.py` (see below)
- Modify: `src/odin/server.py` (see below)
- Modify: `src/odin/aws/rds.py` (delete `MiniStackRds`; `PostgresRds` remains — move the `CONTAINER_HOST = "host.docker.internal"` constant into `src/odin/runtime/colima.py` and import it from there everywhere)
- Modify: `src/odin/aws/provision.py` (delete `MiniStackAws`; keep `PROVISIONED`; `BackingAws` from Task 2 lives in `src/odin/aws/backings.py` — `provision.py` re-exports `PROVISIONED` only, or fold `PROVISIONED` into `backings.py` and update the two importers `plan.py`/`reconciler.py`; choose the fold — delete `provision.py`)
- Delete: `src/odin/aws/embed.py`, `src/odin/runtime/shim.py`, `src/odin/aws/catalog_gen.py`, `tests/aws/test_embed.py`, `tests/aws/test_catalog_gen.py`, `tests/aws/test_rds_rewire.py` (superseded by `tests/aws/test_rds_postgres.py`)
- Modify: `pyproject.toml` (remove `ministack` dependency; project description → "allfather: a Mac-native, AI-operated orchestration canvas. Draw apps, deps, jobs, LLMs and AWS-shaped resources; a control loop runs them for real on Colima/Lima with real open-source backings.")
- Modify: `src/odin/__main__.py` + `src/odin/server.py` docstrings that mention MiniStack
- Test-modify (unit, must stay green): `tests/reconcile/test_plan.py`, `tests/reconcile/test_reconciler.py`, `tests/api/test_apply.py`, `tests/api/test_environments.py`, `tests/conftest.py` (docstring)
- Test-modify (integration, must IMPORT cleanly — execution happens in Task 4): `tests/aws/test_provision_e2e.py`, `tests/aws/test_skeleton_e2e.py`, `tests/aws/test_multikind_e2e.py`, `tests/aws/test_aws_usable_e2e.py`

**Interfaces:**
- Consumes (from Tasks 1-2, both landed — read `src/odin/aws/backings.py` for ground truth): `PostgresRds(runtime, env)` and `BackingAws(runtime, env, root=Path(".odin"), client_factory=None)` with `provision(service, name, subscriptions=())`, `exists(service, name) -> bool`, `deprovision(service, name)`, `ensure_backing(service)`, `facts(service, name) -> dict` (the per-kind World facts incl. container-reachable `endpoint`), `aws_env() -> dict[str, str]` (the injection vars), `gc(active_kinds: set[str])`, `client(service)` (host-side boto3, for tests).
- Produces: `create_app(runtime=None, store=None, rds=None, aws=None, backings: bool = True, complete: bool = True)` — `backings=False` replaces `embed=False` (pure fakes mode for tests); when True and `rds`/`aws` are None, each env's reconciler gets `PostgresRds(_runtime, env)` / `BackingAws(_runtime, env)`.

**Reconciler changes (exact):**
1. Imports: drop `odin.aws.embed`; `CONTAINER_HOST` now from `odin.runtime.colima`; `PROVISIONED` from `odin.aws.backings`; `ProvisionResource` from actions.
2. `__init__`: drop the `aws_env` parameter entirely. Injection now derives from `self._aws`: in `_run_service`, `env_vars = dict(self._aws.aws_env()) if self._aws is not None else {}` (user env still wins, refs still resolve after).
3. `_execute` `ProvisionResource`: rds branch unchanged; other services → `subs = tuple(e.dst for e in stack.edges if e.src == action.id and self._kind_of(stack, e.dst) == "sqs") if action.service == "sns" else ()`; `await asyncio.to_thread(self._aws.provision, action.service, action.id, subs)`; emit starting. Add tiny helper `_kind_of(stack, rid) -> str | None`.
4. `_observe` PROVISIONED branch: observed in `("starting", "healthy")` → `ok = await asyncio.to_thread(self._aws.exists, res.kind, res.id)`; starting+ok → emit healthy with per-kind facts from `self._aws` (Task 2 provides `facts(service, name) -> dict` returning `{"BUCKET"|"QUEUE_URL"|"TOPIC_ARN"|"TABLE": ..., "endpoint": ...}`); healthy+not-ok → emit crashed (plan then re-provisions — its existing pending/crashed branch already does this; no plan.py logic change).
5. End of `tick()` after the action loop: `if self._aws is not None: await asyncio.to_thread(self._aws.gc, {r.kind for r in stack.resources})`.
6. `_observe_rds` docstring: drop the MiniStack wording (`delete_db` now just stops the container; the "stale record" comment is obsolete — keep the delete_db call, it IS the remnant-clearing).

**server.py changes (exact):** drop the five `odin.aws.embed` imports + `MiniStackAws`/`MiniStackRds` imports; import `PostgresRds` + `BackingAws`; `_make_reconciler(env)` builds `env_rds = rds or PostgresRds(_runtime, env)`, `env_aws = aws or (BackingAws(_runtime, env) if backings else None)`, passes `aws=env_aws`, no `aws_env` kwarg; lifespan keeps only the reconciler resume + stop (no ministack start/stop); module docstring updated.

**Unit-test changes (exact):**
- `test_plan.py`: `CreateMiniStackResource` → `ProvisionResource` (3 sites per the coverage map: lines ~5, 34, 114).
- `test_reconciler.py`: `FakeRds.container_name` → `f"allfather-rds-default-{db_id}"` and the crash-injection string at ~line 237 likewise; `test_aws_env_injected_into_app_containers` → build the Reconciler with a `FakeAws` (new tiny fake in that file: `aws_env()` returns `{"AWS_ENDPOINT_URL_S3": "http://host.docker.internal:9000", "AWS_ACCESS_KEY_ID": "allfather", "AWS_SECRET_ACCESS_KEY": "allfather-secret", "AWS_DEFAULT_REGION": "us-east-1"}`, `gc()` records calls, `provision/exists/deprovision/facts` minimal) and assert the spec env got those keys + DATABASE_URL still starts `postgresql://`; add one new test: after a tick with an empty stack, `FakeAws.gc` was called with `set()`.
- `test_apply.py`: `embed=False` → `backings=False` (4 sites); `FakeRds.container_name` string rename.
- `test_environments.py`: replace the `account_for_env` import/assertions with: two envs, same `s3` node label, `backings=False` + per-env `FakeAws` instances recorded distinct — assert each env's reconciler got its OWN aws object (identity check via `app.state.reconcilers`), and no cross-env World leakage (existing world-scoping assertions stay).
- Integration files: swap `ministack_boto_client(...)` for boto3 clients built from `BackingAws(runtime, env).client(service)` (Task 2 provides `client(service)` returning a host-side boto3 client for that backing) and `create_app(embed=True)` → `create_app()` — compile-correct now, executed in Task 4.

- [ ] **Step 1:** Make every change above (this task is a rename/rewire, not a design task — the failing-test step is the existing suite itself).
- [ ] **Step 2:** `uv run pytest -q` → green (expect ~same count; deletions remove ~3 tests, the new gc test adds 1). Collection must be clean — no import errors from the integration files.
- [ ] **Step 3:** `uv run ruff check .` → clean (deleted imports leave no strays).
- [ ] **Step 4:** Boot proof: `uv run python -c "from odin.server import create_app; app = create_app(backings=False, complete=False); print('app ok')"` and `timeout 20 uv run uvicorn odin.server:create_app --factory --port 4299 &` then `curl -sf localhost:4299/health && curl -sf localhost:4299/envs` then kill it — the real factory boots without ministack installed... (skip the uninstall check; just confirm no `import ministack` anywhere: `grep -rn "ministack" src/ --include='*.py'` → zero hits).
- [ ] **Step 5:** Commit `feat!: the switchover — MiniStack removed, reconciler runs real backings`.

---

### Task 5: UI shrink — generated catalog + emulator-era nodes out

**Files:**
- Delete: `ui/src/lib/catalog.generated.ts`, `ui/src/components/nodes/VpcNode.tsx`, `SubnetNode.tsx`, `Ec2Node.tsx`, `LambdaNode.tsx`, `SgNode.tsx`
- Modify: `ui/src/lib/catalog.ts` (drop the `GENERATED_CATALOG` import at L10 and the spread at L243; fix the stale header comment L1-5)
- Modify: `ui/src/components/Canvas.tsx` (remove the five bespoke `nodeTypes` entries at L34-39 — keep `s3` L38 and `dynamodb` L40; remove their entries in `nodeTypeMap`/`defaultDataForType`/`defaultStyleForType`/`zIndexForType` L45-88; the MiniMap `bespoke` color map L619-626 keeps only s3; `typeOrder` L448 shrinks to the kinds that remain)
- Modify: `ui/src/components/Sidebar.tsx` (the `builtins` array L6-14 keeps only S3 + DDB entries; check what remains renders sensibly)
- Modify: `ui/src/components/ConfigPanel.tsx` (drop vpc/subnet/ec2/lambda/sg from `typeConfig` L16-25 and `fieldsForType` L31-88)
- Modify: `ui/src/lib/iam.ts` (with ec2/lambda gone: `iamActionsForTarget` keeps s3/dynamodb + catalog spread; `defaultPermissions` drops lambda; `computeTypes` becomes empty — follow the compiler: delete what nothing references, keep the file if catalog IAM actions still feed edge labels)
- Modify: `ui/src/components/TopBar.tsx` L153 tooltip → "Run the canvas for real (⌘↵): containers via Colima, AWS-shaped resources on real open-source backings"

**Gate:** `cd ui && bunx tsc --noEmit && bun run build` → clean. `grep -rn "GENERATED_CATALOG\|aws_\|MiniStack" ui/src` → zero hits (except none). 

- [ ] **Step 1:** Make the deletions/edits, following the compiler for every dangling reference (`bunx tsc --noEmit` repeatedly).
- [ ] **Step 2:** Build gate green.
- [ ] **Step 3:** Commit `feat(ui)!: drop the generated AWS catalog + emulator-era nodes (vpc/subnet/ec2/lambda/sg)`.

---

### Task 6: Container-reachable ref facts (the 127.0.0.1 bug) + a real connectivity test

**Context:** `reconciler._observe_container` (reconciler.py:163-181) publishes `HOST: "127.0.0.1"` and `endpoint: "127.0.0.1:…"`/`"http://127.0.0.1:…/"` facts for dep/llm/service nodes. Consumers of `${{node.HOST}}` are OTHER CONTAINERS — inside a container, 127.0.0.1 is the container itself, so every cross-container ref except rds's DATABASE_URL is broken. rds already does this right (reconciler.py:152-156 publishes `host.docker.internal`). Existing e2e only asserted env-var PRESENCE, which is why this survived.

**Files:**
- Modify: `src/odin/reconcile/reconciler.py` (`_observe_container` published facts: `HOST` → `CONTAINER_HOST` (host.docker.internal), add `URL: f"http://{CONTAINER_HOST}:{port}/"` for service kind, add `ADDR: f"{CONTAINER_HOST}:{port}"` for dep/llm; keep `endpoint` as the human-facing 127.0.0.1 form the UI displays — document the split in the fact-publishing comment)
- Test: `tests/reconcile/test_reconciler.py` (facts assertions), plus a NEW integration test `tests/aws/test_ref_connectivity_e2e.py`

**Integration test (the real proof):** canvas = dep `cache` (image `redis:7-alpine`, port 6379) + batch `pinger` (image `redis:7-alpine`, command `["sh", "-c", "redis-cli -h $CACHE_HOST -p $CACHE_PORT ping | grep -q PONG"]`, env `{"CACHE_HOST": "${{cache.HOST}}", "CACHE_PORT": "${{cache.PORT}}"}`). Apply, wait for `cache: healthy` then `pinger: done` (done proves a REAL cross-container TCP round-trip through the ref system). Destroy, assert zero leftovers. Marked integration.

- [ ] **Step 1:** Failing unit assertions on the new fact values (extend the existing observe tests in `test_reconciler.py`).
- [ ] **Step 2:** Implement the fact changes. `uv run pytest -q` green.
- [ ] **Step 3:** Run the new integration test: `uv run pytest -m integration tests/aws/test_ref_connectivity_e2e.py -q` → passed.
- [ ] **Step 4:** Commit `fix(reconcile): publish container-reachable ref facts (HOST/ADDR/URL) — 127.0.0.1 refs never worked across containers`.
