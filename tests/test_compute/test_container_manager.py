from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from odin.compute.container_manager import ContainerManager


@pytest.fixture
def manager(tmp_path):
    return ContainerManager(data_dir=tmp_path / ".odin")


async def test_build_image(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("sha256:abc123\n", "", 0)
        result = await manager.build_image(
            vm_name="odin-container-host-vpc_main",
            context_path="/tmp/lambda_my-func",
            tag="lambda-my-func:latest",
        )
        assert result == "sha256:abc123"
        mock_run.assert_called_once_with(
            "odin-container-host-vpc_main",
            "nerdctl", "build", "-t", "lambda-my-func:latest", "/tmp/lambda_my-func",
        )


async def test_run_container(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("container123\n", "", 0)
        container_id = await manager.run_container(
            vm_name="odin-container-host-vpc_main",
            name="lambda-my-func",
            image="lambda-my-func:latest",
            env={"HANDLER": "index.handler"},
        )
        assert container_id == "container123"
        call_args = mock_run.call_args[0]
        assert call_args[0] == "odin-container-host-vpc_main"
        assert "nerdctl" in call_args
        assert "run" in call_args
        assert "-d" in call_args
        assert "--name" in call_args
        assert "lambda-my-func" in call_args


async def test_stop_container(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("", "", 0)
        await manager.stop_container("odin-container-host-vpc_main", "lambda-my-func")
        mock_run.assert_called_once_with(
            "odin-container-host-vpc_main",
            "nerdctl", "stop", "lambda-my-func",
        )


async def test_remove_container(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("", "", 0)
        await manager.remove_container("odin-container-host-vpc_main", "lambda-my-func")
        mock_run.assert_called_once_with(
            "odin-container-host-vpc_main",
            "nerdctl", "rm", "-f", "lambda-my-func",
        )


async def test_exec_in_container(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ('{"result": "ok"}\n', "", 0)
        output = await manager.exec_in_container(
            "odin-container-host-vpc_main", "lambda-my-func", "python handler.py",
        )
        assert output == '{"result": "ok"}\n'


async def test_get_container_logs(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("log line 1\nlog line 2\n", "", 0)
        logs = await manager.get_container_logs("odin-container-host-vpc_main", "lambda-my-func")
        assert "log line 1" in logs


async def test_list_containers(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            '{"Names":"lambda-a","ID":"abc","Image":"img:latest","Status":"Up"}\n'
            '{"Names":"lambda-b","ID":"def","Image":"img2:latest","Status":"Exited"}\n',
            "", 0,
        )
        containers = await manager.list_containers("odin-container-host-vpc_main")
        assert len(containers) == 2
        assert containers[0].name == "lambda-a"
        assert containers[1].status == "Exited"


async def test_build_image_failure_raises(manager):
    with patch.object(manager, "_run_in_vm", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("", "build error", 1)
        with pytest.raises(RuntimeError, match="nerdctl build failed"):
            await manager.build_image("vm", "/path", "tag:latest")


async def test_copy_to_vm(manager):
    with patch.object(manager, "_run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ("", "", 0)
        await manager.copy_to_vm("odin-container-host-vpc_main", "/local/path", "/remote/path")
        mock_run.assert_called_once_with(
            "limactl", "copy", "/local/path", "odin-container-host-vpc_main:/remote/path",
        )
