"""Process helpers: one-shot commands via anyio, long-running daemons via Popen."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import anyio


@dataclass(frozen=True)
class RunResult:
    """Outcome of a one-shot command."""

    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run(*args: str, cwd: Path | str | None = None) -> RunResult:
    """Run a one-shot command and capture its output.

    Never raises on a non-zero exit (caller inspects ``returncode``). A genuinely
    missing binary still raises ``FileNotFoundError`` — actions fail loudly.
    """
    completed = await anyio.run_process(list(args), cwd=cwd, check=False)
    return RunResult(
        stdout=completed.stdout.decode(errors="replace"),
        stderr=completed.stderr.decode(errors="replace"),
        returncode=completed.returncode,
    )


class Daemon:
    """A managed long-running subprocess — a separate OS process, no threads."""

    def __init__(self, *args: str, cwd: Path | str | None = None) -> None:
        self._args = list(args)
        self._cwd = str(cwd) if cwd else None
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._args,
            cwd=self._cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Graceful stop: SIGTERM, wait, then SIGKILL if it overstays."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
