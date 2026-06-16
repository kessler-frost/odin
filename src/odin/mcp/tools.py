from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from odin.simulator.engine import MotoEngine
from odin.simulator.executor import Executor
from odin.simulator.registry import ResourceRegistry


class OdinTools:
    """MCP-compatible tools the agent calls to validate and query infrastructure."""

    def __init__(
        self,
        engine: MotoEngine,
        registry: ResourceRegistry,
        ws_manager: Any = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._executor = Executor(engine)
        self._ws = ws_manager

    def validate_file(self, file_path: str) -> dict[str, Any]:
        """Execute a boto3 file against moto, capture metadata, update registry, broadcast."""
        path = Path(file_path)
        resource_name = path.stem
        service = Executor.detect_service(path)

        # Register as draft
        self._registry.register(resource_name, service=service, file_path=str(path))

        # Snapshot moto resource IDs before execution
        pre_ids = self._snapshot_resource_ids(service)

        # Execute file against moto
        result = self._executor.execute(path)

        # Capture new metadata by diffing resource IDs
        metadata = self._capture_new_metadata(service, pre_ids) if result.success else {}

        # Update registry
        status = "validated" if result.success else "error"
        self._registry.update_status(
            resource_name, status, error=result.error, metadata=metadata
        )

        # Broadcast via WebSocket
        if self._ws is not None:
            event_type = "resource_validated" if result.success else "resource_error"
            event: dict[str, Any] = {
                "type": event_type,
                "name": resource_name,
                "service": service,
                "status": status,
            }
            if result.error:
                event["error"] = result.error
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._ws.broadcast(event))

        return {
            "resource": resource_name,
            "service": service,
            "status": status,
            "error": result.error,
            "metadata": metadata,
        }

    def get_infrastructure_state(self, service: str | None = None) -> dict[str, Any]:
        """Return all resources with status and metadata from registry."""
        entries = (
            self._registry.list_by_service(service)
            if service
            else self._registry.list_all()
        )
        return {
            "resources": [
                {
                    "name": e.name,
                    "service": e.service,
                    "status": e.status,
                    "error": e.error,
                    "file_path": e.file_path,
                    "metadata": e.metadata,
                    "created_at": e.created_at.isoformat(),
                    "updated_at": e.updated_at.isoformat(),
                }
                for e in entries
            ]
        }

    def _snapshot_resource_ids(self, service: str) -> dict[str, set]:
        """Capture resource IDs from moto before execution."""
        ids: dict[str, set] = {}

        if service in ("ec2", "vpc", "subnet", "sg"):
            ec2 = self._engine.get_client("ec2")
            ids["vpcs"] = {v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]}
            ids["subnets"] = {s["SubnetId"] for s in ec2.describe_subnets()["Subnets"]}
            ids["instances"] = {
                i["InstanceId"]
                for r in ec2.describe_instances()["Reservations"]
                for i in r["Instances"]
            }
            ids["security_groups"] = {
                sg["GroupId"] for sg in ec2.describe_security_groups()["SecurityGroups"]
            }

        elif service == "s3":
            s3 = self._engine.get_client("s3")
            ids["buckets"] = {b["Name"] for b in s3.list_buckets()["Buckets"]}

        elif service == "lambda":
            lam = self._engine.get_client("lambda")
            ids["functions"] = {
                f["FunctionName"] for f in lam.list_functions()["Functions"]
            }

        return ids

    def _capture_new_metadata(self, service: str, pre_ids: dict[str, set]) -> dict[str, str]:
        """Diff moto state to find newly created resource IDs."""
        post_ids = self._snapshot_resource_ids(service)
        metadata: dict[str, str] = {}

        for key, pre_set in pre_ids.items():
            post_set = post_ids.get(key, set())
            new_ids = post_set - pre_set
            for new_id in sorted(new_ids):
                # Use singular form for key: "vpc", "subnet", "instance", etc.
                singular = key.rstrip("s")
                # Handle "security_group" from "security_groups"
                if len(new_ids) == 1:
                    metadata[singular] = new_id
                else:
                    idx = sorted(new_ids).index(new_id)
                    metadata[f"{singular}_{idx}"] = new_id

        return metadata
