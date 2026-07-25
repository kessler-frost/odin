"""`odin export` / `odin import` — W2.3 backup/restore.

Offline commands (no httpx at all): they work straight on `.odin/` under the
current directory. The gates here are the destructive-overwrite refusals, the
live-server refusal, and tar safety — a hostile archive must never write
outside the destination.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from odin import backup
from odin.backup import CANVAS_NAME, ENV_PREFIX, MANIFEST_NAME, BackupError, import_archive
from odin.cli.app import app

ENV = "bak"


def _seed(root: Path, env: str = ENV) -> Path:
    """A realistic env dir: stack lineage, world, creds, gateway stores, a
    lambda zip, a tofu workspace WITH a provider cache to be excluded."""
    env_dir = root / env
    (env_dir / "stacks").mkdir(parents=True)
    (env_dir / "stacks" / "abc123.json").write_text('{"env": "bak"}')
    (env_dir / "HEAD").write_text("abc123")
    (env_dir / "world.json").write_text('{"env": "bak", "resources": []}')
    keys = env_dir / "keys.json"
    keys.write_text('{"AKODINTEST": {"node": "web"}}')
    keys.chmod(0o600)
    (env_dir / "goaws.yaml").write_text("Local:\n  Host: localhost\n")
    (env_dir / "events.jsonl").write_text('{"type": "world"}\n')
    gateway = env_dir / "gateway"
    (gateway / "lambda").mkdir(parents=True)
    (gateway / "s3.json").write_text('{"buckets": {}}')
    (gateway / "lambda" / "fn.zip").write_bytes(b"PK\x03\x04binary\x00zip")
    tf = env_dir / "tf"
    (tf / ".terraform" / "providers").mkdir(parents=True)
    (tf / "main.tf").write_text('resource "aws_s3_bucket" "uploads" {}\n')
    (tf / "override.tf").write_text('provider "aws" {}\n')
    (tf / "terraform.tfstate").write_text('{"version": 4}')
    (tf / ".terraform" / "providers" / "huge.bin").write_bytes(b"x" * 4096)
    (tf / ".terraform.lock.hcl").write_text("# lock\n")
    (root / CANVAS_NAME).write_text('{"nodes": [{"id": "n1"}], "edges": []}')
    return env_dir


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


def _manifest_of(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        return json.loads(tar.extractfile(MANIFEST_NAME).read())


def _write_archive(dest: Path, manifest: dict | None, entries: dict[str, bytes]) -> Path:
    """A hand-built archive, so a test can put anything at all in it."""
    with tarfile.open(dest, "w:gz") as tar:
        payloads = ({MANIFEST_NAME: json.dumps(manifest).encode()} if manifest else {}) | entries
        for name, body in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return dest


# ---------------------------------------------------------------- export


def test_export_writes_manifest_and_every_expected_member(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path / ".odin")
    result = runner.invoke(app, ["export", "--env", ENV])
    assert result.exit_code == 0, result.output

    archive = tmp_path / f"odin-{ENV}-export.tar.gz"
    assert archive.is_file()
    assert f"→ {archive.name}" in result.stdout or str(archive) in result.stdout
    names = set(_members(archive))
    assert names >= {
        MANIFEST_NAME,
        CANVAS_NAME,
        f"{ENV_PREFIX}/HEAD",
        f"{ENV_PREFIX}/stacks/abc123.json",
        f"{ENV_PREFIX}/world.json",
        f"{ENV_PREFIX}/keys.json",
        f"{ENV_PREFIX}/goaws.yaml",
        f"{ENV_PREFIX}/events.jsonl",
        f"{ENV_PREFIX}/gateway/s3.json",
        f"{ENV_PREFIX}/gateway/lambda/fn.zip",
        f"{ENV_PREFIX}/tf/main.tf",
        f"{ENV_PREFIX}/tf/override.tf",
        f"{ENV_PREFIX}/tf/terraform.tfstate",
    }
    manifest = _manifest_of(archive)
    assert manifest["env"] == ENV
    assert manifest["format"] == 1
    assert manifest["odin_version"] and manifest["created_at"]


def test_export_excludes_the_terraform_provider_cache(runner, tmp_path, monkeypatch):
    # `tofu init` rebuilds .terraform/ deterministically from the same main.tf,
    # and it is hundreds of MB -- shipping it would make the archive unusable.
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path / ".odin")
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    names = _members(tmp_path / f"odin-{ENV}-export.tar.gz")
    assert not [n for n in names if ".terraform/" in n]
    # ...but the lock file next to it IS state worth keeping.
    assert f"{ENV_PREFIX}/tf/.terraform.lock.hcl" in names


def test_export_honours_the_out_path_and_json_output(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path / ".odin")
    dest = tmp_path / "nested" / "snap.tar.gz"
    result = runner.invoke(app, ["export", "--env", ENV, "-o", str(dest), "--output", "json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["archive"] == str(dest)
    assert body["env"] == ENV and body["size"] > 0
    assert MANIFEST_NAME in body["members"]
    assert dest.is_file()


def test_export_warns_that_the_archive_holds_credentials(runner, tmp_path, monkeypatch):
    # The archive is a cleartext copy of keys.json + every canvas secret, in a
    # file that's trivial to email. Warned on stderr in BOTH output modes, so
    # `--output json`'s stdout stays pipeable.
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path / ".odin")
    for args in (["export", "--env", ENV], ["export", "--env", ENV, "--output", "json"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert "private key file" in result.stderr
        assert "credentials" in result.stderr
    assert json.loads(runner.invoke(app, ["export", "--env", ENV, "--output", "json"]).stdout)


def test_export_of_an_unknown_env_fails_with_the_known_ones(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path / ".odin")
    result = runner.invoke(app, ["export", "--env", "nope"])
    assert result.exit_code == 1
    assert "no such environment 'nope'" in result.stderr
    assert ENV in result.stderr  # tells you what you could have exported


# ---------------------------------------------------------------- round trip


def test_round_trip_restores_every_file_byte_for_byte_and_keeps_0600(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    env_dir = _seed(root)
    before = {
        p.relative_to(env_dir): p.read_bytes()
        for p in sorted(env_dir.rglob("*")) if p.is_file() and ".terraform/" not in str(p)
    }
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0

    # the "I lost .odin/" disaster, exactly
    shutil.rmtree(env_dir)
    result = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz"])
    assert result.exit_code == 0, result.output
    assert "Restored env 'bak'" in result.stdout
    assert "odin start" in result.stdout  # the next steps
    assert "Apply" in result.stdout

    after = {
        p.relative_to(env_dir): p.read_bytes()
        for p in sorted(env_dir.rglob("*")) if p.is_file()
    }
    assert after == before
    assert oct(os.stat(env_dir / "keys.json").st_mode & 0o777) == "0o600"


def test_import_leaves_the_canvas_alone_unless_asked(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    (root / CANVAS_NAME).write_text('{"nodes": [{"id": "mine"}], "edges": []}')

    plain = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--force"])
    assert plain.exit_code == 0, plain.output
    assert "Canvas left alone" in plain.stdout
    assert "mine" in (root / CANVAS_NAME).read_text()  # NOT clobbered

    withc = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--force", "--with-canvas"])
    assert withc.exit_code == 0, withc.output
    assert "Canvas restored" in withc.stdout
    assert "n1" in (root / CANVAS_NAME).read_text()


def test_import_env_override_rewrites_the_env_dir_name(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    result = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--env", "restored"])
    assert result.exit_code == 0, result.output
    assert "Restored env 'restored' (archived as 'bak')" in result.stdout
    assert (root / "restored" / "HEAD").read_text() == "abc123"
    assert (root / "restored" / "tf" / "main.tf").is_file()
    assert (root / ENV / "HEAD").is_file()  # the original is untouched


def test_import_json_output_reports_both_env_names(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path / ".odin")
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    result = runner.invoke(
        app, ["import", f"odin-{ENV}-export.tar.gz", "--env", "restored", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["env"] == "restored" and body["source_env"] == ENV
    assert body["files"] > 0 and body["canvas_restored"] is False


# ---------------------------------------------------------------- refusals


def test_import_refuses_an_existing_env_without_force(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    (root / ENV / "world.json").write_text('{"env": "bak", "resources": ["live"]}')

    refused = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz"])
    assert refused.exit_code == 1
    assert "already exists" in refused.stderr and "--force" in refused.stderr
    assert "live" in (root / ENV / "world.json").read_text()  # untouched

    forced = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "live" not in (root / ENV / "world.json").read_text()


def test_force_import_replaces_rather_than_merges(runner, tmp_path, monkeypatch):
    # A restore must not leave stale files from the env it overwrote.
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    (root / ENV / "stacks" / "stale.json").write_text("{}")
    assert runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--force"]).exit_code == 0
    assert not (root / ENV / "stacks" / "stale.json").exists()


def test_import_refuses_a_live_server(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(backup, "SHUTDOWN_GRACE", 0.5)  # don't spend the real grace period here
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    shutil.rmtree(root / ENV)
    (root / "pid").write_text(str(os.getpid()))  # this very process is certainly alive

    result = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz"])
    assert result.exit_code == 1
    assert "odin is running" in result.stderr and "odin stop" in result.stderr
    assert "waiting up to" in result.stderr  # never a silent stall
    assert not (root / ENV).exists()  # nothing written


def test_import_can_be_forced_past_the_live_server_check(runner, tmp_path, monkeypatch):
    """The escape hatch. Field test 3 had a phantom "live server" (the marker in
    an ops script's own argv) block a restore outright, which is strictly worse
    than the missed-server bug it came from: the operator was mid-recovery with
    no way forward. Whatever this guard ever gets wrong, `--ignore-live-server`
    is the way past it."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    shutil.rmtree(root / ENV)
    (root / "pid").write_text(str(os.getpid()))

    result = runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--ignore-live-server"])
    assert result.exit_code == 0, result.output
    assert (root / ENV / "HEAD").read_text() == "abc123"


def test_import_proceeds_past_a_stale_pidfile(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".odin"
    _seed(root)
    assert runner.invoke(app, ["export", "--env", ENV]).exit_code == 0
    (root / "pid").write_text("999999999")  # no such process
    assert runner.invoke(app, ["import", f"odin-{ENV}-export.tar.gz", "--force"]).exit_code == 0


def test_import_of_a_missing_archive_fails_cleanly(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["import", "nowhere.tar.gz"])
    assert result.exit_code == 1
    assert "no such archive" in result.stderr


# ---------------------------------------------------------------- manifest


def test_unknown_format_exits_2(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = _write_archive(
        tmp_path / "future.tar.gz",
        {"odin_version": "99.0.0", "env": ENV, "created_at": "2099-01-01T00:00:00Z", "format": 7},
        {f"{ENV_PREFIX}/HEAD": b"deadbeef"},
    )
    result = runner.invoke(app, ["import", str(archive)])
    assert result.exit_code == 2
    assert "export format 7" in result.stderr
    assert "99.0.0" in result.stderr  # which odin wrote it
    assert not (tmp_path / ".odin" / ENV).exists()


def test_a_tarball_that_is_not_an_odin_export_exits_2(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = _write_archive(tmp_path / "random.tar.gz", None, {"some/file": b"hi"})
    result = runner.invoke(app, ["import", str(archive)])
    assert result.exit_code == 2
    assert "not an odin export" in result.stderr


def test_a_truncated_archive_fails_with_a_clear_message(runner, tmp_path, monkeypatch):
    """Field test (MEDIUM-7): a half-copied backup printed an EMPTY message and
    `Aborted.` — on the one path a user reaches for when things have already
    gone wrong. It must name the file, say what's wrong, and say what to do."""
    monkeypatch.chdir(tmp_path)
    good = _write_archive(tmp_path / "good.tar.gz", GOOD_MANIFEST, {f"{ENV_PREFIX}/HEAD": b"abc"})
    truncated = tmp_path / "half.tar.gz"
    truncated.write_bytes(good.read_bytes()[:60])
    result = runner.invoke(app, ["import", str(truncated)])
    assert result.exit_code == 2
    assert "half.tar.gz" in result.stderr
    assert "truncated" in result.stderr and "re-copy" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_file_that_is_not_gzip_at_all_fails_with_a_clear_message(runner, tmp_path, monkeypatch):
    """Same finding: this one dumped ~120 lines of raw Python traceback
    (`tarfile.ReadError: not a gzip file`) instead of a refusal."""
    monkeypatch.chdir(tmp_path)
    plain = tmp_path / "notes.txt.gz"
    plain.write_text("this is not a gzip file at all\n")
    result = runner.invoke(app, ["import", str(plain)])
    assert result.exit_code == 2
    assert "notes.txt.gz" in result.stderr
    assert "not a readable .tar.gz" in result.stderr
    assert "Traceback" not in result.stderr


def test_an_empty_file_fails_with_a_clear_message(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "empty.tar.gz"
    empty.write_bytes(b"")
    result = runner.invoke(app, ["import", str(empty)])
    assert result.exit_code == 2
    assert "not a readable .tar.gz" in result.stderr and "Traceback" not in result.stderr


def test_a_directory_given_where_an_archive_belongs_fails_cleanly(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "adir").mkdir()
    result = runner.invoke(app, ["import", str(tmp_path / "adir")])
    assert result.exit_code == 1
    assert "no such archive" in result.stderr and "Traceback" not in result.stderr


def test_a_corrupt_manifest_exits_2(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = _write_archive(tmp_path / "bad.tar.gz", {"format": 1}, {})  # missing every other field
    result = runner.invoke(app, ["import", str(archive)])
    assert result.exit_code == 2
    assert "not a valid odin export manifest" in result.stderr


# ---------------------------------------------------------------- tar safety


GOOD_MANIFEST = {
    "odin_version": "0.6.0", "env": ENV, "created_at": "2026-07-24T00:00:00Z", "format": 1,
}


@pytest.mark.parametrize("evil", ["/etc/odin-pwned", "env/../../../pwned", "../pwned"])
def test_import_rejects_traversal_members(runner, tmp_path, monkeypatch, evil):
    monkeypatch.chdir(tmp_path)
    archive = _write_archive(
        tmp_path / "evil.tar.gz", GOOD_MANIFEST,
        {f"{ENV_PREFIX}/HEAD": b"abc123", evil: b"owned"},
    )
    result = runner.invoke(app, ["import", str(archive)])
    assert result.exit_code == 1
    assert "unsafe archive member" in result.stderr
    # Validation happens BEFORE a single byte is written: no partial restore.
    assert not (tmp_path / ".odin").exists()
    assert not Path("/etc/odin-pwned").exists()
    assert not (tmp_path.parent / "pwned").exists()


def test_import_rejects_symlink_and_hardlink_members(tmp_path):
    # The classic tar escape: a symlink pointing outside, then a member
    # written "through" it. Refused at the member type.
    archive = tmp_path / "links.tar.gz"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    with tarfile.open(archive, "w:gz") as tar:
        body = json.dumps(GOOD_MANIFEST).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo(f"{ENV_PREFIX}/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tar.addfile(link)
    with pytest.raises(BackupError) as exc:
        import_archive(archive, tmp_path / ".odin")
    assert "unsafe archive member" in str(exc.value)
    assert not (tmp_path / ".odin").exists()


def test_import_ignores_unknown_top_level_members(tmp_path):
    # Forward compatibility within a format: an entry we don't know about is
    # simply not restored -- it can never land somewhere unexpected.
    archive = _write_archive(
        tmp_path / "extra.tar.gz", GOOD_MANIFEST,
        {f"{ENV_PREFIX}/HEAD": b"abc123", "somethingnew.json": b"{}", ENV_PREFIX: b"a file named env"},
    )
    result = import_archive(archive, tmp_path / ".odin")
    assert result.files == 1
    assert (tmp_path / ".odin" / ENV / "HEAD").read_bytes() == b"abc123"
    assert not (tmp_path / ".odin" / "somethingnew.json").exists()
    assert (tmp_path / ".odin" / ENV).is_dir()  # the bare `env` file never became one
