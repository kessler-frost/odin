"""`odin translate` / `odin import-tf` — the two directions of canvas <-> TF.

`translate` is the UI code panel's CLI twin (canvas -> Terraform preview);
`import-tf` is the reverse, and prints a canvas-shaped
`{"nodes":..., "edges":...}` on stdout so
`odin import-tf x.tf | odin canvas set -` pipes straight through.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat

TRANSLATE_FILE = typer.Option(
    None, "--file",
    help="Preview an UNSAVED canvas JSON file instead of the env's stored stack.",
)
LIVE = typer.Option(
    [], "--live",
    help="Import a live backing resource as type=id (repeatable), e.g. --live s3=uploads.",
)
IMPORT_ENV = typer.Option(
    "default", "--env", help="Environment whose live backings --live resolves against."
)


def _render_translate(body: dict) -> None:
    typer.echo(body["files"].get("main.tf", ""), nl=False)
    unsupported = body.get("unsupported") or []
    if unsupported:
        typer.echo(f"unsupported: {', '.join(str(u) for u in unsupported)}", err=True)


@app.command()
def translate(
    env: str = http.ENV,
    file: Path | None = TRANSLATE_FILE,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Canvas -> Terraform preview (the code panel's CLI twin). Prints main.tf."""
    graph = http.parse_json_arg(file.read_text(), str(file)) if file is not None else None
    body = http.body_or_fail(
        http.request("POST", url, "/translate", params={"env": env}, body=graph)
    )
    http.emit(body, output, _render_translate)


def _live_resource(spec: str) -> dict:
    kind, sep, resource_id = spec.partition("=")
    if not (sep and kind and resource_id):
        raise http.fail(f"--live expects type=id (e.g. --live s3=uploads), got {spec!r}", 2)
    return {"type": kind, "id": resource_id}


def _import_payload(file: Path | None, live: list[str]) -> dict:
    if live:
        return {"source": "live", "resources": [_live_resource(spec) for spec in live]}
    if file is None:
        raise http.fail("import-tf needs a <file.tf>, or at least one --live type=id", 2)
    return {"source": "hcl", "hcl": file.read_text()}


@app.command("import-tf")
def import_tf(
    file: Path | None = typer.Argument(
        None, help="A .tf file to parse as HCL (required unless --live is given)."
    ),
    live: list[str] = LIVE,
    env: str = IMPORT_ENV,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Terraform -> canvas. Prints {"nodes":..., "edges":...} on stdout (both output
    modes — deliberately canvas-shaped, so it pipes into `odin canvas set -`);
    unsupported resources are noted on stderr, never mixed into stdout."""
    payload = _import_payload(file, live)
    body = http.body_or_fail(
        http.request("POST", url, "/import-tf", params={"env": env}, body=payload)
    )
    # Finding #7: a genuine PARSE failure is a hard error (non-zero exit), so a
    # CI exit-code check catches a broken import -- unlike a well-formed file
    # with only unsupported resources, which stays a success (exit 0) below.
    if body.get("parse_error"):
        raise http.fail(body["parse_error"], 1)
    http.echo_json({"nodes": body["nodes"], "edges": body["edges"]})
    unsupported = body.get("unsupported") or []
    if unsupported:
        typer.echo(f"unsupported: {json.dumps(unsupported)}", err=True)
    for warning in body.get("warnings") or []:  # finding #6: per-node attribute drops, on stderr
        typer.echo(f"warning: {warning}", err=True)
