from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from typing import cast

from mail2rss.db import Database
from mail2rss.feeds import write_all_feeds
from mail2rss.health import HealthState
from mail2rss.http import FeedHttpServer
from tests.test_db import _entry


def test_feed_generation(tmp_path: Path) -> None:
    db = Database(tmp_path / "mail2rss.db")
    db.migrate()
    db.insert_entry(
        _entry("jmap-1", "msg@example.invalid", "https://example.substack.com/p/one")
    )
    db.commit()

    written = write_all_feeds(
        db, tmp_path / "feeds", "secret", "/feeds", "all.xml", 100
    )

    assert tmp_path / "feeds" / "example-publication.xml" in written
    assert (tmp_path / "feeds" / "all.xml").exists()
    assert "Paid" not in (tmp_path / "feeds" / "all.xml").read_text()
    db.close()


def test_http_secret_and_health(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feeds"
    feed_dir.mkdir()
    (feed_dir / "all.xml").write_text("<feed />")
    health = HealthState(interval_seconds=900)
    server = FeedHttpServer("127.0.0.1:0", feed_dir, "secret", "/feeds", health)
    host, port = cast(tuple[str, int], server.server.server_address)

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{host}:{port}"
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["healthy"] is False

        with urllib.request.urlopen(
            f"{base}/feeds/secret/all.xml", timeout=5
        ) as response:
            assert response.status == 200

        try:
            urllib.request.urlopen(f"{base}/feeds/wrong/all.xml", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("wrong secret unexpectedly succeeded")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_serves_without_secret(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feeds"
    feed_dir.mkdir()
    (feed_dir / "all.xml").write_text("<feed />")
    health = HealthState(interval_seconds=900)
    server = FeedHttpServer("127.0.0.1:0", feed_dir, "", "/feeds", health)
    host, port = cast(tuple[str, int], server.server.server_address)

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{host}:{port}"
        with urllib.request.urlopen(f"{base}/feeds/all.xml", timeout=5) as response:
            assert response.status == 200

        with urllib.request.urlopen(
            f"{base}/healthz?show_url=1", timeout=5
        ) as response:
            payload = json.loads(response.read())
        assert payload["feed_paths"] == ["/feeds/all.xml"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
