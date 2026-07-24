#!/bin/sh
# odin uninstaller: the twin of install.sh. Removes odin and the state it
# created on your machine. Leaves the Homebrew tools (colima, opentofu, uv,
# lima) alone — they are general-purpose and you may want them; remove those
# yourself with `brew uninstall` if you like.
set -eu

# 1. Stop a running server + tear down any odin-managed containers/VMs.
command -v odin >/dev/null 2>&1 && odin stop 2>/dev/null || true
if command -v docker >/dev/null 2>&1; then
  docker ps -aq --filter label=odin=1 | xargs -r docker rm -f 2>/dev/null || true
fi
if command -v limactl >/dev/null 2>&1; then
  for vm in $(limactl list -q 2>/dev/null | grep '^odin-ec2-' || true); do
    limactl delete -f "$vm" 2>/dev/null || true
  done
fi

# 2. Remove the odin tool itself.
uv tool uninstall odin 2>/dev/null || true

# 3. Local state lives in ./.odin (per project dir). Tell the user; don't
#    delete a directory we may not be standing in.
echo "odin removed. Per-project state remains in each project's .odin/ directory —"
echo "delete those yourself if you want them gone (e.g. 'rm -rf .odin' in a project)."
