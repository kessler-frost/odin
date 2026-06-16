from __future__ import annotations

from typing import Any

from odin.simulator.registry import ResourceRegistry
from odin.terraform.runner import TofuRunner


def _error_summaries(diagnostics: list[dict]) -> list[str]:
    return [
        d.get("summary", "") for d in diagnostics if d.get("severity") == "error"
    ]


class OdinTools:
    """MCP-compatible tools the agent calls to validate and query infrastructure."""

    def __init__(
        self,
        runner: TofuRunner,
        registry: ResourceRegistry,
        ws_manager: Any = None,
    ) -> None:
        self._runner = runner
        self._registry = registry
        self._ws = ws_manager

    async def validate_infrastructure(self) -> dict[str, Any]:
        """Run `tofu validate` then `tofu plan` against Moto; report any errors."""
        validated = await self._runner.validate()
        if not validated.ok:
            return {
                "valid": False,
                "stage": "validate",
                "errors": _error_summaries(validated.diagnostics),
            }
        planned = await self._runner.plan()
        errors = _error_summaries(planned.diagnostics)
        return {
            "valid": planned.ok and not errors,
            "stage": "plan",
            "errors": errors,
        }

    def get_infrastructure_state(self, service: str | None = None) -> dict[str, Any]:
        """Return the current main.tf config plus the registry's resource list."""
        main_tf = self._runner.work_dir / "main.tf"
        entries = (
            self._registry.list_by_service(service)
            if service
            else self._registry.list_all()
        )
        return {
            "main_tf": main_tf.read_text() if main_tf.exists() else "",
            "resources": [
                {
                    "name": e.name,
                    "service": e.service,
                    "status": e.status,
                    "error": e.error,
                }
                for e in entries
            ],
        }
