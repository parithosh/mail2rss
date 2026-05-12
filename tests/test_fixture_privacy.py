from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ALLOWED_DOMAIN_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.invalid",
    ".invalid",
    ".substack.com",
)
TOKEN_KEYS = {"token", "signature", "sig", "jwt", "session", "key", "auth"}


def test_fixtures_do_not_contain_obvious_private_data() -> None:
    for path in Path("tests/fixtures").glob("*"):
        if path.name == "README.md":
            continue
        text = path.read_text()
        _assert_no_private_emails(path, text)
        _assert_no_tokenized_urls(path, text)


def _assert_no_private_emails(path: Path, text: str) -> None:
    for match in re.finditer(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text):
        domain = match.group(1).lower()
        assert domain.endswith(("example.invalid", "example.com")), (
            f"{path} contains non-fixture email domain: {domain}"
        )


def _assert_no_tokenized_urls(path: Path, text: str) -> None:
    for raw_url in re.findall(r"https?://[^\"'<>\s]+", text):
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower()
        assert host.endswith(ALLOWED_DOMAIN_SUFFIXES), (
            f"{path} contains non-fixture URL host: {host}"
        )
        query = parse_qs(parsed.query)
        forbidden = TOKEN_KEYS.intersection(query)
        forbidden.update(key for key in query if key.startswith("utm_"))
        assert not forbidden, f"{path} contains token-like query keys: {forbidden}"
