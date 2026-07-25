"""`odin tf plan|status|destroy` — Simulate's own tofu-side operations for an
env, independent of the canvas-parity `odin apply`/`odin destroy` pair."""
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


# `tofu plan -detailed-exitcode`'s contract, kept verbatim: 0 no changes,
# 2 changes present, 1 a real error (which is also `body_or_fail`'s exit for
# a 409 refusal). The server-unreachable exit is 3, NOT the usual 2 -- see
# `http.request`'s `unreachable_code`.
_PLAN_EXIT = {"no_changes": 0, "changes": 2, "failed": 1}
_PLAN_UNREACHABLE_EXIT = 3


def _render_tf_plan(body: dict) -> None:
    typer.echo(f"status: {body['status']}  env: {body['env']}  exit code: {body['exit_code']}")
    for line in body.get("tail", []):
        typer.echo(f"  {line}")
    unsupported = body.get("unsupported") or []
    # "no changes" only ever means "no drift in what odin can generate" --
    # a node odin has no Terraform for was never in the plan at all.
    if unsupported:
        typer.echo(f"not covered by this plan (unsupported): {', '.join(unsupported)}")


@tf_app.command("plan")
def tf_plan(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """Drift check: `tofu plan` for the env, through odin's own gateway.

    The SAFE way to plan. The generated `main.tf` under `.odin/<env>/tf` is
    portable, real-AWS Terraform with no endpoint in it — odin injects the
    endpoint and credentials at run time — so running `tofu plan` there by
    hand talks to REAL AWS. This command cannot. It changes nothing.

    Exit codes mirror `tofu plan -detailed-exitcode`: 0 no changes, 2 changes
    present, 1 a real error or a refusal, 3 the odin server is unreachable.
    """
    body = http.body_or_fail(http.request(
        "POST", url, "/tf/plan", params={"env": env}, unreachable_code=_PLAN_UNREACHABLE_EXIT,
    ))
    http.emit(body, output, _render_tf_plan)
    raise typer.Exit(_PLAN_EXIT[body["status"]])


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
