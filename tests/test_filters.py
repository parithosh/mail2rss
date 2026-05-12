from __future__ import annotations

from datetime import UTC, datetime

from mail2rss.config import FilterConfig
from mail2rss.filters import filter_post_parse, filter_pre_parse
from mail2rss.models import ParsedEntry, Publication


def _email(
    subject: str = "Hello",
    from_addr: str = "writer@example.com",
    list_id: str | None = "<example.substack.com>",
) -> dict[str, object]:
    headers: list[dict[str, str]] = []
    if list_id is not None:
        headers.append({"name": "List-ID", "value": list_id})
    return {
        "id": "jmap-1",
        "subject": subject,
        "from": [{"name": "Writer", "email": from_addr}],
        "headers": headers,
    }


def _parsed(canonical_url: str | None = "https://example.com/p/x") -> ParsedEntry:
    return ParsedEntry(
        jmap_id="jmap-1",
        message_id="<m@example>",
        publication=Publication(
            slug="example", display_name="Example", list_id="example.substack.com"
        ),
        subject="Hello",
        canonical_url=canonical_url,
        received_at=datetime.now(UTC).isoformat(),
        body_html="<p>x</p>",
        is_truncated=False,
    )


def test_defaults_accept_everything() -> None:
    cfg = FilterConfig()
    assert filter_pre_parse(_email(list_id=None), cfg) is None
    assert filter_post_parse(_parsed(canonical_url=None), cfg) is None


def test_require_list_id() -> None:
    cfg = FilterConfig(require_list_id=True)
    assert filter_pre_parse(_email(list_id=None), cfg) == "missing_list_id"
    assert filter_pre_parse(_email(), cfg) is None


def test_subject_blocklist_substring_case_insensitive() -> None:
    cfg = FilterConfig(subject_blocklist=["Verification Code"])
    assert (
        filter_pre_parse(_email(subject="123456 is your verification code"), cfg)
        == "subject_blocked:Verification Code"
    )
    assert filter_pre_parse(_email(subject="weekly digest"), cfg) is None


def test_from_blocklist_substring_case_insensitive() -> None:
    cfg = FilterConfig(from_blocklist=["No-Reply@"])
    assert (
        filter_pre_parse(_email(from_addr="no-reply@substack.com"), cfg)
        == "from_blocked:No-Reply@"
    )
    assert filter_pre_parse(_email(from_addr="writer@substack.com"), cfg) is None


def test_require_canonical_url() -> None:
    cfg = FilterConfig(require_canonical_url=True)
    assert (
        filter_post_parse(_parsed(canonical_url=None), cfg) == "missing_canonical_url"
    )
    assert filter_post_parse(_parsed(), cfg) is None
