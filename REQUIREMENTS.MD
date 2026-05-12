# mail2rss

> A JMAP-native, read-only-token-scoped email-to-RSS bridge built for Fastmail
> and Substack paid newsletters. Runs as a single hardened container, outputs
> Atom feeds per publication.

Not to be confused with the unmaintained Rails/PHP/Cloudflare projects of the
same name — this one is JMAP-first, Fastmail-specific, and built for a
homelab security posture.

---

## Requirements

A single-tenant daemon that polls Fastmail via JMAP, finds Substack newsletter
emails in a designated folder, converts them to Atom feeds, and serves the
result for FreshRSS to consume.

## Goal

Take paid Substack content out of email and into FreshRSS, with no Substack
session-cookie maintenance, by leveraging the fact that paid subscribers
already receive full post bodies as email.

## Non-goals (v1)

- Multi-account / multi-user
- Sending or modifying mail (strictly read-only)
- IMAP fallback (JMAP only)
- Web UI for feed management — publications auto-discover from `From:` headers
- Generic email-to-RSS — Substack-specific parsing is fine and expected

## Upstream assumption

The user has a Fastmail rule (or Sieve script) that routes incoming Substack
mail into a dedicated folder (default name: `Substacks`). This service does
not classify mail itself.

---

## Architecture

```
Fastmail (JMAP)
    │  bearer token, read-only
    ▼
poller (long-running daemon)
    │  - Mailbox/query → mailboxId
    │  - Email/queryChanges since stored state
    │  - Email/get for new ids (chained call, back-ref)
    │  - parse + sanitize HTML
    ▼
SQLite state (last queryState, dedup index, parsed entries)
    │
    ▼
Atom file writer (atomic temp+rename)
    │
    ▼
/var/lib/mail2rss/feeds/
    ├── all.xml             # aggregate
    ├── stratechery.xml     # one per detected publication
    ├── pragmatic-engineer.xml
    └── ...
    │
    ▼
Static HTTP server (built-in, behind Traefik)
    │
    ▼
FreshRSS subscribes to https://mail2rss.indenwolken.xyz/<secret>/<slug>.xml
```

### Process model

- Single long-running process, not cron.
- Two concurrent loops:
  1. **Poll loop** (default 900 s) — JMAP query, parse, write feeds.
  2. **HTTP server** — serves `/healthz` and `/feeds/<secret>/<slug>.xml`.
- Optional **JMAP push** mode using the JMAP session `eventSourceUrl` URI
  template for near-instant updates; falls back to poll on disconnect. Implement
  poll first, push as a follow-up flag (`push.enabled = true`).
- Graceful shutdown on SIGTERM: finish in-flight poll, flush state, exit 0.

---

## Fastmail JMAP integration

Read these before coding:

- API entry point and token docs: <https://www.fastmail.com/dev/>
- JMAP crash course: <https://jmap.io/crash-course.html>
- Fastmail's official samples: <https://github.com/fastmail/JMAP-Samples>
- RFC 8620 (JMAP core), RFC 8621 (JMAP mail)

### Auth

- Single API token, scope `urn:ietf:params:jmap:core` + `urn:ietf:params:jmap:mail`,
  flagged **read-only** at token creation in
  Settings → Privacy & Security → Manage API tokens.
- Header: `Authorization: Bearer <token>` on every request.
- On startup, GET `https://api.fastmail.com/jmap/session` and verify:
  - `capabilities` contains `urn:ietf:params:jmap:mail`.
  - Best-effort scope sanity checks: if the session response exposes enough
    information to distinguish read-only from writable mail access, fail loudly
    when the token is writable. Fastmail may not expose this directly, so the
    primary control is creating the API token as read-only in the Fastmail UI.
  - Cache `apiUrl`, `eventSourceUrl`, `accountId` from
    `primaryAccounts["urn:ietf:params:jmap:mail"]`.
- On `401` re-discover the session once, then exit non-zero with a clear log
  line so the supervisor can alert.

### JMAP method calls

Prefer chained method calls with back-references (`#ids`) to minimize round trips.
Single POST to `apiUrl` per poll cycle:

```jsonc
{
  "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
  "methodCalls": [
    ["Mailbox/query", {
      "accountId": "$ACCOUNT",
      "filter": { "name": "Substacks" }
    }, "0"],

    ["Email/queryChanges", {
      "accountId": "$ACCOUNT",
      "sinceQueryState": "$STORED_STATE",
      "filter": { "inMailboxOtherThan": [], "inMailbox": "#0/ids/0" },
      "sort": [{ "property": "receivedAt", "isAscending": false }],
      "calculateTotal": false,
      "maxChanges": 200
    }, "1"],

    ["Email/get", {
      "accountId": "$ACCOUNT",
      "#ids": { "resultOf": "1", "name": "Email/queryChanges", "path": "/added/*/id" },
      "properties": [
        "id", "blobId", "threadId", "messageId",
        "from", "subject", "receivedAt", "sentAt",
        "headers", "preview", "htmlBody", "bodyValues"
      ],
      "bodyProperties": ["partId", "blobId", "size", "type", "charset"],
      "fetchHTMLBodyValues": true
    }, "2"]
  ]
}
```

- First poll: no `sinceQueryState` → use `Email/query` instead of
  `queryChanges`, sorted descending by `receivedAt`, capped at, say, 50
  entries to avoid backfilling years of Substack archives. Store the
  resulting `queryState`.
- Subsequent polls: `Email/queryChanges` with stored state → only added/removed.
- Removed messages are not deleted from generated feeds by default. Once an
  entry has been published, keep it unless an explicit future purge feature
  removes it from SQLite and rewrites the feeds.
- On `cannotCalculateChanges` error: fall back to `Email/query` and reconcile
  via SQLite dedup.
- Honor `rateLimit` `SetError` with exponential backoff (jittered, base 30 s,
  cap 30 min).

### Why JMAP and not IMAP

- Single HTTP round trip for query + fetch.
- Server-side filtering by mailbox by id.
- `queryChanges` for incremental sync.
- EventSource push for near-instant delivery.
- Token-based auth (rotatable, scoped read-only) instead of an app password
  with full mail rights.

---

## Security (non-negotiable)

- **Token handling**
  - Loaded from env var `FASTMAIL_TOKEN`.
  - Sourced from a secrets file (`~/.secrets`-style on host, mounted in via
    Docker `env_file`); never committed, never baked into the image.
  - Never appears in logs, error messages, panics, or stack traces.
  - On startup, the process logs only the token's prefix (first 4 chars)
    for ops debugging.
- **Token scope enforcement**
  - Refuse to start if the session response indicates submission scope is
    granted (`urn:ietf:params:jmap:submission`).
  - If Fastmail exposes a read-only flag in the session response, require it;
    otherwise log that read-only verification is unavailable and rely on the
    Fastmail UI token setting.
- **Network egress**
  - Only `api.fastmail.com` (and `*.fastmail.com` if needed for EventSource).
  - Document this for Pari's pfSense egress rules; the container should run
    on a VLAN with restricted outbound.
- **Container hardening**
  - Non-root user (uid 1000:1000).
  - Read-only root filesystem; only `/var/lib/mail2rss` writable.
  - `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.
  - Distroless or alpine final stage; no shell in the image if feasible.
- **HTTP server**
  - Binds 127.0.0.1 by default; expose via Traefik only.
  - Feed paths include a long random token: `/feeds/<secret>/<slug>.xml`.
    The secret is generated on first run, written to the SQLite state, and
    surfaced via `/healthz?show_url=1` only when called from localhost.
  - Recommend pairing with Pocket ID via Traefik forward auth — feed paths
    are bearer-token-equivalent if leaked, since FreshRSS will fetch them
    unauthenticated.
- **Content sanitization**
  - All HTML bodies pass through `bleach` (or equivalent) with an allow-list:
    block `<script>`, `<iframe>`, `<object>`, `on*` attributes, `javascript:`
    URIs, suspicious `data:` URIs.
  - Strip known Substack tracking pixels and unsubscribe links before
    embedding in Atom.
  - Do not auto-fetch remote images or links during processing.
- **Filesystem**
  - SQLite DB perms `0600`, owned by the container user.
  - Atom files written atomically (`tempfile.NamedTemporaryFile` + `os.replace`).
- **Logging**
  - Structured (JSON), to stdout.
  - Fields: timestamp, level, event, mailbox_id, hashed_email_id,
    publication_slug, counts. `hashed_email_id` is a salted, truncated digest
    of the internal JMAP id for correlation without exposing raw IDs.
    **Never** subjects, bodies, sender addresses, raw message IDs, raw JMAP IDs,
    or tokens.
  - Log level configurable; default `info`.
- **Error handling**
  - Failures are loud and don't silently degrade. A failed poll surfaces in
    `/healthz` (last-success timestamp).
  - SIGTERM: finish current poll, flush state, exit 0.

---

## Substack-specific parsing

- **Publication detection**
  - Primary: `List-ID` header (Substack sets this to the publication, e.g.
    `Stratechery <stratechery.substack.com>` or for custom domains
    `<list.publication.com>`).
  - Fallback: `From:` address local-part / domain.
  - Slug: kebab-case from publication name; persist mapping in SQLite so
    renames don't fragment feeds.
- **Body extraction**
  - Prefer the HTML body part. If only text, embed with `<pre>`.
  - Extract canonical post URL from the first link to `*.substack.com/p/*`
    or matching custom-domain pattern; use as `<link>` in the Atom entry.
- **Tracking removal**
  - Strip Substack `email.mg2.substack.com` redirector wrappers around links
    (rewrite to underlying URL).
  - Drop `<img>` tags pointing to known pixel hosts.
  - Drop the boilerplate footer (unsubscribe, manage subscription, "View in
    browser") — match by anchor href patterns rather than text, for i18n
    safety.
- **Truncation detection**
  - If body contains "Read in app", "Continue reading", or a known paywall
    stub, log a `warning` with the publication slug. Some Substacks send
    truncated emails even to paid subs. Don't fail the entry — just flag.

---

## Data model (SQLite, WAL mode)

```sql
CREATE TABLE state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- known keys: jmap_query_state, feed_url_secret, schema_version

CREATE TABLE publications (
  slug          TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  list_id       TEXT,
  first_seen_at TEXT NOT NULL
);

CREATE TABLE entries (
  jmap_id        TEXT PRIMARY KEY,
  message_id     TEXT,
  publication    TEXT NOT NULL REFERENCES publications(slug),
  subject        TEXT NOT NULL,
  canonical_url  TEXT,
  received_at    TEXT NOT NULL,
  processed_at   TEXT NOT NULL,
  body_html      TEXT NOT NULL          -- already sanitized
);
CREATE INDEX entries_pub_received ON entries(publication, received_at DESC);
CREATE UNIQUE INDEX entries_message_id_unique
  ON entries(message_id)
  WHERE message_id IS NOT NULL;
CREATE UNIQUE INDEX entries_canonical_url_unique
  ON entries(canonical_url)
  WHERE canonical_url IS NOT NULL;
```

- `body_html` lives in SQLite so feed regeneration doesn't require re-fetching
  Fastmail. Acceptable size: paid posts are typically <500 KB.
- `jmap_id` is the primary idempotency key. `message_id` and `canonical_url`
  are optional secondary dedup keys when present, to avoid duplicate entries if
  the same post is reimported or appears under a different JMAP id.
- Schema migrations: simple `schema_version` row + numbered migration scripts.

---

## Atom output

- One file per `publications.slug`: `<output_dir>/<slug>.xml`.
- One aggregate: `<output_dir>/all.xml`.
- Feed-level: title (`Substack: Stratechery`), id (urn-form, stable per slug),
  updated (max entry `received_at`), self link.
- Entry-level:
  - `id`: `urn:fastmail-email:<jmap_id>` — stable, immutable.
  - `title`: subject with `[Publication Name] ` prefix removed if present.
  - `link`: canonical Substack post URL.
  - `published` / `updated`: `received_at`.
  - `author`: publication display name.
  - `content` (type=html): sanitized body.
- Cap entries per feed at `output.max_entries_per_feed` (default 100).
- Generated with `feedgen`. Validates against W3C Feed Validator.

---

## Configuration

`config.toml`:

```toml
[fastmail]
token_env  = "FASTMAIL_TOKEN"      # name of env var to read
mailbox    = "Substacks"            # exact folder name
session_url = "https://api.fastmail.com/jmap/session"

[poll]
interval_seconds = 900
initial_backfill = 50               # max entries on first run

[push]
enabled = false                     # JMAP EventSource

[output]
dir                  = "/var/lib/mail2rss/feeds"
aggregate_filename   = "all.xml"
max_entries_per_feed = 100

[http]
bind                 = "127.0.0.1:8080"
feed_url_path_prefix = "/feeds"

[log]
level  = "info"
format = "json"
```

Validate at startup with `pydantic`. Refuse to run with missing token,
unreadable output dir, or non-existent mailbox (verified via JMAP on boot).

---

## Tech stack

- Python 3.12+
- `httpx` (HTTP/2, async-capable)
- `feedgen`
- `bleach` for HTML sanitization
- `pydantic` v2 for config + JMAP response models
- `sqlite3` (stdlib) — no ORM
- `structlog` for JSON logging
- `pytest` + `pytest-httpx` for unit/integration tests
- `ruff`, `mypy --strict`
- `uv` for dependency management

---

## Testing

- **Unit**: HTML sanitizer (XSS payloads, tracking-pixel removal, link
  rewriting), List-ID parser, slug derivation, Atom rendering.
- **Integration**: fake JMAP server using recorded Fastmail responses
  (sanitized, anonymized fixtures committed to repo). Drives the full pipeline
  from session discovery to written Atom file.
- **Fixture anonymization**: before committing any fixture, replace real email
  addresses, names, domains, message ids, JMAP ids, URLs with tokens, tracking
  ids, unsubscribe links, and paid/private article text. Keep only the minimal
  HTML structure needed to test parsing, sanitization, link rewriting,
  truncation detection, and feed rendering. Use synthetic publication names and
  domains such as `example.substack.com` unless a public domain pattern is
  required for parser coverage.
- **Property tests**: feed re-generation is deterministic for a given DB
  state.
---

## Deployment

- Multi-stage Dockerfile, distroless final stage where possible.
- `docker-compose.yml` with Traefik labels for `mail2rss.indenwolken.xyz`.
- Compose healthcheck: `wget -qO- http://127.0.0.1:8080/healthz || exit 1`,
  considered healthy when `last_successful_poll < 2 × interval_seconds` ago.
  If the final image is distroless, run the healthcheck from Docker Compose or
  Traefik rather than requiring `wget` inside the application image.
- Volumes: one persistent for SQLite + atom files.
- `env_file` for `FASTMAIL_TOKEN`.

---

## Acceptance criteria

1. Pari publishes a paid post in a subscribed Substack → it appears in
   FreshRSS within `interval_seconds + 1 min`, full body, no truncation
   (assuming the publication sends full bodies in email).
2. Atom files validate via W3C Feed Validator (`feedvalidator.org`).
3. `mypy --strict` clean. `ruff check` clean.
4. `docker run --read-only --cap-drop=ALL --security-opt=no-new-privileges`
   works.
5. No tokens, message-IDs, message bodies, or sender addresses appear in any
   log line at any level.
6. Killing the container mid-poll and restarting doesn't duplicate entries
   in the feed (idempotency via SQLite dedup).
7. Rotating the Fastmail token: process exits non-zero with a clear log line;
   restarting with the new token resumes from stored `queryState` without
   replaying old entries.
8. Manual chaos test: Fastmail returns 503 → process backs off, recovers,
   no entries lost.

---

## Open questions for Pari

1. **JMAP push vs poll**: poll is simpler. Push gives near-instant updates
   and reduces JMAP request volume. Want push in v1 or save for v2?
2. **Single feed or per-publication**: per-publication is the default in
   this spec. The aggregate `all.xml` is also produced. Confirm this matches
   your FreshRSS organization preference.
3. **Auth on the feed URL**: random-token-in-path only, or also require
   Pocket ID via Traefik forward auth? FreshRSS supports HTTP basic on feeds,
   so basic-auth + Traefik is viable.
4. **Sieve vs Fastmail UI rules**: spec assumes any rule that lands mail in
   `Substacks/`. If you'd rather codify the rule, a Sieve snippet can live
   in `deploy/sieve/substacks.sieve` and you apply it manually via the
   Fastmail UI. Out of scope for the daemon itself.
5. **Hosting**: same Hetzner VM as the rest of indenwolken.xyz, or break out
   onto a separate small instance with stricter egress?
