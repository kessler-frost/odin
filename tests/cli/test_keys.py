"""`odin keys issue` — the local, offline escape hatch (no httpx at all):
it operates straight on `.odin/<env>/keys.json` under the current directory."""
from __future__ import annotations

import json
from pathlib import Path

from odin.cli.app import app
from odin.gateway.keys import KeyStore


def test_keys_issue_text_mode(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["keys", "issue", "web", "--env", "prod"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("AWS_ACCESS_KEY_ID=AKODIN")
    assert lines[1].startswith("AWS_SECRET_ACCESS_KEY=")
    # the pair landed in the same store the gateway reads
    assert (tmp_path / ".odin" / "prod" / "keys.json").exists()


def test_keys_issue_json_mode_matches_keystore(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["keys", "issue", "web", "-o", "json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    access_key, secret_key = KeyStore(Path(".odin")).issue("default", "web")
    assert body == {"access_key": access_key, "secret_key": secret_key}


def test_keys_issue_is_stable_per_env_node(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["keys", "issue", "web", "-o", "json"])
    second = runner.invoke(app, ["keys", "issue", "web", "-o", "json"])
    assert json.loads(first.stdout) == json.loads(second.stdout)
    other_node = runner.invoke(app, ["keys", "issue", "worker", "-o", "json"])
    assert json.loads(other_node.stdout) != json.loads(first.stdout)


def test_keys_issue_help_documents_the_escape_hatch(runner):
    result = runner.invoke(app, ["keys", "issue", "--help"])
    assert result.exit_code == 0
    flat = " ".join(result.stdout.lower().split())  # rich wraps at 80 cols
    assert "escape hatch" in flat
    assert "bypasses the server" in flat
    assert "keys.json" in flat
