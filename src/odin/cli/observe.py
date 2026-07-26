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
    has to keep working. The empty case used to print a single blank line and
    exit 0, which reads like a broken command rather than an answer, so the
    explanation goes to stderr where it can't get into that loop."""
    for env in body["envs"]:
        typer.echo(env)
    if not body["envs"]:
        typer.echo(
            "no environments yet — an env exists once something has been applied to it. "
            "`odin apply` and the canvas both default to env 'default'.",
            err=True,
        )


@app.command()
def envs(url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """List the environments the server knows about (one per line).

    An env comes into existence when a canvas is applied to it, so a fresh
    odin lists none — not an error, and exit 0 either way.
    """
    body = http.body_or_fail(http.request("GET", url, "/envs"))
    http.emit(body, output, _render_envs)


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
