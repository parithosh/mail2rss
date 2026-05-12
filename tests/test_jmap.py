from __future__ import annotations

import json

from pytest_httpx import HTTPXMock

from mail2rss.jmap import (
    CannotCalculateChangesError,
    FastmailJmapClient,
    JmapAuthError,
)

SESSION = {
    "capabilities": {
        "urn:ietf:params:jmap:core": {},
        "urn:ietf:params:jmap:mail": {},
    },
    "apiUrl": "https://api.fastmail.test/jmap/api",
    "eventSourceUrl": "https://api.fastmail.test/jmap/eventsource/{types}",
    "primaryAccounts": {"urn:ietf:params:jmap:mail": "account-1"},
}


def test_initial_sync_fetches_body_values(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.fastmail.test/jmap/session",
        json=SESSION,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.fastmail.test/jmap/api",
        json={
            "methodResponses": [
                [
                    "Email/query",
                    {"ids": ["email-1"], "queryState": "state-1"},
                    "query",
                ],
                [
                    "Email/get",
                    {"list": [{"id": "email-1", "receivedAt": "2026-05-11T10:00:00Z"}]},
                    "get",
                ],
            ]
        },
    )
    client = FastmailJmapClient("token", "https://api.fastmail.test/jmap/session")

    result = client.initial_sync("mailbox-1", 50)

    assert result.query_state == "state-1"
    assert result.emails[0]["id"] == "email-1"
    request = httpx_mock.get_requests(method="POST")[0]
    payload = json.loads(request.content)
    get_call = payload["methodCalls"][1][1]
    assert get_call["fetchHTMLBodyValues"] is True
    assert get_call["fetchTextBodyValues"] is True
    assert get_call["maxBodyValueBytes"] == 5_000_000
    client.close()


def test_jmap_401_after_session_retry_exits_auth_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.fastmail.test/jmap/session",
        json=SESSION,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.fastmail.test/jmap/api",
        status_code=401,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.fastmail.test/jmap/session",
        json=SESSION,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.fastmail.test/jmap/api",
        status_code=401,
    )
    client = FastmailJmapClient("token", "https://api.fastmail.test/jmap/session")

    try:
        client.initial_sync("mailbox-1", 50)
    except JmapAuthError:
        pass
    else:
        raise AssertionError("expected JmapAuthError")
    finally:
        client.close()


def test_cannot_calculate_changes_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.fastmail.test/jmap/session",
        json=SESSION,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.fastmail.test/jmap/api",
        json={
            "methodResponses": [
                [
                    "error",
                    {"type": "cannotCalculateChanges"},
                    "changes",
                ]
            ]
        },
    )
    client = FastmailJmapClient("token", "https://api.fastmail.test/jmap/session")

    try:
        client.incremental_sync("mailbox-1", "old-state", 200)
    except CannotCalculateChangesError:
        pass
    else:
        raise AssertionError("expected CannotCalculateChangesError")
    finally:
        client.close()
