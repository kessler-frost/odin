# Research: service-coverage phase (EC2 / ECS / VPC / SG / IAM / Lambda / ECR)

Seeds the implementation plan for NORTHSTAR directive 5 ("the major AWS services
— EC2, ECS, VPC, IAM, Lambda, ECR — we should definitely be able to support")
and directive 6 (Nebula for VPCs/SGs/firewalls). Resolution of record: **EC2 =
real Lima VMs** (NORTHSTAR §Resolved 4).

Everything below was prototyped **for real**: OpenTofu 1.12.3, `hashicorp/aws`
**v5.100.0**, run through a logging reverse-proxy in front of a live **MiniStack**
(the OSS emulator, used here only as a capture target — never added to odin).
MiniStack answers the EC2/VPC/SG/IAM/Lambda/ECR/ECS wire protocols for real, so
the provider drove **full apply → plan → destroy** round-trips for
`aws_vpc`+`aws_subnet`+`aws_security_group`, `aws_instance`+`aws_key_pair`,
`aws_ecr_repository`, `aws_lambda_function`+`aws_iam_role`, and
`aws_ecs_cluster`+`aws_ecs_task_definition` — **zero drift on all but ECS**
(whose task-definition re-registers on a JSON-normalization diff, §2e). That
captured the **complete** call sequence per resource (not just the first calls a
pure sink would surface). Harness: `scratchpad/coverage-research/` (`proxy.py` +
`tf-*/` + `capture-*.jsonl`).

## TL;DR — GO. Cheapest first slice = VPC/Subnet/SG (pure model + Nebula, no VM).

- The TF provider drives all seven services against MiniStack-shaped models —
  **zero drift** on VPC/EC2/ECR/Lambda, a known task-definition re-register on
  ECS (§2e). The models are simple, wire-enumerable, and MiniStack's own response
  bytes (captured here) **are** the shapes odin must emit.
- **The architectural crux:** unlike s3/sqs/dynamodb (where a real backing —
  RustFS/goaws/dynalite — terminates the protocol and the gateway only
  synthesizes gaps), **no OSS backing speaks EC2/VPC/IAM/ECR/Lambda/ECS**. So per
  directive 5, odin's **gateway owns the full model** for these services
  (adopting MiniStack's model shapes), answering every describe/state-poll from
  stored state. This is a scale-up of the existing `synth.pure_answer` seam from
  "gap-filler" to "model owner" — the code path already exists (`app.py` returns
  a `pure_answer` directly, no backing needed).
- **Real execution (non-negotiable) comes from the substrate binding**, not the
  model: VPC/SG → **Nebula** network + firewall (primitives already in
  `fabric/nebula.py`); EC2 → **Lima VM** (`compute/` + `LimaRuntime`); ECS task →
  **Colima container** (`ColimaRuntime`); ECR → **registry:2** (CNCF
  Distribution, Apache-2.0 — `docker push` verified working here); Lambda →
  **RIE** container (Apache-2.0). The model answers the provider; the substrate
  makes each resource a real local thing.
- **MiniStack's realness is uneven — and its `_docker` seam is exactly odin's
  substrate-binding precedent** (Q1, §2.6). EC2/VPC/SG/IAM/ECR are **pure
  in-memory** (`ec2.py:3`: *"instances exist in memory only, no real VMs
  launched"*); **ECS `RunTask` spawns real Docker containers**
  (`ecs.py:1295` `docker_client.containers.run(...)`, `ministack=ecs` label +
  reaper) and **Lambda** runs a subprocess worker by default or the **AWS RIE
  Docker image** under `LAMBDA_STRICT`. That `_get_docker()` client is the
  `_docker` seam odin once rewired — so MiniStack already validates
  ECS→container and Lambda→RIE; odin just repoints the seam at Colima. **EC2 is
  the one service where odin adds realness MiniStack lacks** (the Lima VM).
- Recommended sequence: **VPC/SG → IAM(CRUD)+ECR → EC2-on-Lima → Lambda → ECS**,
  with ALB/EKS/RDS-via-TF/CloudFormation/autoscaling recorded **unsupported**.

---

## 1. Provider config (verified — identical to the s3/sqs research)

The exact `skip_*` block odin's `simulate/workspace.py::OVERRIDE_TF` already
writes worked unchanged for all six services; endpoints via `AWS_ENDPOINT_URL`
env var only (no `endpoints{}` block):

```hcl
provider "aws" {
  access_key = "test"  ;  secret_key = "testsecret"  ;  region = "us-east-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true
}
# runner injects: AWS_ENDPOINT_URL=<gateway>, AWS_ACCESS_KEY_ID/SECRET, region
```

No new provider knobs are needed for compute/network/IAM. The existing
`simulate/` machinery (`materialize` + `override.tf` + env-var routing) carries
these services with **zero change** — only the gateway model surface grows.

---

## 2. Captured per-resource call surfaces (deliverable — the model odin must serve)

`[N]` = observed call count. Every call returned 200 unless noted; every plan was
**zero-drift**. Full ordered logs: `capture-<svc>-<phase>.jsonl`.

### 2a. VPC + Subnet + Security Group — EC2 **query** protocol (`Action=` form body, XML)

**Create (22 calls):**
```
CreateVpc                       # req: CidrBlock, InstanceTenancy, AmazonProvidedIpv6CidrBlock, TagSpecification.N
  -> DescribeVpcs x2             # provider re-reads to hydrate computed attrs
  -> DescribeVpcAttribute x3     # enableDnsSupport / enableDnsHostnames / (network-usage-metrics)
  -> DescribeNetworkAcls         # the DEFAULT NACL auto-created with the VPC
  -> DescribeRouteTables         # the MAIN route table auto-created with the VPC
  -> DescribeSecurityGroups      # the DEFAULT security group auto-created with the VPC
CreateSubnet                    # req: VpcId, CidrBlock, AvailabilityZone, TagSpecification.N
CreateSecurityGroup             # req: GroupName, GroupDescription, VpcId, TagSpecification.N
  -> DescribeSubnets / DescribeSecurityGroups (readback)
  -> RevokeSecurityGroupEgress x2   # provider deletes AWS's auto-created allow-all egress (IPv4 + IPv6)
  -> AuthorizeSecurityGroupIngress  # exactly the config's ingress rules (IpPermissions.N.*)
  -> AuthorizeSecurityGroupEgress   # exactly the config's egress rules
```
**Plan (9 calls):** pure `Describe{Vpcs,VpcAttribute x3,NetworkAcls,RouteTables,SecurityGroups,Subnets}` → zero drift.
**Destroy (16 calls):** Describe sweep → `DescribeNetworkInterfaces` (ENI check) →
`DeleteSubnet` → `DeleteSecurityGroup` → `DeleteVpc`; each followed by a Describe
that returns **HTTP 400** (delete-confirm; provider tolerates it).

**Model requirements this dictates:**
1. **A VPC is not just a CIDR.** CreateVpc must auto-mint 3 children the provider
   immediately reads: a **default NACL**, a **main route table**, a **default
   security group** (return their ids in DescribeNetworkAcls/RouteTables/
   SecurityGroups filtered by vpc-id).
2. **`DescribeVpcAttribute`** must answer `enableDnsSupport`/`enableDnsHostnames`
   (defaults true/false).
3. **Security-group create seeds a default allow-all egress rule** — the provider
   *revokes* it then re-authorizes config. So the SG model needs
   Revoke/Authorize {Ingress,Egress} mutating a rule set, seeded with the egress
   default. The `IpPermissions.N.{FromPort,ToPort,IpProtocol,IpRanges.M.CidrIp,
   Ipv6Ranges.M.CidrIpv6,UserIdGroupPairs.M.GroupId}` wire shape parses **directly**
   into the dict `fabric/nebula.py::sg_rules_to_firewall` already consumes (it
   reads `IpProtocol/FromPort/ToPort/IpRanges[].CidrIp/UserIdGroupPairs[].GroupId`
   — an exact match). **No new translation needed for the Nebula compile.**
4. **Delete-confirm** = the post-delete Describe returns an EC2 error envelope.
   The provider tolerated even MiniStack's generic `<Code>` — real AWS uses
   `InvalidVpcID.NotFound` / `InvalidGroup.NotFound`; odin should emit those for
   fidelity but the bar is low.

**ID generation (observed):** created resources get random-hex ids
(`vpc-71f5ad1cdd3f4f0dd`, `subnet-4623fe902ad0dacd5`, `sg-8f9a45d1baedf4c24`,
`sgr-…` per rule); MiniStack's *default* VPC uses a counter (`vpc-00000001`).
odin should mint random-hex ids of the AWS-correct prefix+length.

### 2b. EC2 instance + key pair — EC2 query protocol

**Create (26 calls):** `ImportKeyPair` (aws_key_pair carries `public_key` →
Import, **not** Create; returns keyName/keyFingerprint/keyPairId) → VPC/subnet
create (as 2a) → **`RunInstances`** → *(provider waits ~10 s)* →
`DescribeInstances` polled until `state=running` → hydration reads:
`DescribeInstanceTypes`, `DescribeTags`, `DescribeInstanceAttribute` ×4
(disableApiTermination / userData / instanceInitiatedShutdownBehavior / …),
`DescribeVolumes` (root EBS), `DescribeInstanceCreditSpecifications`
(t2 burstable → `cpuCredits=standard`).
**Plan:** re-reads the same describe set → zero drift.
**Destroy (27 calls):** describe sweep → `ModifyInstanceAttribute` (**400** —
MiniStack lacks it, `InvalidAction`; provider **tolerates** and proceeds) →
`TerminateInstances` → `DescribeInstances` → `DeleteKeyPair` → subnet/vpc delete.

**Model requirements:**
- **`RunInstances` → `DescribeInstances` is a state-polling waiter.** The
  provider sleeps ~10 s before the first poll, then polls until `running`. This
  is the natural seam for **EC2 = Lima VM**: RunInstances kicks a Lima VM boot
  and stores the instance as `pending`; DescribeInstances returns `pending` until
  the VM is SSH-ready, then `running`. Lima's ~30–60 s boot simply means the
  provider polls a few more times — the waiter is built for it (it polls for
  minutes). No timing hack required.
- **Instance model fields** (from the response shapes): instanceId, imageId,
  instanceType, state{code,name}, subnetId, vpcId, privateIpAddress,
  keyName, placement.availabilityZone, rootDeviceName, blockDeviceMapping (1 EBS
  vol), tags, plus the read-only `DescribeInstanceAttribute`/`CreditSpecifications`
  answers. The **private IP** should be the Lima VM's vzNAT/shared-network IP
  (see §3).
- **`ModifyInstanceAttribute`** can be a tolerated stub in v1 (provider survives
  a 400), but is the eventual seam for in-place user_data / source_dest_check.

### 2c. ECR repository — ECR **JSON** protocol (`X-Amz-Target: AmazonEC2ContainerRegistry_V20150921.*`)

**Create (3 calls):** `CreateRepository` → `DescribeRepositories` →
`ListTagsForResource`. **Plan (2):** DescribeRepositories + ListTagsForResource →
zero drift. **Destroy (4):** describe + `DeleteRepository` + post-delete
DescribeRepositories **400**.

Response model (verbatim from MiniStack, JSON):
```json
{"repository":{"repositoryArn":"arn:aws:ecr:us-east-1:000000000000:repository/covres-app",
 "registryId":"000000000000","repositoryName":"covres-app",
 "repositoryUri":"000000000000.dkr.ecr.us-east-1.amazonaws.com/covres-app",
 "createdAt":1784760942,"imageTagMutability":"MUTABLE",
 "imageScanningConfiguration":{"scanOnPush":true},
 "encryptionConfiguration":{"encryptionType":"AES256"}}}
```
Smallest surface of all six. `aws_ecr_repository` is **control-plane only** — it
never pushes images. So odin needs just the 4-action CRUD model; the
**data plane** (image storage) is a separate `registry:2` container per env whose
address odin substitutes into `repositoryUri` (e.g. `127.0.0.1:PORT/covres-app`).
`docker push` to registry:2 **verified here** (image stored, catalog listed).
A real `docker push`/`pull` also needs `GetAuthorizationToken` + a docker-login
shim (registry:2 can run auth-less locally) — a data-plane follow-up, not needed
for the TF resource itself.

### 2d. Lambda function + IAM role — IAM **query** (XML) + Lambda **REST** (path/JSON)

**Create (12 calls):**
```
IAM (query):   CreateRole -> GetRole -> ListRolePolicies -> ListAttachedRolePolicies
               -> AttachRolePolicy -> ListAttachedRolePolicies
Lambda (REST): POST /2015-03-31/functions            (201; returns State="Pending")
               GET  /2015-03-31/functions/<name> x3  (polled until State="Active")
               GET  /2015-03-31/functions/<name>/versions
               GET  /2020-06-30/functions/<name>/code-signing-config
```
**Plan (7):** IAM GetRole/List* + Lambda GET function/versions/code-signing →
zero drift. **Destroy (12):** DetachRolePolicy → `DELETE /functions/<name>` (204)
→ `ListInstanceProfilesForRole` → `DeleteRole`.

**Model requirements:**
- **`aws_lambda_function` REQUIRES an `aws_iam_role`** (execution role ARN). So
  **IAM-as-a-service is a hard prerequisite** for Lambda. IAM here is pure CRUD:
  `CreateRole` stores {RoleName, RoleId=`AROA…`, Arn, Path, AssumeRolePolicy
  document (URL-encoded JSON, stored verbatim), MaxSessionDuration};
  `AttachRolePolicy` stores an attachment referencing a PolicyArn (the managed
  `AWSLambdaBasicExecutionRole` need not exist). **MiniStack does not evaluate
  these** — it stores documents. odin already HAS an evaluator
  (`gateway/policy.py`), so TF-created roles/policies map onto odin's policy
  store as **stored principals/documents**; enforcing assumed-role policies is a
  stretch goal beyond round-tripping TF.
- **Lambda uses a THIRD wire shape** — REST (method + path), not query and not
  X-Amz-Target JSON. `CreateFunction` = `POST /2015-03-31/functions` with a JSON
  body carrying `Code.ZipFile` (base64). It returns `State:"Pending",
  StateReasonCode:"Creating", LastUpdateStatus:"InProgress"`; the provider polls
  `GET /functions/<name>` until `State:"Active"` — same async-create waiter shape
  as EC2. Function model: FunctionName, FunctionArn, Runtime, Role, Handler,
  CodeSize, CodeSha256, Timeout, MemorySize, Version=`$LATEST`, State.
- **Real execution** = the substrate: a per-function container built on the AWS
  base image (`public.ecr.aws/lambda/<runtime>`, which bundles **RIE**,
  Apache-2.0) or RIE injected into a runtime image; `Invoke` POSTs to the RIE
  endpoint. v1 can ship the control-plane state machine and defer real Invoke.

### 2e. ECS cluster + task definition — ECS **JSON** protocol (`AmazonEC2ContainerServiceV20141113.*`)

**Create (5 calls):** `CreateCluster` → *(provider waits ~10 s, ACTIVE waiter)* →
`RegisterTaskDefinition` → `DescribeTaskDefinition` → `DescribeClusters` ×2.
**Plan (2):** DescribeClusters + DescribeTaskDefinition. **Destroy (5):**
DescribeClusters/TaskDefinition → `DeleteCluster` → `DeregisterTaskDefinition`.

Response models (verbatim): cluster → `{clusterArn, clusterName,
status:"ACTIVE", *Count:0, settings:[{containerInsights:disabled}], ...}`;
task-def → `{taskDefinitionArn:".../covres-task:1", family, revision:1,
status:"ACTIVE", containerDefinitions:[...], networkMode:"bridge", ...}` —
**revision is a per-family counter**.

**Honest caveat (captured):** ECS is the **one non-zero-drift** service — the
plan wanted to re-register the task definition (`1 to add / 1 to destroy`). This
is the well-known TF-AWS `container_definitions` JSON-normalization quirk (the
provider canonicalizes the container JSON and re-registers on any byte
difference), **not** a substrate problem — odin's ECS model must echo
`containerDefinitions` in the provider's canonical field order/casing to avoid
it. Not captured live: `RunTask`/`aws_ecs_service` (needs task placement +
networking); the substrate is the **existing `ColimaRuntime`** — RunTask →
`ColimaRuntime.run_container` from the task-def image, task state
(PROVISIONING→RUNNING→STOPPED) mapped from container status via the existing
`_STATUS_TO_PHASE` seam. See §4 sizing.

---

## 2.6. MiniStack's per-service models — what to adopt (Q1, from installed source)

MiniStack root read: `…/site-packages/ministack` (never added to odin). The
**model shapes** are worth adopting; the **emulator plumbing is not** (odin owns
the wire via its gateway).

**Architecture spine (adopt the *shape*):**
- **State** (`core/persistence.py`, `core/responses.py`): each service keeps a
  module-level dict wrapped in `AccountScopedDict` / `AccountRegionScopedDict`
  (per-account, per-region keys), with optional versioned JSON persistence
  (`PERSIST_STATE=1`). → odin's equivalent is the per-env `JsonStore` sidecars
  already in `gateway/stores.py`; per-account/region == odin's per-env scoping.
  Adopt the *keying discipline*, not the class.
- **ID/ARN gen:** `core/arn.py` is only a parser; ids are minted per-service as
  `prefix + random-hex/base32` (IAM `_gen_id("AROA")`; EC2 `vpc-<17hex>`,
  `sg-<8|17hex>` per `_SECURITY_GROUP_ID_RE`; observed on the wire in §2).
  → odin mints the AWS-correct prefix+length per resource; trivial.
- **Dispatch** (`core/router.py`, `app.py`): one ASGI app, routes by header/path/
  query to per-service handlers, each returning wire bytes. → odin already has
  this (`gateway/app.py` + `classify.py`); it just needs the three new protocol
  branches (§3).

**Per-service "how real + what to adopt":**
- **EC2 (`ec2.py`, 5737 ln):** **pure in-memory**, no VM (docstring line 3).
  `_run_instances` stores an instance dict (instanceId, imageId, instanceType,
  state, subnet/vpc, privateIp, blockDeviceMapping, tags) and returns
  `state=running` immediately; a static AMI table backs DescribeImages. VPC/
  subnet/SG handlers live here too. **Adopt:** the state-field lists + the
  auto-created default NACL/RT/SG behavior. **Don't adopt:** the "instant
  running" — odin's Lima VM supplies the real pending→running.
- **ECS (`ecs.py`, 2157 ln):** **real** — `RunTask` calls
  `docker_client.containers.run(cdef["image"], …)`, tracks `_docker_ids` on the
  task, reaps `ministack=ecs` containers. TaskDef stored with a `:revision`
  counter; cluster ACTIVE immediately. **Adopt:** the task/def/cluster model +
  the container-per-container-definition mapping (this IS odin's ColimaRuntime
  binding); the `_get_docker()` seam is the rewire point.
- **ECR (`ecr.py`, 1297 ln):** **metadata only** — stores repository dicts
  (arn/uri/mutability/scan/encryption); **no image layers** (that's registry:2's
  job). **Adopt:** the repo model verbatim (§2c).
- **IAM (`iam.py`, 2965 ln):** **pure CRUD, no evaluation** — `_roles[name] =
  {RoleName, RoleId:_gen_id("AROA"), Arn, Path, AssumeRolePolicyDocument (stored
  verbatim), AttachedPolicies:[policy_arns], MaxSessionDuration}`;
  `AttachRolePolicy` appends an ARN to the list; users/groups/policies parallel.
  **Adopt:** this role/policy/attachment store as odin's TF-IAM resource store —
  it feeds Lambda role ARNs + EC2 instance profiles. odin's *enforcement* is its
  separate edge-engine (`gateway/policy.py`); the two meet only where a TF role's
  attached policies could later seed odin statements (stretch goal).
- **Lambda (`lambda_svc.py` 6124 + `core/lambda_runtime.py` 1244):** **real
  execution, three modes** — a persistent Python/Node **subprocess worker pool**
  (`lambda_runtime.py::_spawn` → `subprocess.Popen`) by default; the **AWS RIE
  Docker image** when `LAMBDA_STRICT=1`; or an HTTP **proxy container** per
  function. Function config stored with the Pending→Active state machine (§2d).
  **Adopt:** the function model + State machine; the RIE-container mode is
  precisely odin's recommended Invoke substrate (Apache-2.0), already gated
  behind the same `_docker` seam.

Net: MiniStack is a **model + wire reference** to reimplement (per NORTHSTAR:
"models remain a design reference," not a runtime dep). Its `_docker` seam
(ECS/Lambda) is the strongest evidence that odin's container/VM substrate mapping
is the right shape — MiniStack itself does it there; odin extends it to EC2 (VM)
and repoints all of it at Colima/Lima.

---

## 3. Substrate mapping + lifecycle (deliverable)

| Service | odin model owner | Real substrate (already in-repo unless noted) | Lifecycle binding |
|---|---|---|---|
| **VPC / Subnet** | gateway EC2 model store | **Nebula network** per env (`fabric/nebula.py::ensure_network`, sticky /16→/24 subnets in `MeshNetwork.allocate_subnet`) | CreateVpc → `ensure_network(env)`; CreateSubnet → `allocate_subnet`. cidr→base_cidr mapping. No VM. |
| **Security Group** | gateway EC2 model store (rule set) | **Nebula firewall** (`sg_rules_to_firewall` → `generate_config` inbound/outbound) | Authorize/Revoke mutate the SG rule set → recompile per-env firewall. Wire `IpPermissions` shape == function input (no adapter). |
| **EC2 instance** | gateway EC2 model store | **Lima VM** (`compute/lima_yaml.py` + `cloud_init.py` + `runtime/lima.py`) | RunInstances → boot VM (t2.* → `INSTANCE_TYPES` cpus/mem), user_data → cloud-init provision script, key_name → ssh pubkey inject; instance `pending` until SSH-ready → `running`; private IP = VM's `shared`(vzNAT) network IP; joins the env's Nebula net. Terminate → VM delete. |
| **ECR repo** | gateway ECR model store | **registry:2** container per env (CNCF Distribution, **Apache-2.0**) | CreateRepository → ensure registry container; `repositoryUri` → `host:port/<name>`; `docker push` works (verified). GetAuthorizationToken shim for login (follow-up). |
| **Lambda fn** | gateway Lambda model store | **RIE** container per function (**Apache-2.0**), on ColimaRuntime | CreateFunction → store code+config, `State:Pending`→`Active`; Invoke → POST to RIE. v1: control plane; Invoke = follow-up. |
| **ECS task** | gateway ECS model store | **Colima container** (`runtime/colima.py`, exists) | RunTask → `run_container` from the task-def image; task state from container `_STATUS_TO_PHASE`. |
| **IAM role/policy** | **odin policy store** (`gateway/policy.py` + a new CRUD role/doc store) | none (pure control plane) | CRUD store of roles/attachments/documents; referenced by Lambda role ARN + EC2 instance profiles. |

**Compute substrate specifics (from `compute/` + `runtime/lima.py`):** VMs boot
from Ubuntu 24.04 cloud images (arm64/amd64 auto-picked); `INSTANCE_TYPES` maps
`t2.micro/small/medium`→cpus/mem/disk; `generate_cloud_init` already injects an
SSH pubkey and (optionally) nerdctl; `shared_network=True` adds the Lima `shared`
(vmnet/vzNAT) interface that yields a stable host-reachable IP → the instance's
`privateIpAddress`. `user_data` (HCL) → the cloud-init provision script verbatim.
Boot is ~30–60 s; the provider's DescribeInstances waiter polls happily across it.

**Gateway architecture change (the load-bearing point for the plan):** these
services are **all-synth** — `app.py`'s pipeline already returns a
`synth.pure_answer` directly with no backing forward. The coverage work
generalizes `synth.py` from "a handful of gap-fill handlers" into
**per-service model modules** (each owning create/describe/delete over a per-env
state store, same `JsonStore` sidecar shape in `gateway/stores.py`), plus three
new `classify.py` branches: **EC2/IAM query** (like `_classify_sns` but
ec2/iam namespaces + param-based resource ids), **ECR JSON-target** (extend
`_classify_target`), **Lambda REST** (method+path). Substrate binding (VM/
Nebula/container/registry create+teardown) is driven from the reconciler or a
new `compute`/`network` provisioner seam, keyed off the model store — the same
provision/deprovision shape `aws/backings.py` uses today.

---

## 4. Sequencing recommendation (deliverable)

Confirms the hypothesis, with one refinement: **insert IAM(CRUD)+ECR as a cheap
slice-2 before EC2** (IAM is a hard Lambda prerequisite and both are pure model,
no substrate — cheap to land early and they de-risk the query/JSON classify
branches before the expensive VM work).

1. **Slice 1 — VPC / Subnet / SG (pure model + Nebula, NO VM).** *Cheapest
   coherent slice, and the foundation EC2 depends on.* New: EC2 query-protocol
   classify branch; EC2 model store for vpc/subnet/sg/rules **incl. the
   auto-created default NACL / main RT / default SG** and `DescribeVpcAttribute`;
   XML response emission; delete-confirm errors. Nebula binding reuses
   `ensure_network` + `sg_rules_to_firewall` (**both already exist**). Delivers
   directive 6. **Size: M** (protocol + ~15 actions + XML; Nebula primitives free).
2. **Slice 2 — IAM(CRUD) + ECR (pure model, minimal/no substrate).** IAM: reuse
   slice-1 query infra; CRUD store for roles/policies/attachments (MiniStack
   shapes; maps onto odin's policy store); unblocks Lambda + instance profiles.
   ECR: extend `_classify_target` for the JSON protocol; 4-action model; optional
   registry:2 data plane. **Size: S each.**
3. **Slice 3 — EC2 on Lima (flagship substrate).** RunInstances→Lima VM boot,
   ImportKeyPair, the pending→running DescribeInstances waiter, hydration reads
   (InstanceTypes/Volumes/CreditSpecifications/Attributes), user_data→cloud-init,
   Terminate→VM delete; instance joins slice-1's Nebula net. Depends on slice 1.
   **Size: L** (real VM lifecycle + ~12 describe actions + boot-state mapping).
4. **Slice 4 — Lambda (control plane → real Invoke).** New Lambda REST classify
   branch; function model with the Pending→Active waiter + code storage; depends
   on slice 2 (IAM). Real Invoke via RIE container is a data-plane follow-up.
   **Size: L** (third wire shape + async state + RIE).
5. **Slice 5 — ECS (containers on Colima).** Cluster/TaskDef/Service/RunTask
   model (JSON-target, reuse existing) → `ColimaRuntime` (exists). Depends on
   nothing new. **Size: M–L.**

**Record as UNSUPPORTED now (directive 5 honesty rule):** `aws_lb`/ELBv2
(needs a real load balancer), `aws_eks_cluster` (k8s), **RDS via TF** (stays on
the reconciler Postgres path — already the recorded Simulate-v1 decision),
`aws_autoscaling_group`, CloudFormation, and deep data-plane fidelity — Lambda
layers/versions/aliases beyond `$LATEST`, EC2 EBS snapshots / secondary ENIs /
spot, ECR image scan *findings*, `ModifyInstanceAttribute` in-place edits (v1
stub). Surface these in `generate_tf`'s existing `unsupported` list
(`iac/hcl.py::_UNSUPPORTED_REASONS`) so Apply tells the user, never silently
drops.

---

## 5. Evidence index

`scratchpad/coverage-research/`: `proxy.py` (the logging proxy — reusable capture
tool), `tf-{vpc,ec2,ecr,lambda}/main.tf`, and `capture-<svc>-<phase>.jsonl` (every
request/response, the authoritative call surface). MiniStack booted via
`uv run --with ministack python -m ministack` (port 4566), proxy on 4610,
`AWS_ENDPOINT_URL` pointed the provider at the proxy. Licenses verified permissive:
**CNCF Distribution / registry:2 = Apache-2.0**, **AWS Lambda RIE = Apache-2.0**
(Nebula = MIT and Lima = Apache-2.0 per prior decisions). No `covres-*` containers
or stray processes left behind.
