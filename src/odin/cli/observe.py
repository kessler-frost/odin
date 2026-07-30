"""`odin world` / `odin envs` / `odin events` / `odin logs` — read-only
inspection.

Status is a one-way projection: these commands read the same World/event-log
the UI projects, they never author anything.
"""
from __future__ import annotations

import typer

from odin.cli import http
from odin.cli.app import app
from odin.cli.http import OutputFormat


def _render_world(body: dict) -> None:
    # The loop that AUTHORS every phase below, first -- a dead or hung
    # reconciler makes this whole table a frozen snapshot, and printing the
    # table without saying so is what let a dead loop look like a converged env
    # (see Reconciler.health). Above the empty-world return too: "world is
    # empty" from a reconciler that never ticked is the same lie.
    # stderr, for `_render_envs`'s reason -- stdout stays the table a pipe
    # expects, and `-o json` carries the whole `reconciler` block for machines.
    verdict = (body.get("reconciler") or {}).get("verdict")
    if verdict:
        typer.echo(f"RECONCILER DOWN: {verdict}", err=True)
    resources = body["resources"]
    if not resources:
        typer.echo(f"env {body['env']}: world is empty")
        return
    id_width = max(len(r["id"]) for r in resources)
    kind_width = max(len(r["kind"]) for r in resources)
    for resource in resources:
        verdict = f"  {resource['verdict']}" if resource.get("verdict") else ""
        typer.echo(
            f"{resource['id']:<{id_width}}  {resource['kind']:<{kind_width}}"
            f"  {resource['phase']}{verdict}"
        )


@app.command()
def world(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """Observed state for an env: one line per resource with its lifecycle phase."""
    body = http.body_or_fail(http.request("GET", url, "/world", params={"env": env}))
    http.emit(body, output, _render_world)


def _render_envs(body: dict) -> None:
    """One env per line on stdout, nothing else -- `odin envs | while read e`
    has to keep working.

    There is deliberately no "no environments yet" branch any more. It printed
    "an env exists once something has been applied to it", and it read
    `body["envs"] == []` -- a signal odin's own `/envs` cannot send:
    `SpecStore.list_envs` floors at `["default"]`, so the route answers
    `{"envs": ["default"]}` on a never-used store (measured, see `envs` below).
    A message keyed on a state that never arrives is the exact shape of the
    four guards that silently never fired. If this ever prints nothing, that is
    a server bug worth chasing loudly, not a fresh-odin state to explain away.
    """
    for env in body["envs"]:
        typer.echo(env)


@app.command()
def envs(url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """List the environments the server knows about (one per line).

    A never-used odin lists `default` — the env `odin apply` and the canvas
    both target when you name none — even though nothing has been applied to
    it yet. Once any env has really been applied to, the list is exactly the
    envs that exist, so `default` drops out unless it is one of them.
    """
    body = http.body_or_fail(http.request("GET", url, "/envs"))
    http.emit(body, output, _render_envs)


def _render_volumes(body: dict) -> None:
    """The volumes table, and above all the count of the ones nothing owns.

    A reclaim that prints nothing looks exactly like a machine with nothing to
    reclaim, so the orphan line is printed even when it is zero -- that zero is
    the whole answer to "is odin holding disk I can get back", and leaving it out
    would make the useful case (a non-zero count) indistinguishable from a
    command the user simply misread."""
    volumes = body["volumes"]
    if volumes is None:
        return  # `body_or_fail` already put the reason on stderr and exited
    if not volumes:
        typer.echo("odin holds no named Docker volumes.")
        return
    width = max(len(v["name"]) for v in volumes)
    for volume in volumes:
        owner = f"env {volume['env']}" if volume["env"] else "no odin.env label"
        state = "in use" if volume["live"] else f"ORPHAN — reclaim with: {volume['reclaim']}"
        typer.echo(f"{volume['name']:<{width}}  {owner:<24}  {state}")
    orphans = body["orphans"]
    typer.echo(f"\n{len(orphans)} of {len(volumes)} volume(s) belong to no live environment.")


@app.command()
def volumes(url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """Named Docker volumes odin is holding, and which are orphaned.

    An RDS instance's data lives on a named volume that deliberately OUTLIVES its
    container, so `docker rm -f -v` never takes one and a Postgres data directory
    can sit on a disk-tight machine with nothing left that names it. This is how
    you see them.

    Nothing here deletes anything, and there is no sweep-everything flag on
    purpose: each orphan row carries the one scoped command that reclaims THAT
    volume. `live` is judged against this server's own store, so a second odin on
    the same machine — every parallel agent worktree has one — shows up here with
    its live environments looking orphaned. Read the row before you run it.
    """
    body = http.body_or_fail(http.request("GET", url, "/volumes"), output)
    http.emit(body, output, _render_volumes)


def _render_events(events_list: list[dict]) -> None:
    for event in events_list:
        http.echo_json(event)


@app.command()
def events(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """The env's durable event log (world deltas, tf runs, denials) — one line per event."""
    # `parsed_or_fail`, not `body_or_fail`: this route answers with a JSON
    # ARRAY, so there is no `error` field to key on -- but a refusal or a
    # non-JSON body still has to fail here rather than reach `_render_events`
    # as a document it cannot iterate.
    body = http.parsed_or_fail(http.request("GET", url, "/events", params={"env": env}))
    http.emit(body, output, _render_events)


def _render_logs(body: dict) -> None:
    if body.get("message"):
        typer.echo(body["message"])
    if body.get("lines"):
        typer.echo(body["lines"])


@app.command()
def logs(
    node: str = typer.Argument("", help="Canvas node label to fetch logs for (optional with --group)."),
    env: str = http.ENV,
    group: str = typer.Option(
        "", "--group",
        help="Read a CloudWatch log group instead, e.g. /aws/lambda/myfn or /ecs/myservice.",
    ),
    tail: int = typer.Option(
        100, "--tail",
        help="Trailing log lines to show, in total — a node with several task "
             "containers spends the budget newest-first; `==>` headers don't count.",
    ),
    url: str = http.URL,
    output: OutputFormat = http.OUTPUT,
) -> None:
    """Real logs off a node's actual backing container/VM — an unknown node
    exits 1, a known-but-not-running one prints an honest message and exits 0.

    `--group` reads odin's CloudWatch Logs sink directly instead, which is how
    the groups the substrates fill in without being drawn are reached
    (`/aws/lambda/{function}` per Invoke, `/ecs/{service}` per task sweep).
    Naming neither a node nor a group exits 1."""
    body = http.body_or_fail(
        http.request("GET", url, "/logs", params={"env": env, "node": node, "group": group, "tail": tail})
    )
    http.emit(body, output, _render_logs)
