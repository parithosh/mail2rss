from __future__ import annotations

import signal
import threading
from types import FrameType

import structlog

from mail2rss.config import AppConfig
from mail2rss.db import Database, insert_entries
from mail2rss.feeds import write_all_feeds
from mail2rss.filters import filter_post_parse, filter_pre_parse
from mail2rss.health import HealthState
from mail2rss.http import FeedHttpServer
from mail2rss.jmap import (
    CannotCalculateChangesError,
    FastmailJmapClient,
    JmapAuthError,
    JmapError,
    JmapRateLimitError,
    PollResult,
)
from mail2rss.logging import hash_identifier
from mail2rss.models import ParsedEntry
from mail2rss.parser import canonical_url_from_html, parse_email

LOGGER = structlog.get_logger()


class Mail2RssDaemon:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.token = config.token
        self.config.validate_paths()
        self.health = HealthState(interval_seconds=config.poll.interval_seconds)
        self.stop_event = threading.Event()
        self.db = Database(config.database_path)
        self.jmap = FastmailJmapClient(self.token, config.fastmail.session_url)
        self.http_server: FeedHttpServer | None = None
        self.http_thread: threading.Thread | None = None
        self.mailbox_id: str | None = None
        self.feed_secret = ""
        self.log_salt = ""

    def run(self) -> int:
        try:
            self._startup()
            self._install_signal_handlers()
            self._start_http()
            self._poll_loop()
        except JmapAuthError as exc:
            LOGGER.error("auth_failed", error=str(exc))
            return 2
        except Exception as exc:
            LOGGER.error("fatal_error", error=str(exc))
            return 1
        finally:
            self._shutdown()
        return 0

    def _startup(self) -> None:
        self.db.migrate()
        if self.config.http.require_secret:
            self.feed_secret = self.db.get_or_create_secret("feed_url_secret")
        self.log_salt = self.db.get_or_create_secret("log_hash_salt")
        self._backfill_canonical_urls()
        LOGGER.info("token_loaded", token_prefix=self.token[:4])
        self.jmap.discover_session()
        self.mailbox_id = self.jmap.mailbox_id_by_name(self.config.fastmail.mailbox)
        write_all_feeds(
            self.db,
            self.config.output.dir,
            self.feed_secret,
            self.config.http.feed_url_path_prefix,
            self.config.output.aggregate_filename,
            self.config.output.max_entries_per_feed,
        )
        self.db.commit()

    def _filter_and_parse(self, email: dict[str, object]) -> ParsedEntry | None:
        cfg = self.config.filters
        reason = filter_pre_parse(email, cfg)
        if reason is not None:
            LOGGER.info(
                "mail_skipped",
                reason=reason,
                hashed_email_id=hash_identifier(
                    str(email.get("id", "")), self.log_salt
                ),
            )
            return None
        parsed = parse_email(email)
        reason = filter_post_parse(parsed, cfg)
        if reason is not None:
            LOGGER.info(
                "mail_skipped",
                reason=reason,
                hashed_email_id=hash_identifier(parsed.jmap_id, self.log_salt),
                publication_slug=parsed.publication.slug,
            )
            return None
        return parsed

    def _backfill_canonical_urls(self) -> None:
        candidates = self.db.entries_missing_canonical_url()
        if not candidates:
            return
        updated = 0
        with self.db.transaction():
            for jmap_id, body_html, list_id in candidates:
                url = canonical_url_from_html(body_html, list_id)
                if url is None:
                    continue
                if self.db.set_canonical_url(jmap_id, url):
                    updated += 1
        self.db.commit()
        if updated:
            LOGGER.info(
                "canonical_url_backfilled",
                considered=len(candidates),
                updated=updated,
            )

    def _start_http(self) -> None:
        self.http_server = FeedHttpServer(
            bind=self.config.http.bind,
            output_dir=self.config.output.dir,
            feed_secret=self.feed_secret,
            path_prefix=self.config.http.feed_url_path_prefix,
            health=self.health,
        )
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            name="mail2rss-http",
            daemon=True,
        )
        self.http_thread.start()
        LOGGER.info("http_started", bind=self.config.http.bind)
        self._log_feed_urls()

    def _log_feed_urls(self) -> None:
        prefix = self.config.http.feed_url_path_prefix
        secret_segment = f"/{self.feed_secret}" if self.feed_secret else ""
        paths = sorted(
            f"{prefix}{secret_segment}/{path.name}"
            for path in self.config.output.dir.glob("*.xml")
        )
        if not paths:
            return
        LOGGER.info(
            "feed_urls_ready",
            bind=self.config.http.bind,
            secret_required=self.config.http.require_secret,
            paths=paths,
        )

    def _poll_loop(self) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                attempt = 0
                self.health.mark_success()
                if self.stop_event.wait(self.config.poll.interval_seconds):
                    break
            except JmapAuthError:
                raise
            except (JmapRateLimitError, JmapError, OSError) as exc:
                attempt += 1
                backoff = _backoff_seconds(attempt)
                self.health.mark_failure(str(exc), backoff)
                LOGGER.warning("poll_failed", error=str(exc), backoff_seconds=backoff)
                self.stop_event.wait(backoff)

    def poll_once(self) -> None:
        if self.mailbox_id is None:
            raise RuntimeError("mailbox_id not initialized")
        query_state = self.db.get_state("jmap_query_state")
        result = (
            self.jmap.initial_sync(self.mailbox_id, self.config.poll.initial_backfill)
            if query_state is None
            else self._incremental_with_fallback(self.mailbox_id, query_state)
        )
        parsed_entries = []
        skipped_count = 0
        for email in result.emails:
            parsed = self._filter_and_parse(email)
            if parsed is None:
                skipped_count += 1
                continue
            parsed_entries.append(parsed)
            if parsed.is_truncated:
                LOGGER.warning(
                    "entry_may_be_truncated",
                    hashed_email_id=hash_identifier(parsed.jmap_id, self.log_salt),
                    publication_slug=parsed.publication.slug,
                )
            if parsed.canonical_url is None:
                LOGGER.warning(
                    "entry_missing_canonical_url",
                    hashed_email_id=hash_identifier(parsed.jmap_id, self.log_salt),
                    publication_slug=parsed.publication.slug,
                )

        inserted = 0
        with self.db.transaction():
            inserted = insert_entries(self.db, parsed_entries)
            self.db.set_state("jmap_query_state", result.query_state)

        while result.has_more_changes and not self.stop_event.is_set():
            result = self.jmap.incremental_sync(
                self.mailbox_id,
                result.query_state,
                self.config.poll.max_changes,
            )
            parsed_entries = []
            for email in result.emails:
                parsed = self._filter_and_parse(email)
                if parsed is None:
                    skipped_count += 1
                    continue
                parsed_entries.append(parsed)
            with self.db.transaction():
                inserted += insert_entries(self.db, parsed_entries)
                self.db.set_state("jmap_query_state", result.query_state)

        written = write_all_feeds(
            self.db,
            self.config.output.dir,
            self.feed_secret,
            self.config.http.feed_url_path_prefix,
            self.config.output.aggregate_filename,
            self.config.output.max_entries_per_feed,
        )
        LOGGER.info(
            "poll_completed",
            mailbox_id=self.mailbox_id,
            fetched_count=len(result.emails),
            inserted_count=inserted,
            skipped_count=skipped_count,
            feed_count=len(written),
        )

    def _incremental_with_fallback(
        self, mailbox_id: str, query_state: str
    ) -> PollResult:
        try:
            return self.jmap.incremental_sync(
                mailbox_id,
                query_state,
                self.config.poll.max_changes,
            )
        except CannotCalculateChangesError:
            LOGGER.warning("cannot_calculate_changes_fallback")
            return self.jmap.initial_sync(mailbox_id, self.config.poll.initial_backfill)

    def _install_signal_handlers(self) -> None:
        def handle_signal(_signum: int, _frame: FrameType | None) -> None:
            self.health.mark_shutdown()
            self.stop_event.set()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def _shutdown(self) -> None:
        self.health.mark_shutdown()
        self.stop_event.set()
        if self.http_server is not None:
            self.http_server.shutdown()
        if self.http_thread is not None:
            self.http_thread.join(timeout=5)
        self.jmap.close()
        self.db.close()


def _backoff_seconds(attempt: int) -> float:
    return float(min(1800.0, 30.0 * (2 ** max(0, attempt - 1))))


def run(config: AppConfig) -> int:
    return Mail2RssDaemon(config).run()
