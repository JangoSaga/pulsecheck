"""Controlled HTTP target for local testing and the compose smoke test.

GET /status/<code>   respond with that status code
GET /delay/<secs>    sleep, then respond 200
Not part of the product.
"""

import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if m := re.fullmatch(r"/status/(\d{3})", self.path):
            self._reply(int(m.group(1)))
        elif m := re.fullmatch(r"/delay/(\d+)", self.path):
            time.sleep(min(int(m.group(1)), 30))
            self._reply(200)
        else:
            self._reply(404)

    do_HEAD = do_GET
    do_POST = do_GET

    def _reply(self, code: int) -> None:
        body = f"status {code}\n".encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()  # noqa: S104
