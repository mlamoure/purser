"""Synthetic HTTPS provider for the disposable onboarding drill; never deploy."""

import json
import ssl
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Provider(BaseHTTPRequestHandler):
    claimed = False

    def log_message(self, *_args) -> None:
        pass  # Claims and access URLs are not log data, even in this fixture.

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.end_headers()
        # Expected after the real client timeout.
        with suppress(BrokenPipeError, ssl.SSLError):
            self.wfile.write(body)

    def do_POST(self) -> None:
        mode = Path("/tmp/feed-mode").read_text()
        if mode == "claim_error":
            self.reply(503, b"unavailable")
        elif mode == "claim_timeout":
            time.sleep(35)
            self.reply(503, b"timed out")
        elif Provider.claimed:
            self.reply(403, b"already claimed")
        else:
            Provider.claimed = True
            access = "https://" + "example:synthetic" + "@feed.example.test:8443/simplefin"
            self.reply(200, access.encode())

    def do_GET(self) -> None:
        mode = Path("/tmp/feed-mode").read_text()
        if mode == "fetch_error":
            self.reply(503, b"unavailable")
        elif mode == "fetch_timeout":
            time.sleep(65)
            self.reply(503, b"timed out")
        else:
            self.reply(200, json.dumps({"errors": [], "accounts": []}).encode())


if __name__ == "__main__":
    Path("/tmp/feed-mode").write_text("claim_error")
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Provider)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/fixture/ca.pem", "/fixture/key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
