"""`odin keys` — LOCAL, OFFLINE credential operations on `.odin/<env>/keys.json`.

Unlike every other control-surface command, nothing here talks to the odin
server: there is no HTTP route for key issuance. These commands operate
directly on the current directory's `.odin/` tree through the same `KeyStore`
the gateway itself uses — a low-level escape hatch for advanced manual
workload wiring, not part of the server API.
"""
from __future__ import annotations

from pathlib import Path

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat
from odin.gateway.keys import KeyStore
from odin.spec.store import SpecStore

keys_app = typer.Typer(
    help="Gateway credential escape hatches (operate on the local .odin/ — no server).",
    no_args_is_help=True,
)
app.add_typer(keys_app, name="keys")


@keys_app.command("issue")
def keys_issue(
    node: str = typer.Argument(..., help="Canvas node label to issue gateway credentials for."),
    env: str = http.ENV,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Issue (or re-read) a node's gateway credentials — a low-level, OFFLINE escape hatch.

    This bypasses the server entirely and writes straight to
    `.odin/<env>/keys.json` in the current directory, through the same
    KeyStore the gateway uses. Meant for advanced manual workload wiring
    (pointing an out-of-band process at the gateway under a node's identity).
    Issuing again for the same (env, node) returns the SAME pair.
    """
    store = SpecStore(Path(".odin"))
    # Release sweep finding #5 (LOW): this is an escape hatch, so it still
    # issues -- but a label that isn't a resource in the env's applied stack
    # would get InvalidClientTokenId at the gateway, so warn clearly (stderr,
    # never polluting the creds on stdout).
    if node not in {r.id for r in store.get_stack(env).resources}:
        typer.echo(
            f"warning: {node!r} is not a resource in env {env!r}'s applied stack -- issuing anyway "
            "(the gateway rejects credentials for a principal it doesn't know).",
            err=True,
        )
    access_key, secret_key = KeyStore(store.root).issue(env, node)
    body = {"access_key": access_key, "secret_key": secret_key}
    http.emit(
        body, output,
        lambda b: typer.echo(
            f"AWS_ACCESS_KEY_ID={b['access_key']}\nAWS_SECRET_ACCESS_KEY={b['secret_key']}"
        ),
    )
