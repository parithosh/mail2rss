from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Publication:
    slug: str
    display_name: str
    list_id: str | None


@dataclass(frozen=True)
class ParsedEntry:
    jmap_id: str
    message_id: str | None
    publication: Publication
    subject: str
    canonical_url: str | None
    received_at: str
    body_html: str
    is_truncated: bool


@dataclass(frozen=True)
class StoredEntry:
    jmap_id: str
    message_id: str | None
    publication_slug: str
    publication_name: str
    subject: str
    canonical_url: str | None
    received_at: str
    body_html: str
