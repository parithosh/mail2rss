# mail2rss Implementation Plan

This plan implements the poll-first v1 from `REQUIREMENTS.MD`. JMAP push is
left as a follow-up after the daemon, parser, persistence, feeds, and container
path are stable.

## Principles

- Keep the first version boring: one process, one account, one configured
  Fastmail mailbox, poll-only sync.
- Use a synchronous runtime for v1: blocking `httpx`, stdlib `sqlite3`, a
  threaded stdlib HTTP server, and one poll worker thread coordinated by shared
  health state. This avoids mixing async HTTP, blocking SQLite, and signal
  handling until JMAP push is implemented.
- Treat SQLite as the source of truth after an email has been parsed.
- Never log raw tokens, subjects, sender addresses, raw JMAP ids, raw message
  ids, or body content.
- Build parser behavior from anonymized fixtures rather than assumptions about
  Substack markup.
- Make every pipeline step independently testable: JMAP client, parser,
  sanitizer, storage, feed renderer, HTTP serving.

## Pre-Flight Decisions

Resolve these before Phase 8 deployment work:

- Feed access for v1 is random-token-in-path only, unless FreshRSS integration
  testing shows Traefik basic auth is painless enough to add immediately.
- Hosting target and Traefik network are deployment inputs, not application
  code. Confirm whether this runs on the existing indenwolken.xyz host before
  writing final compose labels.
- A sustained Fastmail outage does not crash the process. The daemon backs off
  up to the configured cap, `/healthz` reports degraded once the last
  successful poll is stale, and the supervisor keeps the process running.
- Entries without a canonical post URL are still published with a stable Atom
  id and no alternate link, and a safe warning is logged.

## Phase 0: Project Scaffold

Deliverables:

- `pyproject.toml` managed by `uv`
- package layout under `src/mail2rss/`
- test layout under `tests/`
- `ruff`, `mypy --strict`, and `pytest` configuration
- minimal CLI entrypoint, e.g. `mail2rss --config config.toml`

Suggested modules:

- `config.py` - pydantic settings and validation
- `logging.py` - structlog setup and secret-safe processors
- `jmap.py` - Fastmail session discovery and JMAP calls
- `models.py` - typed internal data models
- `db.py` - SQLite schema, migrations, repositories
- `parser.py` - Substack publication/body/link extraction
- `sanitize.py` - HTML allow-list, link rewriting, footer/pixel stripping
- `feeds.py` - Atom generation and atomic file writes
- `http.py` - health and feed static serving
- `daemon.py` - orchestration, polling loop, shutdown
- `main.py` - CLI entrypoint

Implementation choices:

- Use `http.server.ThreadingHTTPServer` for v1. It only needs to serve static
  feed files and `/healthz`; avoid adding an ASGI framework until there is a
  real need.
- Keep internal models minimal. Add dataclasses or pydantic models only where
  they reduce parsing ambiguity or make tests clearer.

Acceptance checks:

- `uv run ruff check`
- `uv run mypy src tests`
- `uv run pytest`

## Phase 1: Configuration, Logging, and State

Implement first because every other component depends on it.

Work:

1. Parse `config.toml` with pydantic v2.
2. Read the Fastmail token from the configured env var.
3. Validate the output directory and database path.
4. Initialize JSON logging with token redaction.
5. Add salted, truncated ID hashing for log correlation.
6. Create SQLite schema and migration handling.
7. Generate and persist `feed_url_secret` on first run.
8. Add a shared in-memory health state object for last success, last failure,
   current backoff, startup status, and shutdown status.

Database details:

- Use WAL mode.
- Store `schema_version`, `jmap_query_state`, `feed_url_secret`, and
  `log_hash_salt` in `state`.
- Use `jmap_id` as the primary entry key.
- Use nullable unique indexes on `message_id` and `canonical_url`.
- On duplicate secondary keys, keep the existing entry unless a future update
  policy is explicitly added.

Tests:

- missing token fails cleanly without logging the value
- secret and salt are created once and reused
- migrations are idempotent
- duplicate JMAP/message/canonical ids do not create duplicate DB entries

## Phase 2: Fastmail JMAP Client

Work:

1. Fetch the JMAP session from `https://api.fastmail.com/jmap/session`.
2. Verify `urn:ietf:params:jmap:mail` is present.
3. Refuse `urn:ietf:params:jmap:submission` if advertised for the token.
4. Perform best-effort read-only checks only when Fastmail exposes enough data.
5. Resolve and cache `apiUrl`, `eventSourceUrl`, and mail `accountId`.
6. Query the configured mailbox by exact name and fail clearly if absent.
7. Implement initial `Email/query` capped by `poll.initial_backfill`.
8. Implement incremental `Email/queryChanges` from stored query state.
9. Fetch HTML body values for added email ids.
10. Handle `cannotCalculateChanges` by falling back to `Email/query`.
11. Handle 401 by rediscovering the session once, then exiting non-zero.
12. Handle rate limits and 5xx responses with jittered exponential backoff.

Important implementation notes:

- Use the same filter and sort for `Email/query` and `Email/queryChanges`.
- Advance `jmap_query_state` only after fetched messages have been processed
  and committed.
- Treat removed ids as non-destructive: do not delete already published
  entries from SQLite or feeds.
- If a query returns more changes than `maxChanges`, loop until complete before
  sleeping again.
- On sustained 5xx/rate-limit failure, keep retrying with capped backoff and
  update shared health state. Do not exit unless the error is an unrecoverable
  auth/configuration failure.

Tests:

- fake JMAP server covers session discovery, mailbox lookup, initial sync,
  incremental sync, `cannotCalculateChanges`, 401, 503, and rate limit cases
- query state is not advanced when parsing or DB writes fail
- removed ids do not remove existing entries

## Phase 3: Fixture Capture and Anonymization

This phase should happen before broad parser work.

Prerequisite: a live Fastmail account with representative Substack messages in
the configured mailbox, or equivalent sanitized samples supplied manually.

Work:

1. Capture a small set of representative Fastmail JMAP `Email/get` responses.
2. Anonymize them before committing:
   - replace real names, addresses, domains, message ids, JMAP ids, and blob ids
   - replace URLs containing tokens or user-specific paths
   - replace tracking ids and unsubscribe/manage-subscription links
   - replace paid/private article text with synthetic text
   - keep only the minimum HTML structure required for parser coverage
3. Add a fixture review checklist in `tests/fixtures/README.md`.
4. Add fixture privacy lint with concrete rules:
   - reject email-looking strings outside `example.*`, `invalid`, or test-only
     domains
   - reject URLs outside an allow-list of synthetic fixture domains
   - reject common token query keys such as `token`, `signature`, `sig`, `jwt`,
     `session`, `key`, `auth`, and `utm_*`
   - reject raw Fastmail/JMAP-looking ids unless they match synthetic fixture
     prefixes
   - reject known personal domains configured in a local deny-list that is not
     committed

Target fixtures:

- normal full-body paid post
- truncated/free post
- custom-domain Substack post
- `email.mg2.substack.com` redirect links
- unsubscribe/manage-subscription footer
- tracking pixel images
- text-only fallback
- unusual `List-ID` formats

Tests:

- fixture privacy lint
- parser tests asserting exact normalized output for each fixture

## Phase 4: Substack Parser and Sanitizer

Work:

1. Detect publication from `List-ID`, falling back to `From`.
2. Generate stable slugs and persist slug mappings.
3. Extract the preferred HTML body value; fall back to text wrapped in `<pre>`.
4. Extract canonical post URLs from Substack/custom-domain post links.
5. Rewrite Substack redirect links to their underlying target.
6. Remove known tracking pixels.
7. Remove footer boilerplate by href patterns, not visible text.
8. Sanitize HTML with `bleach` allow-lists.
9. Detect likely truncation and emit a safe warning.

Tests:

- XSS payloads are stripped
- `javascript:` and suspicious `data:` URIs are removed
- remote assets are not fetched
- redirect links are rewritten
- footer and tracking pixels are removed
- truncation warnings include only safe correlation fields
Slug policy:

- First publication to claim a slug keeps it.
- If another publication normalizes to the same slug, append a stable short
  suffix derived from `list_id` or sender domain.
- Persist the mapping so future renames or collisions do not fragment feeds.

## Phase 5: Feed Generation

Work:

1. Query SQLite for recent entries per publication and aggregate entries.
2. Generate one Atom file per publication slug plus `all.xml`.
3. Use stable feed ids and stable entry ids.
4. Strip publication prefixes from titles at render time.
5. Write files atomically with temp files and `os.replace`.
6. Ensure feed regeneration is deterministic for a given DB state.

Tests:

- per-publication and aggregate feeds contain the expected entries
- output is deterministic
- max entries per feed is enforced
- generated feeds include valid self links using `/feeds/<secret>/<slug>.xml`
- Atom output passes local structural checks
- W3C Feed Validator is run manually before v1 acceptance, or as an optional
  network-enabled CI/deployment check when network access is available

## Phase 6: HTTP Server and Health

Work:

1. Serve `/healthz`.
2. Serve `/feeds/<secret>/<slug>.xml` from the output directory.
3. Return 404 for bad secrets and unknown slugs.
4. Expose `show_url=1` only for localhost callers.
5. Report last successful poll, last failed poll, current backoff, and current
   degraded/healthy state.
6. Keep bind default at `127.0.0.1:8080`.

Implementation notes:

- Use the shared health state object created in Phase 1. Phase 6 should not
  depend on the full daemon orchestration from Phase 7.
- Keep localhost detection simple and tested; if it becomes brittle, replace
  `show_url=1` with a first-run safe log line that prints only feed URLs, never
  the Fastmail token.

Tests:

- health is degraded before first successful poll
- health is degraded when the last success is older than `2 * interval`
- feed paths require the secret
- `show_url=1` is suppressed for non-localhost clients

## Phase 7: Daemon Orchestration

Work:

1. Wire config, logging, DB, JMAP client, parser, feed writer, and HTTP server.
2. Run the HTTP server and poll loop concurrently.
3. Make one poll cycle transactional: fetch, parse, insert, update query state,
   regenerate feeds.
4. On SIGTERM, stop accepting new poll work, finish the in-flight poll, flush
   state, and exit 0.
5. Ensure startup fails clearly for missing token, missing mailbox, invalid
   config, and unwritable state.

Tests:

- full fake-JMAP pipeline writes expected feeds
- restart after mid-poll failure does not duplicate entries
- signal handling exits cleanly
- token rotation/401 exits non-zero without replaying old entries on restart

## Phase 8: Container and Deployment

Work:

1. Add a multi-stage Dockerfile.
2. Run as uid/gid `1000:1000`.
3. Support read-only root filesystem with only `/var/lib/mail2rss` writable.
4. Drop all capabilities and enable no-new-privileges in compose.
5. Add `docker-compose.yml` with Traefik labels and env file wiring.
6. Put the healthcheck in compose so the final image can remain distroless.
7. Document pfSense egress rules for Fastmail endpoints.

Tests:

- `docker build` succeeds
- `docker run --read-only --cap-drop=ALL --security-opt=no-new-privileges`
  starts with a mounted writable state directory
- compose healthcheck reaches `/healthz`

## Phase 9: Hardening and Acceptance

Run the full acceptance suite before calling v1 done.

Checklist:

1. `uv run ruff check`
2. `uv run mypy src tests`
3. `uv run pytest`
4. fixture privacy lint passes
5. generated feeds pass local Atom checks
6. W3C Feed Validator check has been run manually or through a network-enabled
   CI/deploy job
7. no raw token, subject, address, body, raw JMAP id, or raw message id appears
   in logs
8. manual 503 chaos test backs off and recovers
9. manual token rotation test exits non-zero and resumes after restart
10. manual FreshRSS subscription succeeds for one publication and `all.xml`
11. container hardening test passes

## Deferred Work

- JMAP EventSource push using the session `eventSourceUrl`
- explicit purge command for removing already published entries
- basic-auth or Pocket ID enforcement in front of feed URLs
- Sieve rule documentation or deploy helper
- richer per-publication normalization rules if fixtures show divergence
