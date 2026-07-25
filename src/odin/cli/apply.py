"""`odin apply` / `odin destroy` — the canvas-parity lifecycle pair.

`apply` is the UI's Apply button as a command: POST /apply-full — reconcile
the env's backings AND run `tofu apply` through the gateway, then report the
honest per-half outcome (the reconciler half can succeed while tofu fails —
that's `applied_tf_failed`, and a nonzero exit). `destroy` is the teardown
twin (POST /destroy): tofu destroy when a workspace exists, then prune every
backing.

A third honest failure (field test 3): `applied_services_unhealthy` — tofu
genuinely had nothing to do (`tf: ok`) but an ECS service ended the apply
short of its desired task count. Also a nonzero exit, and the one place the
failing image / broken ref is named in the apply's own output.
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


def _echo_unhealthy(item: dict) -> None:
    """One service the apply refuses to score green: WHICH service, WHAT was
    observed, and the real underlying reason when odin knows one. Field test 3:
    the failing image was in /world and in the events stream but NEVER in the
    apply's own output, which is the surface a CI log actually shows."""
    reason = f" — {item['reason']}" if item.get("reason") else ""
    typer.echo(f"unhealthy: {item['node']} — {item['running']}/{item['desired']} tasks running{reason}")


def not_covered(body: dict) -> list[str]:
    """Everything on the canvas the command did NOT act on, in ONE array.

    Fresh-user MISLEAD-1: the README told CI to gate on `.unsupported`, but a
    node whose KIND odin has no model for at all lands in `.skipped` and
    `.unsupported` stayed `[]`. `jq -e '.unsupported | length == 0'` returned
    true — exit 0 — while two drawn nodes were silently dropped. Two arrays
    with adjacent meanings is a gate you can get right and still be wrong, so
    both are also published as one field a gate cannot half-read.

    `skipped` = a canvas node type that never became a Stack resource (a kind
    odin doesn't model, or a typo). `unsupported` = a resource odin models but
    can't generate Terraform for, with the reason. Both are still emitted
    verbatim; this is a union, not a replacement."""
    return [*(body.get("skipped") or []), *(body.get("unsupported") or [])]


def _render_apply(body: dict) -> None:
    typer.echo(f"status: {body['status']}  env: {body['env']}  rev: {body.get('rev') or '-'}")
    for key in ("skipped", "unsupported"):
        values = body.get(key) or []
        if values:
            typer.echo(f"{key}: {', '.join(str(v) for v in values)}")
    if body.get("tf"):
        _echo_tf(body["tf"])
    for item in body.get("unhealthy") or []:
        _echo_unhealthy(item)
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
    body["not_covered"] = not_covered(body)  # the one field a CI gate should read
    http.emit(body, output, _render_apply)
    # `applied` is the ONLY clean outcome -- anything else (tofu failed, or a
    # service that ended the apply short of its desired task count) is a
    # nonzero exit, so a new honest-failure status can never silently score
    # green in CI the way field test 3's outage did.
    if body["status"] != "applied":
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
