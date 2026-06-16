from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ResourceEntry(BaseModel):
    name: str
    service: str
    file_path: str
    status: str = "draft"
    error: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class _RegistryData(BaseModel):
    resources: dict[str, ResourceEntry] = {}


class ResourceRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path
        raw = json.loads(path.read_text())
        self._data = _RegistryData.model_validate(raw)

    def save(self) -> None:
        self._path.write_text(
            self._data.model_dump_json(indent=2)
        )

    def register(self, name: str, *, service: str, file_path: str) -> ResourceEntry:
        entry = ResourceEntry(name=name, service=service, file_path=file_path)
        self._data.resources[name] = entry
        self.save()
        return entry

    def update_status(self, name: str, status: str, *, error: Optional[str] = None, metadata: dict | None = None) -> ResourceEntry:
        entry = self._data.resources[name]
        updates: dict = {"status": status, "error": error, "updated_at": datetime.now(timezone.utc)}
        if metadata is not None:
            updates["metadata"] = metadata
        self._data.resources[name] = entry.model_copy(update=updates)
        self.save()
        return self._data.resources[name]

    def remove(self, name: str) -> None:
        del self._data.resources[name]
        self.save()

    def clear(self) -> None:
        self._data.resources.clear()
        self.save()

    def get(self, name: str) -> Optional[ResourceEntry]:
        return self._data.resources.get(name)

    def list_all(self) -> list[ResourceEntry]:
        return list(self._data.resources.values())

    def list_by_service(self, service: str) -> list[ResourceEntry]:
        return [e for e in self._data.resources.values() if e.service == service]

    def get_errors(self) -> list[ResourceEntry]:
        return [e for e in self._data.resources.values() if e.status == "error"]
