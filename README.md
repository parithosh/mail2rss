# mail2rss

A single-tenant Fastmail JMAP to Atom bridge for Substack newsletters.

`mail2rss` polls a configured Fastmail folder, parses Substack newsletter
emails, sanitizes the HTML, stores parsed entries in SQLite, writes Atom feeds,
and serves those feeds behind a long random URL secret.

## Status

Implemented v1:

- poll-based Fastmail JMAP sync
- SQLite state and idempotent entry storage
- Substack publication detection from `List-ID` / `From`
- HTML sanitization and basic Substack tracking cleanup
- per-publication Atom feeds plus `all.xml`
- `/healthz` and `/feeds/<secret>/<slug>.xml`
- Docker image and compose skeleton

Deferred:

- JMAP EventSource push
- explicit purge command
- auth in front of feed URLs
- live Fastmail/FreshRSS acceptance testing

## Fastmail Setup

1. Create a Fastmail API token with JMAP core and mail access.
2. Mark the token read-only in the Fastmail UI.
3. Create a Fastmail rule or Sieve script that moves Substack messages into a
   dedicated folder, default `Substacks`.
4. Put the token in an environment file:

```sh
FASTMAIL_TOKEN=your-token-here
```

Do not commit the token or bake it into the image.

## Configuration

Start from [config.example.toml](config.example.toml).

Important defaults:

- Fastmail folder: `Substacks`
- poll interval: `900` seconds
- feed output: `/var/lib/mail2rss/feeds`
- HTTP bind: `127.0.0.1:8080`
- feed path prefix: `/feeds`

The daemon also stores SQLite state in the parent of the feed directory by
default: `/var/lib/mail2rss/mail2rss.db`.

## Local Development

Install dependencies and run checks:

```sh
uv run ruff check
uv run ruff format --check
uv run mypy src tests
uv run pytest
```

Run the CLI:

```sh
FASTMAIL_TOKEN=... uv run mail2rss --config config.toml
```

If `--config` is omitted, built-in defaults are used.

## Feed URLs

On first run, `mail2rss` generates a random feed URL secret and stores it in
SQLite. Feed URLs look like:

```text
/feeds/<secret>/all.xml
/feeds/<secret>/<publication-slug>.xml
```

The full path list is logged at boot as `feed_urls_ready` — grab it from
`docker logs mail2rss`, no exec needed. `/healthz?show_url=1` (localhost only)
returns the same list.

FreshRSS can subscribe to those paths through Traefik, for example:

```text
https://mail2rss.indenwolken.xyz/feeds/<secret>/all.xml
```

Treat the secret path as bearer-token-equivalent. Anyone with the URL can fetch
the feed unless additional auth is placed in front of it.

## Filtering ingested mail

`mail2rss` is a generic email→RSS bridge. By default it ingests every message
in the configured mailbox. The `[filters]` config block lets you opt into
per-source rules without baking assumptions into the parser:

```toml
[filters]
require_list_id = false        # drop mail without a List-ID header
require_canonical_url = false  # drop mail when the parser can't find an article URL
subject_blocklist = []         # case-insensitive substring match on the subject
from_blocklist = []            # case-insensitive substring match on the From address
```

Each rule is independent and additive. Examples:

- **Substack newsletters only**: `require_canonical_url = true` catches
  most transactional mail (verification codes, payment receipts, "Premium
  active", recommendations) because they don't link to a `/p/<slug>` page.
- **Generic discussion list**: `require_list_id = true` is the right filter
  when every legitimate post carries a List-ID and you want to ignore replies
  sent directly to you.
- **Belt-and-suspenders**: combine with `subject_blocklist = ["verification"]`
  and `from_blocklist = ["no-reply@"]` for sources where the same domain sends
  both newsletters and transactional mail.

Skipped messages are logged at info level as `mail_skipped` with a `reason`
field (`missing_list_id`, `missing_canonical_url`, `subject_blocked:<pattern>`,
`from_blocked:<pattern>`).

### Disabling the URL secret

For localhost/LAN-only deployments, set `http.require_secret = false` in the
config. Feeds are then served at `/feeds/all.xml` and
`/feeds/<publication-slug>.xml` with no secret segment. Only do this when you
trust everyone who can reach the bind address — there is no other auth on the
feed endpoints. Pair with `bind = "127.0.0.1:8080"` or a `127.0.0.1:8080:8080`
Docker port mapping to keep it off your LAN.

## Health

`/healthz` returns JSON with:

- whether the service is currently healthy
- startup time
- last successful poll
- last failed poll
- last error
- current backoff
- shutdown state

Health is degraded before the first successful poll and when the last successful
poll is older than twice the configured poll interval.

## Docker

Build:

```sh
docker build -t mail2rss:local .
```

Smoke test the hardened image:

```sh
docker run --rm --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges \
  mail2rss:local --help
```

Compose:

```sh
docker compose up -d --build
```

The compose file expects `.env` to contain `FASTMAIL_TOKEN`. It mounts a named
volume at `/var/lib/mail2rss` for SQLite state and generated feeds.

## Security Notes

- The Fastmail token is read from an environment variable only.
- Raw tokens, subjects, sender addresses, message bodies, raw JMAP ids, and raw
  message ids must not appear in logs.
- Logs use salted, truncated hashes for email correlation.
- Already-published entries stay in feeds even if the source message is later
  removed from the Fastmail folder.
- Atom files are written with atomic temp-file replacement.
- SQLite is created with `0600` permissions.

## Test Fixtures

Fixtures under `tests/fixtures/` must be anonymized. See
[tests/fixtures/README.md](tests/fixtures/README.md).

Never commit real email addresses, private article text, tracking tokens,
unsubscribe URLs, raw JMAP ids, raw blob ids, or raw message ids.

## Live Acceptance

Before relying on the deployment, still run the live checks from
[IMPLEMENTATION_PLAN.MD](IMPLEMENTATION_PLAN.MD):

1. Poll a real Substack message from Fastmail.
2. Subscribe FreshRSS to `all.xml` and one publication feed.
3. Validate generated Atom through W3C Feed Validator.
4. Rotate the Fastmail token and verify clean failure/restart.
5. Simulate a Fastmail 503 and verify backoff/recovery.
