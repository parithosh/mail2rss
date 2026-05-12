from __future__ import annotations

from mail2rss.config import FilterConfig
from mail2rss.models import ParsedEntry
from mail2rss.parser import _from, _headers


def filter_pre_parse(email: dict[str, object], cfg: FilterConfig) -> str | None:
    headers = _headers(email)
    if cfg.require_list_id and "list-id" not in headers:
        return "missing_list_id"
    subject = str(email.get("subject") or "")
    if subject and cfg.subject_blocklist:
        lowered = subject.lower()
        for pattern in cfg.subject_blocklist:
            if pattern and pattern.lower() in lowered:
                return f"subject_blocked:{pattern}"
    if cfg.from_blocklist:
        _name, addr = _from(email)
        if addr:
            lowered_addr = addr.lower()
            for pattern in cfg.from_blocklist:
                if pattern and pattern.lower() in lowered_addr:
                    return f"from_blocked:{pattern}"
    return None


def filter_post_parse(parsed: ParsedEntry, cfg: FilterConfig) -> str | None:
    if cfg.require_canonical_url and parsed.canonical_url is None:
        return "missing_canonical_url"
    return None
