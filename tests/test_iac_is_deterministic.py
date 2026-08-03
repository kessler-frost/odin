"""`src/odin/iac/` is the deterministic half of the translation layer, and the
directory name is a CLAIM. This test is what makes it a fact.

The two translators (`iac/hcl.py` canvas->TF, `iac/import_tf.py` TF->canvas)
lived under `agent/` for months, purely because NORTHSTAR directive 2 said "an
agent translates" and the directory was named for the intent before the
implementation turned out deterministic. Neither file has ever imported
`claude_agent_sdk`. But a reader who opened `agent/hcl.py` (the path until
2026-08-03; there is no such file now) reasonably concluded that a model writes
their infrastructure -- and per NORTHSTAR's 2026-07-30 amendment
("deterministic first; intelligence only where no function exists") the fact
that it does NOT is the most important property of the whole layer.

Moving the files fixed the reading. Only this test stops it regressing, and it
pins TWO separate things, because the directory boundary is worth nothing if
either leaks:

  1. **No module under `iac/` reaches a model.** `tests/agent/test_ai_switch.py`
     already whitelists the three SDK importers repo-wide, which covers this
     transitively today; stated here directly so the property survives that
     whitelist being edited for an unrelated reason.
  2. **No module under `iac/` imports from `odin.agent`.** This is the one that
     a plain import-rewrite could silently reintroduce: `iac` may be imported
     BY the model-calling code (`agent/translate.py` does), never the reverse.
     An edge the other way would put a model-reaching module in the import
     graph of every deterministic compile, and no runtime test would notice
     because nothing would call it.

Both are found by AST, not by substring: substring-matching a module name is
the defect `test_ai_switch.py` documents in its own history (a docstring
mention registering as a caller). An `ast.Import` node cannot be a comment.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_IAC = Path(__file__).resolve().parents[1] / "src" / "odin" / "iac"

# Spelled out in full rather than derived from the directory listing: a
# regression that DELETES a file must fail this test, not quietly shrink it
# (`.claude/CLAUDE.md` rule 5 -- a guard parametrized over the thing it guards
# loses the case along with the code).
DETERMINISTIC_MODULES = ["__init__.py", "hcl.py", "import_tf.py"]


def _imported_roots(path: Path) -> set[str]:
    """Every top-level package this module imports, by AST."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module)
    return roots


def test_the_deterministic_package_holds_exactly_these_modules():
    assert sorted(p.name for p in _IAC.glob("*.py")) == sorted(DETERMINISTIC_MODULES), (
        "src/odin/iac/ gained or lost a module. Anything added here is a claim that it "
        "is deterministic and model-free -- list it above once that is true of it."
    )


@pytest.mark.parametrize("module", sorted(DETERMINISTIC_MODULES))
def test_no_deterministic_module_can_reach_a_model(module):
    sdk = {root for root in _imported_roots(_IAC / module) if root.split(".")[0] == "claude_agent_sdk"}
    assert not sdk, (
        f"iac/{module} imports {sorted(sdk)}. The whole point of this directory is that a "
        "model cannot decide what infrastructure a user gets -- model-calling code belongs "
        "in odin/agent/."
    )


@pytest.mark.parametrize("module", sorted(DETERMINISTIC_MODULES))
def test_the_dependency_runs_one_way_only(module):
    """`agent` may import `iac`; `iac` must never import `agent`."""
    backwards = {root for root in _imported_roots(_IAC / module) if root.split(".")[:2] == ["odin", "agent"]}
    assert not backwards, (
        f"iac/{module} imports {sorted(backwards)} -- the dependency points the wrong way. "
        "odin.agent is where the model-calling code lives; importing it from here puts a "
        "model in the import graph of every deterministic compile."
    )
