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

# Match `assets/odin-canvas.png`, the README's hero screenshot: a 1500x820
# viewport at devicePixelRatio 2, which the recorder captures natively at
# 3000x1640 (verified with ffprobe). Output is 1500 wide -- a 2:1 downscale
# from retina, so text stays crisp.
#
# The first version of this script recorded 1440x650 at 1x and emitted 900px
# GIFs. That is a 0.625x downscale of already-non-retina frames: soft text,
# a squat frame, and nodes far smaller than the screenshot they sit beside in
# the README. Resolution is not a detail here -- the whole point of these
# clips is that someone can read the labels.
VIEW_W=1500
VIEW_H=820
VIEW_DPR=2
GIF_W=1500
GIF_FPS=10

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
#
# Grab LOW on the canvas (y=560), below where tiles land. Dragging from the
# drop point itself grabs the NODE that is sitting there and moves it instead
# of panning -- which is why an earlier take scattered the three resources
# across two rows at different heights rather than leaving them in a line.
# The pan is verified by the viewport transform actually changing.
pan_canvas() {
  # Grab point comes from the pane's REAL rect, not a hardcoded y.
  #
  # A fixed `y=560` worked interactively and failed inside a recording: a
  # fresh context has no saved layout in localStorage, so the bottom panel
  # opens taller, 560 lands INSIDE it, and the drag pans nothing. The next
  # drop then hit the node still sitting at the pane centre and vanished --
  # visible only as "SQS did not place a node".
  local rect x y
  rect="$(ab_eval "(()=>{const r=document.querySelector('.react-flow__pane').getBoundingClientRect();return Math.round(r.x+90)+' '+Math.round(r.bottom-70);})()" | tr -d '"')"
  x="${rect%% *}"; y="${rect##* }"
  [ -n "$x" ] && [ -n "$y" ] || { say "pan: could not measure the pane"; return 1; }
  local before after
  before="$(ab_eval "(()=>getComputedStyle(document.querySelector('.react-flow__viewport')).transform)()")"
  agent-browser mouse move "$x" "$y" >/dev/null 2>&1
  agent-browser mouse down left >/dev/null 2>&1
  for d in 60 120 180 240 280; do agent-browser mouse move $((x + d)) "$y" >/dev/null 2>&1; done
  agent-browser mouse up left >/dev/null 2>&1
  sleep 1
  after="$(ab_eval "(()=>getComputedStyle(document.querySelector('.react-flow__viewport')).transform)()")"
  [ "$before" != "$after" ] || { say "pan: viewport did not move (grabbed $x,$y) -- the next drop would land on a node"; return 1; }
}

# drop_tile <catalog-name> <expected-node-type> <x> <y>
#
# Dispatches the REAL HTML5 drag sequence on the REAL tile element --
# `dragstart` -> `dragover` -> `drop` -> `dragend`, carrying the same
# `application/odin-resource` payload Sidebar.tsx sets -- so odin's own
# `onDrop` handler runs exactly as it does for a user, including the
# grid-snapping and de-collision in Canvas.tsx.
#
# Why not `agent-browser drag`, which draws a visible cursor:
#   * it drops at the target's CENTRE and takes no coordinates, so every tile
#     lands on the same point and needs a canvas pan between drops;
#   * and its SOURCE targeting is unreliable here. The ref is right --
#     `get text @e14` returns "Object storage", the S3 tile -- but the drag
#     acts on stale coordinates and grabs a neighbour: asking for S3 produced
#     an ECS Service on one take and an EC2 Instance on the next, a different
#     wrong tile each run. That is the known agent-browser pointer-targeting
#     limitation recorded in .claude/CLAUDE.md, not something this script can
#     fix.
# What is lost is the rendered cursor motion; what is kept is the actual
# drag-and-drop code path, which is what the clip is claiming to show. Taking
# coordinates also means nodes land where we want, so no pan is needed.
drop_tile() {  # drop_tile <aria-name> <abbr> <node-type> <x> <y>
  local placed
  tag_targets
  # The payload is the tile's ABBR, passed explicitly. It cannot be derived
  # from the aria-name: that is `textContent.slice(0,3)`, which for the S3
  # tile ("S3" + "S3 Bucket") yields "S3S", not the "S3" that
  # `Sidebar.tsx::onDragStart` actually puts on the wire. Sending the wrong
  # abbr drops nothing at all, silently.
  ab_eval "(()=>{const el=document.querySelector('[aria-label=\"$1\"]');if(!el)return 'no-tile';const dt=new DataTransfer();dt.setData('application/odin-resource','$2');const pane=document.querySelector('.react-flow__pane');const ev=t=>new DragEvent(t,{bubbles:true,cancelable:true,dataTransfer:dt,clientX:$4,clientY:$5});el.dispatchEvent(ev('dragstart'));pane.dispatchEvent(ev('dragover'));pane.dispatchEvent(ev('drop'));el.dispatchEvent(ev('dragend'));return 'ok';})()" >/dev/null
  sleep 1.2
  placed="$(ab_eval "(()=>[...document.querySelectorAll('.react-flow__node')].map(n=>n.getAttribute('data-id')).filter(id=>id&&id.startsWith('$3-')).length)()" | tr -d '"')"
  if [ "${placed:-0}" -lt 1 ]; then
    say "ABORT: $1 (abbr $2) did not place a '$3' node"
    say "  nodes now: $(ab_eval "(()=>JSON.stringify([...document.querySelectorAll('.react-flow__node')].map(n=>n.getAttribute('data-id'))))()")"
    return 1
  fi
}

# Deliberately a NO-OP: the catalog is never scrolled.
#
# Setting `el.scrollTop` from `eval` desyncs agent-browser's coordinate model.
# With the catalog scrolled to 220, asking for the S3 tile dropped an ECS
# Service -- one row off, repeatably -- while the identical call worked on an
# unscrolled page. A wheel gesture over the sidebar did not scroll it at all.
#
# So the demo uses only tiles visible with the catalog at rest (measured tops:
# S3 310, DynamoDB 448, RDS 503, against a sidebar viewport ending at 620).
# That is possible only because the palette now hides the `(placeholder)`
# kinds: 27 tiles became 18, and SQS at 641 was the first to fall off the
# bottom -- which is why the clip shows S3 + DynamoDB + RDS.
focus_catalog() {
  :
}

reset_sidebar_scroll() {
  ab_eval "(()=>{const el=[...document.querySelectorAll('div')].find(e=>e.scrollHeight>e.clientHeight+30&&/EC2 Instance/.test(e.textContent||''));if(el)el.scrollTop=0;return 'ok';})()" >/dev/null
  sleep 1
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

  # about:blank, NOT the app. `record start` opens a FRESH context, and the
  # pre-record one stays alive holding whatever canvas it loaded; when
  # recording stops it becomes active again and its debounced save writes that
  # STALE canvas over the good one. Measured twice: a clip-1 run that ended
  # with three healthy resources was followed by a saved canvas of one node,
  # and then of none, which left clip 2 rendering an empty Terraform file and
  # a one-frame video. A blank page holds no canvas and cannot save one.
  # Set the viewport on BOTH sides of `record start`. The VIDEO's dimensions
  # are fixed when the recording context is created, from whatever viewport is
  # current at that moment -- setting it only afterwards fixes the page and
  # leaves the video at the old size, which is how a take came out 1500x678
  # instead of 1500x820 right after `close --all` reset the default.
  agent-browser set viewport "$VIEW_W" "$VIEW_H" "$VIEW_DPR" >/dev/null 2>&1
  agent-browser open "about:blank" >/dev/null 2>&1
  agent-browser record start "$WORK/draw.webm" >/dev/null 2>&1
  agent-browser set viewport "$VIEW_W" "$VIEW_H" "$VIEW_DPR" >/dev/null 2>&1  # AFTER record start
  agent-browser open "$BASE/?cb=$RANDOM$RANDOM" >/dev/null 2>&1
  sleep 5
  open_panels
  focus_catalog

  # Spaced across the canvas so the three land in a row with room to breathe;
  # Canvas.tsx snaps each to the 20px grid.
  drop_tile catalog-S3S S3 s3 560 260 || { agent-browser record stop >/dev/null 2>&1; return 1; }
  drop_tile catalog-DDB DDB dynamodb 900 260 || { agent-browser record stop >/dev/null 2>&1; return 1; }
  drop_tile catalog-RDS RDS rds 1240 260 || { agent-browser record stop >/dev/null 2>&1; return 1; }
  reset_sidebar_scroll
  fit_view
  say "nodes on canvas: $(node_count)"

  # Check the SAVED canvas, not just the DOM: the canvas is global and
  # last-writer-wins, so a browser context left alive by an earlier
  # `record start` can overwrite it underneath this run. One such run produced
  # a saved canvas holding `s3-101` TWICE plus tiles this script never drops,
  # and `tofu init` then failed with `Duplicate resource "aws_s3_bucket"` --
  # 180s of timeout recorded into a 3MB clip of nothing happening.
  local saved
  saved="$(curl -sf "$BASE/canvas" | python3 -c "
import json,sys
n=json.load(sys.stdin).get('nodes',[])
ids=[x.get('id') for x in n]
kinds=sorted(x.get('type') for x in n)
print('ok' if kinds==['dynamodb','rds','s3'] and len(set(ids))==3 else f'BAD {kinds} {ids}')" 2>/dev/null || echo 'BAD unreadable')"
  [ "$saved" = "ok" ] || { say "ABORT: saved canvas is $saved -- refusing to record"; agent-browser record stop >/dev/null 2>&1; return 1; }

  ab_eval "(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.trim().toLowerCase()==='apply').click();return 'ok';})()" >/dev/null
  wait_healthy default 3 180
  sleep 3
  agent-browser record stop >/dev/null 2>&1
  sleep 1
  to_gif "$WORK/draw.webm" "$OUT/odin-draw-apply.gif" 4 "$GIF_FPS"
}

# --- clip 2: the generated Terraform ---------------------------------------

clip_code_panel() {
  say "clip 2/2: code panel"
  # The panel renders whatever `/translate` makes of the SAVED canvas, so an
  # empty canvas yields a provider block with no resources.
  local n
  n="$(curl -sf "$BASE/canvas" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('nodes',[])))" 2>/dev/null || echo 0)"
  [ "${n:-0}" -ge 1 ] || { say "ABORT: the saved canvas is empty -- run the draw clip first"; return 1; }

  # Captured as SCREENSHOTS, not video, unlike clip 1.
  #
  # Playwright's screencast only emits a frame when the page changes, and it
  # judged this one barely changed: a slow scroll of a code panel produced a
  # webm with 2 frames, and on other takes exactly 1 -- which gifski rejects
  # outright ("Only a single image file was given"). Clip 1 records fine
  # because nodes appear and badges flip.
  #
  # Stepping the scroll and screenshotting each position is fully
  # deterministic, every frame is a real capture of the real panel, and the
  # result is crisper than video (no inter-frame compression).
  agent-browser close --all >/dev/null 2>&1 || true
  sleep 2
  agent-browser set viewport "$VIEW_W" "$VIEW_H" "$VIEW_DPR" >/dev/null 2>&1
  agent-browser open "$BASE/?cb=$RANDOM$RANDOM" >/dev/null 2>&1
  sleep 5
  ab_eval "(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='{ }');if(!b)return 'no-button';b.click();return 'clicked';})()" >/dev/null
  sleep 4

  local range
  range="$(ab_eval "(()=>{const el=[...document.querySelectorAll('div')].find(e=>e.scrollHeight>e.clientHeight+30&&/aws_s3_bucket/.test(e.textContent||''));return el?(el.scrollHeight-el.clientHeight):0;})()" | tr -d '"')"
  [ "${range:-0}" -gt 0 ] || { say "ABORT: the Terraform panel did not render (nothing to scroll)"; return 1; }
  say "  scrollable range: ${range}px"

  rm -rf "$WORK/shots"; mkdir -p "$WORK/shots"
  local steps=28 i
  for i in $(seq 0 $((steps - 1))); do
    # Hold on the first frames, then ease down the file.
    ab_eval "(()=>{const el=[...document.querySelectorAll('div')].find(e=>e.scrollHeight>e.clientHeight+30&&/aws_s3_bucket/.test(e.textContent||''));if(el)el.scrollTop=Math.round($range*Math.max(0,($i-4))/($steps-6));return 'ok';})()" >/dev/null
    agent-browser screenshot "$WORK/shots/$(printf '%03d' "$i").png" >/dev/null 2>&1
  done
  local got
  got="$(ls "$WORK/shots" | wc -l | tr -d ' ')"
  [ "$got" -ge 10 ] || { say "ABORT: only $got frames captured"; return 1; }
  gifski --quiet --fps 8 --width "$GIF_W" -o "$OUT/odin-code-panel.gif" "$WORK"/shots/*.png || return 1
  say "odin-code-panel.gif: $(du -h "$OUT/odin-code-panel.gif" | cut -f1), $got frames"
}


# --- clip 3: an IAM permission edge, drawn by the CLI, appearing LIVE --------
#
# Driven by `odin canvas set` rather than by the mouse, for two reasons.
#
# The honest one: agent-browser cannot reliably draw a connection on this
# canvas. The handles are 6px, and instrumenting the page showed `pointerdown`
# arriving with a NON-handle target even when the mouse had been moved to the
# handle's measured centre (`pointerup` does land on it). That is recorded in
# .claude/CLAUDE.md as an open limitation, not something this script can fix.
#
# The better one: this clip now shows TWO real features at once. The edge is
# authored through odin's own CLI, and it appears in the already-open browser
# with no reload -- which is the per-env `canvas_updated` convergence working.
# A mouse-drawn edge would have demonstrated only the first.
clip_iam_edge() {
  say "clip 3/3: IAM edge via the CLI, converging live"
  local before="$WORK/iam-before.json" after="$WORK/iam-after.json"
  cat > "$before" <<'JSON'
{"nodes":[
  {"id":"ec2-1","type":"ec2","position":{"x":260,"y":180},"data":{"label":"api-server","instance_type":"t3.micro"}},
  {"id":"s3-1","type":"s3","position":{"x":760,"y":180},"data":{"label":"uploads"}}
],"edges":[]}
JSON
  cat > "$after" <<'JSON'
{"nodes":[
  {"id":"ec2-1","type":"ec2","position":{"x":260,"y":180},"data":{"label":"api-server","instance_type":"t3.micro"}},
  {"id":"s3-1","type":"s3","position":{"x":760,"y":180},"data":{"label":"uploads"}}
],"edges":[
  {"id":"iam-1","source":"ec2-1","target":"s3-1","sourceHandle":"right","targetHandle":"left",
   "data":{"edgeType":"iam","permissions":["s3:GetObject","s3:PutObject","s3:ListBucket"]}}
]}
JSON
  curl -sf -X POST "$BASE/canvas?env=default" -H 'Content-Type: application/json' --data-binary "@$before" >/dev/null

  agent-browser close --all >/dev/null 2>&1 || true
  sleep 2
  agent-browser set viewport "$VIEW_W" "$VIEW_H" "$VIEW_DPR" >/dev/null 2>&1
  agent-browser open "$BASE/?cb=$RANDOM$RANDOM" >/dev/null 2>&1
  sleep 6
  [ "$(ab_eval "(()=>document.querySelectorAll('.react-flow__edge').length)()" | tr -d '"')" = "0" ] \
    || { say "ABORT: the canvas already has an edge -- the clip must show it APPEAR"; return 1; }

  rm -rf "$WORK/iam"; mkdir -p "$WORK/iam"
  local i=0
  # A few frames of the edgeless canvas first, so the edge visibly ARRIVES.
  for i in 0 1 2 3 4 5; do
    agent-browser screenshot "$WORK/iam/$(printf '%03d' "$i").png" >/dev/null 2>&1
  done
  curl -sf -X POST "$BASE/canvas?env=default" -H 'Content-Type: application/json' --data-binary "@$after" >/dev/null
  for i in $(seq 6 27); do
    agent-browser screenshot "$WORK/iam/$(printf '%03d' "$i").png" >/dev/null 2>&1
  done

  local edges
  edges="$(ab_eval "(()=>document.querySelectorAll('.react-flow__edge').length)()" | tr -d '"')"
  [ "${edges:-0}" -ge 1 ] || { say "ABORT: the edge never converged into the open tab"; return 1; }
  ab_eval "(()=>/GetObject/.test(document.body.innerText))()" | grep -q true \
    || { say "ABORT: the edge has no permission label -- the clip would show a bare line"; return 1; }

  gifski --quiet --fps 6 --width "$GIF_W" -o "$OUT/odin-iam-edge.gif" "$WORK"/iam/*.png || return 1
  say "odin-iam-edge.gif: $(du -h "$OUT/odin-iam-edge.gif" | cut -f1), $(ls "$WORK/iam" | wc -l | tr -d ' ') frames"
}

require
mkdir -p "$OUT"

# Close every page before starting. The canvas is GLOBAL and last-writer-wins,
# and each live page holds its own copy plus a debounced save -- so any tab
# left over from an earlier take can overwrite a freshly recorded canvas the
# moment it re-renders. Observed repeatedly: a run that ended with three
# healthy resources left a saved canvas of one node, which then made the
# second clip render an empty Terraform file.
agent-browser close --all >/dev/null 2>&1 || true
# ODIN_GIF_ONLY=draw|code re-records a single clip. Clip 1 costs a real
# destroy + apply (~90s), so iterating on clip 2 should not pay for it.
# Each clip gets its OWN PROCESS. A second `record start` within one run
# produced a one-frame video, and then no video file at all, while the very
# same clip recorded correctly as a standalone invocation -- so "all" re-execs
# rather than calling both functions in sequence. Whatever the recorder holds
# on to, it does not survive a second recording in the same process.
case "${ODIN_GIF_ONLY:-all}" in
  draw) clip_draw_apply ;;
  code) clip_code_panel && ODIN_GIF_ONLY=iam exec "$0" "$BASE" "$OUT" ;;
  iam)  clip_iam_edge ;;
  *)    clip_draw_apply && ODIN_GIF_ONLY=code exec "$0" "$BASE" "$OUT" ;;
  # (the code clip re-execs into the iam clip; see the note above about each
  #  recording needing its own process)
esac
say "done"
