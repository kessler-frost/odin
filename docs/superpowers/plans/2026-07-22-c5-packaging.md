# C5 — Packaging (doctor + one-command install) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `odin doctor` diagnoses the external toolchain with exact fix commands; `scripts/install.sh` takes a fresh Mac from zero to a working `odin` in one command. (Pragmatic scope per the spec — no binary vendoring.)

**Architecture:** A pure `checks()` function returns structured results (name, ok, detail, fix) so the CLI rendering and tests stay separate; the installer is a small, idempotent, POSIX-sh brew+uv script. Spec: `docs/superpowers/specs/2026-07-22-v030-real-backings-design.md` §C5.

## Global Constraints

- Checks must not mutate anything — read-only probes (`shutil.which`, `subprocess.run` version/status calls with short timeouts).
- Required: colima (+ docker CLI), uv. Optional (report but don't fail): lima/limactl (VM isolation), bun (dev builds only), `claude` CLI (the Brain — degraded-but-working without it; say so in the detail).
- Exit code: `odin doctor` exits 1 if any REQUIRED check fails, else 0.
- installer: `#!/bin/sh` (POSIX), idempotent (safe to re-run), no sudo, Homebrew required (fail with a clear message if missing), ends by running `odin doctor`.
- `uv` never pip; imports at top; minimal branching. Commit per task; don't push.

---

### Task 1: `odin doctor`

**Files:**
- Create: `src/odin/doctor.py`
- Modify: `src/odin/__main__.py` (add the `doctor` command; keep Typer style of the existing commands)
- Test: `tests/test_doctor.py` (new)

**Interfaces:**
- Produces: `Check` (frozen dataclass: `name: str, required: bool, ok: bool, detail: str, fix: str`) and `run_checks(which=shutil.which, run=subprocess.run) -> list[Check]` — injectable `which`/`run` so tests fake tool presence. Checks: `colima` binary + `colima status` (running?), `docker` binary, `uv` binary, `limactl` binary (optional), `bun` binary (optional), `claude` binary (optional). Each failing check's `fix` is the exact command (`brew install colima`, `colima start`, `brew install uv`, `brew install lima`, `brew install oven-sh/bun/bun`, `npm-free: install via https://claude.com/claude-code`... use `brew install --cask claude-code` ONLY if that cask actually exists — verify with `brew info --cask claude-code` during implementation; otherwise point at the official install command from docs).
- CLI: `odin doctor` prints one line per check (`✓`/`✗`/`○` for optional-missing, name, detail, then `→ fix` when failing) and exits per the constraint.

- [ ] **Step 1:** Failing tests: fake `which`/`run` for (a) all-good → every required ok, exit-worthy summary `all_ok(checks)` True; (b) colima installed but stopped → required check fails with fix `colima start`; (c) missing optional bun → ok overall, `○` state. Test the pure layer, not Typer output formatting (one smoke test via `typer.testing.CliRunner` asserting exit codes 0/1).
- [ ] **Step 2-4:** RED → implement → GREEN + `uv run pytest -q`. Also run `uv run odin doctor` for real on this Mac and paste output in the report.
- [ ] **Step 5:** Commit `feat(cli): odin doctor — toolchain diagnosis with exact fixes`.

---

### Task 2: `scripts/install.sh` + README install path

**Files:**
- Create: `scripts/install.sh`
- Test: shellcheck-clean (`brew install shellcheck` if absent — or skip if unavailable, note it) + a real run in a scratch dir

**Contract:**
```sh
#!/bin/sh
set -eu
# odin installer: brew tools + uv tool install. Idempotent, no sudo.
command -v brew >/dev/null || { echo "Homebrew required: https://brew.sh"; exit 1; }
brew list colima >/dev/null 2>&1 || brew install colima
brew list uv >/dev/null 2>&1 || brew install uv
colima status >/dev/null 2>&1 || colima start
uv tool install --force git+https://github.com/kessler-frost/odin.git@latest
odin doctor
```
(Exact final content may adjust flags — e.g. `uv tool update-shell` note — but stays within this shape; every deviation gets a comment in the script.)

- [ ] **Step 1:** Write it; `sh -n scripts/install.sh`; shellcheck if available.
- [ ] **Step 2:** Real run: execute in a scratch HOME-safe way (tools already installed → idempotent path; verify it completes and `odin doctor` passes; the `@latest` install will only work after the first release exists — until then test with `@develop` and note that the README/installer reference `@latest` which goes live at release time; do NOT change the script to develop).
- [ ] **Step 3:** Commit `feat(scripts): one-command installer`.
