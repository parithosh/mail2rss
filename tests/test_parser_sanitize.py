from __future__ import annotations

import json
from pathlib import Path

from mail2rss.parser import (
    canonical_url_from_html,
    parse_email,
    slugify,
    strip_publication_prefix,
)
from mail2rss.sanitize import rewrite_substack_redirect, sanitize_html


def test_parse_full_post_fixture() -> None:
    email = json.loads(Path("tests/fixtures/full_post.json").read_text())

    entry = parse_email(email)

    assert entry.publication.slug == "example-publication"
    assert entry.publication.display_name == "Example Publication"
    assert entry.canonical_url == "https://example.substack.com/p/paid-post-title"
    assert "Synthetic article paragraph." in entry.body_html
    assert "<script" not in entry.body_html
    assert "email.mg2.substack.com/o/fixture" not in entry.body_html


def test_sanitize_strips_style_block_and_content() -> None:
    html = (
        "<p>before</p>"
        "<style>@media (max-width: 1024px) { .typography .foo { color: red; } }</style>"
        "<p>after</p>"
    )
    cleaned = sanitize_html(html)
    assert "before" in cleaned
    assert "after" in cleaned
    assert "@media" not in cleaned
    assert "color: red" not in cleaned
    assert ".typography" not in cleaned


def test_sanitize_strips_script_block_and_content() -> None:
    html = '<p>safe</p><script>alert("xss"); var x = 1;</script><p>more</p>'
    cleaned = sanitize_html(html)
    assert "safe" in cleaned
    assert "more" in cleaned
    assert "alert" not in cleaned
    assert "var x" not in cleaned


def test_sanitize_removes_dangerous_html() -> None:
    html = (
        '<p onclick="alert(1)">Hello</p>'
        '<a href="javascript:alert(1)">bad</a>'
        '<iframe src="https://example.invalid"></iframe>'
    )

    cleaned = sanitize_html(html)

    assert "onclick" not in cleaned
    assert "javascript:" not in cleaned
    assert "<iframe" not in cleaned
    assert "Hello" in cleaned


def test_slug_and_title_helpers() -> None:
    assert slugify("The Pragmatic Engineer") == "the-pragmatic-engineer"
    assert (
        strip_publication_prefix(
            "[The Pragmatic Engineer] Weekly", "The Pragmatic Engineer"
        )
        == "Weekly"
    )


def test_rewrite_substack_redirect_new_format() -> None:
    import base64
    import json

    target = "https://www.viksnewsletter.com/p/power-delivery?utm_campaign=x"
    payload = base64.urlsafe_b64encode(json.dumps({"e": target}).encode()).rstrip(b"=")
    url = f"https://substack.com/redirect/2/{payload.decode()}"

    assert rewrite_substack_redirect(url) == target


def test_rewrite_substack_redirect_invalid_payload_passthrough() -> None:
    url = "https://substack.com/redirect/2/not-valid-base64!"
    assert rewrite_substack_redirect(url) == url


def test_rewrite_substack_redirect_signed_payload() -> None:
    import base64
    import json

    target = "https://example.com/p/article"
    payload = (
        base64.urlsafe_b64encode(json.dumps({"e": target}).encode())
        .rstrip(b"=")
        .decode()
    )
    # Substack's real format suffixes a signature segment after a `.`.
    signed = f"{payload}.signaturepartABC_-"
    url = f"https://substack.com/redirect/2/{signed}"

    assert rewrite_substack_redirect(url) == target


def test_canonical_url_accepts_custom_domain() -> None:
    import base64
    import json

    target = "https://www.viksnewsletter.com/p/example-post"
    payload = base64.urlsafe_b64encode(json.dumps({"e": target}).encode()).rstrip(b"=")
    body = (
        f'<p><a href="https://substack.com/@author">profile</a>'
        f'<a href="https://substack.com/redirect/2/{payload.decode()}">read</a></p>'
    )
    assert canonical_url_from_html(body, "viksnewsletter.substack.com") == target


def test_canonical_url_ignores_share_links() -> None:
    body = (
        '<a href="https://substack.com/p/some-share">share</a>'
        '<a href="https://example.substack.com/p/real-post">read</a>'
    )
    assert (
        canonical_url_from_html(body, "example.substack.com")
        == "https://example.substack.com/p/real-post"
    )
