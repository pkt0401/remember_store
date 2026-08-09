import asyncio
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# Importing main constructs SDK clients. Use inert credentials so collection never
# depends on a developer's .env values or sends requests to real services.
os.environ["SUPABASE_URL"] = "https://example.supabase.co"
os.environ["SUPABASE_SERVICE_KEY"] = "test-service-key"
os.environ["SUPABASE_PUBLISHABLE_KEY"] = "test-publishable-key"
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["APP_ENV"] = "development"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


ALICE_ID = "11111111-1111-4111-8111-111111111111"
BOB_ID = "22222222-2222-4222-8222-222222222222"


def _user(user_id=ALICE_ID, *, role="editor", email=None):
    email = email or ("alice@example.com" if user_id == ALICE_ID else "bob@example.com")
    return {
        "id": user_id,
        "username": email,
        "email": email,
        "role": role,
    }


def _request(method: str, path: str, *, cookies=None, user=None, db=None) -> Request:
    headers = []
    if cookies:
        cookie_value = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers.append((b"cookie", cookie_value.encode()))
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    if user is not None:
        request.state.user = user
    if db is not None:
        request.state.db = db
    return request


def _result(data, *, count=None):
    return SimpleNamespace(data=data, count=count)


def _assert_security_headers(response):
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert response.headers["content-security-policy"]


def _assert_private_no_store(response, *, no_transform=False):
    directives = {
        directive.strip().lower()
        for directive in response.headers["cache-control"].split(",")
    }
    assert {"private", "no-store"} <= directives
    assert ("no-transform" in directives) is no_transform


class _MemoryQuery:
    """Small in-memory PostgREST fake that honors the app's visibility filter."""

    def __init__(self, client):
        self.client = client
        self.operation = None
        self.values = None
        self.equal_filters = []
        self.in_filters = []
        self.null_filters = []
        self.visibility_user_id = None
        self.range_bounds = None
        self.limit_count = None
        self.want_count = False

    def select(self, *_args, **kwargs):
        self.operation = "select"
        self.want_count = kwargs.get("count") == "exact"
        return self

    def insert(self, values):
        self.operation = "insert"
        self.values = [dict(value) for value in values] if isinstance(values, list) else [dict(values)]
        return self

    def upsert(self, values, *, on_conflict, ignore_duplicates=False):
        self.operation = "upsert"
        self.values = [dict(value) for value in values]
        self.client.upserts.append({
            "rows": self.values,
            "on_conflict": on_conflict,
            "ignore_duplicates": ignore_duplicates,
        })
        return self

    def update(self, values):
        self.operation = "update"
        self.values = dict(values)
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, column, value):
        self.equal_filters.append((column, value))
        return self

    def in_(self, column, values):
        self.in_filters.append((column, set(values)))
        return self

    def is_(self, column, value):
        assert value == "null"
        self.null_filters.append(column)
        return self

    def or_(self, expression):
        self.client.visibility_expressions.append(expression)
        match = re.search(r"owner_user_id\.eq\.([0-9a-fA-F-]{36})", expression)
        if match:
            self.visibility_user_id = match.group(1)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, start, end):
        self.range_bounds = (start, end)
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def lt(self, column, value):
        self.client.lt_filters.append((column, value))
        return self

    def _matches(self, row):
        if self.visibility_user_id is not None and not (
            row.get("scope") in {None, "shared"}
            or (
                row.get("scope") == "personal"
                and row.get("owner_user_id") == self.visibility_user_id
            )
        ):
            return False
        if any(row.get(column) != value for column, value in self.equal_filters):
            return False
        if any(row.get(column) not in values for column, values in self.in_filters):
            return False
        if any(row.get(column) is not None for column in self.null_filters):
            return False
        return True

    def execute(self):
        self.client.executed_operations.append(self.operation)
        if self.operation == "insert":
            self.client.rows.extend(self.values)
            return _result([dict(row) for row in self.values])
        if self.operation == "upsert":
            self.client.rows.extend(self.values)
            return _result([dict(row) for row in self.values])

        matched = [row for row in self.client.rows if self._matches(row)]
        if self.operation == "select":
            count = len(matched) if self.want_count else None
            if self.range_bounds:
                start, end = self.range_bounds
                matched = matched[start:end + 1]
            if self.limit_count is not None:
                matched = matched[:self.limit_count]
            return _result([dict(row) for row in matched], count=count)
        if self.operation == "update":
            for row in matched:
                row.update(self.values)
            return _result([dict(row) for row in matched])
        if self.operation == "delete":
            matched_ids = {id(row) for row in matched}
            self.client.rows = [row for row in self.client.rows if id(row) not in matched_ids]
            return _result([dict(row) for row in matched])
        raise AssertionError(f"unexpected operation: {self.operation}")


class _MemoryClient:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.upserts = []
        self.visibility_expressions = []
        self.executed_operations = []
        self.lt_filters = []
        self.table_calls = 0

    def table(self, name):
        assert name == "memories"
        self.table_calls += 1
        return _MemoryQuery(self)


class _ProfileQuery:
    """Minimal account_profiles query fake used by username login tests."""

    def __init__(self, client):
        self.client = client
        self.username = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        assert column == "username"
        self.username = value
        return self

    def limit(self, _count):
        return self

    def maybe_single(self):
        return self

    def single(self):
        return self

    def execute(self):
        self.client.lookups.append(self.username)
        email = self.client.profiles.get(self.username)
        return _result([{"email": email, "username": self.username}] if email else [])


class _ProfileClient:
    def __init__(self, profiles=()):
        self.profiles = dict(profiles)
        self.lookups = []
        self.table_calls = []

    def table(self, name):
        assert name == "account_profiles"
        self.table_calls.append(name)
        return _ProfileQuery(self)


def _memory(memory_id, *, scope="shared", owner=None, creator=ALICE_ID, content=None):
    return {
        "id": memory_id,
        "source": "note",
        "content": content or f"{memory_id} content",
        "content_hash": f"hash-{memory_id}",
        "metadata": {"tags": [], "work_date": "2026-08-09"},
        "created_at": "2026-08-09T01:00:00+00:00",
        "expires_at": None,
        "scope": scope,
        "owner_user_id": owner,
        "created_by_user_id": creator,
    }


def test_auth_user_identity_uses_trusted_username_and_app_metadata_role():
    identity = main.auth_user_identity(SimpleNamespace(
        id=ALICE_ID,
        email="Alice@Example.COM",
        user_metadata={"username": "alice"},
        app_metadata={"app_role": "admin"},
    ), trusted_username="alice")

    assert identity == {
        "id": ALICE_ID,
        "username": "alice",
        "email": "alice@example.com",
        "role": "admin",
    }

    with pytest.raises(ValueError, match="UUID"):
        main.auth_user_identity(SimpleNamespace(id="mutable-username", email="alice@example.com"))


def test_auth_user_identity_never_trusts_user_metadata_for_admin_role():
    identity = main.auth_user_identity(SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        user_metadata={"username": "alice", "app_role": "admin", "role": "admin"},
        app_metadata={},
    ))

    assert identity["username"] == "alice@example.com"
    assert identity["role"] == "editor"


def test_canonical_auth_identity_uses_uuid_profile_not_mutable_metadata(monkeypatch):
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        user_metadata={"username": "bob"},
        app_metadata={},
    )
    lookups = []

    def profile_by_id(user_id):
        lookups.append(user_id)
        return {"id": ALICE_ID, "username": "alice", "email": "alice@example.com"}

    monkeypatch.setattr(main, "account_profile_by_user_id", profile_by_id)

    identity = main.canonical_auth_user_identity(auth_user)

    assert lookups == [ALICE_ID]
    assert identity["username"] == "alice"
    assert identity["role"] == "editor"


def test_signup_is_an_open_api_route(monkeypatch):
    assert "/api/signup" in main.OPEN_PATHS
    monkeypatch.setattr(
        main,
        "restore_supabase_session",
        lambda *_args: pytest.fail("public signup must not require an existing session"),
    )

    async def call_next(_request):
        return JSONResponse({"ok": True})

    response = asyncio.run(
        main.auth_middleware(_request("POST", "/api/signup"), call_next)
    )

    assert response.status_code == 200
    _assert_private_no_store(response)
    _assert_security_headers(response)


@pytest.mark.parametrize(
    "username",
    [
        "admin",
        " ADMIN ",
        "ab",
        "alice!",
        "alice@example.com",
        "한글아이디",
    ],
)
def test_public_signup_rejects_reserved_or_invalid_username(username):
    with pytest.raises(ValidationError):
        main.SignupRequest(
            username=username,
            email="alice@example.com",
            password="correct horse battery staple",
        )


def test_signup_normalizes_username_and_passes_it_as_auth_user_metadata(monkeypatch):
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        user_metadata={"username": "alice_01"},
        app_metadata={},
    )
    calls = []

    class FakeAuth:
        def sign_up(self, payload):
            calls.append(payload)
            return SimpleNamespace(user=auth_user, session=None)

    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )
    monkeypatch.setattr(main, "admin_sb", _ProfileClient())
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    response = Response()

    result = main.signup(
        main.SignupRequest(
            username=" Alice_01 ",
            email="Alice@Example.COM ",
            password="correct horse battery staple",
        ),
        _request("POST", "/api/signup"),
        response,
    )

    assert calls == [{
        "email": "alice@example.com",
        "password": "correct horse battery staple",
        "options": {
            "data": {
                "username": "alice_01",
                "email": "alice@example.com",
            }
        },
    }]
    assert result["ok"] is True
    assert result["user"]["username"] == "alice_01"
    assert result["requires_email_confirmation"] is True
    assert response.headers.getlist("set-cookie") == []


def test_signup_sets_session_cookies_when_email_confirmation_is_disabled(monkeypatch):
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        user_metadata={"username": "alice"},
        app_metadata={},
    )
    session = SimpleNamespace(
        access_token="signup-access-token",
        refresh_token="signup-refresh-token",
        expires_in=1800,
    )

    class FakeAuth:
        def sign_up(self, _payload):
            return SimpleNamespace(user=auth_user, session=session)

    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )
    monkeypatch.setattr(main, "admin_sb", _ProfileClient())
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    response = Response()

    result = main.signup(
        main.SignupRequest(
            username="alice",
            email="alice@example.com",
            password="correct horse battery staple",
        ),
        _request("POST", "/api/signup"),
        response,
    )

    cookies = response.headers.getlist("set-cookie")
    assert result["requires_email_confirmation"] is False
    assert result["user"]["id"] == ALICE_ID
    assert any(f"{main.ACCESS_COOKIE}=signup-access-token" in value for value in cookies)
    assert any(f"{main.REFRESH_COOKIE}=signup-refresh-token" in value for value in cookies)


def test_login_resolves_username_to_profile_email_and_admin_role(monkeypatch):
    profiles = _ProfileClient({"admin": "admin@example.com"})
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="admin@example.com",
        user_metadata={"username": "admin"},
        app_metadata={"app_role": "admin"},
    )
    session = SimpleNamespace(
        access_token="admin-access-token",
        refresh_token="admin-refresh-token",
        expires_in=1800,
    )
    login_payloads = []

    class FakeAuth:
        def sign_in_with_password(self, payload):
            login_payloads.append(payload)
            return SimpleNamespace(user=auth_user, session=session)

    monkeypatch.setattr(main, "admin_sb", profiles)
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda user_id: {
            "id": user_id,
            "username": "admin",
            "email": "admin@example.com",
        },
    )
    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_login_failures", {})
    response = Response()

    result = main.login(
        main.LoginRequest(username=" ADMIN ", password="admin-password"),
        _request("POST", "/api/login"),
        response,
    )

    assert profiles.lookups == ["admin"]
    assert login_payloads == [{
        "email": "admin@example.com",
        "password": "admin-password",
    }]
    assert result["user"]["username"] == "admin"
    assert result["user"]["role"] == "admin"


def test_login_accepts_email_without_profile_lookup(monkeypatch):
    profiles = _ProfileClient({"alice": "alice@example.com"})
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        user_metadata={"username": "alice"},
        app_metadata={},
    )
    session = SimpleNamespace(
        access_token="access-token",
        refresh_token="refresh-token",
        expires_in=1800,
    )
    login_payloads = []

    class FakeAuth:
        def sign_in_with_password(self, payload):
            login_payloads.append(payload)
            return SimpleNamespace(user=auth_user, session=session)

    monkeypatch.setattr(main, "admin_sb", profiles)
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda user_id: {
            "id": user_id,
            "username": "alice",
            "email": "alice@example.com",
        },
    )
    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_login_failures", {})

    result = main.login(
        main.LoginRequest(username=" Alice@Example.COM ", password="password"),
        _request("POST", "/api/login"),
        Response(),
    )

    assert profiles.lookups == []
    assert login_payloads == [{
        "email": "alice@example.com",
        "password": "password",
    }]
    assert result["user"]["username"] == "alice"


def test_login_reloads_username_from_authenticated_uuid(monkeypatch):
    profiles = _ProfileClient({"alice": "alice@example.com"})
    auth_user = SimpleNamespace(
        id=BOB_ID,
        email="alice@example.com",
        user_metadata={"username": "alice"},
        app_metadata={},
    )
    session = SimpleNamespace(
        access_token="access-token",
        refresh_token="refresh-token",
        expires_in=1800,
    )
    uuid_lookups = []
    audits = []

    class FakeAuth:
        def sign_in_with_password(self, _payload):
            return SimpleNamespace(user=auth_user, session=session)

    def profile_by_id(user_id):
        uuid_lookups.append(user_id)
        return {"id": BOB_ID, "username": "bob", "email": "alice@example.com"}

    monkeypatch.setattr(main, "admin_sb", profiles)
    monkeypatch.setattr(main, "account_profile_by_user_id", profile_by_id)
    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *args, **kwargs: audits.append((args, kwargs)),
    )
    monkeypatch.setattr(main, "_login_failures", {})

    result = main.login(
        main.LoginRequest(username="alice", password="password"),
        _request("POST", "/api/login"),
        Response(),
    )

    assert uuid_lookups == [BOB_ID]
    assert result["user"]["username"] == "bob"
    assert audits == [(('bob', 'editor', 'login'), {
        "actor_user_id": BOB_ID,
        "ip": "127.0.0.1",
    })]


def test_failed_login_audit_never_uses_attempted_admin_as_actor(monkeypatch):
    audits = []

    class FakeAuth:
        def sign_in_with_password(self, _payload):
            raise ValueError("invalid credentials")

    monkeypatch.setattr(main, "admin_sb", _ProfileClient())
    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *args, **kwargs: audits.append((args, kwargs)),
    )
    monkeypatch.setattr(main, "_login_failures", {})

    with pytest.raises(HTTPException) as exc_info:
        main.login(
            main.LoginRequest(username="admin", password="wrong-password"),
            _request("POST", "/api/login"),
            Response(),
        )

    assert exc_info.value.status_code == 401
    assert audits == [(('unauthenticated', 'unknown', 'login_failed'), {
        "ip": "127.0.0.1",
        "attempted_identifier_kind": "username",
        "attempted_identifier_hash": main.hashlib.sha256(b"admin").hexdigest()[:16],
    })]


def test_restore_supabase_session_validates_user_and_creates_jwt_scoped_client(monkeypatch):
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        app_metadata={},
    )
    auth_api = SimpleNamespace(get_user=lambda token: (
        _result(None) if token != "access-token"
        else SimpleNamespace(user=auth_user)
    ))
    auth_client = SimpleNamespace(auth=auth_api)
    request_db = object()
    calls = []

    def fake_new_client(*, access_token=None):
        calls.append(access_token)
        return auth_client if access_token is None else request_db

    monkeypatch.setattr(main, "new_supabase_client", fake_new_client)
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda user_id: {"id": user_id, "username": "alice"},
    )

    restored = main.restore_supabase_session("access-token", "refresh-token")

    assert restored["user"]["id"] == ALICE_ID
    assert restored["user"]["role"] == "editor"
    assert restored["db"] is request_db
    assert restored["refreshed"] is False
    assert calls == [None, "access-token"]


def test_restore_supabase_session_refreshes_when_only_refresh_cookie_remains(monkeypatch):
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        app_metadata={},
    )
    refreshed_session = SimpleNamespace(
        access_token="new-access-token",
        refresh_token="new-refresh-token",
        expires_in=1800,
    )
    refresh_calls = []

    def unexpected_get_user(_token):
        raise AssertionError("missing access cookie must skip get_user")

    def refresh_session(refresh_token):
        refresh_calls.append(refresh_token)
        return SimpleNamespace(session=refreshed_session, user=auth_user)

    auth_client = SimpleNamespace(auth=SimpleNamespace(
        get_user=unexpected_get_user,
        refresh_session=refresh_session,
    ))
    request_db = object()
    client_calls = []

    def fake_new_client(*, access_token=None):
        client_calls.append(access_token)
        return auth_client if access_token is None else request_db

    monkeypatch.setattr(main, "new_supabase_client", fake_new_client)
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda user_id: {"id": user_id, "username": "alice"},
    )

    restored = main.restore_supabase_session("", "remaining-refresh-token")

    assert restored["user"]["id"] == ALICE_ID
    assert restored["db"] is request_db
    assert restored["access_token"] == "new-access-token"
    assert restored["refresh_token"] == "new-refresh-token"
    assert restored["expires_in"] == 1800
    assert restored["refreshed"] is True
    assert refresh_calls == ["remaining-refresh-token"]
    assert client_calls == [None, "new-access-token"]


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("viewer", {"read"}),
        ("editor", {"read", "write"}),
        ("admin", {"read", "write", "admin"}),
        ("unknown", set()),
    ],
)
def test_role_permission_matrix(role, allowed):
    for action in ("read", "write", "admin", "unknown"):
        assert main.role_allows(role, action) is (action in allowed)


def test_auth_middleware_attaches_user_and_request_scoped_db(monkeypatch):
    request_db = object()
    restored = {
        "user": _user(),
        "db": request_db,
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
        "refreshed": False,
    }
    seen_tokens = []

    def fake_restore(access_token, refresh_token):
        seen_tokens.append((access_token, refresh_token))
        return restored

    monkeypatch.setattr(main, "restore_supabase_session", fake_restore)
    request = _request(
        "POST",
        "/api/ingest",
        cookies={
            main.ACCESS_COOKIE: "access-token",
            main.REFRESH_COOKIE: "refresh-token",
        },
    )

    async def call_next(received):
        assert received.state.user == _user()
        assert received.state.db is request_db
        return JSONResponse({"ok": True})

    response = asyncio.run(main.auth_middleware(request, call_next))

    assert response.status_code == 200
    assert seen_tokens == [("access-token", "refresh-token")]
    _assert_private_no_store(response)
    _assert_security_headers(response)


def test_auth_middleware_blocks_viewer_write(monkeypatch):
    monkeypatch.setattr(
        main,
        "restore_supabase_session",
        lambda *_args: {
            "user": _user(role="viewer"),
            "db": object(),
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 0,
            "refreshed": False,
        },
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    called = False

    async def call_next(_request):
        nonlocal called
        called = True
        return JSONResponse({"ok": True})

    response = asyncio.run(
        main.auth_middleware(_request("PATCH", "/api/memories/abc"), call_next)
    )

    assert response.status_code == 403
    assert not called
    _assert_private_no_store(response)
    _assert_security_headers(response)


def test_auth_middleware_rejects_missing_or_invalid_supabase_session(monkeypatch):
    monkeypatch.setattr(main, "restore_supabase_session", lambda *_args: None)
    called = False

    async def call_next(_request):
        nonlocal called
        called = True
        return JSONResponse({"ok": True})

    response = asyncio.run(
        main.auth_middleware(_request("GET", "/api/memories"), call_next)
    )

    assert response.status_code == 401
    assert not called
    _assert_private_no_store(response)
    _assert_security_headers(response)


def test_auth_middleware_preserves_no_transform_on_streaming_api_response(monkeypatch):
    monkeypatch.setattr(
        main,
        "restore_supabase_session",
        lambda *_args: {
            "user": _user(),
            "db": object(),
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "refreshed": False,
        },
    )

    async def call_next(_request):
        return main.StreamingResponse(
            iter(["stream chunk"]),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform"},
        )

    response = asyncio.run(
        main.auth_middleware(_request("POST", "/api/ask/stream"), call_next)
    )

    assert response.status_code == 200
    _assert_private_no_store(response, no_transform=True)
    _assert_security_headers(response)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("slack", "slack"),
        (" Slack ", "slack"),
        ("EMAIL", "email"),
        ("note", "note"),
        (None, "note"),
        ("memo", "note"),
        ('<img src=x onerror="alert(1)">', "note"),
    ],
)
def test_normalize_source_uses_allowlist(raw, expected):
    assert main.normalize_source(raw) == expected


def test_normalize_parsed_payload_sanitizes_untrusted_fields():
    payload = main.normalize_parsed_payload(
        {
            "source": '<script>alert("x")</script>',
            "records": [
                {
                    "content": "  retained content  ",
                    "metadata": "not-an-object",
                    "tags": "not-a-list",
                    "expires_at": "not-a-date",
                }
            ],
        },
        "fallback should not be used",
    )

    assert payload == {
        "source": "note",
        "records": [
            {
                "content": "retained content",
                "metadata": {},
                "tags": [],
                "expires_at": None,
            }
        ],
    }


def test_normalize_parsed_payload_chunks_long_fallback():
    original = "x" * 13_001

    payload = main.normalize_parsed_payload(
        {"source": "slack", "records": "bad"}, original
    )

    assert payload["source"] == "note"
    assert len(payload["records"]) == 3
    assert all(len(record["content"]) <= 6_000 for record in payload["records"])
    assert "".join(record["content"] for record in payload["records"]) == original
    assert all(record["tags"] == [] for record in payload["records"])


@pytest.mark.parametrize(
    ("scope", "expected_owner"),
    [("personal", ALICE_ID), ("shared", None)],
)
def test_ingest_sets_scope_owner_creator_and_space_scoped_conflict_key(
    monkeypatch, scope, expected_owner
):
    db = _MemoryClient()
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [{
                "content": "remember this",
                "metadata": {"status": "참고"},
                "tags": ["test"],
                "expires_at": None,
            }],
        },
    )
    monkeypatch.setattr(main, "embed", lambda texts: [[0.1] for _ in texts])
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_catalog_cache", {ALICE_ID: (1.0, [{"id": "stale"}])})
    monkeypatch.setattr(main, "_management_catalog_cache", {ALICE_ID: (1.0, [])})

    result = main.ingest(
        main.IngestRequest(text="remember this", scope=scope),
        _request("POST", "/api/ingest", user=_user(), db=db),
    )

    assert result["scope"] == scope
    assert result["saved"] == 1
    assert result["skipped"] == 0
    assert len(db.upserts) == 1
    upsert = db.upserts[0]
    assert upsert["on_conflict"] == "scope,owner_user_id,content_hash"
    assert upsert["ignore_duplicates"] is True
    row = upsert["rows"][0]
    assert row["scope"] == scope
    assert row["owner_user_id"] == expected_owner
    assert row["created_by_user_id"] == ALICE_ID
    assert main._catalog_cache == {}
    assert main._management_catalog_cache == {}


@pytest.mark.parametrize("scope", ["personal", "shared"])
def test_ingest_rejects_openai_api_key_before_parser_or_storage(monkeypatch, scope):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("OpenAI key input must be rejected before external work")

    monkeypatch.setattr(main, "parse_pasted_text", unexpected)
    monkeypatch.setattr(main, "embed", unexpected)

    with pytest.raises(HTTPException) as exc_info:
        main.ingest(
            main.IngestRequest(
                text="API key: sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
                scope=scope,
            ),
            _request("POST", "/api/ingest"),
        )

    assert exc_info.value.status_code == 400
    assert "API 키" in exc_info.value.detail


def test_memory_catalog_is_isolated_by_user_uuid_and_cached_per_user(monkeypatch):
    db = _MemoryClient([
        _memory("shared", scope="shared", creator=BOB_ID),
        _memory("alice-private", scope="personal", owner=ALICE_ID),
        _memory("bob-private", scope="personal", owner=BOB_ID, creator=BOB_ID),
    ])
    monkeypatch.setattr(main, "_catalog_cache", {})
    monkeypatch.setattr(main, "_management_catalog_cache", {})

    alice_first = main.memory_catalog(db, ALICE_ID)
    alice_second = main.memory_catalog(db, ALICE_ID)
    bob_first = main.memory_catalog(db, BOB_ID)

    assert [row["id"] for row in alice_first] == ["shared", "alice-private"]
    assert alice_second == alice_first
    assert [row["id"] for row in bob_first] == ["shared", "bob-private"]
    assert db.table_calls == 2
    assert set(main._catalog_cache) == {ALICE_ID, BOB_ID}
    assert ALICE_ID in db.visibility_expressions[0]
    assert BOB_ID in db.visibility_expressions[1]


def test_list_memories_includes_shared_and_own_but_hides_other_personal():
    db = _MemoryClient([
        _memory("shared", scope="shared", creator=BOB_ID),
        _memory("alice-private", scope="personal", owner=ALICE_ID),
        _memory("bob-private", scope="personal", owner=BOB_ID, creator=BOB_ID),
    ])
    response = Response()

    items = main.list_memories(
        request=_request("GET", "/api/memories", user=_user(), db=db),
        response=response,
    )

    assert {item["id"] for item in items} == {"shared", "alice-private"}
    assert "bob-private" not in {item["id"] for item in items}
    assert response.headers["x-total-count"] == "2"
    by_id = {item["id"]: item for item in items}
    assert by_id["shared"]["scope"] == "shared"
    assert by_id["shared"]["can_edit"] is False
    assert by_id["alice-private"]["scope"] == "personal"
    assert by_id["alice-private"]["can_edit"] is True
    assert db.visibility_expressions == [
        "scope.eq.shared,"
        f"and(scope.eq.personal,owner_user_id.eq.{ALICE_ID})"
    ]


def test_filtered_list_supports_legacy_sender_tags_pagination_and_count(monkeypatch):
    def legacy_row(memory_id, created_at, sender="레거시 담당자", tags=None):
        row = _memory(memory_id, content="진행 중인 레거시 업무")
        row["created_at"] = created_at
        row["metadata"] = {
            "sender": sender,
            "tags": tags or ["AI Tech Innovation팀", "Legacy Project"],
        }
        return row

    rows = [
        legacy_row("newest", "2026-08-07T03:00:00+00:00"),
        legacy_row("middle", "2026-08-07T02:00:00+00:00"),
        legacy_row("oldest", "2026-08-07T01:00:00+00:00"),
        legacy_row("other-person", "2026-08-07T00:00:00+00:00", sender="다른 담당자"),
        legacy_row("other-project", "2026-08-06T23:00:00+00:00", tags=["Other Project"]),
    ]
    calls = []

    def fake_all_memory_catalog(db, user_id):
        calls.append((db, user_id))
        return rows

    db = object()
    monkeypatch.setattr(main, "all_memory_catalog", fake_all_memory_catalog)
    response = Response()

    items = main.list_memories(
        request=_request("GET", "/api/memories", user=_user(), db=db),
        response=response,
        limit=1,
        offset=1,
        person="레거시 담당자",
        project="Legacy Project",
    )

    assert calls == [(db, ALICE_ID)]
    assert response.headers["x-total-count"] == "3"
    assert [item["id"] for item in items] == ["middle"]
    assert items[0]["metadata"]["person"] == "레거시 담당자"
    assert items[0]["metadata"]["project"] == "Legacy Project"


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_other_users_personal_memory_cannot_be_updated_or_deleted(monkeypatch, operation):
    db = _MemoryClient([
        _memory("bob-private", scope="personal", owner=BOB_ID, creator=BOB_ID),
    ])
    request = _request(
        "PATCH" if operation == "update" else "DELETE",
        "/api/memories/bob-private",
        user=_user(),
        db=db,
    )
    monkeypatch.setattr(main, "embed", lambda *_args: (_ for _ in ()).throw(
        AssertionError("hidden memory must not reach embedding")
    ))
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    with pytest.raises(HTTPException) as exc_info:
        if operation == "update":
            main.update_memory(
                "bob-private",
                main.UpdateMemoryRequest(content="attempted overwrite"),
                request,
            )
        else:
            main.delete_memory("bob-private", request)

    assert exc_info.value.status_code == 404
    assert db.executed_operations == ["select"]
    assert db.rows[0]["content"] == "bob-private content"
    assert ALICE_ID in db.visibility_expressions[0]


def test_update_rejects_openai_api_key_before_lookup_or_embedding():
    with pytest.raises(HTTPException) as exc_info:
        main.update_memory(
            "memory-id",
            main.UpdateMemoryRequest(
                content="sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
            ),
            _request("PATCH", "/api/memories/memory-id"),
        )

    assert exc_info.value.status_code == 400
    assert "API 키" in exc_info.value.detail


def test_update_rejects_openai_api_key_nested_in_metadata_before_lookup():
    with pytest.raises(HTTPException) as exc_info:
        main.update_memory(
            "memory-id",
            main.UpdateMemoryRequest(
                content="otherwise safe content",
                metadata={
                    "subject": {
                        "credentials": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
                    }
                },
            ),
            _request("PATCH", "/api/memories/memory-id"),
        )

    assert exc_info.value.status_code == 400
    assert "API 키" in exc_info.value.detail


def test_prepare_answer_searches_shared_plus_requesting_users_personal_memories(monkeypatch):
    all_rows = [
        {
            **_memory("shared", scope="shared", creator=BOB_ID, content="공유 운영 정책"),
            "similarity": 0.94,
        },
        {
            **_memory(
                "alice-private",
                scope="personal",
                owner=ALICE_ID,
                content="앨리스 개인 일정",
            ),
            "similarity": 0.92,
        },
        {
            **_memory(
                "bob-private",
                scope="personal",
                owner=BOB_ID,
                creator=BOB_ID,
                content="밥 개인 비밀",
            ),
            "similarity": 0.99,
        },
    ]

    class FakeRpc:
        def __init__(self, rows):
            self.rows = rows

        def execute(self):
            return _result(self.rows)

    class FakeSearchClient:
        def __init__(self):
            self.calls = []

        def rpc(self, name, params):
            self.calls.append((name, params))
            user_id = params["requesting_user_id"]
            visible = [
                row for row in all_rows
                if row["scope"] == "shared"
                or (row["scope"] == "personal" and row["owner_user_id"] == user_id)
            ]
            return FakeRpc(visible)

    db = FakeSearchClient()
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.1]])
    monkeypatch.setattr(main, "memory_catalog", lambda _db, _user_id: [])
    monkeypatch.setattr(
        main,
        "contextualize_search_question",
        lambda question, _history: question,
    )

    prepared = main.prepare_answer(
        main.AskRequest(
            question="운영 정책과 내 일정을 상세히 알려줘",
            history=[
                {"role": "user", "content": "이전 질문"},
                {"role": "assistant", "content": "OLD_HALLUCINATED_ANSWER"},
            ],
        ),
        db,
        _user(),
    )

    assert len(db.calls) == 1
    name, params = db.calls[0]
    assert name == "match_memories"
    assert params == {
        "query_embedding": [0.1],
        "match_count": main.TOP_K * 3,
        "query_scope": "personal",
        "requesting_user_id": ALICE_ID,
    }
    assert {source["id"] for source in prepared["sources"]} == {
        "shared",
        "alice-private",
    }
    assert "bob-private" not in {source["id"] for source in prepared["sources"]}
    combined = "\n".join(message["content"] for message in prepared["messages"])
    assert "공유 운영 정책" in combined
    assert "앨리스 개인 일정" in combined
    assert "밥 개인 비밀" not in combined
    assert "OLD_HALLUCINATED_ANSWER" not in combined


def test_stream_emits_meta_deltas_and_done_without_network(monkeypatch):
    prepared = {
        "fallback": None,
        "messages": [{"role": "user", "content": "question"}],
        "resolved_question": "resolved question",
        "sources": [{"id": "memory-1"}],
    }
    request_db = object()
    monkeypatch.setattr(
        main,
        "prepare_answer",
        lambda _request, db, user: (
            prepared
            if db is request_db and user["id"] == ALICE_ID
            else pytest.fail("request-scoped identity was not forwarded")
        ),
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="첫 번째 "))]
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="답변"))]
        ),
    ]

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return iter(chunks)

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    response = main.ask_stream(
        main.AskRequest(question="question"),
        _request(
            "POST",
            "/api/ask/stream",
            user=_user(),
            db=request_db,
        ),
    )

    async def collect():
        return [part async for part in response.body_iterator]

    raw_parts = asyncio.run(collect())
    events = [
        json.loads(line)
        for part in raw_parts
        for line in (part.decode() if isinstance(part, bytes) else part).splitlines()
        if line
    ]

    assert [event["type"] for event in events] == ["meta", "delta", "delta", "done"]
    assert events[0]["resolved_question"] == "resolved question"
    assert events[0]["sources"] == [{"id": "memory-1"}]
    assert "".join(event.get("content", "") for event in events) == "첫 번째 답변"
