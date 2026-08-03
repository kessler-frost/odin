"""A test fake whose signature has drifted from the real callee reports a
plausible-looking failure for the WRONG CAUSE. This ratchet catches that.

THE INCIDENT, v0.8.19. `TaskRuntime.run` grew a `volumes` parameter (EFS
mounts). Five fakes across four files did not. The one that mattered was
`tests/api/test_apply_full.py::_DeadTaskRuntime`, which exists to prove a
bad-image apply reports its real reason -- and it went GREEN on its outer
status (`applied_services_unhealthy`) while failing only on the `reason`
string, which read

    TypeError: _DeadTaskRuntime.run() got an unexpected keyword argument 'volumes'

instead of the

    RuntimeError: pull access denied for nginx:this-tag-does-not-exist-9z9z

it exists to assert. The injection fired -- for a cause the test did not mean.
Had that assertion been on the status alone it would have PASSED and proven
nothing. That is CLAUDE.md honesty rule 4's "a fault injection that silently
does nothing looks exactly like a bug", one level up: the fault fired, and
silently re-routed an unrelated test's verdict.

So this fixes the SHAPE, not the five instances (honesty rule 2): whenever a
real substrate seam grows a parameter, every stand-in for it must grow the same
one, and this fails loudly at that moment instead of corrupting some other
test's answer months later.

HOW IT FINDS FAKES, stated plainly because an unstated discovery rule is the
next thing to go stale (honesty rule 5 -- a guard that iterates a registry
matching ZERO things passes forever):

  * It parses every `tests/**/*.py` with `ast` and looks for a CLASS containing
    an `async def` whose NAME matches an owned callee's (`run`, `ensure`). It
    is SHAPE-based, not name-based, so a fake called `_Dead...`, `Fake...` or
    anything else is found equally.
  * A method is attributed to a specific real callee only when it carries that
    callee's DISCRIMINATOR parameters (below). `LoadBalancerProxy.ensure` and
    `RedisCache.ensure` are different callees that happen to share a method
    name, and they are correctly excluded rather than checked against the wrong
    signature.
  * KNOWN BLIND SPOTS, all three deliberate and each pinned by its own test
    below rather than merely asserted here: a fake that absorbs everything
    through `**kwargs` is skipped (it CANNOT drift, which is why several in
    this repo are written that way); a fake defined outside `tests/` or built
    dynamically at runtime is invisible to a source scan; and a fake that drops
    one of its callee's DISCRIMINATOR parameters stops being attributable to
    that callee and is skipped too. The third is acceptable because the drift
    this exists for runs the other way -- the REAL seam grows a parameter and
    the fakes do not, which is fully covered. None of the three is claimed to
    be covered.

Scoped to callees odin OWNS. Asserting against third-party signatures would
make this noise rather than signal the first time a dependency moved.

NOT PARAMETRIZED over the discovered fakes, deliberately, and that is honesty
rule 5's second lesson: a test parametrized over the thing it guards loses a
CASE when discovery regresses, and a run whose test count silently drops reads
exactly like success. So the whole sweep is one test that asserts its own
discovery was non-empty, plus `test_the_checker_catches_a_planted_bad_fake`,
which feeds the checker a fake it is guaranteed to hate.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import NamedTuple

from odin.compute.functions import FunctionRuntime
from odin.compute.tasks import TaskRuntime

TESTS = Path(__file__).resolve().parent

# The real callees this ratchet owns, each with the parameter names that
# identify a stand-in for it. The discriminators are spelled out rather than
# derived from the signature, on purpose: they must keep naming the SAME seam
# even as it grows parameters, and a discriminator computed from the current
# signature would drift along with it -- the exact circularity this file exists
# to prevent one level up.
OWNED = {
    "TaskRuntime.run": (TaskRuntime.run, ("env", "task_id", "container_def")),
    "FunctionRuntime.ensure": (FunctionRuntime.ensure, ("runtime", "handler", "env_vars", "code_dir")),
}


class Finding(NamedTuple):
    callee: str
    where: str
    missing: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"{self.where} stands in for {self.callee}, and the way odin really calls it -- "
            f"{'; '.join(self.missing)} -- so that call raises TypeError, and the test around it "
            f"reports THAT instead of what it means to assert"
        )


def _fake_signature(node: ast.AsyncFunctionDef) -> inspect.Signature:
    """The fake's own signature, rebuilt from source so no test module has to
    be imported to check one."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults: dict[str, object] = dict(
        zip([a.arg for a in positional[len(positional) - len(args.defaults):]], args.defaults, strict=True)
    )
    defaults.update({
        a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None
    })
    empty = inspect.Parameter.empty
    parameters = [
        inspect.Parameter(a.arg, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=defaults.get(a.arg, empty))
        for a in positional if a.arg != "self"
    ]
    if args.vararg:
        parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    parameters += [
        inspect.Parameter(a.arg, inspect.Parameter.KEYWORD_ONLY, default=defaults.get(a.arg, empty))
        for a in args.kwonlyargs
    ]
    if args.kwarg:
        parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def _shortfall(real, node: ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Would a PRODUCTION-SHAPED call to this fake raise TypeError? Asked by
    actually attempting the bind, which is the only formulation that is exactly
    right.

    Both production call sites pass the REQUIRED parameters positionally and
    the DEFAULTED ones by keyword:

        runtime.run(env, task_id, container_def, extra_env=..., cpu=..., memory=..., volumes=...)
        substrate.ensure(env, name, runtime, handler, container_env, code_dir,
                         memory_mib=..., volumes=...)

    so that is the call this reconstructs from the REAL signature and binds
    against the FAKE's. Two cheaper rules were tried first and both were wrong,
    which is why this one is worth its length:

      * exact NAME equality flagged `FakeFunctionRuntime.ensure` for calling its
        second parameter `name` where the real one says `function_name` -- two
        findings, both pure noise, on fakes that work perfectly, because that
        parameter is passed positionally and its name can never bite;
      * a parameter COUNT let a fake through that had enough parameters in total
        but too few before its defaults, so the sixth positional argument landed
        on `memory_mib` and the `memory_mib=` keyword then collided with it.

    `bind` gets both right for free, and it needs no edit when either seam grows
    a parameter -- the next `volumes` fails here on the day it is added."""
    parameters = inspect.signature(real).parameters
    required = [n for n, p in parameters.items() if n != "self" and p.default is inspect.Parameter.empty]
    optional = [n for n, p in parameters.items() if n != "self" and p.default is not inspect.Parameter.empty]
    try:
        _fake_signature(node).bind(*required, **dict.fromkeys(optional, None))
    except TypeError as exc:
        return (str(exc),)
    return ()


def _params(node: ast.AsyncFunctionDef) -> tuple[list[str], bool]:
    """This method's parameter names (minus `self`), and whether it absorbs
    anything extra through `**kwargs`."""
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs) if a.arg != "self"]
    return names, args.kwarg is not None


def check_source(text: str, where: str) -> tuple[list[Finding], list[str]]:
    """`(findings, the fakes that were actually examined)` for one module's
    SOURCE.

    Text-in rather than path-in so `test_the_checker_catches_a_planted_bad_fake`
    can hand it a fake that exists nowhere on disk -- a planted class would
    otherwise be swept up by the real scan below and turn the guard-the-guard
    into a self-fulfilling failure.

    It returns BOTH halves from ONE walk, and that is not a tidy-up. The first
    cut had `_scan` re-implement the matching to build `examined` separately,
    and mutation testing killed it: breaking the attribution inside this
    function left the OTHER copy still matching, so `examined` stayed non-empty
    and the empty-sweep assertion -- the guard-the-guard -- never fired. TWO
    mutants survived on exactly that (`if method.name != callee.__name__` ->
    `if True`, and widening the `**kwargs` skip to everything). Two
    implementations of one rule cannot check each other; one can."""
    findings: list[Finding] = []
    examined: list[str] = []
    for klass in (n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ClassDef)):
        for method in (m for m in klass.body if isinstance(m, ast.AsyncFunctionDef)):
            for label, (callee, discriminators) in OWNED.items():
                if method.name != callee.__name__:
                    continue
                names, absorbs_kwargs = _params(method)
                if not set(discriminators) <= set(names):
                    continue  # a different callee that happens to share a method name
                where_exactly = f"{where}::{klass.name}.{method.name}"
                examined.append(where_exactly)
                if absorbs_kwargs:
                    continue  # cannot drift -- see the module docstring's blind spots
                missing = _shortfall(callee, method)
                if missing:
                    findings.append(Finding(label, where_exactly, missing))
    return findings, examined


def _scan() -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    examined: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        found, seen = check_source(path.read_text(), str(path.relative_to(TESTS.parent)))
        findings += found
        examined += seen
    return findings, examined


def test_every_fake_substrate_accepts_what_the_real_one_declares():
    findings, examined = _scan()

    # GUARD THE GUARD, half one. `scripts/gate.sh` shipped a partition built
    # from a pattern that matched zero lines -- an exact cover of nothing, green
    # forever. An empty sweep here means the discovery broke, not that the tree
    # is clean, and the two must never read alike.
    assert examined, (
        "this ratchet examined ZERO fakes, which means its discovery is broken -- not that every "
        "fake is correct. See the module docstring for how it finds them."
    )
    assert len(examined) >= 2, f"only {len(examined)} fake(s) found; the repo has several: {examined}"
    assert not findings, "\n".join(str(f) for f in findings)


def test_the_checker_catches_a_planted_bad_fake():
    """GUARD THE GUARD, half two: a checker that can never fail proves nothing.

    The bad fake lives in a STRING, never as a real class, so the sweep above
    cannot see it -- otherwise planting it would break the very test it is
    meant to validate."""
    # THE INCIDENT'S OWN SIGNATURE, verbatim: `TaskRuntime.run` exactly as it
    # read before EFS added `volumes`. So this is not a synthetic bad case --
    # it is the one that really shipped and really re-routed another test's
    # verdict.
    planted = (
        "class _PlantedStaleFake:\n"
        "    async def run(self, env, task_id, container_def, extra_env=None, cpu=None, memory=None):\n"
        "        raise RuntimeError('boom')\n"
    )
    findings, _examined = check_source(planted, "planted.py")

    assert len(findings) == 1, f"the checker did not flag a fake missing real parameters: {findings}"
    (finding,) = findings
    assert finding.callee == "TaskRuntime.run"
    # It names WHAT is missing, because "something drifted" is not actionable.
    assert "volumes" in finding.missing[0]
    assert "volumes" in str(finding) and "_PlantedStaleFake" in str(finding)


def test_a_kwargs_absorbing_fake_is_skipped_and_that_is_stated():
    """The blind spot the docstring names, asserted rather than assumed. A
    `**kwargs` fake genuinely cannot drift, so flagging it would be noise -- but
    a reader has to be able to check that this is a decision and not an
    accident."""
    absorbing = (
        "class _AbsorbingFake:\n"
        "    async def run(self, env, task_id, container_def, **kwargs):\n"
        "        raise RuntimeError('boom')\n"
    )
    findings, examined = check_source(absorbing, "planted.py")
    assert findings == []
    # ...and it WAS examined, so "skipped deliberately" and "never seen"
    # stay distinguishable in the sweep's own non-empty assertion.
    assert examined == ["planted.py::_AbsorbingFake.run"]


def test_a_fake_naming_a_required_parameter_differently_is_not_flagged():
    """The false positive the first cut of this ratchet produced, pinned so it
    cannot come back.

    `FakeFunctionRuntime.ensure` calls its second parameter `name` where the
    real one says `function_name`, and production passes that positionally --
    so it can never fail. Two such findings were pure noise on fakes that work,
    and noise is how a ratchet gets deleted."""
    renamed = (
        "class _RenamedFake:\n"
        "    async def ensure(self, env, name, runtime, handler, env_vars, code_dir,\n"
        "                     memory_mib=None, volumes=None):\n"
        "        return 1\n"
    )
    findings, examined = check_source(renamed, "planted.py")
    assert findings == []
    assert examined == ["planted.py::_RenamedFake.ensure"], (
        "it must be EXAMINED and found fine -- passing by not being looked at proves nothing"
    )


def test_a_fake_with_too_few_positional_slots_is_flagged():
    """The other half of the arity rule: renaming is fine, DROPPING is not.

    This fake keeps every DISCRIMINATOR (`runtime`, `handler`, `env_vars`,
    `code_dir`) and drops one required slot, which is the only shape of
    dropped-parameter the discriminator can still recognise -- see the module
    docstring's third blind spot."""
    short = (
        "class _ShortFake:\n"
        "    async def ensure(self, env, runtime, handler, env_vars, code_dir,\n"
        "                     memory_mib=None, volumes=None):\n"
        "        return 1\n"
    )
    (finding,), _examined = check_source(short, "planted.py")
    # The real bind's own words: the sixth positional argument lands on
    # `memory_mib`, and the `memory_mib=` keyword then collides with it.
    assert "memory_mib" in finding.missing[0]


def test_a_fake_that_drops_a_discriminator_is_invisible_and_that_is_stated():
    """The blind spot, asserted rather than left implicit.

    Attribution is BY discriminator, so a fake that drops one of those
    parameters stops being recognisable as a stand-in for that seam at all and
    is silently skipped. That is a real limit, and it is acceptable because the
    drift this ratchet exists for runs the other way -- the REAL seam grows a
    parameter and the fakes do not, which is fully covered. A reader has to be
    able to check that this is a decision, not an accident."""
    dropped = (
        "class _DroppedDiscriminatorFake:\n"
        "    async def run(self, env, task_id, extra_env=None, volumes=None):\n"
        "        return None\n"
    )
    assert check_source(dropped, "planted.py") == ([], [])


def test_a_different_callee_sharing_a_method_name_is_not_checked():
    """`LoadBalancerProxy.ensure` and `RedisCache.ensure` are real fakes in this
    repo with the same method name and a completely different signature.
    Attributing them to `FunctionRuntime.ensure` would report four phantom
    missing parameters each -- and a ratchet that cries wolf gets deleted."""
    other = (
        "class _ProxyFake:\n"
        "    async def ensure(self, root, env, lb_name, listeners):\n"
        "        return {}\n"
    )
    assert check_source(other, "planted.py") == ([], [])
