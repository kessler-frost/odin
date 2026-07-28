"""`odin chat` — ask for a canvas change in English, review it, then apply it.

The owner's rule for this whole surface: *"canvas and navigating things around IS
the language of odin and not chatting with a bot to update things around - that
we'll add later too but this is a separate thing."* So this command is an
ADDITION to the canvas, and the shape follows directly:

  odin chat "give the thumbnailer read access to uploads"   -> DOES it
  odin chat "..." --dry-run                                 -> just shows the plan
  odin chat --clear                                         -> forget the conversation

**The canvas is the review surface** (owner decision, 2026-07-28). The edit
lands where you are already looking: the open UI redraws over its event stream, the
change goes onto the browser's own undo stack, and Cmd-Z reverses it. Asking a
person to confirm a diff in a terminal, when the thing the diff describes is on
screen behind it, is the worse review.

**What it still never does is APPLY.** Editing the drawing is reversible;
building from it creates real containers and, for rds, can destroy real data.
That button stays yours.

The save goes through `POST /canvas` — the same endpoint the UI's own save uses —
so an agent-authored canvas gets identical validation to a hand-drawn one. There
is no privileged path.

The proposal is printed as one sentence per change, and REFUSALS are printed too,
on stderr: an op odin declined is the most useful line in the output, because it
is the difference between "it did what I asked" and "it did some of what I
asked".
"""
from __future__ import annotations

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat


@app.command("chat")
def chat(
    message: str = typer.Argument("", help="What you want changed, in plain English."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would change without touching the canvas.",
    ),
    clear: bool = typer.Option(
        False, "--clear",
        help="Forget the conversation so far. The canvas is untouched.",
    ),
    env: str = http.ENV,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Change the canvas in plain English (--dry-run to look first)."""
    if clear:
        cleared = http.body_or_fail(http.request("POST", url, "/chat/clear", params={"env": env}))
        typer.echo(f"cleared {cleared['turns_forgotten']} turn(s) — the canvas is unchanged")
        return
    if not message.strip():
        raise http.fail("say what you want changed, e.g. odin chat \"add a redis cache\"", 2)

    body = http.body_or_fail(
        http.request("POST", url, "/chat", params={"env": env},
                     body={"message": message, "dry_run": dry_run})
    )
    if output is OutputFormat.json:
        http.echo_json(body)
        return

    if body.get("reply"):
        typer.echo(body["reply"])
    for change in body.get("changes") or []:
        typer.echo(f"  - {change}")
    # On STDERR and never mixed into the plan: a refusal is not part of the
    # answer, it is the part of the request odin did not perform.
    for refusal in body.get("refused") or []:
        typer.echo(f"skipped: {refusal['reason']}", err=True)
    if body.get("note"):
        typer.echo(f"note: {body['note']}", err=True)

    # ALWAYS say whether anything would change, even when the answer is "no".
    # The agent's own `reply` is prose and can claim an action it never proposed
    # -- measured: "Added a read-access permission edge..." with zero ops -- so
    # the line below is what stops a reply from standing alone as an implied
    # success. Silence here read as "done" and exited 0.
    if not body.get("changes"):
        typer.echo("no changes made", err=True)
        return
    if dry_run:
        typer.echo("\nnothing was changed — this was a dry run", err=True)
        return
    # The server saved it. Said out loud rather than left implied: the canvas
    # moved under whoever is looking at it, and "it did what I asked" should not
    # have to be inferred from the absence of an error.
    typer.echo(f"canvas saved ({body.get('rev', '')[:12]}) — Cmd-Z in the UI undoes it")
