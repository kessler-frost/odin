"""`odin translate` / `odin import-tf` — the two directions of canvas <-> TF.

`translate` is the UI code panel's CLI twin (canvas -> Terraform preview);
`import-tf` is the reverse, and prints a canvas-shaped
`{"nodes":..., "edges":...}` on stdout so
`odin import-tf x.tf | odin canvas set -` pipes straight through.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat

TRANSLATE_FILE = typer.Option(
    None, "--file",
    help="Preview an UNSAVED canvas JSON file instead of the canvas saved on the server.",
)
LIVE = typer.Option(
    [], "--live",
    help="Import a live backing resource as type=id (repeatable), e.g. --live s3=uploads.",
)
IMPORT_ENV = typer.Option(
    "default", "--env", help="Environment whose live backings --live resolves against."
)


_EMPTY_CANVAS_NOTE = (
    "note: the saved canvas is empty, so there is no Terraform to print -- "
    "draw something and `odin canvas set`, or pass --file <canvas.json>"
)


def _render_translate(body: dict) -> None:
    typer.echo(body["files"].get("main.tf", ""), nl=False)
    unsupported = body.get("unsupported") or []
    if unsupported:
        typer.echo(f"unsupported: {', '.join(str(u) for u in unsupported)}", err=True)


def _graph(url: str, env: str, file: Path | None) -> dict:
    """Findings B4 / MEDIUM-10: with no `--file`, translate the canvas SAVED ON
    THE SERVER -- `odin apply`'s own default (`cli/apply.py::_graph`), and what
    README ("print the Terraform your canvas becomes") and this command's own
    help ("the code panel's CLI twin") both promise. Posting no body instead
    made the server translate the env's STORED STACK, which is empty until
    something has been applied, so `odin canvas set x.json && odin translate >
    main.tf` produced a valid-looking EMPTY file with exit 0. The code panel
    previews the canvas, not the last-applied stack; so does this now."""
    if file is not None:
        return http.parse_json_arg(file.read_text(), str(file))
    # Per-env since v0.7.9. Without `env` this previewed the DEFAULT env's canvas
    # whatever `--env` said -- the same omission `cli/apply.py::_graph` had, and
    # there it was building infrastructure rather than printing it.
    return http.body_or_fail(http.request("GET", url, "/canvas", params={"env": env}))


@app.command()
def translate(
    env: str = http.ENV,
    file: Path | None = TRANSLATE_FILE,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Canvas -> Terraform preview (the code panel's CLI twin). Prints main.tf.

    With no --file this translates the canvas saved on the server -- the same
    default `odin apply` uses.
    """
    graph = _graph(url, env, file)
    body = http.body_or_fail(
        http.request("POST", url, "/translate", params={"env": env}, body=graph)
    )
    # An empty canvas genuinely translates to nothing, so this stays exit 0
    # (`apply` treats an empty canvas as a legitimate teardown) -- but never
    # SILENTLY, or a CI `translate > main.tf` step scores an empty file as a
    # clean run (finding U5's "empty output, exit 0" family).
    if not graph.get("nodes") and output is not OutputFormat.json:
        typer.echo(_EMPTY_CANVAS_NOTE, err=True)
    http.emit(body, output, _render_translate)


def _live_resource(spec: str) -> dict:
    kind, sep, resource_id = spec.partition("=")
    if not (sep and kind and resource_id):
        raise http.fail(f"--live expects type=id (e.g. --live s3=uploads), got {spec!r}", 2)
    return {"type": kind, "id": resource_id}


def _project_hcl(directory: Path) -> str:
    """Every `*.tf` in a directory, concatenated into one configuration.

    That is exactly what a Terraform PROJECT is -- tofu itself reads every `.tf`
    in its working directory as a single config, order-independent -- so a
    directory imports as ONE canvas. Before v0.7.1 a directory argument died
    with a raw traceback (`IsADirectoryError`), and since multiple file
    arguments are a usage error, importing a real project meant a shell loop
    plus hand-merging the JSON fragments (field test B7). The `# ----` headers
    keep the file boundaries visible in whatever the parser reports.
    """
    files = sorted(directory.glob("*.tf"))
    if not files:
        raise http.fail(f"no *.tf files in {directory} -- is that the right directory?", 2)
    typer.echo(
        f"note: importing {len(files)} .tf file(s) from {directory} as ONE canvas: "
        f"{', '.join(f.name for f in files)}",
        err=True,
    )
    return "\n".join(f"# ---- {f.name}\n{f.read_text()}" for f in files)


def _import_payload(file: Path | None, live: list[str]) -> dict:
    if live:
        return {"source": "live", "resources": [_live_resource(spec) for spec in live]}
    if file is None:
        raise http.fail("import-tf needs a <file.tf> or directory, or at least one --live type=id", 2)
    if not file.exists():
        raise http.fail(f"no such file or directory: {file}", 2)
    if not file.is_dir():
        return {"source": "hcl", "hcl": file.read_text()}
    # A DIRECTORY carries its zips too: a lambda's body is in one, and this
    # payload is the only way it can reach the parser (the server does the
    # parsing, so reading the zip client-side and dropping it here would leave
    # the recovery working in unit tests and nowhere else).
    archives = {
        path.name: base64.b64encode(path.read_bytes()).decode()
        for path in sorted(file.glob("*.zip"))
    }
    return {"source": "hcl", "hcl": _project_hcl(file), "archives": archives}


@app.command("import-tf")
def import_tf(
    file: Path | None = typer.Argument(
        None, help="A .tf file, or a DIRECTORY of them (a whole Terraform project, "
                   "imported as one canvas). Required unless --live is given.",
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
