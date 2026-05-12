from __future__ import annotations

import base64
import binascii
import html
import json
from html.parser import HTMLParser
from typing import cast
from urllib.parse import parse_qs, unquote, urlparse

import bleach

ALLOWED_TAGS = [
    "a",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "ul",
]

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}

ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

FOOTER_HREF_MARKERS = (
    "unsubscribe",
    "manage_subscription",
    "manage-subscription",
    "email-settings",
    "action=unsubscribe",
)

PIXEL_HOST_MARKERS = (
    "email.mg2.substack.com",
    "substackcdn.com",
)

DROP_TAGS_WITH_CONTENT = frozenset({"style", "script", "noscript", "head", "title"})


def sanitize_html(raw_html: str) -> str:
    rewritten = _SubstackHtmlRewriter().rewrite(raw_html)
    return cast(
        str,
        bleach.clean(
            rewritten,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=ALLOWED_PROTOCOLS,
            strip=True,
        ),
    )


def text_to_html(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def rewrite_substack_redirect(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "email.mg2.substack.com":
        params = parse_qs(parsed.query)
        for key in ("url", "u", "redirect", "target"):
            values = params.get(key)
            if values:
                candidate = unquote(values[0])
                if candidate.startswith(("http://", "https://")):
                    return candidate
        return url
    if host == "substack.com" and parsed.path.startswith("/redirect/"):
        decoded = _decode_substack_redirect_payload(parsed.path)
        if decoded is not None:
            return decoded
    return url


def _decode_substack_redirect_payload(path: str) -> str | None:
    parts = path.rstrip("/").split("/")
    if len(parts) < 4:
        return None
    # The trailing segment is a JWS-style "<base64-json>.<base64-signature>".
    # Only the first segment decodes to the JSON we care about.
    payload = parts[-1].split(".", 1)[0]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    target = data.get("e")
    if isinstance(target, str) and target.startswith(("http://", "https://")):
        return target
    return None


def is_footer_href(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in FOOTER_HREF_MARKERS)


def is_tracking_pixel(src: str, width: str | None, height: str | None) -> bool:
    parsed = urlparse(src)
    host = parsed.netloc.lower()
    host_matches = any(marker in host for marker in PIXEL_HOST_MARKERS)
    tiny = width in {"1", "0"} or height in {"1", "0"}
    marker = "open" in parsed.path.lower() or "track" in parsed.path.lower()
    return host_matches and (tiny or marker)


class _SubstackHtmlRewriter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.skip_depth = 0

    def rewrite(self, raw_html: str) -> str:
        self.feed(raw_html)
        self.close()
        return "".join(self.parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value for key, value in attrs}
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag.lower() in DROP_TAGS_WITH_CONTENT:
            self.skip_depth = 1
            return
        if tag.lower() == "a":
            href = attrs_dict.get("href")
            if href and is_footer_href(href):
                self.skip_depth = 1
                return
        if tag.lower() == "img":
            src = attrs_dict.get("src")
            if src and is_tracking_pixel(
                src, attrs_dict.get("width"), attrs_dict.get("height")
            ):
                return

        rewritten_attrs: list[tuple[str, str]] = []
        for key, value in attrs:
            if value is None:
                continue
            lowered_key = key.lower()
            if lowered_key.startswith("on"):
                continue
            if tag.lower() == "a" and lowered_key == "href":
                value = rewrite_substack_redirect(value)
            rewritten_attrs.append((key, value))
        self.parts.append(_format_start_tag(tag, rewritten_attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if not self.skip_depth and tag.lower() not in {"br", "hr", "img"}:
            self.parts.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.skip_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.skip_depth:
            self.parts.append(f"&#{name};")


def _format_start_tag(tag: str, attrs: list[tuple[str, str]]) -> str:
    if not attrs:
        return f"<{tag}>"
    rendered = " ".join(
        f'{key}="{html.escape(value, quote=True)}"' for key, value in attrs
    )
    return f"<{tag} {rendered}>"
