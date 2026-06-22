"""M4 — the per-kind probe registry routes to the right check."""
from __future__ import annotations

from odin.reconcile.probes import ProbeEngine


async def _http_yes(url):
    return True


async def _http_no(url):
    return False


async def _tcp_yes(host, port):
    return True


async def _tcp_no(host, port):
    return False


async def test_service_probes_http():
    assert await ProbeEngine(_http_yes, _tcp_no).healthy("service", 8000) is True
    assert await ProbeEngine(_http_no, _tcp_no).healthy("service", 8000) is False


async def test_dep_probes_tcp_not_http():
    assert await ProbeEngine(_http_no, _tcp_yes).healthy("dep", 6379) is True
    assert await ProbeEngine(_http_yes, _tcp_no).healthy("dep", 6379) is False


async def test_llm_probes_http_or_tcp():
    assert await ProbeEngine(_http_no, _tcp_yes).healthy("llm", 1234) is True
    assert await ProbeEngine(_http_yes, _tcp_no).healthy("llm", 1234) is True
