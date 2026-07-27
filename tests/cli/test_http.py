"""What every command does with a response no renderer can be handed.

Field test 5: v0.7.4 taught the server to refuse a malformed canvas with 422
and a genuinely useful message, and the CLI showed none of it -- `body_or_fail`
only inspected `error`, so FastAPI's `detail` document sailed through into the
renderer and died there (`KeyError: 'status'`, 39 lines of Rich traceback),
after printing the 422 to STDOUT in `-o json` mode as if it were an apply
result. The bodies below are VERBATIM what the real server answers -- captured
from `POST /apply-full` and `POST /translate` on a running odin.

Every command that POSTs a body is covered, because every one of them reaches
this: `apply --file` and `translate --file` post a file directly, bypassing
the client-side check `odin canvas set` runs.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from odin.cli.app import app
from tests.cli.conftest import BASE

GRAPH = {"nodes": [{"id": "s3-1", "type": "s3", "data": {"label": ["not", "a", "string"]}}], "edges": []}

# The real 422 from `POST /apply-full` with the canvas above, verbatim --
# including `input`, which echoes the WHOLE document back and must never be
# printed, and pydantic's "Value error, " prefix in front of the message
# `spec/translate.canvas_problems` actually wrote.
MALFORMED_422 = {
    "detail": [{
        "type": "value_error",
        "loc": ["body"],
        "msg": "Value error, this canvas cannot be applied — node[0] ('s3-1'): "
               "data.label must be a string, not a list",
        "input": GRAPH,
        "ctx": {"error": {}},
    }]
}
# ...and the plain-pydantic shape, where `loc` names the field: `{"nodes":
# "oops", "edges": 3}`.
FIELD_422 = {
    "detail": [
        {"type": "list_type", "loc": ["body", "nodes"], "msg": "Input should be a valid list", "input": "oops"},
        {"type": "list_type", "loc": ["body", "edges"], "msg": "Input should be a valid list", "input": 3},
    ]
}
SERVER_MESSAGE = "node[0] ('s3-1'): data.label must be a string, not a list"


@pytest.fixture
def canvas_file(tmp_path):
    path = tmp_path / "c-malformed.json"
    path.write_text(json.dumps(GRAPH))
    return str(path)


@respx.mock
@pytest.mark.parametrize("output", [[], ["-o", "json"]])
def test_apply_of_a_malformed_canvas_shows_the_servers_message(runner, canvas_file, output):
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(422, json=MALFORMED_422))
    result = runner.invoke(app, ["apply", "--env", "f5bad", "--file", canvas_file, *output])
    assert result.exit_code == 1
    assert SERVER_MESSAGE in result.stderr
    assert "Traceback" not in result.stderr and "KeyError" not in result.stderr
    # ...and NOTHING on stdout: in JSON mode the 422 document used to land
    # there, where a `| jq` pipeline reads it as the apply's own result.
    assert result.stdout == ""


@respx.mock
@pytest.mark.parametrize("output", [[], ["-o", "json"]])
def test_translate_of_a_malformed_canvas_shows_the_servers_message(runner, canvas_file, output):
    respx.post(f"{BASE}/translate").mock(return_value=httpx.Response(422, json=MALFORMED_422))
    result = runner.invoke(app, ["translate", "--file", canvas_file, *output])
    assert result.exit_code == 1
    assert SERVER_MESSAGE in result.stderr
    assert "KeyError" not in result.stderr
    assert result.stdout == ""


@respx.mock
def test_a_422_never_echoes_the_document_back(runner, canvas_file):
    """`input` carries the entire canvas -- a whole file quoted back at the
    user buries the one sentence that says what is wrong with it."""
    respx.post(f"{BASE}/apply-full").mock(return_value=httpx.Response(422, json=MALFORMED_422))
    result = runner.invoke(app, ["apply", "--file", canvas_file])
    assert "not a string" not in result.stderr  # the input echo
    assert "Value error," not in result.stderr  # pydantic's own prefix
    assert result.stderr.count("\n") == 1  # one line, not 39


@respx.mock
def test_field_level_422_names_each_field(runner, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"nodes": "oops", "edges": 3}))
    respx.post(f"{BASE}/translate").mock(return_value=httpx.Response(422, json=FIELD_422))
    result = runner.invoke(app, ["translate", "--file", str(path)])
    assert result.exit_code == 1
    assert "nodes: Input should be a valid list" in result.stderr
    assert "edges: Input should be a valid list" in result.stderr


@respx.mock
def test_canvas_set_shows_a_server_side_refusal_too(runner, tmp_path):
    """`canvas set` pre-validates with the server's own `canvas_problems`, so
    it rarely gets here -- but "rarely" is not "never" (the client check is
    deliberately narrower than the model), and the previous agent's "this is
    unreachable" call is exactly what left `apply --file` broken."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"nodes": [], "edges": []}))
    respx.post(f"{BASE}/canvas").mock(return_value=httpx.Response(422, json=FIELD_422))
    result = runner.invoke(app, ["canvas", "set", str(path)])
    assert result.exit_code == 1
    assert "Input should be a valid list" in result.stderr
    assert result.stdout == ""


@respx.mock
def test_import_tf_shows_a_server_side_refusal_too(runner, tmp_path):
    path = tmp_path / "main.tf"
    path.write_text('resource "aws_s3_bucket" "b" {}')
    respx.post(f"{BASE}/import-tf").mock(
        return_value=httpx.Response(422, json={"detail": [
            {"loc": ["body", "hcl"], "msg": "Input should be a valid string", "input": None},
        ]})
    )
    result = runner.invoke(app, ["import-tf", str(path)])
    assert result.exit_code == 1
    assert "hcl: Input should be a valid string" in result.stderr
    assert result.stdout == ""


@respx.mock
def test_a_string_detail_passes_straight_through(runner):
    """FastAPI's other refusal shape: HTTPException/404, where `detail` is a
    plain string rather than a list of field errors."""
    respx.get(f"{BASE}/world").mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    result = runner.invoke(app, ["world"])
    assert result.exit_code == 1
    assert "HTTP 404" in result.stderr and "Not Found" in result.stderr


@respx.mock
def test_a_non_json_body_is_a_message_not_a_traceback(runner):
    """An unhandled server exception answers `Internal Server Error` as PLAIN
    TEXT; `response.json()` on that is a JSONDecodeError in the user's face."""
    respx.get(f"{BASE}/world").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    result = runner.invoke(app, ["world"])
    assert result.exit_code == 1
    assert "HTTP 500" in result.stderr and "Internal Server Error" in result.stderr
    assert "JSONDecodeError" not in result.stderr


@respx.mock
def test_events_fails_cleanly_on_a_refusal(runner):
    """`odin events` renders a JSON ARRAY, so it has no `error` field to key
    on and bypassed the guard entirely -- a refusal reached its renderer as a
    document it cannot iterate."""
    respx.get(f"{BASE}/events").mock(return_value=httpx.Response(422, json=FIELD_422))
    result = runner.invoke(app, ["events"])
    assert result.exit_code == 1
    assert "HTTP 422" in result.stderr
    assert result.stdout == ""


@respx.mock
def test_odins_own_refusal_convention_is_untouched(runner):
    """The regression guard for the fix itself: a 409/403 body carrying
    `error` (+ `fix`) is odin's own, and still renders as its own sentence --
    NOT as a generic "the server refused this" wrapper."""
    respx.post(f"{BASE}/tf/destroy").mock(return_value=httpx.Response(
        409, json={"error": "tofu not installed", "fix": "brew install opentofu"}
    ))
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "tofu not installed — brew install opentofu"


@respx.mock
def test_an_odin_payload_on_a_500_still_reaches_its_renderer(runner):
    """The other half of that guard: `/tf/destroy` answers 500 with the tofu
    tail its command prints. An error STATUS is not the test -- the body is."""
    respx.post(f"{BASE}/tf/destroy").mock(return_value=httpx.Response(500, json={
        "status": "failed", "env": "default", "exit_code": 1, "tail": ["Error: bucket not empty"],
    }))
    result = runner.invoke(app, ["tf", "destroy"])
    assert result.exit_code == 1
    assert "status: failed" in result.stdout
    assert "Error: bucket not empty" in result.stdout


# --- field test 6 F9: a URL httpx will not even dial -------------------------
#
# `ODIN_URL=localhost:4520 odin envs` raised ~90 lines of httpx traceback and
# exited 1. The two real exceptions -- PROBED against the installed httpx, and
# neither reachable from `except (ConnectError, ConnectTimeout)`:
#   'localhost:4720'            -> UnsupportedProtocol (a TransportError)
#   'http://localhost:notaport' -> InvalidURL (NOT an httpx.HTTPError at all)
#
# No respx mock: the point is that nothing is ever dialled, so there is nothing
# to mock. These call the REAL httpx with the REAL bad value.

SCHEMELESS = "localhost:4720"
BAD_PORT = "http://localhost:notaport"

# EVERY command that takes a `--url` (i.e. every one that makes a request) --
# `grep 'url: str = http.URL' src/odin/cli/*.py`, minus `tf plan`, whose exit is
# 3 by design and gets its own test below. The fix belongs where they all meet
# (`http.request`), and this is what proves it is not a patch on the one command
# that happened to be reported. `keys issue` is absent on purpose: it is fully
# offline and takes no URL at all.
URL_COMMANDS = (
    ["envs"], ["world"], ["events"], ["logs", "n1"], ["apply"], ["destroy"],
    ["canvas", "get"], ["tf", "status"], ["tf", "destroy"],
)


@pytest.mark.parametrize("command", URL_COMMANDS)
@pytest.mark.parametrize("url", [SCHEMELESS, BAD_PORT])
def test_a_url_httpx_cannot_dial_is_one_line_and_exit_two(runner, command, url):
    result = runner.invoke(app, [*command, "--url", url])
    assert result.exit_code == 2, f"{command} with {url!r}: {result.stdout}{result.stderr}"
    assert "Traceback" not in result.stderr
    assert "UnsupportedProtocol" not in result.stderr and "InvalidURL" not in result.stderr
    assert result.stderr.count("\n") == 1, result.stderr
    assert repr(url) in result.stderr
    assert "--url or ODIN_URL" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("command", [["canvas", "set"], ["translate", "--file"], ["import-tf"]])
def test_the_file_posting_commands_reach_it_too(runner, tmp_path, command):
    """The three commands that read a file before they dial. They must refuse on
    the URL rather than after -- and none of them may print a traceback."""
    path = tmp_path / ("main.tf" if command[0] == "import-tf" else "c.json")
    path.write_text('resource "aws_s3_bucket" "b" {}' if command[0] == "import-tf" else '{"nodes": [], "edges": []}')
    result = runner.invoke(app, [*command, str(path), "--url", SCHEMELESS])
    assert result.exit_code == 2, result.stderr
    assert repr(SCHEMELESS) in result.stderr
    assert "Traceback" not in result.stderr


def test_the_message_names_what_is_actually_wrong_with_the_url(runner):
    """httpx's own sentence, quoted rather than re-derived -- it is the component
    that rejected the URL. "Could not reach odin server ... Try `odin start`"
    would send the user to start a server that is very likely already up."""
    missing_scheme = runner.invoke(app, ["world", "--url", SCHEMELESS]).stderr
    assert "missing an 'http://' or 'https://' protocol" in missing_scheme
    assert "odin start" not in missing_scheme

    bad_port = runner.invoke(app, ["world", "--url", BAD_PORT]).stderr
    assert "Invalid port: 'notaport'" in bad_port


def test_the_env_var_reaches_it_the_same_way_the_flag_does(runner):
    """The reported form was the environment variable, not the flag."""
    result = runner.invoke(app, ["envs"], env={"ODIN_URL": SCHEMELESS})
    assert result.exit_code == 2
    assert repr(SCHEMELESS) in result.stderr


def test_tf_plan_answers_three_so_a_bad_url_cannot_look_like_drift(runner):
    """`tf plan`'s 2 already means "changes present", so the malformed-URL exit
    rides on the same `unreachable_code` a down server does -- for the identical
    reason. A flat 2 here would let a typo'd ODIN_URL pass a CI drift gate as
    real drift."""
    result = runner.invoke(app, ["tf", "plan", "--url", SCHEMELESS])
    assert result.exit_code == 3
    assert repr(SCHEMELESS) in result.stderr
