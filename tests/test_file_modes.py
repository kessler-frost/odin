"""Field-test finding (v0.7.0, agent A "B2"): the files that actually hold
every secret were world-readable (0644).

SECURITY.md rests its entire secrets argument on the file mode ("`SecureString`
buys you the file mode and nothing else"), so a 0644 file carrying secret
plaintext doesn't just leak — it voids the document. The v0.6.0 pass locked the
files it knew about (canvas.json, stack revisions, world.json, keys.json, the
gateway sidecars); the field test found the rest: `tf/main.tf`,
`tf/terraform.tfstate`(+`.backup`), `events.jsonl`, and the export archive.

This test is the standing guard, and it is deliberately WHOLE-STORE rather than
per-writer: it builds an env store the way a real Apply does (every writer
below is the production one, no hand-written files), plants known canary
secrets, then walks the finished tree and asserts that NOTHING containing a
canary is readable by group or other. A new writer that forgets its mode fails
here without anyone having to remember to add a case.

`terraform.tfstate` is tofu's file, not odin's: `_rewrite_in_place` below is
exactly what OpenTofu's local state manager does (open the existing path
O_RDWR, truncate, write — no rename), which is why pre-creating it 0600 in
`workspace.materialize()` is durable across every apply. If that ever stopped
being true, this test's state-file assertions would be the thing that noticed.
"""
from __future__ import annotations

import asyncio
import stat
import tarfile
from pathlib import Path

from odin.agent.hcl import TfProject
from odin.api.ws import ConnectionManager
from odin.backup import export_env
from odin.gateway.keys import KeyStore
from odin.simulate import workspace
from odin.spec.models import FieldValue, ResourceDesired, Stack, World, WorldDelta
from odin.spec.store import SpecStore
from odin.util import hold_store_lock

ENV = "modes"

# Distinct, greppable stand-ins for the four real secret shapes the field test
# followed through the store: an rds password, a `secret` node's value, a
# gateway-issued workload credential, and a resolved DATABASE_URL.
RDS_PASSWORD = "rdsCanary001"
SECRET_VALUE = "secretCanary002"
WORKLOAD_KEY = "workloadSecretCanary003"
CANARIES = (RDS_PASSWORD, SECRET_VALUE, WORKLOAD_KEY)


def _stack() -> Stack:
    return Stack(
        env=ENV,
        resources=(
            ResourceDesired(
                id="db", kind="rds",
                fields={"password": FieldValue(value=RDS_PASSWORD, sensitive=True)},
            ),
            ResourceDesired(
                id="app-secret", kind="secret",
                fields={"secretString": FieldValue(value=SECRET_VALUE, sensitive=True)},
            ),
        ),
    )


def _rewrite_in_place(path: Path, text: str) -> None:
    """What OpenTofu's local state manager does to `terraform.tfstate` on every
    apply: reopen the SAME inode read/write, truncate, write. Creation mode is
    whatever created the file first — which is the whole basis of the fix."""
    with path.open("r+") as f:
        f.truncate(0)
        f.write(text)


def _build_store(root: Path) -> Path:
    """One env store, written by the real writers only — the closest thing to a
    post-Apply `.odin/<env>/` that needs neither Colima nor tofu."""
    store = SpecStore(root)
    store.apply(_stack())
    store.write_world(World(
        env=ENV,
        resources=(),
    ))
    # The rds DATABASE_URL fact embeds the password verbatim (the Fabric
    # resolves refs out of it, so it can't be redacted) — the field test found
    # it in world.json AND, three times over, in events.jsonl.
    delta = WorldDelta(
        env=ENV, resource_id="db", kind="rds", phase="healthy",
        facts={"DATABASE_URL": f"postgresql://app:{RDS_PASSWORD}@127.0.0.1:5432/appdb"},
        verdict=f"docker run -e AWS_SECRET_ACCESS_KEY={WORKLOAD_KEY} …",
    )
    store.apply_delta(delta)
    asyncio.run(ConnectionManager(root).broadcast(delta.model_dump()))

    KeyStore(root).issue(ENV, "db")

    project = TfProject(
        files={"main.tf": f'resource "aws_db_instance" "db" {{\n  password = "{RDS_PASSWORD}"\n}}\n'},
        binary_files={"fn.zip": b"PK\x03\x04" + SECRET_VALUE.encode()},
    )
    tf = workspace.materialize(root, ENV, project)
    # tofu's own writes, at tofu's own timing: state after the first apply, the
    # backup after the second.
    _rewrite_in_place(tf / "terraform.tfstate", f'{{"password": "{RDS_PASSWORD}"}}')
    _rewrite_in_place(tf / "terraform.tfstate.backup", f'{{"password": "{SECRET_VALUE}"}}')
    return root / ENV


def _leaky(paths: list[Path]) -> list[str]:
    return [
        f"{p} mode={oct(stat.S_IMODE(p.stat().st_mode))}"
        for p in paths
        if stat.S_IMODE(p.stat().st_mode) & 0o077
    ]


def _carrying_a_canary(root: Path) -> list[Path]:
    return [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and any(c.encode() in p.read_bytes() for c in CANARIES)
    ]


def test_no_secret_bearing_file_is_group_or_world_readable(tmp_path: Path):
    env_dir = _build_store(tmp_path / ".odin")
    tainted = _carrying_a_canary(env_dir)
    # Non-vacuity: the walk must actually FIND the files the field test found,
    # or a passing mode assertion means nothing.
    names = {p.relative_to(env_dir).as_posix() for p in tainted}
    assert {"world.json", "events.jsonl", "tf/main.tf", "tf/terraform.tfstate",
            "tf/terraform.tfstate.backup", "tf/fn.zip"} <= names, names
    assert any(n.startswith("stacks/") for n in names), names
    assert _leaky(tainted) == []


def test_env_and_store_directories_are_not_group_or_world_readable(tmp_path: Path):
    """Defense in depth for anything a future writer forgets: without traverse
    permission on `.odin/<env>/` the mode of a file inside it stops mattering."""
    root = tmp_path / ".odin"
    env_dir = _build_store(root)
    assert _leaky([root, env_dir, env_dir / "tf", env_dir / "stacks"]) == []


def test_a_directory_another_writer_left_loose_is_tightened(tmp_path: Path):
    """Fresh-user MISLEAD-4: SECURITY.md says "the directories holding them are
    0700" without qualification, and `.odin/` and `.odin/<env>/` were 0755 —
    while `.odin/default/` was 0700. Two code paths, one claim, and the claim
    lost: whichever writer got there first decided the mode, and the goaws
    config writer and `odin start`'s pidfile dir both used a plain `mkdir`.

    So the store is reproduced in exactly that order — the loose directories
    already in place — and must come out matching the document anyway."""
    root = tmp_path / ".odin"
    (root / ENV).mkdir(parents=True)  # a plain mkdir under umask 022: 0755
    assert _leaky([root, root / ENV]) != []  # non-vacuity: they really are loose
    env_dir = _build_store(root)
    hold_store_lock(root).release()  # what the server's lifespan does on startup
    assert _leaky([root, env_dir, env_dir / "tf", env_dir / "stacks"]) == []


def test_export_archive_and_its_restored_files_are_owner_only(tmp_path: Path):
    """`odin export` prints "treat it like a private key file" and the archive
    holds keys.json in cleartext — so the archive itself, and every file a
    restore lays back down, are owner-only too."""
    root = tmp_path / ".odin"
    _build_store(root)
    archive = tmp_path / "export.tar.gz"
    archive.write_bytes(b"stale world-readable file")
    archive.chmod(0o644)  # an existing loose file must be re-tightened, not inherited
    export_env(root, ENV, archive)
    assert _leaky([archive]) == []
    with tarfile.open(archive, "r:gz") as tar:
        loose = [m.name for m in tar.getmembers() if m.isfile() and m.mode & 0o077]
    assert loose == []
