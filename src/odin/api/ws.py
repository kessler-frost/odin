from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections and broadcasts state updates.

    The durable event log is scoped per environment (`<root>/<env>/events.jsonl`,
    parallel to that env's `world.json`), so the log panel never mixes envs."""

    def __init__(self, root: Path | str = ".odin") -> None:
        self._connections: set[WebSocket] = set()
        self._root = Path(root)

    def _log(self, env: str) -> Path:
        return self._root / env / "events.jsonl"

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)  # idempotent

    async def broadcast(self, message: dict[str, Any]) -> None:
        env = message.get("env", "default")
        path = self._log(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(message) + "\n")
        # Best-effort: a broken viewer must never stall reconciliation. A failed
        # send drops that socket; the durable per-env log above is the source of
        # truth, and live viewers backfill from /events on (re)connect.
        dead = []
        for connection in list(self._connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self._connections.discard(connection)

    def get_events(self, env: str = "default") -> list[dict[str, Any]]:
        path = self._log(env)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
