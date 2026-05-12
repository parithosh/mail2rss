# mail2rss

A small Fastmail → Atom bridge. Point it at a folder in your Fastmail account,
get an RSS/Atom feed of the messages in it. Useful for newsletters (Substack
and others), mailing lists, or any service that emails you instead of offering
a feed.

- Polls Fastmail over JMAP, no IMAP and no third-party relays.
- Sanitizes the HTML before serving it (no scripts, no tracking pixels).
- Stores entries in SQLite so historical posts stay in the feed even if the
  source mail is later deleted.
- Serves Atom 1.0 over HTTP for FreshRSS / Miniflux / NetNewsWire / etc.
- Per-publication feeds (auto-detected from `List-ID` / `From`) plus an
  aggregate `all.xml`.

## Quickstart (Docker Compose)

```sh
git clone https://github.com/parithosh/mail2rss.git
cd mail2rss

# 1. Fastmail API token (read-only, mail scope is enough)
cp .env.example .env
$EDITOR .env          # set FASTMAIL_TOKEN=fmu1-...

# 2. Pick a mailbox + tweak settings
cp config.example.toml config.toml
$EDITOR config.toml   # at minimum: [fastmail].mailbox

# 3. Run
docker compose up -d --build

# 4. Find the feed URLs (logged on every startup)
docker compose logs mail2rss | grep feed_urls_ready
```

You'll see something like:

```json
{"event": "feed_urls_ready", "bind": "0.0.0.0:8080", "paths": ["/feeds/<secret>/all.xml", "/feeds/<secret>/<pub>.xml"]}
```

Subscribe your reader to `http://<host>:8080/feeds/<secret>/all.xml`.

## Fastmail setup

1. Create an API token in Fastmail settings → Privacy & Security → API tokens.
2. Give it **JMAP core** + **Mail (read-only)** scopes.
3. (Optional but recommended) Set up a Sieve rule that files newsletter mail
   into a dedicated folder, e.g. `Newsletters` or `Substack`. Point
   `[fastmail].mailbox` at that folder name. The lookup is exact-match.

The token is never written to disk or logs — only the four-character prefix
(`fmu1`) is logged so you can confirm the right one loaded.

## Configuration

`config.toml` keys (full example in `config.example.toml`):

| Section / key | Default | Notes |
| --- | --- | --- |
| `[fastmail].mailbox` | `Substacks` | Exact folder name in Fastmail. |
| `[fastmail].token_env` | `FASTMAIL_TOKEN` | Env var to read the token from. |
| `[poll].interval_seconds` | `900` | 15 min. Min 30 s. |
| `[poll].initial_backfill` | `50` | Messages pulled on first sync. |
| `[output].dir` | `/var/lib/mail2rss/feeds` | Where Atom files are written. |
| `[output].max_entries_per_feed` | `100` | Per `<slug>.xml` and `all.xml`. |
| `[http].bind` | `127.0.0.1:8080` | Use `0.0.0.0:8080` with Docker port mapping. |
| `[http].require_secret` | `true` | See **Feed URL security** below. |
| `[filters].*` | all off | See **Filtering**. |
| `[log].level` / `[log].format` | `info` / `json` | |

### Feed URL security

By default, feeds live at `/feeds/<random-secret>/all.xml`. The secret is
generated on first run, stored in SQLite, and treated as a bearer token
embedded in the URL — anyone with the URL can read the feed. The full path
list is logged at boot under the `feed_urls_ready` event.

For local-only deployments, you can drop the secret:

```toml
[http]
bind = "127.0.0.1:8080"
require_secret = false
```

Feeds then live at `/feeds/all.xml` and `/feeds/<slug>.xml`. Only do this when
the bind address is unreachable from the outside (loopback, private network,
or a `127.0.0.1:8080:8080` Docker port mapping).

If you expose `mail2rss` publicly, either keep the secret on or put proper
auth (Traefik / Caddy / nginx basic-auth, OAuth proxy, VPN, etc.) in front of
it.

### Filtering

`mail2rss` is a generic email → RSS bridge: by default every message in the
configured mailbox becomes a feed entry. The `[filters]` block lets you opt
into per-source noise filters without baking assumptions into the parser.

```toml
[filters]
require_list_id = false        # drop mail without a List-ID header
require_canonical_url = false  # drop mail when the parser can't extract an article URL
subject_blocklist = []         # case-insensitive substring match on Subject
from_blocklist = []            # case-insensitive substring match on the From address
```

Combinations that work well in practice:

- **Substack-only mailbox**: `require_canonical_url = true` catches most
  transactional mail (verification codes, payment receipts, "Premium active",
  recommendations) because they don't link to a `/p/<slug>` post page.
- **Mailing list / Google Groups**: `require_list_id = true` filters out
  direct replies and one-off mail to the same address.
- **Mixed sources**: layer `subject_blocklist = ["verification code", "payment
  receipt"]` and `from_blocklist = ["no-reply@", "billing@"]` on top.

Skipped messages are logged at info as `mail_skipped` with a `reason` field
(`missing_list_id`, `missing_canonical_url`, `subject_blocked:<pattern>`,
`from_blocked:<pattern>`) so you can iterate on rules from real data.

## Health and observability

`/healthz` returns JSON with the daemon's current state — useful for Docker
healthchecks, k8s probes, or just a quick `curl` from the host:

```json
{
  "healthy": true,
  "started_at": "...",
  "last_successful_poll": "...",
  "last_failed_poll": null,
  "last_error": null,
  "current_backoff_seconds": 0.0,
  "shutting_down": false
}
```

Health goes degraded before the first successful poll, and if the last
success is older than twice the poll interval.

When called from `127.0.0.1`/`::1`, `/healthz?show_url=1` also returns the
current feed paths — handy if you've lost the boot log.

Logs are structured JSON; key events:

| event | meaning |
| --- | --- |
| `feed_urls_ready` | Atom files written and HTTP server is up. |
| `poll_completed` | Per-cycle summary: `fetched_count`, `inserted_count`, `skipped_count`, `feed_count`. |
| `mail_skipped` | An incoming mail was dropped by `[filters]`; includes `reason`. |
| `poll_failed` | Polling errored; includes `backoff_seconds` until retry. |
| `canonical_url_backfilled` | Older entries got article URLs after a parser improvement. |

Email-identifying fields in logs are salted-hashed; raw subjects, addresses,
bodies, and JMAP/Message-IDs never appear.

## Running without Docker

```sh
uv sync
FASTMAIL_TOKEN=fmu1-... uv run mail2rss --config config.toml
```

If `--config` is omitted, built-in defaults are used. SQLite lands at
`/var/lib/mail2rss/mail2rss.db` by default; override with `db_path` at the
top level of `config.toml` if you'd rather keep it elsewhere.

## Development

```sh
uv sync --all-extras
uv run ruff check
uv run ruff format --check
uv run mypy src tests
uv run pytest
```

Test fixtures under `tests/fixtures/` must be anonymized — never commit real
email addresses, private article text, unsubscribe links, or raw JMAP/blob
IDs. See `tests/fixtures/README.md`.

## Security notes

- The Fastmail token is read from an environment variable only and never
  persisted by the daemon.
- SQLite is created with `0600` permissions.
- Atom files are written via atomic temp-file replace so readers never see a
  truncated feed.
- HTML is sanitized via `bleach` with a strict allowlist (no scripts, no
  iframes, no event handlers). Substack tracking pixels and unsubscribe
  footers are stripped before sanitization.
- Already-published entries stay in feeds even if the source mail is later
  deleted from the Fastmail folder — readers won't randomly lose history.

## License

Not yet licensed. Treat as all-rights-reserved until a LICENSE file is added.
