# tests/test_handler.py
"""
Unit tests for handler.py using pytest.

We do not hit a real database. Instead we monkeypatch the
get_db_connection() function to return a fake connection and cursor.
We focus on:
  - OpenAPI style validation with Pydantic (400 on bad input)
  - Happy path with valid input (200, correct envelope)
  - Debug key behaviour
  - CORS headers presence
"""

import json
import importlib
import os

import pytest


# ------------------------------
# Helpers for fake DB behaviour
# ------------------------------
class FakeCursor:
    def __init__(self, rows, total):
        self._rows = rows
        self._total = total
        self.executed = []  # capture the SQL for optional inspection

    def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or [])))

    def fetchone(self):
        # First SELECT is the COUNT query
        return {"total": self._total}

    def fetchall(self):
        # Second SELECT returns the page of rows
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, rows, total):
        self.rows = rows
        self.total = total
        self.open = True

    def cursor(self):
        return FakeCursor(self.rows, self.total)


def http_event(query, headers=None):
    """Build a minimal HTTP API v2 style event."""
    return {
        "rawQueryString": query,
        "headers": headers or {},
        "requestContext": {"requestId": "req-123"},
        "path": "/trendfinder",
    }


# ------------------------------
# Fixtures
# ------------------------------
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure environment is clean and predictable for each test."""
    # Keep page size limits sensible for tests
    monkeypatch.setenv("DEFAULT_PAGE_SIZE", "50")
    monkeypatch.setenv("MAX_PAGE_SIZE", "500")
    # Clear debug keys unless a test sets them
    monkeypatch.delenv("DEBUG_KEYS", raising=False)
    # Force English logs at INFO
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    # Reload the handler module after env changes
    if "handler" in globals():
        import handler
        importlib.reload(handler)
    yield


# ------------------------------
# Tests
# ------------------------------
def test_validation_missing_required_returns_400(monkeypatch):
    import handler  # import after fixture ran

    # No DB access should occur because validation fails
    monkeypatch.setattr(handler, "get_db_connection", lambda: None)

    event = http_event("start_date=2025-01-01&end_date=2025-01-31")  # country missing
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "bad_request"
    # Expect a details list that mentions the missing field
    errors_text = json.dumps(body.get("details", []))
    assert "country" in errors_text


def test_validation_bad_date_returns_400(monkeypatch):
    import handler

    monkeypatch.setattr(handler, "get_db_connection", lambda: None)

    event = http_event("country=Kenya&start_date=bad&end_date=2025-01-31")
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "bad_request"
    errors_text = json.dumps(body.get("details", []))
    assert "start_date" in errors_text


def test_page_size_above_max_rejected_by_validation(monkeypatch):
    import handler

    monkeypatch.setattr(handler, "get_db_connection", lambda: None)

    # page_size beyond MAX_PAGE_SIZE should trigger 400 from Pydantic
    event = http_event(
        "country=Kenya&start_date=2025-01-01&end_date=2025-02-01&page_size=9999"
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 400


def test_success_200_envelope_and_rows(monkeypatch):
    import handler

    rows = [
        {
            "event_id": "E1",
            "event_date": "2025-01-02",
            "country": "Kenya",
            "admin1": "Nairobi",
            "event_type": "Protest",
            "sub_event_type": "Peaceful protest",
            "actor1": "Civic group",
            "actor2": "",
            "fatalities": 0,
            "latitude": -1.29,
            "longitude": 36.82,
        },
        {
            "event_id": "E2",
            "event_date": "2025-01-03",
            "country": "Kenya",
            "admin1": "Nairobi",
            "event_type": "Protest",
            "sub_event_type": "Peaceful protest",
            "actor1": "Civic group",
            "actor2": "",
            "fatalities": 0,
            "latitude": -1.30,
            "longitude": 36.83,
        },
    ]
    total = 10

    # Stub the DB connection
    monkeypatch.setattr(handler, "get_db_connection", lambda: FakeConn(rows, total))

    event = http_event(
        "country=Kenya&start_date=2025-01-01&end_date=2025-02-01&page=1&page_size=2"
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200

    body = json.loads(resp["body"])
    assert "meta" in body and "data" in body
    assert body["meta"]["page"] == 1
    assert body["meta"]["page_size"] == 2
    assert body["meta"]["total"] == total
    assert body["meta"]["total_pages"] >= 1
    assert len(body["data"]) == 2


def test_debug_key_includes_debug_section(monkeypatch):
    # Allow a known debug key
    monkeypatch.setenv("DEBUG_KEYS", "TEST123")
    importlib.invalidate_caches()
    import handler
    # Reload so handler reads new env DEBUG_KEYS
    importlib.reload(handler)

    rows = []
    total = 0
    monkeypatch.setattr(handler, "get_db_connection", lambda: FakeConn(rows, total))

    headers = {"X-Debug-Key": "TEST123"}
    event = http_event(
        "country=Kenya&start_date=2025-01-01&end_date=2025-02-01", headers=headers
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["meta"].get("debug") is not None  # debug section present


def test_cors_headers_present_on_success(monkeypatch):
    import handler
    monkeypatch.setattr(handler, "get_db_connection", lambda: FakeConn([], 0))

    event = http_event(
        "country=Kenya&start_date=2025-01-01&end_date=2025-02-01&page=1&page_size=1"
    )
    resp = handler.handler(event, None)
    headers = resp["headers"]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in headers["Access-Control-Allow-Methods"]
    assert "X-Correlation-Id" in headers["Access-Control-Allow-Headers"]
