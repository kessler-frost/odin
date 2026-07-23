"""`odin canvas` — read/write the saved canvas.

The server's `/canvas` routes have NO `env` parameter on purpose: there is
one global canvas file (`.odin/canvas.json`) that every env's apply reads.
These commands honestly mirror that — no fake `--env` here.
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


def _render_canvas(body: dict) -> None:
    typer.echo(f"canvas: {len(body['nodes'])} nodes, {len(body['edges'])} edges")


@canvas_app.command("get")
def canvas_get(url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """Print the saved canvas ({"nodes": [...], "edges": [...]})."""
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
    body = http.body_or_fail(http.request("POST", url, "/canvas", body=graph))
    http.emit(body, output, lambda b: typer.echo(f"canvas {b['status']}"))
