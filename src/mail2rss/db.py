from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from mail2rss.models import ParsedEntry, Publication, StoredEntry

SCHEMA_VERSION = "1"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        os.chmod(self.path, 0o600)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
              key   TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publications (
              slug          TEXT PRIMARY KEY,
              display_name  TEXT NOT NULL,
              list_id       TEXT,
              first_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entries (
              jmap_id        TEXT PRIMARY KEY,
              message_id     TEXT,
              publication    TEXT NOT NULL REFERENCES publications(slug),
              subject        TEXT NOT NULL,
              canonical_url  TEXT,
              received_at    TEXT NOT NULL,
              processed_at   TEXT NOT NULL,
              body_html      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS entries_pub_received
              ON entries(publication, received_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS entries_message_id_unique
              ON entries(message_id)
              WHERE message_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS entries_canonical_url_unique
              ON entries(canonical_url)
              WHERE canonical_url IS NOT NULL;
            """
        )
        self.set_state("schema_version", SCHEMA_VERSION)
        self.conn.commit()

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO state(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def get_or_create_secret(self, key: str, nbytes: int = 32) -> str:
        current = self.get_state(key)
        if current:
            return current
        value = secrets.token_urlsafe(nbytes)
        self.set_state(key, value)
        self.conn.commit()
        return value

    def resolve_publication(self, publication: Publication) -> Publication:
        existing = self._find_publication(publication)
        if existing is not None:
            return existing

        slug = publication.slug
        suffix_source = publication.list_id or publication.display_name
        counter = 0
        while self._slug_exists(slug):
            counter += 1
            slug = f"{publication.slug}-{_short_suffix(suffix_source, counter)}"

        resolved = Publication(
            slug=slug,
            display_name=publication.display_name,
            list_id=publication.list_id,
        )
        self.conn.execute(
            """
            INSERT INTO publications(slug, display_name, list_id, first_seen_at)
            VALUES(?, ?, ?, ?)
            """,
            (
                resolved.slug,
                resolved.display_name,
                resolved.list_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        return resolved

    def insert_entry(self, entry: ParsedEntry) -> bool:
        publication = self.resolve_publication(entry.publication)
        try:
            self.conn.execute(
                """
                INSERT INTO entries(
                  jmap_id, message_id, publication, subject, canonical_url,
                  received_at, processed_at, body_html
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.jmap_id,
                    entry.message_id,
                    publication.slug,
                    entry.subject,
                    entry.canonical_url,
                    entry.received_at,
                    datetime.now(UTC).isoformat(),
                    entry.body_html,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def list_publications(self) -> list[Publication]:
        rows = self.conn.execute(
            "SELECT slug, display_name, list_id FROM publications ORDER BY slug"
        ).fetchall()
        return [
            Publication(
                slug=str(row["slug"]),
                display_name=str(row["display_name"]),
                list_id=row["list_id"],
            )
            for row in rows
        ]

    def list_entries(
        self, publication_slug: str | None, limit: int
    ) -> list[StoredEntry]:
        where = ""
        params: list[object] = []
        if publication_slug is not None:
            where = "WHERE e.publication = ?"
            params.append(publication_slug)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT
              e.jmap_id,
              e.message_id,
              e.publication AS publication_slug,
              p.display_name AS publication_name,
              e.subject,
              e.canonical_url,
              e.received_at,
              e.body_html
            FROM entries e
            JOIN publications p ON p.slug = e.publication
            {where}
            ORDER BY e.received_at DESC, e.jmap_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            StoredEntry(
                jmap_id=str(row["jmap_id"]),
                message_id=row["message_id"],
                publication_slug=str(row["publication_slug"]),
                publication_name=str(row["publication_name"]),
                subject=str(row["subject"]),
                canonical_url=row["canonical_url"],
                received_at=str(row["received_at"]),
                body_html=str(row["body_html"]),
            )
            for row in rows
        ]

    def entries_missing_canonical_url(self) -> list[tuple[str, str, str | None]]:
        rows = self.conn.execute(
            """
            SELECT e.jmap_id, e.body_html, p.list_id
            FROM entries e
            JOIN publications p ON p.slug = e.publication
            WHERE e.canonical_url IS NULL
            """
        ).fetchall()
        return [
            (str(row["jmap_id"]), str(row["body_html"]), row["list_id"]) for row in rows
        ]

    def set_canonical_url(self, jmap_id: str, url: str) -> bool:
        try:
            self.conn.execute(
                "UPDATE entries SET canonical_url = ? WHERE jmap_id = ?",
                (url, jmap_id),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def transaction(self) -> sqlite3.Connection:
        return self.conn

    def _find_publication(self, publication: Publication) -> Publication | None:
        row = None
        if publication.list_id:
            row = self.conn.execute(
                """
                SELECT slug, display_name, list_id
                FROM publications
                WHERE list_id = ?
                """,
                (publication.list_id,),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT slug, display_name, list_id
                FROM publications
                WHERE slug = ? AND (list_id IS NULL OR list_id = ?)
                """,
                (publication.slug, publication.list_id),
            ).fetchone()
        if row is None:
            return None
        return Publication(
            slug=str(row["slug"]),
            display_name=str(row["display_name"]),
            list_id=row["list_id"],
        )

    def _slug_exists(self, slug: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM publications WHERE slug = ?", (slug,)
        ).fetchone()
        return row is not None


def insert_entries(db: Database, entries: Iterable[ParsedEntry]) -> int:
    inserted = 0
    for entry in entries:
        if db.insert_entry(entry):
            inserted += 1
    return inserted


def _short_suffix(value: str, counter: int) -> str:
    import hashlib

    digest = hashlib.sha1(f"{value}:{counter}".encode()).hexdigest()
    return digest[:6]
