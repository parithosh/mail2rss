from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import cast

import structlog
from feedgen.feed import FeedGenerator

from mail2rss.db import Database
from mail2rss.models import StoredEntry
from mail2rss.parser import strip_publication_prefix

LOGGER = structlog.get_logger()


def write_all_feeds(
    db: Database,
    output_dir: Path,
    feed_secret: str,
    path_prefix: str,
    aggregate_filename: str,
    max_entries: int,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    secret_segment = f"/{feed_secret}" if feed_secret else ""
    publications = db.list_publications()
    for publication in publications:
        entries = db.list_entries(publication.slug, max_entries)
        path = output_dir / f"{publication.slug}.xml"
        xml = _render_feed(
            title=f"Substack: {publication.display_name}",
            feed_id=f"urn:mail2rss:publication:{publication.slug}",
            self_href=f"{path_prefix}{secret_segment}/{publication.slug}.xml",
            entries=entries,
            max_entries=max_entries,
        )
        _atomic_write(path, xml)
        written.append(path)

    aggregate_entries = db.list_entries(None, max_entries)
    aggregate_path = output_dir / aggregate_filename
    xml = _render_feed(
        title="Substack: All",
        feed_id="urn:mail2rss:aggregate:all",
        self_href=f"{path_prefix}{secret_segment}/all.xml",
        entries=aggregate_entries,
        max_entries=max_entries,
    )
    _atomic_write(aggregate_path, xml)
    written.append(aggregate_path)
    return written


def _render_feed(
    title: str,
    feed_id: str,
    self_href: str,
    entries: list[StoredEntry],
    max_entries: int,
) -> bytes:
    fg = FeedGenerator()
    fg.id(feed_id)
    fg.title(title)
    fg.link(href=self_href, rel="self")
    fg.language("en")
    updated = _latest_updated(entries) or datetime.now().astimezone()
    fg.updated(updated)

    for entry in entries[:max_entries]:
        fe = fg.add_entry()
        fe.id(f"urn:fastmail-email:{entry.jmap_id}")
        fe.title(strip_publication_prefix(entry.subject, entry.publication_name))
        published = _parse_datetime(entry.received_at)
        fe.published(published)
        fe.updated(published)
        fe.author({"name": entry.publication_name})
        if entry.canonical_url:
            fe.link(href=entry.canonical_url, rel="alternate")
        fe.content(entry.body_html, type="html")
        LOGGER.debug(
            "feed_entry_rendered",
            feed_id=feed_id,
            jmap_id=entry.jmap_id,
            publication_slug=entry.publication_slug,
            has_link=entry.canonical_url is not None,
            canonical_url=entry.canonical_url,
        )
    return cast(bytes, fg.atom_str(pretty=True))


def _latest_updated(entries: list[StoredEntry]) -> datetime | None:
    if not entries:
        return None
    return max(_parse_datetime(entry.received_at) for entry in entries)


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        "wb", delete=False, dir=path.parent, prefix=f".{path.name}."
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)
