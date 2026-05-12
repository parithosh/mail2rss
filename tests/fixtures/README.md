# Fixture Privacy Checklist

Fixtures must be anonymized before commit.

- Use only synthetic names and addresses.
- Use `example.*`, `.invalid`, or explicit test-only domains.
- Replace raw Fastmail JMAP ids, blob ids, and message ids with synthetic ids.
- Remove tokenized URLs, unsubscribe links, tracking ids, and private article
  text.
- Keep only the minimum HTML structure required to test parsing and sanitizing.
