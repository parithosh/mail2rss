from __future__ import annotations

import re
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import urlparse

import structlog

from mail2rss.models import ParsedEntry, Publication
from mail2rss.sanitize import rewrite_substack_redirect, sanitize_html, text_to_html

LOGGER = structlog.get_logger()

LIST_ID_RE = re.compile(r"^\s*(?:(?P<name>.*?)\s*)?<(?P<id>[^>]+)>\s*$")
SLUG_RE = re.compile(r"[^a-z0-9]+")
TRUNCATION_MARKERS = (
    "read in app",
    "continue reading",
    "this post is for paid subscribers",
    "upgrade to paid",
)


def parse_email(email: dict[str, object]) -> ParsedEntry:
    headers = _headers(email)
    publication = _publication(email, headers)
    raw_body = _html_body(email) or text_to_html(_text_body(email) or "")
    sanitized_body = sanitize_html(raw_body)
    canonical_url = _canonical_url(sanitized_body, publication.list_id)
    subject = str(email.get("subject") or "(no subject)")
    received_at = str(email.get("receivedAt") or email.get("sentAt") or "")
    if not received_at:
        raise ValueError("Email is missing receivedAt/sentAt")
    return ParsedEntry(
        jmap_id=str(email["id"]),
        message_id=_message_id(email),
        publication=publication,
        subject=subject,
        canonical_url=canonical_url,
        received_at=received_at,
        body_html=sanitized_body,
        is_truncated=_is_truncated(sanitized_body),
    )


def slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "publication"


def strip_publication_prefix(subject: str, publication_name: str) -> str:
    prefix = f"[{publication_name}] "
    if subject.startswith(prefix):
        return subject[len(prefix) :]
    return subject


def _headers(email: dict[str, object]) -> dict[str, str]:
    raw = email.get("headers")
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and isinstance(value, str):
                result[name.lower()] = value
    return result


def _publication(email: dict[str, object], headers: dict[str, str]) -> Publication:
    list_id = headers.get("list-id")
    if list_id:
        parsed = LIST_ID_RE.match(list_id)
        if parsed:
            display = (parsed.group("name") or "").strip().strip('"')
            identifier = parsed.group("id").strip().lower()
            if not display:
                display = _display_from_domain(identifier)
            return Publication(
                slug=slugify(display),
                display_name=display,
                list_id=identifier,
            )

    from_name, from_addr = _from(email)
    display = from_name or _display_from_domain(from_addr.split("@")[-1])
    list_id_fallback = from_addr.split("@")[-1].lower() if from_addr else None
    return Publication(
        slug=slugify(display),
        display_name=display,
        list_id=list_id_fallback,
    )


def _from(email: dict[str, object]) -> tuple[str | None, str]:
    raw = email.get("from")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            name = first.get("name")
            email_addr = first.get("email")
            return (
                name if isinstance(name, str) and name else None,
                email_addr if isinstance(email_addr, str) else "",
            )
    header = _headers(email).get("from", "")
    name, addr = parseaddr(header)
    return name or None, addr


def _display_from_domain(domain: str) -> str:
    first = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return first.title() or "Publication"


def _message_id(email: dict[str, object]) -> str | None:
    raw = email.get("messageId")
    if isinstance(raw, list) and raw:
        return str(raw[0])
    if isinstance(raw, str):
        return raw
    headers = _headers(email)
    return headers.get("message-id")


def _html_body(email: dict[str, object]) -> str | None:
    return _body_value(email, "htmlBody")


def _text_body(email: dict[str, object]) -> str | None:
    return _body_value(email, "textBody")


def _body_value(email: dict[str, object], body_key: str) -> str | None:
    body_parts = email.get(body_key)
    body_values = email.get("bodyValues")
    if not isinstance(body_parts, list) or not isinstance(body_values, dict):
        return None
    values: list[str] = []
    for part in body_parts:
        if not isinstance(part, dict):
            continue
        part_id = part.get("partId")
        if not isinstance(part_id, str):
            continue
        body_value = body_values.get(part_id)
        if isinstance(body_value, dict) and isinstance(body_value.get("value"), str):
            values.append(str(body_value["value"]))
    return "\n".join(values) if values else None


def canonical_url_from_html(body_html: str, list_id: str | None) -> str | None:
    return _canonical_url(body_html, list_id)


SHARE_LINK_HOSTS = {"substack.com", "open.substack.com"}


def _canonical_url(body_html: str, list_id: str | None) -> str | None:
    extractor = _LinkExtractor()
    extractor.feed(body_html)
    domains = _candidate_domains(list_id)
    fallback: str | None = None
    share_skipped = 0
    rejected_hosts: list[str] = []
    for url in extractor.links:
        rewritten = rewrite_substack_redirect(url)
        parsed = urlparse(rewritten)
        host = parsed.netloc.lower()
        if not parsed.path.startswith("/p/"):
            continue
        if host in SHARE_LINK_HOSTS:
            share_skipped += 1
            continue
        if host.endswith(".substack.com") or host in domains:
            LOGGER.debug(
                "canonical_url_extracted",
                list_id=list_id,
                result=rewritten,
                reason="list_id_match" if host in domains else "substack_subdomain",
                share_links_skipped=share_skipped,
                total_links=len(extractor.links),
            )
            return rewritten
        rejected_hosts.append(host)
        if fallback is None:
            fallback = rewritten
    LOGGER.debug(
        "canonical_url_extracted",
        list_id=list_id,
        result=fallback,
        reason="fallback" if fallback else "no_match",
        share_links_skipped=share_skipped,
        non_matching_hosts=sorted(set(rejected_hosts)),
        total_links=len(extractor.links),
    )
    return fallback


def _candidate_domains(list_id: str | None) -> set[str]:
    if not list_id:
        return set()
    value = list_id.lower()
    if "@" in value:
        value = value.split("@", 1)[1]
    return {value}


def _is_truncated(body_html: str) -> bool:
    lowered = re.sub(r"<[^>]+>", " ", body_html).lower()
    return any(marker in lowered for marker in TRUNCATION_MARKERS)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)
