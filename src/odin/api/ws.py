from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import WebSocket

EVENTS_LOG = Path(".odin/events.jsonl")


class ConnectionManager:
    """Manages WebSocket connections and broadcasts state updates."""

    def __init__(self, max_events: int = 500) -> None:
        self._connections: list[WebSocket] = []
        self._log_path = EVENTS_LOG
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        with self._log_path.open("a") as f:
            f.write(json.dumps(message) + "\n")
        for connection in list(self._connections):
            await connection.send_json(message)

    def get_events(self) -> list[dict[str, Any]]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def clear_events(self) -> None:
        self._log_path.unlink(missing_ok=True)
