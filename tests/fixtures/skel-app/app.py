"""Tiny skeleton app: HTTP 200 iff it received DATABASE_URL (proves ref wiring)."""
import http.server
import os


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = bool(os.environ.get("DATABASE_URL"))
        self.send_response(200 if ok else 503)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"no-db")

    def log_message(self, *args):
        pass


http.server.HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
