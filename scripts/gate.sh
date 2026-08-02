#!/bin/sh
# The release gate: every integration test, in partitions that fit the harness.
#
# WHY THIS EXISTS, measured rather than assumed: a single
# `pytest -m integration` invocation CANNOT finish here. The harness kills a
# background task at ~61 minutes and the suite needs ~65 -- observed twice,
# once dying at 42/84 and once at 82/84 with zero failures and no exit code
# written. A foreground call caps at 600s. So the suite is run as N serial
# invocations, and the only thing that makes that honest is proving the
# partition covers every test exactly once.
#
# That proof is the point of this script. The three times it was done by hand,
# the partition was rebuilt from `ls` output each time -- and a partition built
# by hand is precisely the thing that silently drops a file and reports green
# over a suite it never ran.
#
#   ./scripts/gate.sh              # all partitions, in order
#   ./scripts/gate.sh 2            # just partition 2 (resume after a failure)
#
# Every pytest exit code is read on its own line and never through a pipe:
# `cmd | tail` reports TAIL's status, which has manufactured a false green in
# this repo before (see CLAUDE.md honesty rule 4).
set -eu

cd "$(dirname "$0")/.."
OUT="${TMPDIR:-/tmp}/odin-gate"
mkdir -p "$OUT"

# Partition 1 is everything outside tests/simulate -- fast, no VMs, ~5 minutes.
# The simulate files are split in half because they hold the VM boots, which is
# where the wall clock goes.
# `--collect-only -q` prints `path::test` per line; `--quiet` on top of that
# collapses to `path: N`, which the previous form of this line parsed for and
# got ZERO matches from -- an empty partition that the cover check below then
# passes trivially (0 == 0), running nothing and reporting green. Caught by
# running it, which is the entire lesson this file is about.
uv run pytest -m integration -p no:randomly --collect-only -q 2>/dev/null \
  | sed -n 's|^\(tests/[^:]*\.py\)::.*|\1|p' | sort -u > "$OUT/all.txt"

# Guards the guard: a collection that returns nothing must ABORT, not sail
# through as a vacuous exact cover.
[ -s "$OUT/all.txt" ] || { echo "collected NO integration tests -- aborting" >&2; exit 2; }
grep -v '^tests/simulate/' "$OUT/all.txt" > "$OUT/p1.txt" || true
grep    '^tests/simulate/' "$OUT/all.txt" > "$OUT/sim.txt" || true
half=$(( ($(wc -l < "$OUT/sim.txt") + 1) / 2 ))
head -n "$half"      "$OUT/sim.txt" > "$OUT/p2.txt"
tail -n +$((half+1)) "$OUT/sim.txt" > "$OUT/p3.txt"

# THE GUARD. Rejoin the partitions and diff against the full list. A partition
# that drops a file would otherwise report green over a suite it never ran --
# the failure this script exists to prevent, so it must abort rather than warn.
cat "$OUT/p1.txt" "$OUT/p2.txt" "$OUT/p3.txt" | sort > "$OUT/rejoined.txt"
if ! diff -q "$OUT/all.txt" "$OUT/rejoined.txt" > /dev/null; then
  echo "PARTITION IS NOT AN EXACT COVER -- refusing to run:" >&2
  diff "$OUT/all.txt" "$OUT/rejoined.txt" >&2 || true
  exit 2
fi
echo "partition verified: $(wc -l < "$OUT/all.txt" | tr -d ' ') files, exact cover, no overlap"

# A dirty machine makes the next partition lie, so say what is standing rather
# than sweeping it -- another agent's containers are not this script's to remove.
standing=$(docker ps -aq --filter "label=odin=1" 2>/dev/null | wc -l | tr -d ' ')
volumes=$(docker volume ls -q --filter "name=odin-" 2>/dev/null | wc -l | tr -d ' ')
vms=$(limactl list -q 2>/dev/null | wc -l | tr -d ' ')
[ "$standing$volumes$vms" = "000" ] || \
  echo "WARNING: starting dirty -- $standing containers, $volumes volumes, $vms VMs"

run_partition() {
  n="$1"
  echo ""
  echo "=== partition $n: $(wc -l < "$OUT/p$n.txt" | tr -d ' ') files ==="
  # `xargs` with -r so an empty list is a no-op rather than a whole-suite run:
  # an earlier hand-rolled version used `mapfile`, which macOS bash 3.2 lacks,
  # expanded to zero arguments, and silently ran EVERYTHING while its author
  # believed it was running a quarter.
  xargs -r uv run pytest -m integration -q -p no:randomly < "$OUT/p$n.txt" \
    > "$OUT/p$n.log" 2>&1
  status=$?
  echo "PARTITION $n EXIT: $status"
  tail -3 "$OUT/p$n.log"
  return $status
}

if [ $# -gt 0 ]; then
  run_partition "$1"
  exit $?
fi

failed=0
for n in 1 2 3; do
  run_partition "$n" || failed=1
done

echo ""
if [ "$failed" -eq 0 ]; then
  echo "GATE GREEN -- all partitions exit 0. Logs in $OUT/"
else
  echo "GATE RED -- at least one partition failed. Logs in $OUT/"
fi
exit "$failed"
