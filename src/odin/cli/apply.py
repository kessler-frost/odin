"""`odin apply` / `odin destroy` — the canvas-parity lifecycle pair.

`apply` is the UI's Apply button as a command: POST /apply-full — reconcile
the env's backings AND run `tofu apply` through the gateway, then report the
honest per-half outcome (the reconciler half can succeed while tofu fails —
that's `applied_tf_failed`, and a nonzero exit). `destroy` is the teardown
twin (POST /destroy): tofu destroy when a workspace exists, then prune every
backing.
"""
from __future__ import annotations

from pathlib import Path

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat

FILE = typer.Option(
    None, "--file",
    help="Canvas JSON file to apply (default: the canvas currently saved on the server).",
)


def _graph(url: str, file: Path | None) -> dict:
    if file is not None:
        return http.parse_json_arg(file.read_text(), str(file))
    return http.body_or_fail(http.request("GET", url, "/canvas"))


def _echo_tf(tf: dict) -> None:
    typer.echo(f"tf: {tf['status']} (exit code {tf['exit_code']})")
    for line in tf.get("tail", []):
        typer.echo(f"  {line}")


def _render_apply(body: dict) -> None:
    typer.echo(f"status: {body['status']}  env: {body['env']}  rev: {body.get('rev') or '-'}")
    for key in ("skipped", "unsupported"):
        values = body.get(key) or []
        if values:
            typer.echo(f"{key}: {', '.join(str(v) for v in values)}")
    if body.get("tf"):
        _echo_tf(body["tf"])
    if body.get("note"):
        typer.echo(f"note: {body['note']}")


@app.command()
def apply(
    env: str = http.ENV,
    file: Path | None = FILE,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Apply a canvas to an env: reconcile backings + tofu apply (the UI's Apply button)."""
    graph = _graph(url, file)
    body = http.body_or_fail(
        http.request("POST", url, "/apply-full", params={"env": env}, body=graph)
    )
    http.emit(body, output, _render_apply)
    if body["status"] == "applied_tf_failed":
        raise typer.Exit(1)


def _render_destroy(body: dict) -> None:
    typer.echo(f"status: {body['status']}  env: {body['env']}")
    if body.get("tf"):
        _echo_tf(body["tf"])


@app.command()
def destroy(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """Tear an env down: tofu destroy (when a workspace exists) + prune every backing."""
    body = http.body_or_fail(http.request("POST", url, "/destroy", params={"env": env}))
    http.emit(body, output, _render_destroy)
