#!/bin/sh
# odin uninstaller: the twin of install.sh. Removes odin, the state it created
# on your machine, and the caches it filled. Leaves the Homebrew tools (colima,
# docker, opentofu, uv, lima) alone — they are general-purpose and you may want
# them; remove those yourself with `brew uninstall` if you like.
#
# Scope, stated up front because a teardown that quietly reaches further than
# you expect is worse than one that leaves something behind:
#
#   * containers/VMs — by DEFAULT only the environments recorded in ./.odin,
#     i.e. the project you are standing in. `--all-envs` sweeps every
#     odin-labelled container and every odin-ec2-* VM on the machine.
#     (Env names are global: two projects both using env `default` share the
#     same containers, so those cannot be told apart. Said out loud because
#     nothing can fix it.)
#   * ~/.cache/odin — odin's OpenTofu plugin cache. Machine-wide by nature,
#     hundreds of MB, and useless once odin is gone. Always removed.
#   * odin's own built images (odin-dynalite, odin-nebula) — always removed.
#     `--images` also removes the third-party backings odin pulled.
#   * ./.odin state directories — NEVER removed; they are your envs' only
#     record. The paths are printed so you can delete them yourself.
#
# --dry-run prints every one of the above without removing anything.
set -eu

ALL_ENVS=0
IMAGES=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --all-envs) ALL_ENVS=1 ;;
    --images) IMAGES=1 ;;
    --dry-run|-n) DRY=1 ;;
    -h|--help)
      echo "usage: uninstall.sh [--all-envs] [--images] [--dry-run]"
      echo "  --all-envs  remove EVERY odin container/VM on this machine, not just this project's"
      echo "  --images    also remove the third-party backing images odin pulled"
      echo "  --dry-run   show what would be removed, remove nothing"
      exit 0
      ;;
    *) echo "uninstall.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$DRY" -eq 1 ]; then
    echo "  would run: $*"
  else
    "$@" >/dev/null 2>&1 || true
  fi
}

# 1. Stop a running server (this project's).
command -v odin >/dev/null 2>&1 && [ "$DRY" -eq 0 ] && odin stop >/dev/null 2>&1 || true

# 2. Containers + VMs.
envs=""
if [ "$ALL_ENVS" -eq 0 ] && [ -d .odin ]; then
  for d in .odin/*/; do
    [ -d "$d" ] || continue
    envs="$envs $(basename "$d")"
  done
fi

if command -v docker >/dev/null 2>&1; then
  if [ "$ALL_ENVS" -eq 1 ]; then
    echo "Containers: every odin-labelled container on this machine."
    for c in $(docker ps -aq --filter label=odin=1 2>/dev/null || true); do
      run docker rm -f "$c"
    done
  else
    echo "Containers: envs recorded in ./.odin —${envs:- (none found)}"
    for e in $envs; do
      for c in $(docker ps -aq --filter label=odin=1 --filter "label=odin-env=$e" 2>/dev/null || true); do
        run docker rm -f "$c"
      done
    done
  fi
fi

if command -v limactl >/dev/null 2>&1; then
  # Only ever `odin-ec2-*`; a VM you named yourself is never matched.
  for vm in $(limactl list -q 2>/dev/null | grep '^odin-ec2-' || true); do
    keep=1
    if [ "$ALL_ENVS" -eq 1 ]; then
      keep=0
    else
      for e in $envs; do
        case "$vm" in "odin-ec2-$e-"*) keep=0 ;; esac
      done
    fi
    if [ "$keep" -eq 0 ]; then run limactl delete -f "$vm"; fi
  done
fi

# 3. Images odin itself built (always) and, with --images, the ones it pulled.
if command -v docker >/dev/null 2>&1; then
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^odin-' || true); do
    echo "Image (built by odin): $img"
    run docker rmi -f "$img"
  done
  if [ "$IMAGES" -eq 1 ]; then
    for img in rustfs/rustfs:latest admiralpiett/goaws:v0.5.4 registry:2 postgres:16-alpine redis:7-alpine; do
      docker image inspect "$img" >/dev/null 2>&1 || continue
      echo "Image (pulled by odin): $img"
      run docker rmi -f "$img"
    done
  else
    echo "Backing images odin pulled (rustfs, goaws, registry:2, postgres, redis) kept."
    echo "  re-run with --images to remove those too."
  fi
fi

# 4. The OpenTofu plugin cache odin fills — hundreds of MB, and dead weight
#    once odin is gone. (simulate/runner.py: ~/.cache/odin/tofu-plugin-cache)
if [ -d "$HOME/.cache/odin" ]; then
  echo "Cache: $HOME/.cache/odin ($(du -sh "$HOME/.cache/odin" 2>/dev/null | cut -f1))"
  run rm -rf "$HOME/.cache/odin"
fi

# 5. Remove the odin tool itself.
run uv tool uninstall odin

# 6. Local state lives in ./.odin (per project dir). Tell the user; don't
#    delete a directory we may not be standing in.
echo
if [ "$DRY" -eq 1 ]; then
  echo "Dry run — nothing was removed."
else
  echo "odin removed."
fi
if [ -d .odin ]; then
  echo "Per-project state kept: $(pwd)/.odin — delete it yourself with 'rm -rf .odin'."
fi
if [ "$ALL_ENVS" -eq 0 ]; then
  echo "Other projects' odin containers/VMs were left alone; 'uninstall.sh --all-envs'"
  echo "removes every one of them (odin itself is gone, so nothing else will)."
fi
