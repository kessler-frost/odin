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


def _canonical(stack: Stack) -> bytes:
    return json.dumps(
        stack.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def rev_of(stack: Stack) -> str:
    return hashlib.sha256(_canonical(stack)).hexdigest()


class SpecStore:
    def __init__(self, root: Path | str = ".odin") -> None:
        self._root = Path(root)

    def _env_dir(self, env: str) -> Path:
        return self._root / env

    def apply(self, stack: Stack) -> str:
        """Persist a Stack revision and move HEAD to it. Returns the rev."""
        rev = rev_of(stack)
        stacks = self._env_dir(stack.env) / "stacks"
        stacks.mkdir(parents=True, exist_ok=True)
        (stacks / f"{rev}.json").write_text(stack.model_dump_json(indent=2))
        (self._env_dir(stack.env) / "HEAD").write_text(rev)
        return rev

    def list_envs(self) -> list[str]:
        if not self._root.exists():
            return ["default"]
        envs = sorted(p.name for p in self._root.iterdir() if (p / "HEAD").exists())
        return envs or ["default"]

    def head(self, env: str = "default") -> str | None:
        head = self._env_dir(env) / "HEAD"
        return head.read_text().strip() if head.exists() else None

    def get_stack(self, env: str = "default", rev: str | None = None) -> Stack:
        rev = rev or self.head(env)
        if rev is None:
            return Stack(env=env)
        path = self._env_dir(env) / "stacks" / f"{rev}.json"
        return Stack.model_validate_json(path.read_text())

    def current_world(self, env: str = "default") -> World:
        path = self._env_dir(env) / "world.json"
        if not path.exists():
            return World(env=env)
        return World.model_validate_json(path.read_text())

    def write_world(self, world: World) -> None:
        env_dir = self._env_dir(world.env)
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "world.json").write_text(world.model_dump_json(indent=2))

    def apply_delta(self, delta: WorldDelta) -> World:
        """Upsert one resource's observed state and persist the new World."""
        world = self.current_world(delta.env)
        observed = ResourceObserved(
            id=delta.resource_id,
            kind=delta.kind,
            phase=delta.phase,
            facts=delta.facts,
            verdict=delta.verdict,
            restarts=(r.restarts if (r := world.get(delta.resource_id)) else 0),
        )
        others = tuple(r for r in world.resources if r.id != delta.resource_id)
        new_world = World(env=delta.env, resources=(*others, observed))
        self.write_world(new_world)
        return new_world
