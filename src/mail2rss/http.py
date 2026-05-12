from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, unquote, urlparse

from mail2rss.health import HealthState


class FeedHttpServer:
    def __init__(
        self,
        bind: str,
        output_dir: Path,
        feed_secret: str,
        path_prefix: str,
        health: HealthState,
    ) -> None:
        host, port = _parse_bind(bind)
        handler = self._handler(output_dir, feed_secret, path_prefix, health)
        self.server = ThreadingHTTPServer((host, port), handler)

    def serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _handler(
        output_dir: Path,
        feed_secret: str,
        path_prefix: str,
        health: HealthState,
    ) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self: Self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/healthz":
                    payload = health.snapshot()
                    query = parse_qs(parsed.query)
                    if query.get("show_url") == ["1"] and _is_local_client(self):
                        secret_segment = f"/{feed_secret}" if feed_secret else ""
                        payload["feed_paths"] = [
                            f"{path_prefix}{secret_segment}/{path.name}"
                            for path in sorted(output_dir.glob("*.xml"))
                        ]
                    _write_json(self, payload, HTTPStatus.OK)
                    return
                feed_name = _feed_name(
                    parsed.path,
                    path_prefix=path_prefix,
                    feed_secret=feed_secret,
                )
                if feed_name is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                path = (output_dir / feed_name).resolve()
                if output_dir.resolve() not in path.parents:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not path.exists() or path.suffix != ".xml":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/atom+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self: Self, _format: str, *_args: object) -> None:
                return

        return Handler


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port_raw = bind.rpartition(":")
    if not sep or not host:
        raise ValueError(f"Invalid http bind value: {bind}")
    return host, int(port_raw)


def _feed_name(path: str, path_prefix: str, feed_secret: str) -> str | None:
    prefix = path_prefix.rstrip("/")
    expected = f"{prefix}/{feed_secret}/" if feed_secret else f"{prefix}/"
    if not path.startswith(expected):
        return None
    name = unquote(path[len(expected) :])
    if "/" in name or not name.endswith(".xml"):
        return None
    return name


def _write_json(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, object],
    status: HTTPStatus,
) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _is_local_client(handler: BaseHTTPRequestHandler) -> bool:
    host = handler.client_address[0]
    return host in {"127.0.0.1", "::1", "localhost"}
