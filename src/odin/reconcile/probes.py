"""Per-kind health probes — the Assertion Engine.

A container-backed workload is healthy when its kind's probe passes: an app
answers HTTP, a dependency accepts a TCP connection, an LLM serves /v1/models
(or at least accepts TCP). Probes are injected (the reconciler hands in its
http/tcp checkers) so tests stay deterministic. Adding a kind = one branch here.
"""
from __future__ import annotations


class ProbeEngine:
    def __init__(self, http_ok, tcp_ok) -> None:
        self._http = http_ok
        self._tcp = tcp_ok

    async def healthy(self, kind: str, host_port: int) -> bool:
        if kind == "dep":
            return await self._tcp("127.0.0.1", host_port)
        if kind == "llm":
            return (
                await self._http(f"http://127.0.0.1:{host_port}/v1/models")
                or await self._tcp("127.0.0.1", host_port)
            )
        return await self._http(f"http://127.0.0.1:{host_port}/")  # service
