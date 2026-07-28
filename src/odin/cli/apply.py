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


def _graph(url: str, env: str, file: Path | None, output: OutputFormat) -> dict:
    """The canvas this apply will build, from THIS env.

    The `env` argument was missing until v0.8.9, and the canvas has been
    per-environment since v0.7.9 -- so `odin apply --env prod` fetched
    DEFAULT's canvas and applied it to prod. Found by field test: an env whose
    canvas held s3+sqs+rds came up with the default env's lambda instead, and
    its own two resources never existed.

    Worse than showing the wrong thing, because apply RECONCILES: any resource
    the target env legitimately had and the default canvas does not name is
    torn down. `/canvas` has honoured `?env=` since v0.7.9; this call site was
    simply never updated, which is the same drift the canvas router's own
    docstring describes for the route itself.
    """
    if file is not None:
        return http.parse_json_arg(file.read_text(), str(file))
    return http.body_or_fail(http.request("GET", url, "/canvas", params={"env": env}), output)


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


def _echo_unhealthy_resource(item: dict) -> None:
    """`unhealthy_resources` is a different shape from `unhealthy` above (which is
    ECS-only: running/desired tasks), so it needs its own line rather than being
    squeezed into that one.

    It exists because tofu can only describe a failed RDS or Lambda as an
    unexpected state -- famously as `last error: %!s(<nil>)`, which is the
    provider's own output and not something odin can rewrite. odin's records DO
    hold the real reason, and this is the surface a CI log actually shows: text
    mode rendered nothing at all here, so the reason reached JSON readers only."""
    reason = f" — {item['reason']}" if item.get("reason") else " (no reason recorded; observed status only)"
    typer.echo(f"unhealthy: {item.get('kind', '?')} {item['node']} is {item['observed']}{reason}")


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
    for item in body.get("unhealthy_resources") or []:
        _echo_unhealthy_resource(item)
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
    graph = _graph(url, env, file, output)
    # `not_covered` -- the one field a CI gate should read -- comes from the
    # SERVER (v0.7.4). v0.7.3 computed it here, which left the same trap open
    # for `curl /apply-full`: an agent or a CI job without the odin CLI got the
    # two easily-confused arrays and nothing else. See `server.not_covered`.
    body = http.body_or_fail(
        http.request("POST", url, "/apply-full", params={"env": env}, body=graph), output
    )
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
    """Tear an env down: tofu destroy (when a workspace exists) + prune every backing.

    A failed destroy exits 1 with the server's own `error` on stderr -- and, in
    `-o json`, the WHOLE failure body on stdout: `still_standing.tf_state`,
    `still_standing.containers` and the `tf.tail` that says why. Field test 6
    F7 measured zero bytes there, so a script gating on the payload read an
    empty string and a human got no diagnosis at all.
    """
    body = http.body_or_fail(http.request("POST", url, "/destroy", params={"env": env}), output)
    http.emit(body, output, _render_destroy)
