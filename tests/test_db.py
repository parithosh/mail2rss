from __future__ import annotations

from pathlib import Path

from mail2rss.db import Database
from mail2rss.models import ParsedEntry, Publication


def _entry(
    jmap_id: str, message_id: str | None, canonical_url: str | None
) -> ParsedEntry:
    return ParsedEntry(
        jmap_id=jmap_id,
        message_id=message_id,
        publication=Publication(
            slug="example-publication",
            display_name="Example Publication",
            list_id="example.substack.com",
        ),
        subject="Subject",
        canonical_url=canonical_url,
        received_at="2026-05-11T10:00:00Z",
        body_html="<p>Body</p>",
        is_truncated=False,
    )


def test_db_deduplicates_secondary_ids(tmp_path: Path) -> None:
    db = Database(tmp_path / "mail2rss.db")
    db.migrate()

    assert db.insert_entry(
        _entry("jmap-1", "msg@example.invalid", "https://example.com/p/1")
    )
    assert not db.insert_entry(
        _entry("jmap-2", "msg@example.invalid", "https://example.com/p/2")
    )
    assert not db.insert_entry(
        _entry("jmap-3", "msg3@example.invalid", "https://example.com/p/1")
    )

    db.commit()
    entries = db.list_entries("example-publication", 10)
    assert len(entries) == 1
    db.close()


def test_slug_collision_gets_stable_suffix(tmp_path: Path) -> None:
    db = Database(tmp_path / "mail2rss.db")
    db.migrate()

    pub1 = db.resolve_publication(
        Publication("example", "Example", "example.substack.com")
    )
    pub2 = db.resolve_publication(Publication("example", "Example", "custom.invalid"))

    assert pub1.slug == "example"
    assert pub2.slug.startswith("example-")
    db.close()
