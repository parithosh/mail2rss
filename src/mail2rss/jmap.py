from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

JMAP_CORE = "urn:ietf:params:jmap:core"
JMAP_MAIL = "urn:ietf:params:jmap:mail"
JMAP_SUBMISSION = "urn:ietf:params:jmap:submission"


class JmapError(RuntimeError):
    pass


class JmapAuthError(JmapError):
    pass


class JmapRateLimitError(JmapError):
    pass


class CannotCalculateChangesError(JmapError):
    pass


@dataclass(frozen=True)
class JmapSession:
    api_url: str
    event_source_url: str | None
    account_id: str


@dataclass(frozen=True)
class PollResult:
    emails: list[dict[str, object]]
    query_state: str
    has_more_changes: bool = False


class FastmailJmapClient:
    def __init__(self, token: str, session_url: str) -> None:
        self._token = token
        self._session_url = session_url
        self._client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Authorization": f"Bearer {token}"},
        )
        self.session: JmapSession | None = None

    def close(self) -> None:
        self._client.close()

    def discover_session(self) -> JmapSession:
        response = self._client.get(self._session_url)
        if response.status_code == 401:
            raise JmapAuthError("Fastmail token was rejected during session discovery")
        response.raise_for_status()
        data = response.json()
        capabilities = data.get("capabilities", {})
        if JMAP_MAIL not in capabilities:
            raise JmapError("JMAP mail capability is not available")
        if JMAP_SUBMISSION in capabilities:
            raise JmapError("JMAP submission capability is present; token is too broad")
        primary_accounts = data.get("primaryAccounts", {})
        account_id = primary_accounts.get(JMAP_MAIL)
        if not isinstance(account_id, str) or not account_id:
            raise JmapError("Session does not advertise a primary mail account")
        api_url = data.get("apiUrl")
        if not isinstance(api_url, str) or not api_url:
            raise JmapError("Session does not advertise apiUrl")
        event_source_url = data.get("eventSourceUrl")
        event_source = event_source_url if isinstance(event_source_url, str) else None
        self.session = JmapSession(
            api_url=api_url,
            event_source_url=event_source,
            account_id=account_id,
        )
        return self.session

    def mailbox_id_by_name(self, mailbox_name: str) -> str:
        session = self._require_session()
        response = self._api(
            [
                [
                    "Mailbox/query",
                    {
                        "accountId": session.account_id,
                        "filter": {"name": mailbox_name},
                    },
                    "mailboxes",
                ]
            ]
        )
        args = _method_args(response, "Mailbox/query")
        ids = args.get("ids")
        if not isinstance(ids, list) or not ids:
            raise JmapError(f"Fastmail mailbox not found: {mailbox_name}")
        if len(ids) > 1:
            raise JmapError(f"Fastmail mailbox name is ambiguous: {mailbox_name}")
        return str(ids[0])

    def initial_sync(self, mailbox_id: str, limit: int) -> PollResult:
        session = self._require_session()
        response = self._api(
            [
                [
                    "Email/query",
                    {
                        "accountId": session.account_id,
                        "filter": {"inMailbox": mailbox_id},
                        "sort": [{"property": "receivedAt", "isAscending": False}],
                        "position": 0,
                        "limit": limit,
                        "calculateTotal": False,
                    },
                    "query",
                ],
                [
                    "Email/get",
                    {
                        "accountId": session.account_id,
                        "#ids": {
                            "resultOf": "query",
                            "name": "Email/query",
                            "path": "/ids",
                        },
                        "properties": _email_properties(),
                        "bodyProperties": [
                            "partId",
                            "blobId",
                            "size",
                            "type",
                            "charset",
                        ],
                        "fetchHTMLBodyValues": True,
                        "fetchTextBodyValues": True,
                        "maxBodyValueBytes": 5_000_000,
                    },
                    "get",
                ],
            ]
        )
        query_args = _method_args(response, "Email/query")
        query_state = query_args.get("queryState")
        if not isinstance(query_state, str):
            raise JmapError("Email/query response did not include queryState")
        return PollResult(
            emails=_email_list(response),
            query_state=query_state,
        )

    def incremental_sync(
        self, mailbox_id: str, since_query_state: str, max_changes: int
    ) -> PollResult:
        session = self._require_session()
        response = self._api(
            [
                [
                    "Email/queryChanges",
                    {
                        "accountId": session.account_id,
                        "sinceQueryState": since_query_state,
                        "filter": {"inMailbox": mailbox_id},
                        "sort": [{"property": "receivedAt", "isAscending": False}],
                        "calculateTotal": False,
                        "maxChanges": max_changes,
                    },
                    "changes",
                ],
                [
                    "Email/get",
                    {
                        "accountId": session.account_id,
                        "#ids": {
                            "resultOf": "changes",
                            "name": "Email/queryChanges",
                            "path": "/added/*/id",
                        },
                        "properties": _email_properties(),
                        "bodyProperties": [
                            "partId",
                            "blobId",
                            "size",
                            "type",
                            "charset",
                        ],
                        "fetchHTMLBodyValues": True,
                        "fetchTextBodyValues": True,
                        "maxBodyValueBytes": 5_000_000,
                    },
                    "get",
                ],
            ]
        )
        changes_args = _method_args(response, "Email/queryChanges")
        new_state = changes_args.get("newQueryState")
        if not isinstance(new_state, str):
            raise JmapError("Email/queryChanges response did not include newQueryState")
        has_more = bool(changes_args.get("hasMoreChanges", False))
        return PollResult(
            emails=_email_list(response),
            query_state=new_state,
            has_more_changes=has_more,
        )

    def _api(self, method_calls: list[list[object]], retry_session: bool = True) -> Any:
        session = self._require_session()
        payload = {
            "using": [JMAP_CORE, JMAP_MAIL],
            "methodCalls": method_calls,
        }
        response = self._client.post(session.api_url, json=payload)
        if response.status_code == 401:
            if retry_session:
                self.discover_session()
                return self._api(method_calls, retry_session=False)
            raise JmapAuthError("Fastmail token was rejected by JMAP API")
        if response.status_code in {429, 503}:
            raise JmapRateLimitError(f"Fastmail returned HTTP {response.status_code}")
        if response.status_code >= 500:
            raise JmapError(f"Fastmail returned HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        _raise_method_errors(data)
        return data

    def _require_session(self) -> JmapSession:
        if self.session is None:
            return self.discover_session()
        return self.session


def sleep_with_backoff(attempt: int, base: float = 30.0, cap: float = 1800.0) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    jittered = delay * random.uniform(0.8, 1.2)
    time.sleep(jittered)
    return float(jittered)


def _method_args(response: Any, name: str) -> dict[str, Any]:
    for method_name, args, _call_id in response.get("methodResponses", []):
        if method_name == name and isinstance(args, dict):
            return args
    raise JmapError(f"Missing JMAP method response: {name}")


def _email_list(response: Any) -> list[dict[str, object]]:
    args = _method_args(response, "Email/get")
    raw = args.get("list", [])
    if not isinstance(raw, list):
        raise JmapError("Email/get list was not an array")
    return [item for item in raw if isinstance(item, dict)]


def _raise_method_errors(response: Any) -> None:
    for method_name, args, _call_id in response.get("methodResponses", []):
        if method_name != "error":
            continue
        error_type = args.get("type") if isinstance(args, dict) else None
        if error_type == "cannotCalculateChanges":
            raise CannotCalculateChangesError("Fastmail cannot calculate query changes")
        if error_type == "rateLimit":
            raise JmapRateLimitError("Fastmail returned JMAP rateLimit")
        raise JmapError(f"Fastmail JMAP method error: {error_type or 'unknown'}")


def _email_properties() -> list[str]:
    return [
        "id",
        "blobId",
        "threadId",
        "messageId",
        "from",
        "subject",
        "receivedAt",
        "sentAt",
        "headers",
        "preview",
        "htmlBody",
        "textBody",
        "bodyValues",
    ]
