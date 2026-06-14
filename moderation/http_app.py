"""Minimal HTTP entrypoint for the moderation service."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from moderation.product_events import ProductEventError


def handle_product_event_post(
    path: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    product_event_service: Any,
    b2b_to_mod_key: str,
) -> tuple[int, dict[str, Any]]:
    if path != "/api/v1/events/product":
        return 404, {"error": "Not found"}

    if headers.get("X-Service-Key") != b2b_to_mod_key:
        return 401, {"error": "Unauthorized"}

    try:
        if not raw_body:
            raise json.JSONDecodeError("empty body", "", 0)
        payload = json.loads(raw_body.decode("utf-8"))
        result = product_event_service.handle(payload)
    except ProductEventError as error:
        return error.status_code, {"error": error.message}
    except json.JSONDecodeError:
        return 400, {"error": "Request body must be valid JSON"}

    return 200, result.as_json()


def make_handler(product_event_service: Any, b2b_to_mod_key: str) -> type[BaseHTTPRequestHandler]:
    class ModerationRequestHandler(BaseHTTPRequestHandler):
        server_version = "NeoMarketModeration/1.0"

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            status_code, payload = handle_product_event_post(
                self.path,
                self.headers,
                self._read_body(),
                product_event_service,
                b2b_to_mod_key,
            )
            self._send_json(status_code, payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length)

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ModerationRequestHandler


def serve(host: str, port: int, product_event_service: Any, b2b_to_mod_key: str) -> None:
    handler = make_handler(product_event_service, b2b_to_mod_key)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
