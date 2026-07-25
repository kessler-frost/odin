"""Shared HTTP plumbing for the odin control-surface CLI.

Every server-backed command funnels through `request()`, so the "server not
running" experience is uniform: a friendly pointer to `odin start` on stderr
and exit code 2. API-level refusals (409 busy / superseded / tofu-not-
installed) funnel through `body_or_fail()`: stderr + exit code 1. Output is
two-mode everywhere -- `emit()` prints raw JSON to stdout (nothing else) in
JSON mode, or delegates to the command's own text renderer.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import httpx
import typer

DEFAULT_URL = "http://localhost:4200"

# Long tofu applies can stream through these calls -- no read deadline, only
# a connect one (the connect failure is what `request()` translates for
# users; a slow apply is not an error).
_TIMEOUT = httpx.Timeout(None, connect=10.0)

URL = typer.Option(
    DEFAULT_URL, "--url", envvar="ODIN_URL", help="Base URL of the running odin server."
)
ENV = typer.Option("default", "--env", help="Target environment.")


class OutputFormat(str, Enum):
    text = "text"
    json = "json"


OUTPUT = typer.Option(
    OutputFormat.text, "-o", "--output",
    help="Output format: human-readable text, or raw JSON on stdout for piping.",
)


def fail(message: str, code: int = 1) -> typer.Exit:
    """Echo `message` to stderr and return an Exit for the caller to raise."""
    typer.echo(message, err=True)
    return typer.Exit(code)


def request(
    method: str, url: str, path: str,
    params: dict | None = None, body: dict | None = None, unreachable_code: int = 2,
) -> httpx.Response:
    """One round-trip to the odin server; a connection failure exits 2.

    `unreachable_code` overrides that 2 for the one command whose own exit
    codes need it: `odin tf plan` mirrors `tofu plan -detailed-exitcode`,
    where 2 already means "changes present" -- a down server must not be
    able to masquerade as drift in a CI gate, so it exits 3 there."""
    try:
        return httpx.request(
            method, f"{url.rstrip('/')}{path}", params=params, json=body, timeout=_TIMEOUT
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise fail(
            f"Could not reach odin server at {url} — is it running? Try `odin start`.", unreachable_code
        ) from None


def body_or_fail(response: httpx.Response) -> dict:
    """The parsed body; a truthy `error` (409 busy / superseded /
    tofu-not-installed / GET /logs's "no such node") goes to stderr with
    exit code 1 instead. Checked by VALUE, not just key presence: a typed
    Pydantic response model (GET /logs's `LogsResponse`) always serializes
    an `error` key, `null` on the success path -- a presence-only check
    would wrongly treat every one of those as a failure."""
    body = response.json()
    if not body.get("error"):
        return body
    raise fail(" — ".join(str(part) for part in (body["error"], body.get("fix")) if part))


def emit(body: object, output: OutputFormat, render: Callable[[dict], None]) -> None:
    """JSON mode: raw JSON to stdout, nothing else. Text mode: `render`."""
    echo_json(body) if output is OutputFormat.json else render(body)


def echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload))


def read_text_arg(file: str) -> str:
    """The file's text, or stdin when `file` is `-`."""
    return sys.stdin.read() if file == "-" else Path(file).read_text()


def parse_json_arg(text: str, source: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise fail(f"{source} is not valid JSON: {exc}") from None
