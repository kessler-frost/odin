#!/bin/sh
# odin installer: Homebrew tools + odin itself, then a health check.
# Idempotent — safe to re-run. No sudo required.
set -eu

command -v brew >/dev/null || { echo "Homebrew is required first: https://brew.sh"; exit 1; }

for pkg in colima opentofu uv; do
  brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg"
done
# lima is optional (VM isolation + EC2 nodes); comment out to skip.
brew list lima >/dev/null 2>&1 || brew install lima

colima status >/dev/null 2>&1 || colima start

uv tool install --force "odin @ git+https://github.com/kessler-frost/odin.git@latest"

odin doctor
