"""Minimal HTTP entrypoint for the moderation service."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from typing import Any, Mapping

from moderation.errors import ModerationError, UnauthorizedError


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
    except ModerationError as error:
        return error.status_code, {"error": error.message}
    except json.JSONDecodeError:
        return 400, {"error": "Request body must be valid JSON"}

    return 200, result.as_json()


def handle_get_next_post(
    path: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    queue_service: Any,
) -> tuple[int, dict[str, Any] | None]:
    if path != "/api/v1/product-moderation/get-next":
        return 404, {"error": "Not found"}

    try:
        moderator_id = headers.get("X-Moderator-Id")
        if moderator_id is None:
            raise UnauthorizedError("X-Moderator-Id header is required")
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise json.JSONDecodeError("non-object body", "", 0)
        card = queue_service.get_next(payload.get("queueId"), moderator_id)
    except ModerationError as error:
        return error.status_code, {"error": error.message}
    except json.JSONDecodeError:
        return 400, {"error": "Request body must be a JSON object"}

    if card is None:
        return 204, None
    return 200, card.as_json()


def handle_approve_post(
    path: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    decision_service: Any,
) -> tuple[int, dict[str, Any] | None]:
    product_id = _match_product_action(path, "approve")
    if product_id is None:
        return 404, {"error": "Not found"}

    try:
        moderator_id = headers.get("X-Moderator-Id")
        if moderator_id is None:
            raise UnauthorizedError("X-Moderator-Id header is required")
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise json.JSONDecodeError("non-object body", "", 0)
        result = decision_service.approve(product_id, moderator_id, payload)
    except ModerationError as error:
        return error.status_code, {"error": error.message}
    except json.JSONDecodeError:
        return 400, {"error": "Request body must be a JSON object"}

    return 200, result.as_json()


def make_handler(
    product_event_service: Any,
    b2b_to_mod_key: str,
    queue_service: Any | None = None,
    decision_service: Any | None = None,
) -> type[BaseHTTPRequestHandler]:
    class ModerationRequestHandler(BaseHTTPRequestHandler):
        server_version = "NeoMarketModeration/1.0"

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            raw_body = self._read_body()
            if self.path == "/api/v1/events/product":
                status_code, payload = handle_product_event_post(
                    self.path,
                    self.headers,
                    raw_body,
                    product_event_service,
                    b2b_to_mod_key,
                )
            elif self.path == "/api/v1/product-moderation/get-next" and queue_service is not None:
                status_code, payload = handle_get_next_post(
                    self.path,
                    self.headers,
                    raw_body,
                    queue_service,
                )
            elif _match_product_action(self.path, "approve") is not None and decision_service is not None:
                status_code, payload = handle_approve_post(
                    self.path,
                    self.headers,
                    raw_body,
                    decision_service,
                )
            else:
                status_code, payload = 404, {"error": "Not found"}
            self._send_json(status_code, payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length)

        def _send_json(self, status_code: int, payload: dict[str, Any] | None) -> None:
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            if payload is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

    return ModerationRequestHandler


def _match_product_action(path: str, action: str) -> str | None:
    match = re.fullmatch(rf"/api/v1/products/([^/]+)/{action}", path)
    if match is None:
        return None
    return match.group(1)


def serve(
    host: str,
    port: int,
    product_event_service: Any,
    b2b_to_mod_key: str,
    queue_service: Any | None = None,
    decision_service: Any | None = None,
) -> None:
    handler = make_handler(product_event_service, b2b_to_mod_key, queue_service, decision_service)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
