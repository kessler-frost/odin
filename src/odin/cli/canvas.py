"""`odin canvas` — read/write the saved canvas.

The server's `/canvas` routes have NO `env` parameter on purpose: there is
one global canvas file (`.odin/canvas.json`) that every env's apply reads.
These commands honestly mirror that — no fake `--env` here.

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
def canvas_get(url: str = http.URL, output: OutputFormat = http.JSON_OUTPUT) -> None:
    """Print the saved canvas ({"nodes": [...], "edges": [...]}) as JSON."""
    body = http.body_or_fail(http.request("GET", url, "/canvas"))
    http.emit(body, output, _render_canvas)


@canvas_app.command("set")
def canvas_set(
    file: str = typer.Argument(..., help="Canvas JSON file, or - to read stdin."),
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Save a canvas JSON ({"nodes": [...], "edges": [...]}) as THE canvas."""
    graph = http.parse_json_arg(http.read_text_arg(file), file)
    placed = place_unpositioned(graph)
    if placed:
        typer.echo(
            f"no \"position\" on {len(placed)} node(s) — placed on the grid: {', '.join(placed)}",
            err=True,
        )
    body = http.body_or_fail(http.request("POST", url, "/canvas", body=graph))
    http.emit(body, output, lambda b: typer.echo(f"canvas {b['status']}"))
