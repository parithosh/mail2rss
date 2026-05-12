from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HealthState:
    interval_seconds: int
    started_at: datetime = field(default_factory=utc_now)
    last_successful_poll: datetime | None = None
    last_failed_poll: datetime | None = None
    last_error: str | None = None
    current_backoff_seconds: float = 0.0
    shutting_down: bool = False
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def mark_success(self) -> None:
        with self._lock:
            self.last_successful_poll = utc_now()
            self.last_error = None
            self.current_backoff_seconds = 0.0

    def mark_failure(self, error: str, backoff_seconds: float) -> None:
        with self._lock:
            self.last_failed_poll = utc_now()
            self.last_error = error
            self.current_backoff_seconds = backoff_seconds

    def mark_shutdown(self) -> None:
        with self._lock:
            self.shutting_down = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = utc_now()
            stale_after = self.interval_seconds * 2
            if self.last_successful_poll is None:
                healthy = False
            else:
                age = (now - self.last_successful_poll).total_seconds()
                healthy = age < stale_after
            return {
                "healthy": healthy,
                "started_at": self.started_at.isoformat(),
                "last_successful_poll": self.last_successful_poll.isoformat()
                if self.last_successful_poll
                else None,
                "last_failed_poll": self.last_failed_poll.isoformat()
                if self.last_failed_poll
                else None,
                "last_error": self.last_error,
                "current_backoff_seconds": self.current_backoff_seconds,
                "shutting_down": self.shutting_down,
            }
