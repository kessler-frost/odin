"""Every `ODIN_*` knob odin reads, and the one place to see them all.

Before this module 28 variables were read directly in 19 files, each with its
own ad-hoc parse and no validation at all, so a non-numeric
`ODIN_DISPATCH_TICKS` did not fail at startup -- it failed whenever that code
path first ran, if it ever ran. `validate_all()` (called by
`server.create_app`) now fails the process at startup instead, naming the
variable and what it wanted.

READ FRESH, ALWAYS -- and this is the part not to "tidy".
`settings` is ONE object, but every section on it is a PROPERTY that builds its
`BaseSettings` from `os.environ` at the moment you touch it. That is
deliberate and load-bearing: 118 `monkeypatch.setenv`/`delenv` calls across 23
of these variables live in `tests/`, and every one of them runs inside a test
body, long after import. A module-level `settings = ...Settings()` evaluated
once at import would make all 118 silently ineffective -- the test sets the
variable, the code reads a value captured at import, and the test passes for
the wrong reason behind a green suite. Every reader this module replaced
already promised "read fresh (not cached at import) so a test can monkeypatch
it"; the singleton keeps that promise rather than quietly dropping it.

The cost is measured, not assumed: constructing a section is ~67us on this
machine, and the hottest reader (`reconcile/drift.py` and
`reconcile/dispatch.py`, once per reconciler tick, one second apart) pays it
once per tick.

STRICT WHERE A TYPO IS A NUMBER, LENIENT WHERE A TYPO MUST FAIL SAFE.
Numbers are typed and bounded, so a garbage value is a startup error. The
on/off flags (`ODIN_AI`, `ODIN_DEBUG_AGENT`, `ODIN_REAP_EC2_VMS`,
`ODIN_TRANSLATE_REFINE`, `ODIN_BACKING_MESH`) stay `str` on purpose: each one's
documented contract is that an unrecognised value does NOT do the dangerous
thing. `ODIN_REAP_EC2_VMS`'s own comment says it -- "a safety net you disabled
by mistyping the value is not a safety net" -- and a mistyped `ODIN_AI` must
stop model calls rather than enable them (`tests/agent/test_ai_switch.py`
pins that). A pydantic `bool` would reject both at construction and take the
fail-safe away, so the interpretation stays where its reasoning lives, beside
the code that acts on it.

NOT HERE, deliberately:
  * `ODIN_URL` -- typer reads it itself (`envvar=` on the `--url` option in
    `cli/http.py` and `__main__.py`), so it never reaches `os.environ` in
    odin's own code and there is nothing here to route it through.
  * `ODIN_KEEP_IT_ARTIFACTS` -- a test-harness switch, read only in `tests/`.
"""
from __future__ import annotations

import os

from pydantic import AliasChoices, AliasGenerator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _odin_env(field_name: str) -> AliasChoices:
    """`ODIN_` + the field name, upper-cased -- what `env_prefix` would do on
    its own, made an explicit ALIAS so that pydantic's error names the
    ENVIRONMENT VARIABLE rather than the field.

    That is the whole reason this exists rather than leaning on `env_prefix`:
    the point of validating at startup is telling the user which knob to fix,
    and "1 validation error ... dispatch_ticks" names something they cannot
    grep for in their own shell profile. With the alias it reads
    `ODIN_DISPATCH_TICKS`. Fields whose natural name would generate the WRONG
    variable (`port` -> `ODIN_PORT`) set their own alias, which pydantic keeps
    (an explicitly-set alias has `alias_priority=2` and a generator does not
    override it).
    """
    return AliasChoices(f"ODIN_{field_name}".upper())


# `env_ignore_empty` is not cosmetic. Every reader replaced here treated an
# EMPTY value as unset -- `os.environ.get(name) or DEFAULT`, or
# `admission._set_budget_env`'s truthiness test over its precedence tuple.
# Without it, `ODIN_MIN_DISK_GIB=` (exported empty by a shell script, which is
# how it happens) would become a ValidationError where it used to mean "not
# set". That is a behaviour change nobody asked for and it would land as a
# startup crash.
_CONFIG = SettingsConfigDict(
    env_prefix="ODIN_", env_ignore_empty=True, extra="ignore",
    alias_generator=AliasGenerator(validation_alias=_odin_env), populate_by_name=True,
)

# The one well-known gateway port. It is a domain constant as much as a
# default -- `aws/backings.py` takes it as a function default for callers that
# never look at the environment -- so `odin.gateway` re-exports it from here
# rather than holding a second copy that could drift.
DEFAULT_GATEWAY_PORT = 4266


class GatewaySettings(BaseSettings):
    """odin's checking reverse proxy, and the post-apply readiness budgets its
    per-service controllers verify with."""

    model_config = _CONFIG

    # `ge=0`, NOT `ge=1`: **0 means "bind an ephemeral port"** and is the value
    # the entire test suite runs on (`tests/conftest.py` setdefaults it), and
    # the isolation .claude/CLAUDE.md prescribes for parallel agents -- an
    # ephemeral port is STRONGER isolation than any fixed number, because
    # nothing can collide with it. A `ge=1` bound looks tighter and would have
    # made every test that boots the app fail at startup.
    port: int = Field(DEFAULT_GATEWAY_PORT, ge=0, le=65535,
                      validation_alias=AliasChoices("ODIN_GATEWAY_PORT"))

    # Anything other than `0`/`false`/`no`/`off` -- including unset and a typo
    # -- leaves the startup EC2-VM reaper ON. A safety net you disabled by
    # mistyping the value is not a safety net. See
    # `gateway/models/ec2compute.py::_reaper_enabled` for what you give up by
    # setting it.
    reap_ec2_vms: str = Field("1", validation_alias=AliasChoices("ODIN_REAP_EC2_VMS"))

    # Deliberately the SAME 60s as `iac/hcl.py`'s `timeouts.update` (the
    # tf-side twin of the ECS steady-state check) -- one number for "how long
    # may a task legitimately take to come up", not two.
    ecs_steady_timeout: float = Field(60.0, gt=0)

    # None = derive at the call site. Both of these default to a base timeout
    # that lives in the controller's own module (`lambdactl.READY_TIMEOUT` and
    # `rdsctl._CREATE_TIMEOUT`) plus that module's margin, and the margin is
    # not slack: the verification has to OUTLAST the work it verifies, or it
    # hard-stops while the deploy is still probing and reports "still
    # deploying" instead of the real reason. Copying those numbers here would
    # be a second source of truth for a bound whose reasoning lives there.
    lambda_active_timeout: float | None = Field(None, gt=0)
    rds_available_timeout: float | None = Field(None, gt=0)


class ReconcileSettings(BaseSettings):
    """The reconciler loop's cadences, counted in ticks (~1s each at the
    production poll)."""

    model_config = _CONFIG

    # Every tick, and NOTHING may turn it down: for a dispatcher, being late is
    # not a late report, it is a trigger the user calls broken.
    # `tests/reconcile/test_dispatch_cadence.py` is a repo-wide grep ratchet
    # against anyone shortening -- or lengthening -- this in a test.
    dispatch_ticks: int = Field(1, ge=1)

    # Ten, because a drift sweep is a REPORT rather than an action -- the
    # deliberate difference from the dispatcher above.
    drift_sweep_ticks: int = Field(10, ge=1)


class SimulateSettings(BaseSettings):
    """`tofu init`/`apply`/`destroy`, as run by `simulate/runner.py`."""

    model_config = _CONFIG

    # Release finding #3: a wedged apply has been observed running for hours
    # with nothing to stop it. This bounds `init` and `apply` -- each gets its
    # OWN budget, not one shared across the whole call.
    tofu_timeout: float = Field(600.0, gt=0)

    # Field test 2, finding B6: `odin destroy` on a RESTORED env was killed by
    # hand at 8m26s of `tofu destroy` with no progress and no timeout. Nothing
    # was broken about the existing bound -- 8m26s is 506s, comfortably under
    # the 600s above, so the timeout simply had not fired yet; and because
    # `_init_then` gives `init` its OWN full budget, the worst case for one
    # `/destroy` was 20 minutes. Neither is a bound anyone waits out. A destroy
    # against local substrates is fast when it works at all (measured: a
    # 12-resource env in 63s, three EC2 VMs in 62s, the slowest single
    # operation an `aws_db_instance` destroy at 1m1s), so 300s is generous for
    # a working teardown and a fifth of the old worst case for a wedged one.
    # It is a DEADLINE ACROSS THE WHOLE CALL (init included), not per phase.
    tofu_destroy_timeout: float = Field(300.0, gt=0)

    # Owner directive B3: tofu's own default (`-parallelism=10`) means a big
    # canvas fans out up to 10 heavy resource operations at once -- EC2 boots,
    # ECS convergence waits -- on top of whatever else Apply is already doing.
    tofu_parallelism: int = Field(4, ge=1)


class ComputeSettings(BaseSettings):
    """The EC2-as-real-Lima-VM substrate, and the admission check that decides
    whether a canvas fits on this Mac at all."""

    model_config = _CONFIG

    # 300s is generous for a healthy boot -- the nebula mesh e2e boots two VMs
    # and finishes ENTIRELY in 74.6s on an idle machine. It is not generous on
    # a busy one. Measured at the tail of a 57-minute integration suite (dozens
    # of VMs and containers created and destroyed before it), a VM reached
    # `[VZ] - vm state change: running` in one second and then never signalled
    # a running guest: `limactl start --timeout=300s` gave up at exactly 300s,
    # the instance went `terminated`, and the whole apply failed with it. So
    # the ceiling is real work-in-progress, not a bug -- but a hard constant
    # left a user on a loaded Mac with no recourse at all, which is the part
    # worth fixing.
    #
    # THE DEFAULT DELIBERATELY DOES NOT MOVE. Raising it for everyone would
    # make a genuinely hung boot take longer to report, and a slow boot and a
    # dead one look identical until the clock runs out.
    boot_timeout: float = Field(300.0, gt=0)

    # Owner directive B2: draw N EC2 nodes and RunInstances spawns N concurrent
    # `InstanceVm.boot` calls -- with nothing bounding them, N concurrent
    # `limactl create`/`start` calls stampede the Mac at once.
    max_concurrent_vm_boots: int = Field(3, ge=1)

    # The free-disk floor an apply needs. Matches `cli/doctor.py`'s own
    # `MIN_DISK_GIB`, so `odin doctor` and the live admission check agree on
    # what "enough disk" means -- they disagreed at every other value once.
    min_disk_gib: float = Field(10.0, gt=0)

    # The CONTAINER pool's budget, in MiB, absolute (None = take
    # `_DEFAULT_BUDGET_RATIO` of the container runtime's total instead).
    #
    # TWO NAMES, ON PURPOSE. `ODIN_MEMORY_BUDGET_MIB` is the original, and it
    # is in ROADMAP, in `odin doctor`'s output, and in whatever shells and CI
    # jobs already export it -- so it still works, forever. It reads as "odin's
    # memory budget" beside `ODIN_VM_MEMORY_BUDGET_MIB` though, which is
    # exactly the misreading `reconcile/admission.py`'s headline warns about:
    # the two pools are DISJOINT, so a user rejected on the EC2/VM pool who
    # raises the unqualified one changes nothing and gets the same rejection.
    # Hence the qualified name, listed FIRST because someone who set both meant
    # the specific one. The order here IS the precedence, and
    # `admission._budget_origin` reads the same order to name the variable that
    # actually set the number -- a rejection can never point at one the user
    # has not set.
    container_memory_budget_mib: float | None = Field(
        None, gt=0,
        validation_alias=AliasChoices(
            "ODIN_CONTAINER_MEMORY_BUDGET_MIB", "ODIN_MEMORY_BUDGET_MIB",
        ),
    )

    # The HOST/VM pool's budget (`ec2` nodes = real Lima VMs), in MiB, absolute
    # (None = the same ratio of REAL host memory). Disjoint from the pool
    # above -- see its comment.
    vm_memory_budget_mib: float | None = Field(None, gt=0)


class MeshSettings(BaseSettings):
    """The self-hosted Nebula mesh: the lighthouse, and the sidecars that put
    backing containers on the overlay."""

    model_config = _CONFIG

    # An explicit pin, for reproducing a collision and for a user who needs a
    # specific port open. Honoured verbatim: no probing, no reallocation.
    # None = allocate from `fabric/nebula.py`'s `LIGHTHOUSE_PORTS` range.
    lighthouse_port: int | None = Field(None, ge=1, le=65535)

    # Mesh membership for backings is OFF unless the env actually has a Nebula
    # network anyway (`ensure_network` ran, i.e. the canvas has a VPC), so an
    # env of bare s3/sqs nodes pays nothing. `0` disables it outright; every
    # other value, including a typo, leaves it on.
    backing_mesh: str = Field("1", validation_alias=AliasChoices("ODIN_BACKING_MESH"))

    # The address a sidecar dials to reach the HOST lighthouse. It cannot be
    # discovered from inside the sidecar itself: `--add-host` is incompatible
    # with `--network container:`, which is how the sidecar gets into the
    # backing's namespace. None = `fabric/sidecar.py::HOST_GATEWAY_IP`, Lima's
    # user-mode gateway (verified live: `getent hosts host.docker.internal` ->
    # 192.168.5.2, and the host lighthouse listens on 0.0.0.0:4242). Override
    # for a host whose user-mode gateway differs.
    underlay: str | None = Field(None, validation_alias=AliasChoices("ODIN_MESH_UNDERLAY"))

    # How often a mesh verdict is re-taken: a PASSING one every 30s, a FAILING
    # one every 5s so a recovery shows up promptly.
    sweep_seconds: float = Field(30.0, gt=0,
                                 validation_alias=AliasChoices("ODIN_MESH_SWEEP_SECONDS"))
    recheck_seconds: float = Field(5.0, gt=0,
                                   validation_alias=AliasChoices("ODIN_MESH_RECHECK_SECONDS"))


class AiSettings(BaseSettings):
    """Every model call odin can make, and the budgets they run under.

    All five flags/timeouts here are subordinate to `ODIN_AI`: it is the one
    switch a CI job or an `ODIN_AI=0 odin apply` relies on, so a user who set
    it does not also have to know the per-feature flags exist.
    """

    model_config = _CONFIG

    # THREE-way, not a bool, and the third state is the point: set-to-off,
    # set-to-on, or UNSET -- in which case the UI switch (`agent/ai.py`'s
    # persisted `runtime_enabled`) decides and its default is off. An
    # unrecognised value is a FOURTH outcome: no model calls at all, plus a
    # warning naming the value. `agent/ai.py::off_reason` owns that reading,
    # because the sentence it returns is what every degradation path quotes.
    enabled: str = Field("", validation_alias=AliasChoices("ODIN_AI"))

    # Opt IN (`1`/`true`/`yes`/`on`), OFF by default: the canvas -> Terraform
    # translation (`iac/hcl.py`) is fully deterministic, and this pass is a
    # best-effort ADD-ON that can only attach comments/tags/unset arguments
    # (`validate_refinement` rejects anything else), never change the
    # architecture -- so leaving it off costs polish, not correctness.
    translate_refine: str = ""

    # The refine pass's budget. It no longer sits on any request's critical
    # path (`translate()` returns the deterministic skeleton immediately and
    # refines on a BACKGROUND task), so this only bounds how long that task
    # runs before giving up.
    translate_timeout: float = Field(45.0, gt=0)

    # The failure-explanation agent, ON by default -- the deliberate difference
    # from the refine pass above. That pass is optional decoration over an
    # already-correct translation, so it opts IN; this one is the whole
    # feature, and it cannot corrupt anything (read-only, prose out), so it
    # opts OUT via `0`/`false`/`no`/`off`.
    debug_agent: str = "1"

    # 90s because the explanation IS on the request's critical path (a human is
    # waiting) and it has to cover a COLD start: measured on a real M8 run, the
    # first nested-CLI launch took ~65s wall-clock end to end and a warm one
    # ~49s, so a 60s budget turned a perfectly good diagnosis into "agent
    # unavailable" purely on startup cost.
    debug_timeout: float = Field(90.0, gt=0)

    # The canvas-chat turn budget.
    chat_timeout: float = Field(60.0, gt=0)


def env_names(section: type[BaseSettings], field: str) -> tuple[str, ...]:
    """Every environment variable `field` answers to, in PRECEDENCE order.

    One place derives the name, so a message that quotes a variable and the
    reader that honours it can never disagree -- naming
    `ODIN_CONTAINER_MEMORY_BUDGET_MIB` in a rejection whose number came from
    the legacy `ODIN_MEMORY_BUDGET_MIB` would point the user at a variable they
    have not set.
    """
    return tuple(str(choice) for choice in section.model_fields[field].validation_alias.choices)


def env_name(section: type[BaseSettings], field: str) -> str:
    """The variable a message should tell the user to set: the primary name."""
    return env_names(section, field)[0]


def set_env_name(section: type[BaseSettings], field: str) -> str | None:
    """The FIRST of a field's names actually set in the environment, in
    precedence order, or None when the default is what is in force.

    The one legitimate `os.environ` read left in odin: it reports WHICH
    variable spoke, not what it said. `reconcile/admission.py` needs it so a
    rejection can name the number's real origin.
    """
    return next((name for name in env_names(section, field) if os.environ.get(name)), None)


class Settings:
    """THE settings object -- `from odin.settings import settings`.

    Each property constructs its section from the CURRENT environment. See the
    module docstring for why that is a requirement and not an oversight.
    """

    @property
    def gateway(self) -> GatewaySettings:
        return GatewaySettings()

    @property
    def reconcile(self) -> ReconcileSettings:
        return ReconcileSettings()

    @property
    def simulate(self) -> SimulateSettings:
        return SimulateSettings()

    @property
    def compute(self) -> ComputeSettings:
        return ComputeSettings()

    @property
    def mesh(self) -> MeshSettings:
        return MeshSettings()

    @property
    def ai(self) -> AiSettings:
        return AiSettings()

    def sections(self) -> tuple[BaseSettings, ...]:
        """Every section, constructed."""
        return (self.gateway, self.reconcile, self.simulate, self.compute, self.mesh, self.ai)

    def validate_all(self) -> None:
        """Fail HERE -- at startup, with pydantic naming the variable and what
        it wanted -- rather than whenever the offending knob's code path first
        runs, which for most of these is "maybe never".

        Constructing a section IS validating it, so this is `sections()` and
        nothing else.
        """
        self.sections()


settings = Settings()
