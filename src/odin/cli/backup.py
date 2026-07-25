"""`odin export` / `odin import` — back up and restore one environment (W2.3).

Like `odin keys`, and unlike every server-backed command, these talk to NO
HTTP route: they operate straight on `.odin/` in the current directory. That
is deliberate. The failure these exist for ("I lost `.odin/`") is exactly the
one where the server can't start, so restore has to work with odin down —
and importing into a LIVE store is refused outright, because a running odin
holds per-env Reconcilers and an in-memory World that would carry on
reconciling against a store they never read.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from pydantic import BaseModel

from odin.backup import BackupError, default_archive_name, export_env, import_archive
from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat
from odin.util import SHUTDOWN_GRACE, LiveServer

ROOT = Path(".odin")

# `-o` means "output FILE" on these two commands (the `tar`/`cc` convention --
# what anyone reaching for a backup tool will type), so the CLI's usual
# `-o/--output` format switch is long-form only here. Deliberately different
# from the rest of the CLI, and called out in `--out`'s own help.
JSON_OR_TEXT = typer.Option(
    OutputFormat.text, "--output",
    help="Output format: human-readable text, or raw JSON on stdout for piping.",
)


def _run(work: Callable[[], BaseModel]) -> dict:
    """Every BackupError becomes stderr + its own exit code (2 = an archive
    this odin can't read, 1 = a refusal)."""
    try:
        return work().model_dump()
    except BackupError as exc:
        raise http.fail(str(exc), exc.code) from None


@app.command("export")
def export_command(
    env: str = http.ENV,
    out: Path | None = typer.Option(
        None, "-o", "--out",
        help="Archive path (default: odin-<env>-export.tar.gz here). "
             "Note: -o is the archive path, not the output format -- use --output json for that.",
    ),
    output: OutputFormat = JSON_OR_TEXT,
) -> None:
    """Back up an environment's state to a tar.gz — offline, no server needed.

    Captures the env's whole control plane: the Stack revision lineage + HEAD,
    world.json, its issued gateway credentials, the gateway's synth stores and
    lambda zips, and the tofu workspace INCLUDING terraform.tfstate. The
    provider cache (`tf/.terraform/`) is deliberately left out — `tofu init`
    rebuilds it from the same main.tf.

    The shared `.odin/canvas.json` rides along at its own archive path;
    `odin import --with-canvas` is what restores it.

    This is control-plane state, not data: restoring gives you fresh, empty
    backing containers matching the archived desired state — the objects that
    were in a bucket are not in the archive.
    """
    dest = out or Path(default_archive_name(env))
    result = _run(lambda: export_env(ROOT, env, dest))
    # The archive carries the env's issued gateway credentials and every canvas
    # secret, in cleartext, in a file that's trivial to email. Say so where the
    # user will actually see it -- on stderr, in both output modes, never
    # polluting the JSON on stdout (same discipline as `odin keys issue`).
    typer.echo(
        f"note: {dest} contains this env's issued credentials and any canvas secrets "
        "in cleartext -- treat it like a private key file.",
        err=True,
    )
    http.emit(
        result, output,
        lambda b: typer.echo(
            f"Exported env {b['env']!r} → {b['archive']} "
            f"({len(b['members'])} entries, {b['size'] / 1024:.1f} KiB)"
        ),
    )


@app.command("import")
def import_command(
    archive: Path = typer.Argument(..., help="Archive written by `odin export`."),
    env: str | None = typer.Option(
        None, "--env", help="Restore under this env name instead of the archived one."
    ),
    force: bool = typer.Option(
        False, "--force", help="Replace an existing env directory (destructive)."
    ),
    with_canvas: bool = typer.Option(
        False, "--with-canvas",
        help="Also restore the shared .odin/canvas.json, REPLACING the current canvas.",
    ),
    ignore_live_server: bool = typer.Option(
        False, "--ignore-live-server",
        help="Skip the live-server check entirely. The escape hatch for a restore "
             "that odin wrongly believes is unsafe — only when you have checked "
             "yourself that no odin is running against this store.",
    ),
    output: OutputFormat = JSON_OR_TEXT,
) -> None:
    """Restore an environment from an `odin export` archive — offline.

    Refuses to run while odin is up (`odin stop` first), refuses to overwrite
    an existing env directory without `--force`, and refuses any archive with
    an absolute, `..`-traversing, or link member.

    A server that is still SHUTTING DOWN is waited for, not refused: uvicorn
    with odin's reconcilers in its lifespan takes well over 6 seconds to let go
    of the store, so `odin stop && odin import` in one script just works.

    Restoring state does NOT start containers: it puts odin's model of the
    world back, then `odin start` + Apply converges reality to it.
    """
    http.emit(
        _run(lambda: import_archive(
            archive, ROOT, env=env, force=force, with_canvas=with_canvas,
            ignore_live_server=ignore_live_server, on_wait=_wait_notice,
        )),
        output, _render_import,
    )


def _wait_notice(server: LiveServer) -> None:
    """Never stall silently: if the store is busy, say so and say for how long,
    on stderr so `--output json` on stdout stays pipeable."""
    typer.echo(
        f"odin is running ({server.detail}) — waiting up to {SHUTDOWN_GRACE:.0f}s "
        "for it to release this store …",
        err=True,
    )


def _render_import(body: dict) -> None:
    renamed = f" (archived as {body['source_env']!r})" if body["env"] != body["source_env"] else ""
    typer.echo(
        f"Restored env {body['env']!r}{renamed} from {body['archive']} — "
        f"{body['files']} files, written by odin {body['odin_version']} at {body['created_at']}."
    )
    typer.echo("Canvas restored: .odin/canvas.json replaced." if body["canvas_restored"] else
               "Canvas left alone (pass --with-canvas to restore it too).")
    typer.echo(
        f"\nNext steps:\n"
        f"  odin start\n"
        f"  then hit Apply (or `odin apply --env {body['env']}`) to reconcile the substrates.\n"
        f"Importing state does not boot containers — the reconciler plus one Apply\n"
        f"converges reality to the restored desired state (backings come back empty:\n"
        f"odin exports control-plane state, not container volumes)."
    )
