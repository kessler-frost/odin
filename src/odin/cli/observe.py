"""`odin world` / `odin envs` / `odin events` — read-only inspection.

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


@app.command()
def envs(url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """List the environments the server knows about."""
    body = http.body_or_fail(http.request("GET", url, "/envs"))
    http.emit(body, output, lambda b: typer.echo("\n".join(b["envs"])))


def _render_events(events_list: list[dict]) -> None:
    for event in events_list:
        http.echo_json(event)


@app.command()
def events(env: str = http.ENV, url: str = http.URL, output: OutputFormat = http.OUTPUT) -> None:
    """The env's durable event log (world deltas, tf runs, denials) — one line per event."""
    body = http.request("GET", url, "/events", params={"env": env}).json()
    http.emit(body, output, _render_events)
