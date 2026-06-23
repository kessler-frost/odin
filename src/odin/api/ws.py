from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import WebSocket

EVENTS_LOG = Path(".odin/events.jsonl")


class ConnectionManager:
    """Manages WebSocket connections and broadcasts state updates."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._log_path = EVENTS_LOG
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)  # idempotent

    async def broadcast(self, message: dict[str, Any]) -> None:
        with self._log_path.open("a") as f:
            f.write(json.dumps(message) + "\n")
        # Best-effort: a broken viewer must never stall reconciliation. A failed
        # send drops that socket; the durable events.jsonl above is the source of
        # truth, and live viewers backfill from /events on (re)connect.
        dead = []
        for connection in list(self._connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self._connections.discard(connection)

    def get_events(self) -> list[dict[str, Any]]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]
