# The intelligence layer — canvas gestures as the language

**Status:** the FIRST GESTURE IS BUILT and shipped in v0.8.0 — an ecs box drawn
inside an ec2 box places that workload's tasks in that instance's VM, end to
end. The rest of this document is still design. Owner directive 2026-07-27,
after v0.7.7; first gesture landed 2026-07-28.

**What changed against this document's own predictions, now that it has been
built** (kept here rather than rewritten away, because the misses are the useful
part):

- The unlocking change it named — `LimaRuntime.VM` being a class constant — was
  exactly right, and became `LimaRuntime(vm=...)` per instance.
- It framed the gesture as **Fargate → EC2 launch type**. That turned out to be
  wrong in a way worth recording: odin already emits `launch_type = "EC2"`
  unconditionally and has NO Fargate substrate, so flipping the label would have
  claimed a distinction odin cannot back. What shipped is PLACEMENT — where the
  container actually runs — expressed as a real
  `placement_constraints { memberOf }` so it survives `tofu plan` and the
  provider round trip.
- It missed a prerequisite entirely: an expanded EC2 box did not SURVIVE a
  reload, because a leaf's stored height was dropped on load. The gesture was
  unusable until that was fixed — and fixing it revealed the same bug for every
  other kind, which the owner then caught and which is now general.
- Its four stated costs were all real, and all four are now CLOSED (v0.8.1).
  This bullet said they "remain real and unaddressed" for a while after they
  stopped being either, which is the stale-caveat failure this repo keeps
  finding in its own docs — so, with the code that answers each:
  - *the unlocking change* — `LimaRuntime(vm=...)` per instance, no longer a
    class constant.
  - *ordering* — `hcl.py::_placement_dependency` emits
    `depends_on = [aws_instance.<host>]`, so tofu (which already owns ordering
    here) will not schedule a task before its instance exists. A real
    dependency, not a wait loop.
  - *capacity* — `spec/capacity.py::overcommitted` refuses the apply BEFORE
    anything is built, naming the instance, what was asked of it and what it
    has. Arithmetic, not a scheduler: one sum per instance, and a canvas that
    fits pays nothing. v0.8.2 finished it by making the number it reasons about
    authorable at all (`memory` on the ecs node reached neither the HCL nor the
    UI before, so every task was the 512 MiB default).
  - *failure meaning* — `compute/tasks.py::TaskRuntime(placed_on=...)` phrases a
    placed task's failure against the instance it was placed in, so "the VM is
    not up" and "the task failed" stay distinguishable. They need opposite
    responses from a person.

> "canvas and navigating things around IS the language of odin, and not
> chatting with a bot to update things around" — the owner.

A chat/agent surface is a separate, later thing (NORTHSTAR's canvas↔Terraform
translator). This document is about making **spatial gestures carry real
architectural meaning**, because moving a box is the fastest way a person can
say what they want.

---

## The invariant, first, because it is what can go wrong

**A gesture may change what containment genuinely determines. It must never
silently rewrite what a person authored.**

- the node's `label` — never
- any field the user typed — never
- fields that containment *determines* — yes, and odin **says so**

This is honesty rule 2 applied to the canvas: report the outcome, do not
quietly perform one. The v0.7.6 release was largely about an import that
"silently renamed every resource"; a gesture that silently rewrites config is
the same bug wearing a nicer coat.

---

## 1. ECS inside EC2 — and why it can be REAL, not a label

The owner's example: expand an EC2 box, drop an ECS box inside it, and that
means **ECS on the EC2 launch type rather than Fargate**. That is a real AWS
distinction — ECS tasks run either on EC2 container instances or on Fargate.

### What is true today (checked, not assumed)

| fact | where |
| --- | --- |
| `launch_type` is hardcoded `"EC2"` for every ecs node | `agent/hcl.py:759` |
| ...deliberately, as the "LEAST-FICTION" choice | `agent/hcl.py:708-711` |
| the field exists end to end | `gateway/records.py:402`, `ecsctl.py:566` |
| ECS tasks actually run as plain containers on the host | `compute/tasks.py::TaskRuntime` |
| **there is no container-instance concept at all** | grep: zero hits |

So today the launch type is a **declared string that nothing acts on**. A
gesture that only flipped that string would *increase* fiction — the canvas
would claim a placement odin does not perform. That is the shape this repo
keeps finding and must not add on purpose.

### The version that is real, using machinery odin already has

Both halves already exist:

- an EC2 node **is** a Lima VM, named `odin-ec2-{env}-{instance_id}`
  (`compute/instances.py::vm_name`)
- `LimaRuntime` already runs containers **inside** a Lima VM via `nerdctl`
  (`runtime/lima.py`), behind the same `RuntimeDriver` protocol
- `TaskRuntime` takes an **injectable** driver (`compute/tasks.py:63`)

So the gesture can mean exactly what it looks like:

```
ecs OUTSIDE any ec2 box  ->  launch_type FARGATE
                             task container runs on the host (ColimaRuntime)
                             "serverless": no VM you manage

ecs INSIDE an ec2 box    ->  launch_type EC2
                             task container runs INSIDE that VM
                             TaskRuntime(runtime=LimaRuntime(vm=vm_name(env, id)))
                             you manage the VM; the task lands on it
```

That is genuine ECS-on-EC2 placement, not a relabel. It also makes Fargate a
real distinction rather than a word, and it reuses `RuntimeDriver` rather than
inventing a mechanism.

**The user-facing sentence, in the owner's own framing, which is clearer than
the mechanism:** *Fargate = odin picks where it runs. EC2 = you drew the box
it runs in.* That happens to be exactly what the AWS distinction means — with
Fargate you do not manage the capacity, with EC2 you do.

**One naming trap, because `LimaRuntime` already exists and means something
else.** There are THREE runtime bindings here and only two of them are this
feature:

| | driver | container lives |
| --- | --- | --- |
| Fargate (outside any box) | `ColimaRuntime` | host docker — no VM the user drew |
| EC2 (inside a drawn box) | `LimaRuntime` bound to `odin-ec2-{env}-{id}` | inside THAT VM |
| odin's existing "VM isolation" mode | `LimaRuntime` bound to `odin-host` | one SHARED VM, unrelated to ecs placement |

Fargate is **not** Lima — `TaskRuntime.__init__` is `runtime or ColimaRuntime()`,
so the default path is host docker. Lima is what makes the EC2 case real. The
third row is why `LimaRuntime.VM` being a class constant is the unlocking
change: the mechanism is right, it is just hardwired to the wrong VM.

### What it costs — stated up front

1. **`LimaRuntime.VM` is a class constant** (`"odin-host"`, `lima.py:49`). It
   has to become per-instance. Small, but it is the change that unlocks this.
2. **Ordering.** The EC2 VM must be running before its task can start. Today
   nothing sequences an ecs node behind an ec2 node.
3. **Capacity.** A VM has finite memory; several tasks placed in one instance
   can exhaust it. Real ECS answers this with capacity providers. The parked
   app layer (`app-layer-parked`) had a memory-aware scheduler that is the
   nearest prior art — worth reading before inventing another.
4. **Failure meaning.** "The VM is not up" and "the task failed" must not
   collapse into one status. See the `ensure()` note below — odin has this
   bug elsewhere already.

### Reversibility

Dragging the ecs node back out must return it to Fargate and to the host
runtime. Every inference in this layer must be undone by the opposite gesture,
or the canvas stops being a language and becomes a trap.

---

## 2. IAM edges across the whole catalog

The drawn-edge → compiled-policy → enforced-at-the-gateway path is **real
today**; what is thin is the action vocabulary outside `s3:*`.

Extend to sqs / sns / dynamodb / lambda / ecs / secretsmanager / logs / ecr.
Neither the compiler nor the enforcement point changes — only the table that
maps (source kind, target kind) to a default action set, plus the per-edge
permission editing the UI already has.

Test shape that matters: a drawn edge must produce a grant that is **actually
enforced** (a real call succeeds, and the same call from an env without the
edge is denied). That is how the s3 path was proven; the rest should not be
held to a weaker standard.

---

## 3. Placement that infers intent from geometry, generally

Containment is the first and clearest case because odin already computes it:
`ui/src/lib/containment.ts::computeContainment` stamps a node's `vpc`/`subnet`
from which rectangle holds its centre, and `withContainment` applies it.

The generalisation is a per-kind table of *what being inside X means*, applied
on that same path. Adjacency and grouping come later and are weaker signals —
containment is unambiguous, "next to" is not.

Each inference must:
- be reversible by the opposite gesture
- preserve identity (see the invariant)
- be **visible** — the config panel should show what containment decided and
  why, not merely show a changed value

---

## 4. Then, separately: the chat/agent surface

NORTHSTAR's canvas↔Terraform translation agent. Explicitly an **addition** to
the canvas language, not a replacement for it.

---

## Related debt this layer will trip over

- **`fabric/sidecar.py::ensure()` collapses "the join failed" into "there is
  no mesh here"** — a broad `except` returns `None`, the same value as the
  no-mesh case. A real `AttributeError` surfaced as a decorative security
  group behind one WARNING line. Any placement feature that reports status
  through the same path inherits this. Honesty rule 2.
- **No ratchet for "call an `async def` without `await`."** Five bugs of that
  class were found on 2026-07-27, including the sidecar one. The naive scan is
  useless (87 and 255 hits in two attempts, dominated by `Path.exists` /
  `str.join` collisions); it needs receiver resolution to be worth having. A
  checker wrong two times in three trains people to ignore it, which is worse
  than the gap.
