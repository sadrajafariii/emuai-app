"""
Serves the landing page at http://127.0.0.1:8001
Run alongside main.py (which runs the app on port 8000).
"""
import http.server
import os

PORT = 8001
DIR  = os.path.join(os.path.dirname(__file__), "website")

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)
    def log_message(self, fmt, *args):
        print(f"[landing] {fmt % args}")

print(f"Landing page → http://127.0.0.1:{PORT}")
http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
