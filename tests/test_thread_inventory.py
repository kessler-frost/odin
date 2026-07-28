"""odin's own code starts ONE thread, and it is test-only.

The ninth ratchet, and the one that keeps the other eight honest. The
de-threading directive is "no `threading`, no `multiprocessing`, no
`to_thread`", with a single carve-out: if something is genuinely unavoidable,
leave the boundary VISIBLE and say why. This test IS that boundary, expressed
as code instead of prose, because prose about thread inventories has already
gone stale twice in this repo:

  * `.claude/CLAUDE.md` described the v0.7.6 inventory well after v0.7.7 landed
    (fixed once, drifted again).
  * it then listed `__main__.py`'s log relays, `compute/instances.py`'s boot
    semaphore and `fabric/nebula.py`'s locks as "still standing" when all three
    had already been converted -- measured 2026-07-27, when the real count was
    exactly the two entries below.

A prose inventory cannot fail a build. This can.

## Why `serve_in_thread` stays

`gateway/app.py::serve_in_thread` runs the gateway on a real thread for two
integration tests that must dial a REAL bound port
(`test_ecs_crash_observability_e2e.py`, `test_ecs_failed_update_keeps_serving_e2e.py`).
Both are SYNC tests doing blocking boto3 and docker work inline. The production
path is `serve_on_loop`, which serves on the CALLER's event loop -- so
converting these would mean the blocking calls stall the very loop serving the
gateway they are calling, i.e. a deadlock. Making them async without also making
boto3 async just moves the block. That is the "genuinely unavoidable" case, and
production never touches it.

## Why `JsonStore`'s locks stay, and when they may go

They exist to stop that thread interleaving a read with a write. Every critical
section in `JsonStore` is synchronous (no `async def`, no `await` anywhere in
the file), so on a single event loop the locks guard nothing -- which is exactly
why they are the one lock in odin that DELETES rather than becoming an
`asyncio.Lock`. What keeps them alive is `serve_in_thread`, their last real
contender.

So this test states the dependency: **if `serve_in_thread` is ever removed, the
`JsonStore` locks can and should come out in the same change.** Without that
written down as an assertion, the next person reads the docstring's "the lock
has nothing left to guard", deletes it, and introduces a real race in two tests
that no unit test would catch.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "odin"

# module -> why this thread/lock is allowed to exist. Every entry is a
# deliberate, documented boundary; anything not listed here fails.
ALLOWED: dict[str, str] = {
    "gateway/app.py": (
        "serve_in_thread/stop_in_thread — TEST-ONLY. Two sync integration tests need a real "
        "bound port; production uses serve_on_loop on the caller's loop."
    ),
    "gateway/stores.py": (
        "JsonStore's per-env locks — they guard against serve_in_thread above, the last "
        "off-loop caller. Remove them in the same change that removes serve_in_thread."
    ),
}

_USES_THREADING = re.compile(r"\bthreading\.[A-Za-z]")


def _threading_lines(path: Path) -> list[tuple[int, str]]:
    """Lines that really USE threading — not comments, not docstrings."""
    text = path.read_text()
    tree = ast.parse(text)
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if number in doc_lines or stripped.startswith("#"):
            continue
        if _USES_THREADING.search(line) or stripped in {"import threading", "import multiprocessing"}:
            found.append((number, stripped))
    return found


def _offenders() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        lines = _threading_lines(path)
        if lines:
            out[path.relative_to(SRC).as_posix()] = lines
    return out


def test_no_module_starts_a_thread_without_a_recorded_reason():
    found = _offenders()
    unexpected = {module: lines for module, lines in found.items() if module not in ALLOWED}
    assert not unexpected, (
        "threading appeared in a module with no recorded justification. odin is single-event-loop "
        "by directive: write async, and convert subprocess work rather than wrapping it.\n"
        + "\n".join(
            f"  {module}:\n" + "\n".join(f"    {n}: {text}" for n, text in lines)
            for module, lines in unexpected.items()
        )
    )


def test_the_recorded_reasons_have_not_gone_stale():
    """The direction that actually bit: prose surviving its own subject.

    An entry here for a module that no longer touches threading is a caveat
    describing a fix that already landed -- exactly what CLAIMED that
    `__main__.py`, `compute/instances.py` and `fabric/nebula.py` still held
    threads for a whole release after they stopped.
    """
    found = _offenders()
    stale = sorted(set(ALLOWED) - set(found))
    assert not stale, (
        "these modules no longer use threading, so delete their ALLOWED entry:\n"
        + "\n".join(f"  {module}" for module in stale)
    )


def test_the_store_locks_last_contender_still_exists():
    """The dependency, asserted rather than hoped for.

    `JsonStore`'s locks are justified ONLY by `serve_in_thread`. If that helper
    goes and this assertion starts failing, the locks are now guarding nothing
    on a single loop and should come out in the same change -- along with both
    ALLOWED entries above.
    """
    app = (SRC / "gateway" / "app.py").read_text()
    assert "def serve_in_thread(" in app, (
        "serve_in_thread is gone — JsonStore's per-env locks have no remaining off-loop "
        "contender. Delete them (every critical section there is synchronous, so nothing "
        "can preempt a read-modify-write on one event loop) and drop both ALLOWED entries."
    )


def test_to_thread_stays_at_zero():
    """`asyncio.to_thread` IS a thread pool, so it does not satisfy the rule --
    it hides it. Went 28 -> 0 in v0.7.7 and must stay there."""
    offenders = [
        f"{path.relative_to(SRC).as_posix()}:{n}"
        for path in sorted(SRC.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "to_thread(" in line and not line.strip().startswith("#") and "`" not in line
    ]
    assert not offenders, "to_thread is a thread pool wearing a costume:\n" + "\n".join(f"  {o}" for o in offenders)
