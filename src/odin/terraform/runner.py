"""Drive the `tofu` CLI: init / validate / plan / apply / destroy against Moto."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from odin.process import run
from odin.terraform.provider import render_provider


@dataclass
class PlanResult:
    """Structured outcome of a tofu run. `diagnostics` holds errors/warnings."""

    ok: bool
    diagnostics: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    raw: str = ""

    @property
    def errors(self) -> list[str]:
        return [
            d.get("summary", "") for d in self.diagnostics if d.get("severity") == "error"
        ]


def _parse_ndjson(output: str) -> tuple[list[dict], list[dict]]:
    """Parse tofu's `-json` stream (newline-delimited) into diagnostics + changes."""
    diagnostics: list[dict] = []
    changes: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind == "diagnostic":
            diagnostics.append(obj.get("diagnostic", {}))
        elif kind in ("planned_change", "resource_drift", "apply_complete"):
            changes.append(obj.get("change", obj.get("hook", obj)))
    return diagnostics, changes


class TofuRunner:
    """Runs OpenTofu against a Moto endpoint in a single working directory."""

    def __init__(self, work_dir: Path | str, endpoint: str, region: str = "us-east-1") -> None:
        self._dir = Path(work_dir)
        self._endpoint = endpoint
        self._region = region

    @property
    def work_dir(self) -> Path:
        return self._dir

    def write_provider(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "provider.tf").write_text(
            render_provider(self._endpoint, self._region)
        )

    async def ensure_init(self) -> None:
        """Write the provider and run `tofu init` once (if not already done)."""
        self.write_provider()
        if (self._dir / ".terraform").exists():
            return
        result = await run("tofu", "init", "-input=false", "-no-color", cwd=self._dir)
        if not result.ok:
            raise RuntimeError(f"tofu init failed:\n{result.stderr or result.stdout}")

    async def validate(self) -> PlanResult:
        await self.ensure_init()
        result = await run("tofu", "validate", "-json", cwd=self._dir)
        data = json.loads(result.stdout or "{}")
        return PlanResult(
            ok=bool(data.get("valid", result.ok)),
            diagnostics=data.get("diagnostics", []),
            raw=result.stdout,
        )

    async def plan(self) -> PlanResult:
        await self.ensure_init()
        result = await run(
            "tofu", "plan", "-json", "-input=false", "-no-color", cwd=self._dir
        )
        diagnostics, changes = _parse_ndjson(result.stdout)
        return PlanResult(ok=result.ok, diagnostics=diagnostics, changes=changes, raw=result.stdout)

    async def apply(self) -> PlanResult:
        await self.ensure_init()
        result = await run(
            "tofu", "apply", "-json", "-auto-approve", "-input=false", "-no-color",
            cwd=self._dir,
        )
        diagnostics, changes = _parse_ndjson(result.stdout)
        return PlanResult(ok=result.ok, diagnostics=diagnostics, changes=changes, raw=result.stdout)

    async def destroy(self) -> PlanResult:
        if not (self._dir / ".terraform").exists():
            return PlanResult(ok=True)
        result = await run(
            "tofu", "destroy", "-json", "-auto-approve", "-input=false", "-no-color",
            cwd=self._dir,
        )
        diagnostics, changes = _parse_ndjson(result.stdout)
        return PlanResult(ok=result.ok, diagnostics=diagnostics, changes=changes, raw=result.stdout)
