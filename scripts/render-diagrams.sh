#!/bin/sh
# Render docs/diagrams/*.mmd to ASCII, for pasting into README.md and
# docs/internals.md.
#
# The .mmd files are the source; the ASCII in the docs is generated output. When
# a diagram is wrong, edit the .mmd and re-run this — hand-editing the ASCII
# leaves the two disagreeing, and the next person to regenerate silently reverts
# the fix.
#
#   ./scripts/render-diagrams.sh              # all of them
#   ./scripts/render-diagrams.sh apply-pipeline
#
# Uses beautiful-mermaid (https://github.com/lukilabs/beautiful-mermaid) in a
# throwaway directory, so nothing is added to this repo's dependencies.
set -eu

DIAGRAMS="$(cd "$(dirname "$0")/../docs/diagrams" && pwd)"
WORK="${TMPDIR:-/tmp}/odin-mermaid"

if [ ! -d "$WORK/node_modules/beautiful-mermaid" ]; then
  echo "installing beautiful-mermaid into $WORK …" >&2
  mkdir -p "$WORK"
  (cd "$WORK" && bun init -y >/dev/null 2>&1 && bun add beautiful-mermaid >/dev/null 2>&1)
fi

cat > "$WORK/render.ts" <<'TS'
import { renderMermaidASCII } from "beautiful-mermaid";
const src = await Bun.file(Bun.argv[2]).text();
console.log(renderMermaidASCII(src, { useAscii: true }));
TS

for src in "$DIAGRAMS"/*.mmd; do
  name="$(basename "$src" .mmd)"
  [ $# -eq 0 ] || [ "$name" = "${1:-}" ] || continue
  printf '\n=== %s ===\n\n' "$name"
  (cd "$WORK" && bun render.ts "$src")
done
