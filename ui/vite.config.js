import { defineConfig } from 'vite';
import { resolve } from 'path';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const BACKEND_PORT = 4201;

// Every backend prefix the app can reach, because `TopBar.tsx` and `Canvas.tsx`
// both set `const API = ''` -- so in `odin start --dev` a fetch is SAME-ORIGIN,
// and that origin is Vite. Anything not listed here is answered by Vite (a 404,
// or index.html for a GET) and never reaches uvicorn.
//
// THIS LIST HAD ROTTED COMPLETELY. It was last touched 2026-06-21 (`121b79d`)
// and still proxied `/validate`, `/simulate`, `/suggest-defaults`, `/services`,
// `/state`, `/errors`, `/vm`, `/invoke` and `/ws` -- NONE of which are routes
// any more; the validate-only flow they belonged to was retired in the pivot.
// Meanwhile it forwarded none of `/apply-full`, `/world`, `/stream`, `/envs`,
// `/logs`, `/translate`, `/ai` or `/tf/*`. Of the 12 paths the UI actually
// calls, 4 were proxied. So Apply, the World projection and the event stream
// were all broken in dev mode -- the Apply button POSTs `/apply-full` and got
// Vite's 404.
//
// Prefix match, so `/chat` covers `/chat/clear`, `/envs` covers `/envs/rm`, and
// `/tf` covers `/tf/apply|plan|destroy|status`.
//
// KEEP THIS IN STEP WITH THE ROUTES. Each entry below was verified to exist in
// `src/odin/server.py` or `src/odin/api/` at the time of writing; a stale entry
// is harmless but a MISSING one silently breaks that feature in dev only, which
// is the worst place for it -- nothing in the test suite drives the dev server.
const BACKEND_ROUTES = [
  '/ai',
  '/apply',        // also covers /apply-full
  '/canvas',
  '/chat',         // also covers /chat/clear
  '/destroy',
  '/envs',         // also covers /envs/rm
  '/events',
  '/health',
  '/import-tf',
  '/logs',
  '/mesh',
  '/stream',       // SSE: the reconnecting EventSource in BottomPanel.tsx
  '/tf',           // also covers /tf/apply, /tf/plan, /tf/destroy, /tf/status
  '/translate',
  '/volumes',
  '/world',
];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  server: {
    port: 4200,
    proxy: Object.fromEntries(
      BACKEND_ROUTES.map((route) => [route, `http://localhost:${BACKEND_PORT}`]),
    ),
    fs: {
      allow: [resolve(__dirname, '..')],
    },
  },
});
