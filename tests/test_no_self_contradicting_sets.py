"""A set literal that repeats a member is a MERGE ARTIFACT, not a style nit.

MEASURED, v0.8.18. The kms/ebs three-way merge kept BOTH sides of three
declarations, and in every case `frozenset`/`set` silently deduped so behaviour
stayed correct and no test noticed:

  reconcile/tf_status.py   TF_OWNED_KINDS listed "elasticache", "rds", "alb"
                           twice -- one line ending "kms", the next "ebs"
  ui/src/lib/catalog.ts    two contradictory `// Today: ...` placeholder lists,
                           one still naming `ebs`, the other adding `kms`
  docs/limits.md           two contradictory pair counts, 47/331 and 42/336

The third one is why this file exists. Both doc claims sat four lines apart and
the ratchet over them PASSED, because it asked `toContain` -- which the true
copy satisfies while the false copy is never looked at. "Does this appear
somewhere" is not the same question as "is this what the file says", and
answering the easy one is the single most repeated defect in this repo's
audits (see CLAUDE.md honesty rule 1 and the IAM record grep replaced this
same release).

Correct behaviour is exactly what makes this class dangerous: dedupe means the
only symptom is a declaration that disagrees with itself, which the next reader
resolves by guessing. This test removes the guess.

SCOPE, deliberately narrow. Only FLAT set and `frozenset({...})` literals of
string constants. Tuples and lists are excluded because odin uses
`(("engine", "engine"), ("db_name", "dbName"))` old->new mapping tables all
over `agent/import_tf.py`, where a repeated value is correct and intended --
flagging those would produce 25 false positives against 1 real finding, and a
guard that cries wolf gets deleted.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "odin"


def _flat_string_set_literals(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """Every `{...}` / `frozenset({...})` whose members are all string constants.

    `ast.walk` reaches a `frozenset({...})` TWICE -- once as the Call and once as
    the Set inside it -- so the wrapped literal is collected first and skipped on
    the second visit. Without that, every frozenset offender is reported twice,
    which is exactly what the first version of this scanner did (it printed
    `tf_status.py:87` on two consecutive lines). Caught by
    `test_the_scanner_actually_finds_a_planted_duplicate` below, which is the
    entire argument for writing a guard-the-guard rather than eyeballing output.
    """
    wrapped = {
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
    }
    found: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        inner: ast.AST | None = None
        if isinstance(node, ast.Set) and id(node) not in wrapped:
            inner = node
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "frozenset"
            and node.args
            and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
        ):
            inner = node.args[0]
        if inner is None:
            continue
        elts = inner.elts  # type: ignore[attr-defined]
        values = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if values and len(values) == len(elts):
            found.append((node.lineno, values))
    return found


def test_no_set_literal_in_src_repeats_a_member():
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        for lineno, values in _flat_string_set_literals(ast.parse(path.read_text())):
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                offenders.append(f"{path.relative_to(_SRC.parent.parent)}:{lineno} repeats {repeated}")
    assert offenders == [], (
        "a set literal lists the same member twice. The set dedupes, so behaviour is "
        "fine and nothing else will ever tell you -- but the declaration now disagrees "
        "with itself, which is how a merge that kept both sides looks:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scanner_actually_finds_a_planted_duplicate():
    """Guards the guard. A scanner that silently matches nothing passes forever.

    This is the vacuity failure `scripts/gate.sh` shipped and had to fix: a
    partition built from a pattern that matched zero lines was an exact cover of
    nothing, and passed trivially.
    """
    planted = ast.parse('X = frozenset({"a", "b", "a"})\nY = {"c", "c"}\n')
    found = _flat_string_set_literals(planted)
    assert [values for _, values in found] == [["a", "b", "a"], ["c", "c"]]


def test_the_scanner_does_not_flag_old_to_new_mapping_tables():
    """The false-positive class the scope note names, pinned so a later widening
    of this scanner has to confront it rather than drown the signal."""
    pairs = ast.parse('T = (("engine", "engine"), ("db_name", "dbName"))\n')
    assert _flat_string_set_literals(pairs) == []
