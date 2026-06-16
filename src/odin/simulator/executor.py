from __future__ import annotations

import traceback
from pathlib import Path

from pydantic import BaseModel

from odin.simulator.engine import MotoEngine


class ExecutionResult(BaseModel):
    resource_name: str
    service: str
    success: bool
    error: str | None = None


class Executor:
    """Executes agent-generated boto3 resource files against the moto engine."""

    def __init__(self, engine: MotoEngine) -> None:
        self._engine = engine

    @staticmethod
    def detect_service(file_path: Path) -> str:
        prefix = file_path.stem.split("_")[0]
        known = {"s3", "ec2", "lambda", "iam", "vpc", "subnet", "sg", "sts"}
        return prefix if prefix in known else "unknown"

    def execute(self, file_path: Path) -> ExecutionResult:
        resource_name = file_path.stem
        service = self.detect_service(file_path)
        code = file_path.read_text()
        scope: dict = {}
        try:
            compiled = compile(code, str(file_path), "exec")
            exec(compiled, scope)  # noqa: S102
        except Exception:
            return ExecutionResult(
                resource_name=resource_name,
                service=service,
                success=False,
                error=traceback.format_exc(),
            )
        return ExecutionResult(
            resource_name=resource_name,
            service=service,
            success=True,
        )
