"""`odin chat` — ask for a canvas change in English, review it, then apply it.

The owner's rule for this whole surface: *"canvas and navigating things around IS
the language of odin and not chatting with a bot to update things around - that
we'll add later too but this is a separate thing."* So this command is an
ADDITION to the canvas, and the shape follows directly:

  odin chat "give the thumbnailer read access to uploads"   -> shows the plan
  odin chat "..." --apply                                   -> saves it

**Two calls, never one.** The default prints what WOULD change and writes
nothing. Applying is a second, explicit act by a person. An agent that edited the
canvas someone was looking at would be taking the language away from them, which
is exactly what this surface is not for.

`--apply` reuses `POST /canvas` — the same endpoint the UI's own save uses — so
an agent-authored canvas goes through identical validation to a hand-drawn one.
There is no privileged path.

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
    message: str = typer.Argument(..., help="What you want changed, in plain English."),
    apply: bool = typer.Option(
        False, "--apply",
        help="Save the proposed canvas. Without this, nothing is written.",
    ),
    env: str = http.ENV,
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Propose a canvas change in plain English (add --apply to save it)."""
    body = http.body_or_fail(
        http.request("POST", url, "/chat", params={"env": env}, body={"message": message})
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
        typer.echo("no changes proposed — nothing was changed", err=True)
        return
    if not apply:
        typer.echo("\nnothing was changed — re-run with --apply to save this", err=True)
        return

    saved = http.body_or_fail(
        http.request("POST", url, "/canvas", body=body["canvas"], params={"env": env})
    )
    typer.echo(f"canvas {saved['status']}")
