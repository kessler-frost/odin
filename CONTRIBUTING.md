# Contributing to Odin

Thanks for your interest in Odin. Here's how to get set up and what to keep in
mind.

## Development setup

Odin uses [uv](https://github.com/astral-sh/uv) for Python and
[bun](https://bun.sh/) for the UI.

```bash
uv sync --extra dev               # backend dependencies
cd ui && bun install && cd ..     # UI dependencies
```

Run it in dev mode (Vite HMR on `:4200`, auto-reloading API on `:4201`):

```bash
uv run odin start --dev
```

## Before opening a pull request

Please make sure these pass locally. The same checks run in CI:

```bash
uv run ruff check .                 # lint
uv run pytest                       # backend tests
cd ui && bunx tsc --noEmit && bun run build   # UI typecheck + build
```

## Conventions

- **Python:** use `uv`, keep imports at the top of files, prefer `pathlib` over
  string paths, and lean on Pydantic for structured data instead of regex
  parsing.
- **JS/TS:** use `bun` (not npm, npx, yarn, or pnpm).
- Keep changes focused and the diff easy to read.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0.
