#!/usr/bin/env bash
#
# Record the README GIFs against a LIVE odin, in a real browser.
#
#   scripts/record-gifs.sh [BASE_URL] [OUT_DIR]
#
# Defaults: http://127.0.0.1:4200 and ./assets. The server must already be
# running with a real runtime behind it -- these clips show real containers
# going healthy, which is the entire point of recording them rather than
# mocking them.
#
# Requires: agent-browser, ffmpeg, gifski.
#
# ---------------------------------------------------------------------------
# Three things about agent-browser that this script exists to encode, each
# measured here rather than assumed:
#
#  1. `drag` drops at the target element's CENTRE and takes no coordinates, and
#     it will NOT drop onto an injected marker div -- a transparent 40x40
#     positioned child of the pane reported `✓ Done` and placed nothing. So
#     every tile is dropped on the pane itself and odin's own de-collision
#     lays them out. That is also the more honest demo: it is what a user gets.
#  2. Refs go stale on ANY page change. Dropping a node re-renders the canvas,
#     so a ref captured before the drop is dead afterwards -- three drags need
#     three snapshots. Skipping this silently places only the first node.
#  3. `record start` opens a FRESH browser context: the viewport must be set
#     AFTER it, and anything injected before it is gone. Hence the ordering
#     below, which looks redundant and is not.
#
# The catalog tiles are plain draggable <div>s with no ARIA role, so they do
# not appear in the accessibility snapshot at all. `label_catalog` adds a role
# and a stable name so `drag` can address them. That only makes existing
# elements addressable -- the drag itself is a real HTML5 drag of the real
# tile, which is what must be true for the recording to mean anything.
# ---------------------------------------------------------------------------
set -uo pipefail

BASE="${1:-http://127.0.0.1:4200}"
OUT="${2:-assets}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

VIEW_W=1440
VIEW_H=650
GIF_W=900

say() { printf '  %s\n' "$*"; }

require() {
  for cmd in agent-browser ffmpeg gifski; do
    command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
  done
  curl -sf "$BASE/health" >/dev/null || { echo "no odin at $BASE" >&2; exit 1; }
}

# --- browser helpers -------------------------------------------------------

ab_eval() { agent-browser eval "$1" 2>/dev/null | tail -1; }

# Name the pane and the catalog tiles so `drag` can address them. Both are
# real page elements; this only makes them visible to the accessibility
# snapshot, which the tiles otherwise miss entirely (plain draggable <div>s).
# The pane is named explicitly rather than located by shape: a heuristic like
# "the generic near the zoom controls" resolves to a different element once
# nodes exist on the canvas, which silently dropped 2 of 3 tiles.
tag_targets() {
  ab_eval "(()=>{const p=document.querySelector('.react-flow__pane');p.setAttribute('role','button');p.setAttribute('aria-label','odin-pane');[...document.querySelectorAll('[draggable=true]')].forEach(el=>{const t=el.textContent.trim();el.setAttribute('role','button');el.setAttribute('aria-label','catalog-'+t.slice(0,3).replace(/[^A-Za-z0-9]/g,''));});return 'ok';})()" >/dev/null
}

# Pan the canvas left so the pane's CENTRE is empty again.
#
# This is load-bearing, not decoration. `drag` drops at the target's centre,
# and after the first tile lands there the node itself covers that point --
# every later drop hits the node instead of the pane and vanishes. Measured:
# without a pan between drops, 3 drags produce 1 node. It reproduces only on a
# NON-EMPTY canvas, which is exactly the trap that hid odin's own drop-placement
# bug.
#
# Low-level mouse gestures are unreliable for odin's 6px connection handles
# (see .claude/CLAUDE.md) but pan fine, being a large-area drag on the pane:
# verified moving the viewport transform from `matrix(1,0,0,1,0,0)` to
# `matrix(1,0,0,1,240,0)`.
pan_canvas() {
  agent-browser mouse move 500 380 >/dev/null 2>&1
  agent-browser mouse down left >/dev/null 2>&1
  for d in 60 120 180 240 280; do agent-browser mouse move $((500 - d)) 380 >/dev/null 2>&1; done
  agent-browser mouse up left >/dev/null 2>&1
  sleep 1
}

drop_tile() {  # drop_tile <catalog-name>
  local tile pane
  tag_targets
  # BOTH refs must come from the SAME snapshot -- taking a second one
  # renumbers them and invalidates the first.
  agent-browser snapshot -i 2>/dev/null > "$WORK/snap.txt"
  tile="$(grep "\"$1\"" "$WORK/snap.txt" | sed -n 's/.*\[ref=\(e[0-9]*\)\].*/\1/p' | head -1)"
  pane="$(grep '"odin-pane"' "$WORK/snap.txt" | sed -n 's/.*\[ref=\(e[0-9]*\)\].*/\1/p' | head -1)"
  [ -n "$tile" ] && [ -n "$pane" ] || { say "could not resolve $1 (tile=$tile pane=$pane)"; return 1; }
  agent-browser drag "@$tile" "@$pane" >/dev/null 2>&1
  sleep 1.5
}

fit_view() {
  ab_eval "(()=>{const b=[...document.querySelectorAll('button')].find(x=>/fit view/i.test(x.getAttribute('title')||x.getAttribute('aria-label')||''));if(b)b.click();return 'ok';})()" >/dev/null
  sleep 2
}

node_count() { ab_eval "(()=>document.querySelectorAll('.react-flow__node').length)()"; }

open_panels() {
  # Sidebar open, config panel closed: the canvas is the subject.
  ab_eval "(()=>{const btn=[...document.querySelectorAll('button')].find(b=>/^resources$/i.test(b.textContent.trim()));if(btn)btn.click();return 'ok';})()" >/dev/null
  sleep 1
}

# --- state helpers ---------------------------------------------------------

reset_canvas() {
  curl -sf -X POST "$BASE/canvas" -H 'Content-Type: application/json' \
    -d '{"nodes":[],"edges":[]}' >/dev/null
}

destroy_env() {
  curl -sf -X POST "$BASE/destroy?env=${1:-default}" >/dev/null 2>&1 || true
}

# Wait for every resource in the env to reach `healthy`. A real `tofu apply`
# of one queue measured ~25s, plus ~5s to observation -- so the timeout is
# generous ON PURPOSE. Polling short and declaring failure is how a working
# apply gets reported as broken.
wait_healthy() {  # wait_healthy <env> <expected-count> <timeout-s>
  local env="$1" want="$2" deadline=$(( SECONDS + $3 ))
  while [ $SECONDS -lt $deadline ]; do
    local got
    got="$(curl -sf "$BASE/world?env=$env" | python3 -c "
import json,sys
w=json.load(sys.stdin)
print(sum(1 for r in w.get('resources',[]) if r.get('phase')=='healthy'))" 2>/dev/null || echo 0)"
    [ "$got" -ge "$want" ] && { say "all $want healthy after ${SECONDS}s"; return 0; }
    sleep 3
  done
  say "TIMEOUT: only reached $(curl -sf "$BASE/world?env=$env" | head -c 200)"
  return 1
}

# --- encode ----------------------------------------------------------------

to_gif() {  # to_gif <in.webm> <out.gif> <speed> <fps>
  local src="$1" dst="$2" speed="$3" fps="$4"
  rm -rf "$WORK/frames"; mkdir -p "$WORK/frames"
  ffmpeg -nostdin -v error -i "$src" \
    -vf "setpts=PTS/${speed},fps=${fps},scale=${GIF_W}:-2:flags=lanczos" \
    "$WORK/frames/%04d.png" || return 1
  gifski --quiet --fps "$fps" --width "$GIF_W" -o "$dst" "$WORK"/frames/*.png || return 1
  say "$(basename "$dst"): $(du -h "$dst" | cut -f1), $(ls "$WORK/frames" | wc -l | tr -d ' ') frames"
}

# --- clip 1: draw three resources and apply them ---------------------------

clip_draw_apply() {
  say "clip 1/2: draw + apply"
  reset_canvas
  destroy_env default
  sleep 2

  agent-browser open "$BASE/" >/dev/null 2>&1
  agent-browser record start "$WORK/draw.webm" >/dev/null 2>&1
  agent-browser set viewport "$VIEW_W" "$VIEW_H" >/dev/null 2>&1   # AFTER record start
  agent-browser open "$BASE/" >/dev/null 2>&1
  sleep 5
  open_panels

  drop_tile catalog-S3S
  pan_canvas
  drop_tile catalog-SQS
  pan_canvas
  drop_tile catalog-DDB
  fit_view
  say "nodes on canvas: $(node_count)"
  [ "$(node_count)" -eq 3 ] || { say "ABORT: expected 3 nodes, refusing to record a misleading clip"; agent-browser record stop >/dev/null 2>&1; return 1; }

  ab_eval "(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.trim().toLowerCase()==='apply').click();return 'ok';})()" >/dev/null
  wait_healthy default 3 180
  sleep 3
  agent-browser record stop >/dev/null 2>&1
  sleep 1
  to_gif "$WORK/draw.webm" "$OUT/odin-draw-apply.gif" 4 12
}

# --- clip 2: the generated Terraform ---------------------------------------

clip_code_panel() {
  say "clip 2/2: code panel"
  agent-browser open "$BASE/" >/dev/null 2>&1
  agent-browser record start "$WORK/code.webm" >/dev/null 2>&1
  agent-browser set viewport "$VIEW_W" "$VIEW_H" >/dev/null 2>&1
  agent-browser open "$BASE/" >/dev/null 2>&1
  sleep 5
  ab_eval "(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='{ }').click();return 'ok';})()" >/dev/null
  sleep 4
  # Scroll to the aws_sqs_queue resource, because the README's alt text says
  # the clip shows it. The container is picked by CONTENT, not by class:
  # `pre`/`code` do not scroll at all (so an earlier `p.scrollTop = 180` moved
  # nothing), and matching on `overflow-y-auto` picked the SIDEBAR, which is
  # first in DOM order -- that clip scrolled the resource list while the
  # Terraform pane sat still. "The scrollable element containing
  # aws_s3_bucket" resolves to exactly one node.
  ab_eval "(()=>{const el=[...document.querySelectorAll('div')].find(e=>e.scrollHeight>e.clientHeight+30&&/aws_s3_bucket/.test(e.textContent||''));if(!el)return 'none';let y=0;const max=el.scrollHeight-el.clientHeight;const step=()=>{y+=3;el.scrollTop=y;if(y<max)requestAnimationFrame(step);};step();return 'scrolling';})()" >/dev/null
  sleep 5
  agent-browser record stop >/dev/null 2>&1
  sleep 1
  to_gif "$WORK/code.webm" "$OUT/odin-code-panel.gif" 2 12
}

require
mkdir -p "$OUT"
# ODIN_GIF_ONLY=draw|code re-records a single clip. Clip 1 costs a real
# destroy + apply (~90s), so iterating on clip 2 should not pay for it.
case "${ODIN_GIF_ONLY:-all}" in
  draw) clip_draw_apply ;;
  code) clip_code_panel ;;
  *)    clip_draw_apply && clip_code_panel ;;
esac
say "done"
