"""Append-only, content-addressed Spec Store (one lineage per environment).

Layout under `<root>/<env>/`:
- `stacks/<rev>.json`  — an immutable Stack revision (rev = sha256 of canonical JSON)
- `HEAD`               — the current rev
- `world.json`         — the latest observed World

No GC in the skeleton (revisions accumulate; fine at this scale).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from odin.spec.models import ResourceObserved, Stack, World, WorldDelta
from odin.util import atomic_write_text


def _canonical(stack: Stack) -> bytes:
    return json.dumps(
        stack.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def rev_of(stack: Stack) -> str:
    return hashlib.sha256(_canonical(stack)).hexdigest()


# What a store file IS, which is the whole of what decides its recovery. Only
# two answers, and they are opposites -- see `StoreUnreadable`.
CACHE = "cache"
DESIRED = "desired"
# The gateway's own two, so `_load` and `StoreUnreadable` serve every store odin
# keeps rather than the spec store alone -- and so one handler in server.py
# answers for all four. This module imports nothing from the gateway (only
# `spec.models` and `util`), so the gateway reaching back here closes no cycle.
CONTROL = "control"
CREDENTIALS = "credentials"


class StoreUnreadable(Exception):
    """A store file that is there and cannot be read back.

    Field test 6, F5. `world.json` overwritten with invalid UTF-8 made every
    route touching that env answer with a bare

        UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 57

    -- which names no file, because that exception carries no path -- and the
    advice bolted onto it sent the user to `odin world`, which reads the SAME
    file, fails identically, and then recommends itself. Measured on a real
    server: `GET /world` 500'd, and the env's reconciler went on ticking with
    `consecutive_failures` climbing past 20 while `world.json` stayed corrupt
    byte-for-byte, because every writer reads before it writes. Nothing heals
    it on its own.

    Raised HERE, at the read site, because this is the only frame that knows
    which file it was and what the file is FOR -- and `role` is the whole of
    the recovery:

      CACHE (`world.json`) -- observed state odin re-authors from reality.
        DELETING IT IS THE FIX, measured: on a 4-resource env, `rm world.json`
        had all four back to `healthy` with byte-identical facts one tick later
        and `consecutive_failures` back to 0. The only field held nowhere else
        is `ResourceObserved.restarts`, and nothing acts on it today (`plan()`
        never reads it; `agent/debugger.py` displays it).

      DESIRED (`HEAD`, `stacks/<rev>.json`) -- the only record of what the user
        asked for. Deleting it destroys that; it has to be restored (`odin
        import` of an `odin export` archive) or re-authored by re-applying the
        canvas.

    Server-side, `server.py::_EXCEPTION_VERDICTS` turns this into a JSON
    verdict that names the path and the role's own recovery -- and, because
    that recovery is looked up through a map, a role nobody mapped says so
    instead of rendering an empty instruction."""

    def __init__(self, path: Path, role: str, cause: Exception) -> None:
        # `.absolute()`, not the configured path: the store root is deliberately
        # cwd-relative, so a bare `.odin/<env>/world.json` in the advice is only
        # a runnable `rm` if the reader happens to be in the directory odin was
        # started in. Pure string work -- no filesystem call, unlike `resolve()`.
        self.path = path.absolute()
        self.role = role
        super().__init__(f"{self.path} could not be read back -- {type(cause).__name__}: {cause}")


def _load(path: Path, role: str, parse):
    """`parse(path's text)`, or `StoreUnreadable` naming the file and its role.

    ONE try/except for the whole store on purpose. `ValueError` covers both
    halves of a corrupt file -- `read_text` raises `UnicodeDecodeError` for
    bytes that are not UTF-8 at all, and Pydantic raises `ValidationError` (a
    `ValueError`) for text that decodes but is not the document it should be --
    and `OSError` covers a path that is no longer a readable file (a directory
    where a file belongs). Anything else is a real bug and is left to travel."""
    try:
        return parse(path.read_text())
    except (OSError, ValueError) as exc:
        raise StoreUnreadable(path, role, exc) from exc


class SpecStore:
    def __init__(self, root: Path | str = ".odin") -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _env_dir(self, env: str) -> Path:
        return self._root / env

    def apply(self, stack: Stack) -> str:
        """Persist a Stack revision and move HEAD to it. Returns the rev."""
        rev = rev_of(stack)
        stacks = self._env_dir(stack.env) / "stacks"
        # Security finding #3: a Stack revision carries every field's raw
        # value in cleartext (rds `password`, etc.), immutably -- 0600 is
        # the only thing stopping another local account from reading it.
        atomic_write_text(stacks / f"{rev}.json", stack.model_dump_json(indent=2), mode=0o600)
        atomic_write_text(self._env_dir(stack.env) / "HEAD", rev)
        return rev

    def list_envs(self) -> list[str]:
        if not self._root.exists():
            return ["default"]
        envs = sorted(p.name for p in self._root.iterdir() if (p / "HEAD").exists())
        return envs or ["default"]

    def head(self, env: str = "default") -> str | None:
        head = self._env_dir(env) / "HEAD"
        return _load(head, DESIRED, str.strip) if head.exists() else None

    def get_stack(self, env: str = "default", rev: str | None = None) -> Stack:
        rev = rev or self.head(env)
        if rev is None:
            return Stack(env=env)
        path = self._env_dir(env) / "stacks" / f"{rev}.json"
        return _load(path, DESIRED, Stack.model_validate_json)

    def current_world(self, env: str = "default") -> World:
        path = self._env_dir(env) / "world.json"
        if not path.exists():
            return World(env=env)
        return _load(path, CACHE, World.model_validate_json)

    def write_world(self, world: World) -> None:
        # Security finding #3: a resource's observed `facts` can carry a live
        # credential in cleartext (rds's DATABASE_URL embeds user:password) --
        # NOT redacted (the Fabric resolves `${{node.attr}}` refs straight out
        # of these same facts, functionally, at reconcile time), so 0600 is
        # the only defense available for this file.
        atomic_write_text(self._env_dir(world.env) / "world.json", world.model_dump_json(indent=2), mode=0o600)

    def apply_delta(self, delta: WorldDelta) -> World:
        """Upsert one resource's observed state and persist the new World.

        Tracks consecutive crashes: reset to 0 on healthy, +1 on each fresh
        crash. NOT read by `plan` -- it was written for the parked workload
        layer's give-up-on-a-crash-loop rule, and the only reader in live code
        today is `agent/debugger.py`, which displays it (honesty rule 3: this
        docstring claimed `plan` used it long after `plan` stopped). It is the
        one field `world.json` holds that nothing else does, which is why
        `StoreUnreadable` can call that file rebuildable."""
        world = self.current_world(delta.env)
        prior = world.get(delta.resource_id)
        prev = prior.restarts if prior else 0
        fresh_crash = delta.phase == "crashed" and prior is not None and prior.phase != "crashed"
        restarts = 0 if delta.phase == "healthy" else prev + (1 if fresh_crash else 0)
        observed = ResourceObserved(
            id=delta.resource_id,
            kind=delta.kind,
            phase=delta.phase,
            facts=delta.facts,
            verdict=delta.verdict,
            restarts=restarts,
        )
        others = tuple(r for r in world.resources if r.id != delta.resource_id)
        new_world = World(env=delta.env, resources=(*others, observed))
        self.write_world(new_world)
        return new_world
