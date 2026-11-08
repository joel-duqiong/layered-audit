"""跨仓库的第二群 Agent：极小的对外服务（汇率 + 风控）。

刻意用 Python stdlib（不引框架），证明跨仓库 ≠ 跨语言同构：
跨仓库边是**双边声明**出来的（能力清单），不是靠同一个抽取器。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

CALLS: dict[str, int] = {}

CAPABILITIES = {
    "service": "fx",
    "endpoints": [{"method": "GET", "path": "/fx"}],
    "tables_owned": ["fx_rate"],
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/fx":
            currency = (parse_qs(parsed.query).get("currency") or ["CNY"])[0]
            CALLS[currency] = CALLS.get(currency, 0) + 1
            rate = 100 if currency == "CNY" else 92
            body = json.dumps({"currency": currency, "rate": rate}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/stats":
            body = json.dumps({"calls": CALLS}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/capabilities":
            body = json.dumps(CAPABILITIES).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8098), Handler).serve_forever()
