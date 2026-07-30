"""`odin env rm` — decommission an environment.

`odin destroy --env X` is a TEARDOWN: it deletes the resources and deliberately
keeps the environment, because the desired state is what makes a retry possible
and the reconciler is what converges the next apply. That is right for a
teardown and wrong for a decommission, and until now odin had only the first —
seven environments accumulated during one field-test session, each with a
reconciler ticking forever over nothing.

`odin env rm` is the second verb: the same teardown, and then the env itself —
its `.odin/<env>/` directory, its gateway-issued credentials, its synthesized
control-plane records, its reconciler, and its entry in `odin envs`.

It reports whether the END STATE HOLDS, never whether it tried. Every way it can
fall short leaves the environment exactly as it was, so a failure is retryable
rather than half-done, and each one says what is still standing.
"""
from __future__ import annotations

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat

env_app = typer.Typer(help="Environment lifecycle (see `odin envs` to list them).", no_args_is_help=True)
app.add_typer(env_app, name="env")

# The only two statuses that mean the environment is gone -- the CLI's own copy
# of `server._REMOVE_OK`, deliberately. The server already sends an `error` for
# everything else (which is what `body_or_fail` exits 1 on), so this is the
# second half of the same guard: a server that ever answers a NEW status
# without an `error` exits nonzero here rather than being read as success by a
# script. Exactly what `odin apply` does with `status != "applied"`.
_REMOVED = {"removed", "not_found"}


def _render_rm(body: dict) -> None:
    typer.echo(f"status: {body['status']}  env: {body['env']}")
    forgotten = body.get("forgotten") or {}
    if body.get("state_dir"):
        typer.echo(f"removed {body['state_dir']}")
    # Only the halves that HELD something -- a removal that forgot nothing but
    # a directory should not print six lines of zeroes.
    for name, value in sorted(forgotten.items()):
        if value:
            typer.echo(f"forgot {name}: {value}")


@env_app.command("rm")
def env_rm(
    name: str = typer.Argument(..., help="The environment to remove."),
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Tear an env down AND forget it — state directory, credentials, gateway
    records, reconciler, and its entry in `odin envs`.

    Destructive and not undoable: everything under `.odin/<name>/` goes,
    including the canvas and every Stack revision. `odin export --env <name>`
    first if you might want it back.

    Exits 1 with the server's own reason on stderr if the env is NOT gone —
    a failed teardown, a reconciler that would not stop, a container still
    standing, or a state directory odin could not delete. In every one of those
    cases nothing was deleted, so re-running after fixing the cause is safe.
    An env that never existed exits 0 (`not_found`): nothing was removed, and
    nothing was created.
    """
    body = http.body_or_fail(http.request("POST", url, "/envs/rm", params={"env": name}), output)
    http.emit(body, output, _render_rm)
    if body["status"] not in _REMOVED:
        raise typer.Exit(1)
