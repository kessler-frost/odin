#!/bin/sh
# odin installer: Homebrew tools + odin itself, then a health check.
# Idempotent — safe to re-run. No sudo required.
#
# There is exactly ONE global `odin` entrypoint (uv's tool slot), and three
# documented ways to fill it:
#
#   1. this script                       -> a pinned copy of the `latest` branch
#   2. uv tool install "git+…@latest"    -> the same thing, by hand
#   3. uv tool install --editable ".[dev]" (from a clone) -> a DEV install that
#      tracks your working tree
#
# They all target the same slot, so installing one replaces another. Replacing
# a *pinned* install is an upgrade and this script just does it. Replacing a
# *development* install throws away the link to someone's checkout, so this
# script REFUSES unless you pass --force:
#
#   curl -fsSL …/install.sh | sh -s -- --force
set -eu

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      echo "usage: install.sh [--force]"
      echo "  --force  replace an existing DEVELOPMENT (editable) odin install too"
      exit 0
      ;;
    *) echo "install.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

command -v brew >/dev/null || { echo "Homebrew is required first: https://brew.sh"; exit 1; }
command -v uv >/dev/null || brew install uv

# --- Guard the one global `odin` slot before touching it --------------------
# uv records what it installed in a per-tool receipt; an editable install names
# the checkout it points at. Reading it is the only way to know whether this
# install would eat a contributor's working tree.
receipt="$(uv tool dir 2>/dev/null || echo "$HOME/.local/share/uv/tools")/odin/uv-receipt.toml"
if [ "$FORCE" -eq 0 ] && [ -f "$receipt" ] && grep -q 'editable' "$receipt"; then
  checkout=$(sed -n 's/.*editable = "\([^"]*\)".*/\1/p' "$receipt" | head -1)
  echo "Refusing to install: the global 'odin' command is a DEVELOPMENT install."
  echo
  echo "  it points at: ${checkout:-a local checkout}"
  echo "  receipt:      $receipt"
  echo
  echo "Installing over it would silently detach 'odin' from that working tree."
  echo "Pick one:"
  echo "  * keep developing        — you already have odin; nothing to do here."
  echo "  * really replace it      — re-run with --force:"
  echo "        curl -fsSL https://raw.githubusercontent.com/kessler-frost/odin/main/install.sh | sh -s -- --force"
  echo "  * go back to dev later   — cd ${checkout:-<your clone>} && uv tool install --editable '.[dev]'"
  exit 1
fi

# --- Toolchain --------------------------------------------------------------
# `docker` is its own formula: `brew deps colima` is just `lima`, so installing
# colima alone leaves a Mac with no docker CLI — and everything in odin (and
# `odin doctor` itself) shells out to one.
for pkg in colima docker opentofu; do
  brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg"
done
# lima is optional (VM isolation + EC2 nodes); comment out to skip.
brew list lima >/dev/null 2>&1 || brew install lima

colima status >/dev/null 2>&1 || colima start

# --force here is deliberate and now earned: either the slot is empty, or it
# holds a pinned install (an upgrade), or the user asked for it above.
uv tool install --force "odin @ git+https://github.com/kessler-frost/odin.git@latest"

echo
echo "Installed. Checking this machine can actually run odin:"
odin doctor
