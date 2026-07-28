"""`odin canvas` — read/write the saved canvas.

The canvas is PER-ENVIRONMENT (`.odin/<env>/canvas.json`), so both commands
take `--env` and default to `default`, exactly like `odin apply`/`destroy`.

This file used to say the opposite -- "no `env` parameter on purpose ... no
fake `--env` here" -- which was true of the one global canvas it was written
for and became a lie the moment the canvas moved. Left as a note because a
caveat outliving its subject is the doc failure this repo keeps auditing for.

The canvas JSON shape is documented in the README ("The canvas JSON schema").
The one field a hand-authored canvas keeps forgetting is `position`, because
translate/apply don't need it — only the UI does, and a node without one used
to blank the whole canvas. `canvas set` fills those in here, out loud, so a
canvas written by an agent or a `jq` one-liner renders the moment it lands.
"""
from __future__ import annotations

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat
from odin.spec.translate import canvas_problems

canvas_app = typer.Typer(
    help="Read/write the saved canvas (one global canvas.json — no env scoping).",
    no_args_is_help=True,
)
app.add_typer(canvas_app, name="canvas")

# The same layout the UI applies to an unpositioned node (Canvas.tsx `onGrid`),
# on odin's 20px grid: five per row, left to right, top to bottom.
_COLUMNS = 5
_ORIGIN, _COL_STEP, _ROW_STEP = 80, 260, 200


def _positioned(node: dict) -> bool:
    position = node.get("position")
    return isinstance(position, dict) and all(
        isinstance(position.get(axis), int | float) for axis in ("x", "y")
    )


def place_unpositioned(graph: dict) -> list[str]:
    """Give every node lacking a usable `position` one on the grid, in canvas
    order. Returns the ids placed, so the caller can say what it did."""
    missing = [n for n in graph.get("nodes") or [] if isinstance(n, dict) and not _positioned(n)]
    for index, node in enumerate(missing):
        node["position"] = {
            "x": _ORIGIN + (index % _COLUMNS) * _COL_STEP,
            "y": _ORIGIN + (index // _COLUMNS) * _ROW_STEP,
        }
    return [str(node.get("id", "?")) for node in missing]


def _render_canvas(body: dict) -> None:
    typer.echo(f"canvas: {len(body['nodes'])} nodes, {len(body['edges'])} edges")


@canvas_app.command("get")
def canvas_get(
    env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.JSON_OUTPUT,
) -> None:
    """Print this env's saved canvas ({"nodes": [...], "edges": [...]}) as JSON."""
    body = http.body_or_fail(http.request("GET", url, "/canvas", params={"env": env}))
    http.emit(body, output, _render_canvas)


@canvas_app.command("set")
def canvas_set(
    file: str = typer.Argument(..., help="Canvas JSON file, or - to read stdin."),
    env: str = http.ENV,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Save a canvas JSON ({"nodes": [...], "edges": [...]}) as THIS ENV's canvas."""
    graph = http.parse_json_arg(http.read_text_arg(file), file)
    # Field test 4, P4-5: this used to store anything that was valid JSON, so a
    # canvas that could never be applied sat on disk until the next translate
    # or apply tripped over it. The check is the SERVER's own
    # (`spec/translate.py::canvas_problems`, what POST /canvas enforces) run one
    # step earlier, so the failure names the file the user just wrote rather
    # than an HTTP status. It is deliberately narrow -- see that function: a
    # node whose KIND odin can't build is NOT a problem (it applies, and is
    # reported as skipped), and a missing `position` is repaired below, not
    # refused.
    problems = canvas_problems(graph)
    if problems:
        raise http.fail("\n".join([f"{file} is not a usable canvas:", *(f"  {p}" for p in problems)]))
    placed = place_unpositioned(graph)
    if placed:
        typer.echo(
            f"no \"position\" on {len(placed)} node(s) — placed on the grid: {', '.join(placed)}",
            err=True,
        )
    body = http.body_or_fail(http.request("POST", url, "/canvas", body=graph, params={"env": env}))
    http.emit(body, output, lambda b: typer.echo(f"canvas {b['status']}"))
