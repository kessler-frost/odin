"""A docker-client lookalike that routes MiniStack's container spawns to allfather.

MiniStack's service modules call ``<module>._docker.containers.run(...)`` (the
`docker` Python SDK). We monkeypatch that module global with this shim so the
container is actually booted by allfather's ``ColimaRuntime`` and shows up in
allfather's World — while MiniStack's control-plane bookkeeping (instance dict,
status flips, endpoint) runs unchanged. Only the surface MiniStack actually
touches is implemented.
"""
from __future__ import annotations

from odin.runtime.colima import ColimaRuntime, ContainerSpec

# Docker statuses MiniStack treats as "the container died".
_DEAD = {"exited", "dead", "removing", "absent"}


class _ShimContainer:
    def __init__(self, runtime: ColimaRuntime, name: str, cid: str) -> None:
        self._rt = runtime
        self.name = name
        self.id = cid
        self.attrs: dict = {"State": {"Status": "running"}}

    @property
    def status(self) -> str:
        return self._rt.status(self.name)

    def reload(self) -> None:
        self.attrs = {"State": {"Status": self.status}}

    def stop(self, **_: object) -> None:
        self._rt.stop(self.name)

    def remove(self, **_: object) -> None:
        self._rt.stop(self.name)


def _normalize_env(env: object) -> dict[str, str]:
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    if isinstance(env, list):  # docker SDK ["K=V", ...] form
        return dict(item.split("=", 1) for item in env)
    return {}


def _normalize_ports(ports: object) -> dict[int, int]:
    out: dict[int, int] = {}
    if isinstance(ports, dict):
        for cport, hport in ports.items():
            cp = int(str(cport).split("/")[0])
            out[cp] = int(hport) if hport else 0
    return out


class _Containers:
    def __init__(self, runtime: ColimaRuntime) -> None:
        self._rt = runtime

    def run(self, image: str | None = None, **kwargs: object) -> _ShimContainer:
        image = image or str(kwargs.get("image"))
        name = str(kwargs["name"])
        spec = ContainerSpec(
            name=name,
            image=image,
            env=_normalize_env(kwargs.get("environment")),
            ports=_normalize_ports(kwargs.get("ports")),
            labels={str(k): str(v) for k, v in (kwargs.get("labels") or {}).items()},
        )
        handle = self._rt.run_container(spec)
        return _ShimContainer(self._rt, name, handle.id)

    def get(self, name_or_id: str) -> _ShimContainer:
        return _ShimContainer(self._rt, name_or_id, name_or_id)

    def list(self, **_: object) -> list[_ShimContainer]:
        return []


class AllfatherDockerShim:
    """Stands in for a `docker.from_env()` client inside MiniStack."""

    def __init__(self, runtime: ColimaRuntime | None = None) -> None:
        self.containers = _Containers(runtime or ColimaRuntime())

    def ping(self) -> bool:
        return True
