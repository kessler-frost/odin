"""Shared HTTP plumbing for the odin control-surface CLI.

Every server-backed command funnels through `request()`, so the "server not
running" experience is uniform: a friendly pointer to `odin start` on stderr
and exit code 2. API-level refusals (409 busy / superseded / tofu-not-
installed, and anything FastAPI itself refuses) funnel through
`body_or_fail()`: stderr + exit code 1. Output is two-mode everywhere --
`emit()` prints raw JSON to stdout (nothing else) in JSON mode, or delegates
to the command's own text renderer.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

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

# For commands whose OUTPUT IS a document rather than a report. `odin canvas
# get` defaulted to a one-line summary, so the README's own
# `odin canvas get | jq …` example died on the first pipe (fresh-user
# FRICTION-1) -- a default that breaks the documented use of the command.
JSON_OUTPUT = typer.Option(
    OutputFormat.json, "-o", "--output",
    help="Output format: raw JSON on stdout (the default — this command's output is a document), or `-o text` for a summary.",
)


def fail(message: str, code: int = 1) -> typer.Exit:
    """Echo `message` to stderr and return an Exit for the caller to raise."""
    typer.echo(message, err=True)
    return typer.Exit(code)


# The exceptions httpx raises for a URL it cannot even ATTEMPT a connection
# with -- both PROBED against the installed httpx rather than assumed, because
# their class hierarchy is the whole trap here:
#
#   httpx.request("GET", "localhost:4200/world")
#     -> UnsupportedProtocol("Request URL is missing an 'http://' or 'https://' protocol.")
#   httpx.request("GET", "http://localhost:notaport/world")
#     -> InvalidURL("Invalid port: 'notaport'")
#
# `UnsupportedProtocol` is a `TransportError`, so it slips past
# `except (ConnectError, ConnectTimeout)` above; `InvalidURL` subclasses plain
# `Exception` and is NOT an `httpx.HTTPError` at all, so it slips past
# `except httpx.HTTPError` too -- which is how `odin status` kept its own hole
# after `request()` was fixed. Field test 6 F9: `ODIN_URL=localhost:4520 odin
# envs` printed a rich httpx traceback and exited 1. Re-measured here against a
# live server before the fix: `ODIN_URL=localhost:4720 odin world` -> exit 1
# with 176 lines on stderr (the report said ~90) and 0 bytes on stdout; `odin
# status --url http://localhost:notaport` -> exit 1 with 140 lines, out of
# `_reconciler_health`, a second site the field test never reached.
URL_FAULTS = (httpx.UnsupportedProtocol, httpx.InvalidURL)


def url_fault_reason(url: str, exc: Exception) -> str:
    """Why `url` could not be used, in httpx's OWN words.

    One spelling for every command that takes a `--url`/`ODIN_URL`: `request()`
    below and `odin status`'s health probe both say this, so a user cannot be
    told two different things about one malformed value. httpx's message is
    quoted rather than re-derived -- it is the component that rejected the URL,
    and re-implementing its parsing here is how a diagnosis drifts away from
    the thing it diagnoses."""
    return f"{url!r} is not a usable odin URL: {exc}"


def request(
    method: str, url: str, path: str,
    params: dict | None = None, body: dict | None = None, unreachable_code: int = 2,
) -> httpx.Response:
    """One round-trip to the odin server; a connection failure exits 2.

    `unreachable_code` overrides that 2 for the one command whose own exit
    codes need it: `odin tf plan` mirrors `tofu plan -detailed-exitcode`,
    where 2 already means "changes present" -- a down server must not be
    able to masquerade as drift in a CI gate, so it exits 3 there.

    A URL httpx will not even dial (`URL_FAULTS`) takes the SAME exit as an
    unreachable one, and for the same reason: the README's contract already
    reads "2 a usage error or an unreachable server", and for `tf plan` a 2
    would let a typo'd `ODIN_URL` masquerade as drift exactly the way a down
    server would."""
    try:
        return httpx.request(
            method, f"{url.rstrip('/')}{path}", params=params, json=body, timeout=_TIMEOUT
        )
    except URL_FAULTS as exc:
        raise fail(
            f"{url_fault_reason(url, exc)} Pass a full base URL like {DEFAULT_URL} "
            "with --url or ODIN_URL.", unreachable_code
        ) from None
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise fail(
            f"Could not reach odin server at {url} — is it running? Try `odin start`.", unreachable_code
        ) from None


# How much of an unrecognised body to quote back. Enough to carry a real
# message, short enough that a 422 echoing the whole canvas back (FastAPI's
# `input` field) cannot bury it.
_SNIPPET = 400


def _detail_line(item: object) -> str:
    """One entry of FastAPI's `detail`, as a sentence a person can act on.

    A 422 carries a LIST of per-field errors: `msg` is the message, `loc`
    names the field it is about (its first element is always the request part
    -- "body" -- which tells the user nothing), and `input` echoes the whole
    document back, which is exactly what must not be printed. Pydantic
    prefixes a validator's own ValueError with "Value error, ", noise in
    front of a message written for a human. An HTTPException's `detail` is a
    plain string and passes straight through."""
    if not isinstance(item, dict):
        return str(item)
    location = " -> ".join(str(part) for part in list(item.get("loc") or [])[1:])
    message = str(item.get("msg", item)).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


def _refusal(response: httpx.Response, body: object) -> str:
    """What the server said, for a response no renderer can be handed."""
    detail = body.get("detail") if isinstance(body, dict) else None
    detail = json.dumps(body)[:_SNIPPET] if detail is None else detail
    lines = detail if isinstance(detail, list) else [detail]
    return (
        f"odin server refused this request (HTTP {response.status_code}): "
        + "; ".join(_detail_line(line) for line in lines)
    )


def _renderable(body: object) -> bool:
    """Whether an ERROR response's body is still one the caller can use.

    Two shapes qualify, and both are odin's own. `error` is the refusal
    convention every route in `server.py` uses (409 busy, the 403 CSRF
    rejection, a failed `/destroy`), which `body_or_fail` below turns into
    the message. `status` is an odin payload proper: `/tf/plan` and
    `/tf/destroy` answer 500 with the very tofu tail their commands print.
    A body with neither came from FastAPI, not from a route."""
    return isinstance(body, dict) and bool(body.get("error") or "status" in body)


def parsed_or_fail(response: httpx.Response) -> Any:
    """The parsed body of a response a caller can actually render.

    Anything else -- a body that is not JSON at all (a 500's bare "Internal
    Server Error", a proxy's HTML) or an error status carrying FastAPI's own
    refusal document -- ends here, on stderr, with exit code 1.

    Field test 5: v0.7.4 taught the server to answer a malformed canvas with
    422 and a genuinely useful message (`node[0] ('s3-1'): data.label must be
    a string, not a list`), and the CLI showed none of it. This function only
    looked at `error`, so the validation document sailed through into the
    renderer, which died on `KeyError: 'status'` after 39 lines of Rich
    traceback -- and in `-o json` mode printed the 422 to STDOUT as if it were
    an apply result. `odin apply --file` and `odin translate --file` POST a
    file directly, bypassing the client-side check `odin canvas set` runs, so
    every posting command could reach it.

    An error status alone is NOT the test -- see `_renderable`: odin's own
    refusals and honest-failure payloads ride on 403/409/500 and stay the
    caller's to render (and to exit non-zero on)."""
    try:
        body = response.json()
    except ValueError:
        raise fail(
            f"odin server returned HTTP {response.status_code} with a non-JSON body: "
            f"{response.text.strip()[:_SNIPPET]}"
        ) from None
    if response.is_success or _renderable(body):
        return body
    raise fail(_refusal(response, body))


def body_or_fail(response: httpx.Response, output: OutputFormat | None = None) -> dict:
    """`parsed_or_fail`, plus odin's own refusal convention: a truthy `error`
    (409 busy / superseded / tofu-not-installed / a failed `/destroy` /
    GET /logs's "no such node") goes to stderr with exit code 1. Checked by
    VALUE, not just key presence: a typed Pydantic response model (GET
    /logs's `LogsResponse`) always serializes an `error` key, `null` on the
    success path -- a presence-only check would wrongly treat every one of
    those as a failure.

    `output` is what the command was asked to PRINT, and passing it is what
    keeps `-o json` machine-readable on the failure path (field test 6 F7).
    `odin destroy -o json` on a failed destroy wrote ZERO BYTES to stdout: the
    server's 500 body carries `still_standing.tf_state`,
    `still_standing.containers` and a `tf.tail` with the correct diagnosis, and
    all of it died here because this function only ever reached `fail`. So
    `odin destroy -o json | jq .status` got a parse error and the best
    diagnostic odin produced never reached anyone. The one-line `error` still
    goes to stderr, and the exit code is still 1 -- a JSON body on stdout is
    the ANSWER, never the verdict.

    Deliberately narrower than "any error response": this runs only AFTER
    `parsed_or_fail` has established the body is odin's OWN payload. FastAPI's
    422 validation document still prints nothing to stdout, because a `| jq`
    pipeline reading that as an apply result is the field-test-5 bug and
    `parsed_or_fail` exists to stop it."""
    body = parsed_or_fail(response)
    if not body.get("error"):
        return body
    if output is OutputFormat.json:
        echo_json(body)
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
