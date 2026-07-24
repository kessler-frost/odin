"""`odin tf status|destroy` — Simulate's own tofu-side operations for an env,
independent of the canvas-parity `odin apply`/`odin destroy` pair."""
from __future__ import annotations

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat

tf_app = typer.Typer(help="Direct tofu-side operations for an env.", no_args_is_help=True)
app.add_typer(tf_app, name="tf")


def _render_tf_status(body: dict) -> None:
    workspace = "exists" if body["workspace_exists"] else "absent"
    typer.echo(f"env: {body['env']}  running: {body['running']}  workspace: {workspace}")
    last = body.get("last")
    if last is None:
        typer.echo("last run: none")
        return
    typer.echo(f"last run: {'ok' if last['ok'] else 'failed'} (exit code {last['exit_code']})")
    for line in last.get("tail", []):
        typer.echo(f"  {line}")


@tf_app.command("status")
def tf_status(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """The env's tofu state: running?, workspace on disk?, last run's outcome."""
    body = http.body_or_fail(http.request("GET", url, "/tf/status", params={"env": env}))
    http.emit(body, output, _render_tf_status)


def _render_tf_destroy(body: dict) -> None:
    typer.echo(f"status: {body['status']}  env: {body['env']}  exit code: {body['exit_code']}")
    for line in body.get("tail", []):
        typer.echo(f"  {line}")


@tf_app.command("destroy")
def tf_destroy(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """Run `tofu destroy` for the env's workspace (tofu's half only — see `odin destroy`)."""
    body = http.body_or_fail(http.request("POST", url, "/tf/destroy", params={"env": env}))
    http.emit(body, output, _render_tf_destroy)
    if body["status"] == "failed":
        raise typer.Exit(1)
