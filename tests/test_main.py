import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import supabase
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
        self.published_shared_only = False
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
        self.published_shared_only = "publication_status.eq.published" in expression
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
        if self.visibility_user_id is not None:
            shared_visible = row.get("scope") in {None, "shared"}
            if self.published_shared_only:
                shared_visible = shared_visible and (
                    row.get("publication_status", "published") == "published"
                )
            personal_visible = (
                row.get("scope") == "personal"
                and row.get("owner_user_id") == self.visibility_user_id
                and (
                    not self.published_shared_only
                    or row.get("publication_status", "published") == "published"
                )
            )
            if not (shared_visible or personal_visible):
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


def _memory(
    memory_id,
    *,
    scope="shared",
    owner=None,
    creator=ALICE_ID,
    content=None,
    publication_status="published",
    proposal_id=None,
):
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
        "publication_status": publication_status,
        "proposal_id": proposal_id,
        "approved_at": (
            "2026-08-09T01:00:00+00:00"
            if publication_status == "published"
            else None
        ),
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


@pytest.mark.parametrize(
    ("error_code", "upstream_status", "expected_status", "expected_detail"),
    [
        ("email_exists", 422, 409, "이미 가입된 이메일"),
        ("user_already_exists", 422, 409, "이미 가입된 이메일"),
        ("weak_password", 422, 400, "비밀번호가 보안 정책"),
        ("email_address_invalid", 400, 400, "사용할 수 없는 이메일 주소"),
        ("email_address_not_authorized", 400, 400, "사용할 수 없는 이메일 주소"),
        ("over_request_rate_limit", 429, 429, "잠시 후 다시 시도"),
        ("over_email_send_rate_limit", 429, 429, "잠시 후 다시 시도"),
    ],
)
def test_signup_maps_supabase_auth_errors_to_specific_user_messages(
    monkeypatch,
    error_code,
    upstream_status,
    expected_status,
    expected_detail,
):
    class FakeSignupError(Exception):
        def __init__(self):
            super().__init__("upstream auth error")
            self.message = "upstream auth error"
            self.code = error_code
            self.status = upstream_status

    class FakeAuth:
        def sign_up(self, _payload):
            raise FakeSignupError()

    monkeypatch.setattr(main, "account_profile_by_username", lambda _username: None)
    monkeypatch.setattr(
        main,
        "new_supabase_client",
        lambda **_kwargs: SimpleNamespace(auth=FakeAuth()),
    )

    with pytest.raises(HTTPException) as raised:
        main.signup(
            main.SignupRequest(
                username="alice",
                email="alice@example.com",
                password="correct horse battery staple",
            ),
            _request("POST", "/api/signup"),
            Response(),
        )

    assert raised.value.status_code == expected_status
    assert expected_detail in raised.value.detail


def test_signup_error_mapper_supports_real_supabase_email_exists_error():
    exc = supabase.AuthApiError(
        "User already registered",
        422,
        "email_exists",
    )

    mapped = main.signup_http_exception(exc)

    assert mapped.status_code == 409
    assert "이미 가입된 이메일" in mapped.detail


def test_signup_error_mapper_supports_real_supabase_weak_password_error():
    exc = supabase.AuthWeakPasswordError(
        "Password should be at least 8 characters",
        422,
        ["length"],
    )

    mapped = main.signup_http_exception(exc)

    assert mapped.status_code == 400
    assert "비밀번호가 보안 정책" in mapped.detail


def test_signup_error_mapper_supports_legacy_error_attributes():
    class LegacyAuthError(Exception):
        error_code = "email_exists"
        status_code = 422

    mapped = main.signup_http_exception(LegacyAuthError("upstream auth error"))

    assert mapped.status_code == 409
    assert "이미 가입된 이메일" in mapped.detail


def test_signup_error_mapper_distinguishes_database_and_unknown_server_errors():
    class UnknownServerError(Exception):
        status = 500

    database_error = main.signup_http_exception(
        RuntimeError("Database error saving new user")
    )
    unknown_server_error = main.signup_http_exception(
        UnknownServerError("upstream internal error")
    )

    assert database_error.status_code == 503
    assert "데이터베이스 설정 오류" in database_error.detail
    assert unknown_server_error.status_code == 503
    assert "서비스에 일시적인 오류" in unknown_server_error.detail
    assert database_error.detail != unknown_server_error.detail


def test_signup_error_mapper_maps_status_only_rate_limit():
    class StatusOnlyError(Exception):
        status = 429

    mapped = main.signup_http_exception(StatusOnlyError("upstream auth error"))

    assert mapped.status_code == 429
    assert "잠시 후 다시 시도" in mapped.detail


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


def test_login_rejects_email_without_profile_lookup(monkeypatch):
    profiles = _ProfileClient({"alice": "alice@example.com"})
    login_payloads = []
    audits = []

    class FakeAuth:
        def sign_in_with_password(self, payload):
            login_payloads.append(payload)
            raise RuntimeError("invalid credentials")

    monkeypatch.setattr(main, "admin_sb", profiles)
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

    with pytest.raises(HTTPException) as raised:
        main.login(
            main.LoginRequest(username=" Alice@Example.COM ", password="password"),
            _request("POST", "/api/login"),
            Response(),
        )

    assert profiles.lookups == []
    assert login_payloads == [{
        "email": "missing-account@invalid.local",
        "password": "password",
    }]
    assert raised.value.status_code == 401
    assert raised.value.detail == "아이디 또는 비밀번호가 맞지 않아요."
    assert audits[-1][0][2] == "login_failed"
    assert audits[-1][1]["attempted_identifier_kind"] == "invalid"


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


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_auth_architecture_page_rejects_non_admin_roles(monkeypatch, role):
    monkeypatch.setattr(
        main,
        "restore_supabase_session",
        lambda *_args: {
            "user": _user(role=role),
            "db": object(),
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "refreshed": False,
        },
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    async def call_next(_request):
        pytest.fail("non-admin must not reach the protected page route")

    response = asyncio.run(
        main.auth_middleware(_request("GET", "/auth-architecture"), call_next)
    )

    assert response.status_code == 403
    _assert_private_no_store(response)
    _assert_security_headers(response)


def test_auth_architecture_page_allows_admin(monkeypatch):
    monkeypatch.setattr(
        main,
        "restore_supabase_session",
        lambda *_args: {
            "user": _user(role="admin"),
            "db": object(),
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "refreshed": False,
        },
    )

    async def call_next(request):
        assert request.state.user["role"] == "admin"
        return JSONResponse({"ok": True})

    response = asyncio.run(
        main.auth_middleware(_request("GET", "/auth-architecture"), call_next)
    )

    assert response.status_code == 200
    _assert_private_no_store(response)
    _assert_security_headers(response)


def test_auth_architecture_route_revalidates_admin_role():
    assert main.required_action("GET", "/auth-architecture") == "admin"
    with pytest.raises(HTTPException) as raised:
        main.auth_architecture(
            _request("GET", "/auth-architecture", user=_user(role="editor"))
        )
    assert raised.value.status_code == 403


def test_auth_architecture_link_is_hidden_unless_session_is_admin():
    html = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'<a class="architecture-button role-hidden" id="architectureBtn"',
        html,
    )
    assert '$("architectureBtn").classList.toggle("role-hidden", !isAdmin())' in html
    assert '$("architectureBtn").classList.add("role-hidden")' in html


@pytest.mark.parametrize("path", ["/api/memories", "/auth-architecture"])
def test_auth_middleware_rejects_missing_or_invalid_supabase_session(
    monkeypatch,
    path,
):
    monkeypatch.setattr(main, "restore_supabase_session", lambda *_args: None)
    called = False

    async def call_next(_request):
        nonlocal called
        called = True
        return JSONResponse({"ok": True})

    response = asyncio.run(
        main.auth_middleware(_request("GET", path), call_next)
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


def test_personal_ingest_sets_owner_creator_and_published_state(monkeypatch):
    scope = "personal"
    expected_owner = ALICE_ID
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
    monkeypatch.setattr(main, "consume_ai_use", lambda user_id: 9)
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
    assert result["status"] == "published"
    assert result["proposal_id"] is None
    assert result["remaining_uses"] == 9
    assert len(db.upserts) == 1
    upsert = db.upserts[0]
    assert upsert["on_conflict"] == "scope,owner_user_id,content_hash"
    assert upsert["ignore_duplicates"] is True
    row = upsert["rows"][0]
    assert row["scope"] == scope
    assert row["owner_user_id"] == expected_owner
    assert row["created_by_user_id"] == ALICE_ID
    assert row["publication_status"] == "published"
    assert row["proposal_id"] is None
    assert row["approved_at"]
    assert main._catalog_cache == {}
    assert main._management_catalog_cache == {}


def test_shared_ingest_by_regular_user_creates_pending_proposal_with_author_vote(
    monkeypatch,
):
    request_db = _MemoryClient()

    class ProposalClient(_MemoryClient):
        def __init__(self):
            super().__init__()
            self.rpc_calls = []

        def rpc(self, name, params):
            self.rpc_calls.append((name, params))
            return SimpleNamespace(
                execute=lambda: _result([{
                    "proposal_id": params["requested_proposal_id"],
                    "inserted_count": 1,
                }])
            )

    service_db = ProposalClient()
    monkeypatch.setattr(main, "admin_sb", service_db)
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [{
                "content": "팀 전체에 공유할 운영 정보",
                "metadata": {"status": "참고"},
                "tags": ["운영"],
                "expires_at": None,
            }],
        },
    )
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.1]])
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    result = main.ingest(
        main.IngestRequest(text="팀 전체에 공유할 운영 정보", scope="shared"),
        _request("POST", "/api/ingest", user=_user(), db=request_db),
    )

    assert result["status"] == "pending"
    assert result["published"] is False
    assert result["approval_count"] == 1
    assert result["required_approvals"] == 2
    assert result["proposal_id"]
    assert request_db.upserts == []
    assert len(service_db.rpc_calls) == 1
    name, params = service_db.rpc_calls[0]
    assert name == "create_shared_memory_proposal"
    assert params["creator_user_id"] == ALICE_ID
    assert params["requested_proposal_id"] == result["proposal_id"]
    assert len(params["proposal_records"]) == 1
    pending_row = params["proposal_records"][0]
    assert pending_row["scope"] == "shared"
    assert pending_row["owner_user_id"] is None
    assert pending_row["publication_status"] == "pending"
    assert pending_row["proposal_id"] == result["proposal_id"]
    assert pending_row["approved_at"] is None


def test_shared_ingest_by_admin_is_published_without_a_proposal(monkeypatch):
    db = _MemoryClient()
    monkeypatch.setattr(main, "admin_sb", db)
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [{
                "content": "관리자가 바로 공개하는 팀 정보",
                "metadata": {"status": "참고"},
                "tags": [],
                "expires_at": None,
            }],
        },
    )
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.2]])
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    result = main.ingest(
        main.IngestRequest(text="관리자가 바로 공개하는 팀 정보", scope="shared"),
        _request(
            "POST",
            "/api/ingest",
            user=_user(role="admin"),
            db=db,
        ),
    )

    assert result["status"] == "published"
    assert result["published"] is True
    assert result["proposal_id"] is None
    assert result["approval_count"] == 2
    assert len(db.upserts) == 1
    row = db.upserts[0]["rows"][0]
    assert row["scope"] == "shared"
    assert row["publication_status"] == "published"
    assert row["approved_at"]


@pytest.mark.parametrize(
    ("user_id", "proposal_status", "approval_count", "published"),
    [
        (ALICE_ID, "pending", 1, False),
        (BOB_ID, "published", 2, True),
    ],
)
def test_resaving_pending_shared_content_is_idempotent_for_author_and_approves_for_other_user(
    monkeypatch,
    user_id,
    proposal_status,
    approval_count,
    published,
):
    proposal_id = "33333333-3333-4333-8333-333333333333"
    content = "두 명의 동의가 필요한 팀 정보"
    pending = _memory(
        "pending-memory",
        scope="shared",
        creator=ALICE_ID,
        content=content,
        publication_status="pending",
        proposal_id=proposal_id,
    )
    pending["content_hash"] = main.hashlib.sha256(
        f"note\0{content}".encode()
    ).hexdigest()
    service_db = _MemoryClient([pending])
    request_db = _RpcClient({
        "approve_shared_memory_proposal": [{
            "proposal_id": proposal_id,
            "proposal_status": proposal_status,
            "approval_count": approval_count,
            "required_approvals": 2,
            "published": published,
        }],
    })
    monkeypatch.setattr(main, "admin_sb", service_db)
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [{
                "content": content,
                "metadata": {"status": "참고"},
                "tags": [],
                "expires_at": None,
            }],
        },
    )
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.3]])
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "invalidate_catalog_cache", lambda: None)

    result = main.ingest(
        main.IngestRequest(text=content, scope="shared"),
        _request(
            "POST",
            "/api/ingest",
            user=_user(user_id),
            db=request_db,
        ),
    )

    assert request_db.calls == [(
        "approve_shared_memory_proposal",
        {"target_proposal_id": proposal_id},
    )]
    assert result["saved"] == 0
    assert result["skipped"] == 1
    assert result["proposal_id"] == proposal_id
    assert result["status"] == proposal_status
    assert result["published"] is published
    assert result["approval_count"] == approval_count
    assert result["required_approvals"] == 2


def test_partial_create_collision_approves_other_proposal_but_not_own(monkeypatch):
    own_proposal_id = "33333333-3333-4333-8333-333333333333"
    other_proposal_id = "44444444-4444-4444-8444-444444444444"
    contents = ["새로 제안되는 내용", "동시에 다른 제안에 들어간 내용"]
    hashes = [
        main.hashlib.sha256(f"note\0{content}".encode()).hexdigest()
        for content in contents
    ]

    class PartialCollisionClient(_MemoryClient):
        def __init__(self):
            super().__init__()
            self.rpc_calls = []

        def rpc(self, name, params):
            self.rpc_calls.append((name, params))

            def execute():
                requested_id = params["requested_proposal_id"]
                self.rows = [
                    {
                        **_memory(
                            "own-pending",
                            creator=BOB_ID,
                            content=contents[0],
                            publication_status="pending",
                            proposal_id=requested_id,
                        ),
                        "content_hash": hashes[0],
                    },
                    {
                        **_memory(
                            "other-pending",
                            creator=ALICE_ID,
                            content=contents[1],
                            publication_status="pending",
                            proposal_id=other_proposal_id,
                        ),
                        "content_hash": hashes[1],
                    },
                ]
                return _result([{
                    "proposal_id": requested_id,
                    "inserted_count": 1,
                }])

            return SimpleNamespace(execute=execute)

    service_db = PartialCollisionClient()
    request_db = _RpcClient({
        "approve_shared_memory_proposal": [{
            "proposal_id": other_proposal_id,
            "proposal_status": "published",
            "approval_count": 2,
            "required_approvals": 2,
            "published": True,
        }],
    })
    monkeypatch.setattr(main, "admin_sb", service_db)
    monkeypatch.setattr(
        main,
        "uuid",
        SimpleNamespace(
            uuid4=lambda: uuid.UUID(own_proposal_id),
            UUID=uuid.UUID,
        ),
    )
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [
                {
                    "content": content,
                    "metadata": {"status": "참고"},
                    "tags": [],
                    "expires_at": None,
                }
                for content in contents
            ],
        },
    )
    monkeypatch.setattr(main, "embed", lambda texts: [[0.1] for _ in texts])
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "invalidate_catalog_cache", lambda: None)

    result = main.ingest(
        main.IngestRequest(text="\n".join(contents), scope="shared"),
        _request(
            "POST",
            "/api/ingest",
            user=_user(BOB_ID),
            db=request_db,
        ),
    )

    assert request_db.calls == [(
        "approve_shared_memory_proposal",
        {"target_proposal_id": other_proposal_id},
    )]
    assert result["proposal_id"] == own_proposal_id
    assert result["status"] == "pending"
    assert result["approval_count"] == 1
    assert result["saved"] == 1
    assert result["skipped"] == 1


def test_create_race_approves_every_collided_pending_proposal(monkeypatch):
    proposal_ids = [
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
    ]
    contents = ["첫 번째 충돌 기억", "두 번째 충돌 기억"]
    hashes = [
        main.hashlib.sha256(f"note\0{content}".encode()).hexdigest()
        for content in contents
    ]

    class AllCollisionClient(_MemoryClient):
        def rpc(self, name, params):
            assert name == "create_shared_memory_proposal"

            def execute():
                self.rows = [
                    {
                        **_memory(
                            f"pending-{index}",
                            creator=ALICE_ID,
                            content=content,
                            publication_status="pending",
                            proposal_id=proposal_ids[index],
                        ),
                        "content_hash": hashes[index],
                    }
                    for index, content in enumerate(contents)
                ]
                return _result([{"proposal_id": None, "inserted_count": 0}])

            return SimpleNamespace(execute=execute)

    class ApprovalClient:
        def __init__(self):
            self.calls = []

        def rpc(self, name, params):
            self.calls.append((name, params))
            proposal_id = params["target_proposal_id"]
            return SimpleNamespace(
                execute=lambda: _result([{
                    "proposal_id": proposal_id,
                    "proposal_status": "published",
                    "approval_count": 2,
                    "required_approvals": 2,
                    "published": True,
                }])
            )

    service_db = AllCollisionClient()
    request_db = ApprovalClient()
    monkeypatch.setattr(main, "admin_sb", service_db)
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [
                {
                    "content": content,
                    "metadata": {"status": "참고"},
                    "tags": [],
                    "expires_at": None,
                }
                for content in contents
            ],
        },
    )
    monkeypatch.setattr(main, "embed", lambda texts: [[0.2] for _ in texts])
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "invalidate_catalog_cache", lambda: None)

    result = main.ingest(
        main.IngestRequest(text="\n".join(contents), scope="shared"),
        _request(
            "POST",
            "/api/ingest",
            user=_user(BOB_ID),
            db=request_db,
        ),
    )

    assert {
        params["target_proposal_id"] for _name, params in request_db.calls
    } == set(proposal_ids)
    assert len(request_db.calls) == 2
    assert result["saved"] == 0
    assert result["skipped"] == 2
    assert result["status"] == "published"
    assert result["approval_count"] == 2


def test_pending_proposal_list_marks_author_vote_and_other_users_eligibility(
    monkeypatch,
):
    proposal_id = "33333333-3333-4333-8333-333333333333"
    tables = {
        "shared_memory_proposals": [{
            "id": proposal_id,
            "content": "팀 전체에 공유할 운영 정보",
            "source": "note",
            "created_by_user_id": ALICE_ID,
            "status": "pending",
            "required_approvals": 2,
            "created_at": "2026-08-10T01:00:00+00:00",
            "published_at": None,
        }],
        "shared_memory_proposal_approvals": [{
            "proposal_id": proposal_id,
            "approver_user_id": ALICE_ID,
        }],
        "account_profiles": [{"id": ALICE_ID, "username": "alice"}],
    }

    class Query:
        def __init__(self, rows):
            self.rows = rows
            self.equal_filters = []
            self.in_filters = []

        def select(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

        def eq(self, column, value):
            self.equal_filters.append((column, value))
            return self

        def in_(self, column, values):
            self.in_filters.append((column, set(values)))
            return self

        def limit(self, _count):
            return self

        def execute(self):
            rows = [
                row for row in self.rows
                if all(row.get(column) == value for column, value in self.equal_filters)
                and all(row.get(column) in values for column, values in self.in_filters)
            ]
            return _result([dict(row) for row in rows])

    class ProposalReadClient:
        def table(self, name):
            return Query(tables[name])

    monkeypatch.setattr(main, "admin_sb", ProposalReadClient())

    author_view = main.list_shared_memory_proposals(
        _request("GET", "/api/shared-memory-proposals", user=_user())
    )["proposals"][0]
    other_view = main.list_shared_memory_proposals(
        _request(
            "GET",
            "/api/shared-memory-proposals",
            user=_user(BOB_ID),
        )
    )["proposals"][0]

    assert author_view["created_by_username"] == "alice"
    assert author_view["approval_count"] == 1
    assert author_view["required_approvals"] == 2
    assert author_view["approved_by_me"] is True
    assert author_view["can_approve"] is False
    assert other_view["approval_count"] == 1
    assert other_view["approved_by_me"] is False
    assert other_view["can_approve"] is True


@pytest.mark.parametrize(
    ("loaded_count", "expected_count", "has_more", "next_offset"),
    [
        (3, 2, True, 6),
        (2, 2, False, None),
        (1, 1, False, None),
    ],
)
def test_shared_proposal_get_pagination_contract(
    monkeypatch,
    loaded_count,
    expected_count,
    has_more,
    next_offset,
):
    calls = []
    rows = [{"id": f"proposal-{index}"} for index in range(loaded_count)]

    def fake_load(user, proposal_id=None, *, page_size=100, offset=0):
        calls.append((user["id"], proposal_id, page_size, offset))
        return rows

    monkeypatch.setattr(main, "load_shared_proposals", fake_load)

    result = main.list_shared_memory_proposals(
        _request("GET", "/api/shared-memory-proposals", user=_user()),
        limit=2,
        offset=4,
    )

    assert calls == [(ALICE_ID, None, 3, 4)]
    assert result == {
        "proposals": rows[:expected_count],
        "has_more": has_more,
        "next_offset": next_offset,
    }


def test_shared_proposal_loader_uses_server_side_range(monkeypatch):
    proposal_rows = [
        {
            "id": f"proposal-{index}",
            "content": f"proposal {index}",
            "source": "note",
            "created_by_user_id": None,
            "status": "pending",
            "required_approvals": 2,
            "created_at": f"2026-08-10T00:{index:02d}:00+00:00",
            "published_at": None,
        }
        for index in range(10)
    ]

    class PageQuery:
        def __init__(self, client, table_name):
            self.client = client
            self.table_name = table_name
            self.equal_filters = []
            self.in_filters = []
            self.bounds = None

        def select(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

        def eq(self, column, value):
            self.equal_filters.append((column, value))
            return self

        def in_(self, column, values):
            self.in_filters.append((column, set(values)))
            return self

        def range(self, start, end):
            self.bounds = (start, end)
            self.client.ranges.append(self.bounds)
            return self

        def execute(self):
            rows = self.client.tables[self.table_name]
            rows = [
                row for row in rows
                if all(row.get(column) == value for column, value in self.equal_filters)
                and all(row.get(column) in values for column, values in self.in_filters)
            ]
            if self.bounds:
                start, end = self.bounds
                rows = rows[start:end + 1]
            return _result([dict(row) for row in rows])

    class PageClient:
        def __init__(self):
            self.tables = {
                "shared_memory_proposals": proposal_rows,
                "shared_memory_proposal_approvals": [],
                "account_profiles": [],
            }
            self.ranges = []

        def table(self, name):
            return PageQuery(self, name)

    client = PageClient()
    monkeypatch.setattr(main, "admin_sb", client)

    page = main.load_shared_proposals(_user(), page_size=3, offset=4)

    assert [row["id"] for row in page] == [
        "proposal-4",
        "proposal-5",
        "proposal-6",
    ]
    assert client.ranges == [(4, 6)]


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (101, 0), (10, -1)],
)
def test_shared_proposal_get_rejects_invalid_pagination(
    monkeypatch,
    limit,
    offset,
):
    monkeypatch.setattr(
        main,
        "load_shared_proposals",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid pagination must be rejected before database access"
        ),
    )

    with pytest.raises(HTTPException) as raised:
        main.list_shared_memory_proposals(
            _request("GET", "/api/shared-memory-proposals", user=_user()),
            limit=limit,
            offset=offset,
        )

    assert raised.value.status_code == 422


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_distinct_user_or_admin_can_approve_pending_proposal(monkeypatch, role):
    proposal_id = "33333333-3333-4333-8333-333333333333"
    db = _RpcClient({
        "approve_shared_memory_proposal": [{
            "proposal_id": proposal_id,
            "proposal_status": "published",
            "approval_count": 2,
            "required_approvals": 2,
            "published": True,
        }],
    })
    invalidations = []
    monkeypatch.setattr(
        main,
        "load_shared_proposals",
        lambda user, requested_id=None: [{
            "id": requested_id,
            "content": "승인된 팀 정보",
            "source": "note",
            "created_by_user_id": ALICE_ID,
            "created_by_username": "alice",
            "status": "published",
            "created_at": "2026-08-10T01:00:00+00:00",
            "approval_count": 2,
            "required_approvals": 2,
            "approved_by_me": True,
            "can_approve": False,
        }],
    )
    monkeypatch.setattr(
        main,
        "invalidate_catalog_cache",
        lambda: invalidations.append(True),
    )
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *_args, **_kwargs: pytest.fail(
            "approval audit must be atomic inside the SQL RPC"
        ),
    )

    result = main.approve_shared_memory_proposal(
        proposal_id,
        _request(
            "POST",
            f"/api/shared-memory-proposals/{proposal_id}/approve",
            user=_user(BOB_ID, role=role),
            db=db,
        ),
    )

    assert db.calls == [(
        "approve_shared_memory_proposal",
        {"target_proposal_id": proposal_id},
    )]
    assert result["status"] == "published"
    assert result["published"] is True
    assert result["approval_count"] == 2
    assert invalidations == [True]


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
        "and(scope.eq.shared,publication_status.eq.published),"
        "and(scope.eq.personal,publication_status.eq.published,"
        f"owner_user_id.eq.{ALICE_ID})"
    ]


def test_published_shared_is_visible_to_every_user_but_pending_is_hidden(monkeypatch):
    db = _MemoryClient([
        _memory(
            "published-shared",
            scope="shared",
            creator=ALICE_ID,
            content="공개된 팀 운영 정보",
        ),
        _memory(
            "pending-shared",
            scope="shared",
            creator=ALICE_ID,
            content="아직 승인되지 않은 팀 정보",
            publication_status="pending",
            proposal_id="33333333-3333-4333-8333-333333333333",
        ),
        _memory("alice-private", scope="personal", owner=ALICE_ID),
        _memory("bob-private", scope="personal", owner=BOB_ID, creator=BOB_ID),
    ])
    monkeypatch.setattr(main, "_catalog_cache", {})
    monkeypatch.setattr(main, "_management_catalog_cache", {})

    alice_ids = {row["id"] for row in main.memory_catalog(db, ALICE_ID)}
    bob_ids = {row["id"] for row in main.memory_catalog(db, BOB_ID)}
    bob_list_ids = {
        row["id"]
        for row in main.list_memories(
            request=_request(
                "GET",
                "/api/memories",
                user=_user(BOB_ID),
                db=db,
            ),
            response=Response(),
        )
    }

    assert alice_ids == {"published-shared", "alice-private"}
    assert bob_ids == {"published-shared", "bob-private"}
    assert bob_list_ids == {"published-shared", "bob-private"}
    assert "pending-shared" not in alice_ids
    assert "pending-shared" not in bob_ids
    assert all(
        "publication_status.eq.published" in expression
        for expression in db.visibility_expressions
    )


def test_shared_author_cannot_patch_or_delete_published_memory(monkeypatch):
    db = _MemoryClient([
        _memory(
            "published-shared",
            scope="shared",
            creator=ALICE_ID,
            content="공개 후에는 관리자만 변경",
        ),
    ])
    monkeypatch.setattr(
        main,
        "consume_ai_use",
        lambda *_args: pytest.fail("forbidden updates must not consume a use"),
    )
    monkeypatch.setattr(
        main,
        "embed",
        lambda *_args: pytest.fail("forbidden updates must not create embeddings"),
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    with pytest.raises(HTTPException) as patch_error:
        main.update_memory(
            "published-shared",
            main.UpdateMemoryRequest(content="작성자가 바꾸려는 내용"),
            _request(
                "PATCH",
                "/api/memories/published-shared",
                user=_user(),
                db=db,
            ),
        )

    with pytest.raises(HTTPException) as delete_error:
        main.delete_memory(
            "published-shared",
            _request(
                "DELETE",
                "/api/memories/published-shared",
                user=_user(),
                db=db,
            ),
        )

    assert patch_error.value.status_code == 404
    assert delete_error.value.status_code == 404
    assert db.rows[0]["content"] == "공개 후에는 관리자만 변경"


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
    monkeypatch.setattr(main, "consume_ai_use", lambda user_id: 9)

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
    assert events[0]["remaining_uses"] == 9
    assert "".join(event.get("content", "") for event in events) == "첫 번째 답변"


class _RpcResult:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return _result(self.value)


class _RpcClient:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _RpcResult(self.responses[name])


def test_consume_ai_use_returns_atomic_balance_and_maps_exhaustion(monkeypatch):
    client = _RpcClient({"consume_ai_use": [{"remaining_uses": 9}]})
    monkeypatch.setattr(main, "admin_sb", client)

    assert main.consume_ai_use(ALICE_ID) == 9
    assert client.calls == [(
        "consume_ai_use",
        {"target_user_id": ALICE_ID},
    )]

    client.responses["consume_ai_use"] = []
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda _user_id: {"id": ALICE_ID, "remaining_uses": 0},
    )
    with pytest.raises(HTTPException) as raised:
        main.consume_ai_use(ALICE_ID)

    assert raised.value.status_code == 402
    assert raised.value.detail == main.AI_USES_EXHAUSTED_MESSAGE
    assert raised.value.headers == {"X-Remaining-Uses": "0"}


def test_consume_ai_use_distinguishes_missing_migration(monkeypatch):
    client = _RpcClient({
        "consume_ai_use": RuntimeError(
            "Could not find the function public.consume_ai_use"
        )
    })
    monkeypatch.setattr(main, "admin_sb", client)

    with pytest.raises(HTTPException) as raised:
        main.consume_ai_use(ALICE_ID)

    assert raised.value.status_code == 503
    assert "migration_ai_usage_credits.sql" in raised.value.detail


def test_consume_ai_use_empty_result_with_missing_profile_is_503(monkeypatch):
    client = _RpcClient({"consume_ai_use": []})
    monkeypatch.setattr(main, "admin_sb", client)
    monkeypatch.setattr(main, "account_profile_by_user_id", lambda _user_id: None)

    with pytest.raises(HTTPException) as raised:
        main.consume_ai_use(ALICE_ID)

    assert raised.value.status_code == 503
    assert "migration_ai_usage_credits.sql" in raised.value.detail


def test_usage_balance_reads_current_users_database_balance(monkeypatch):
    lookups = []
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda user_id: (
            lookups.append(user_id)
            or {"id": user_id, "remaining_uses": 6}
        ),
    )

    result = main.usage_balance(
        _request("GET", "/api/usage-balance", user=_user())
    )

    assert result == {"remaining_uses": 6}
    assert lookups == [ALICE_ID]


def test_ingest_refunds_use_when_processing_fails(monkeypatch):
    refunds = []
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(
        main,
        "refund_ai_use_after_failure",
        lambda user_id, remaining: refunds.append((user_id, remaining)) or 10,
    )
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: (_ for _ in ()).throw(RuntimeError("parser unavailable")),
    )

    with pytest.raises(HTTPException) as raised:
        main.ingest(
            main.IngestRequest(text="valid memory"),
            _request("POST", "/api/ingest", user=_user(), db=object()),
        )

    assert raised.value.status_code == 502
    assert refunds == [(ALICE_ID, 9)]


def test_ask_charges_once_and_refunds_processing_failure(monkeypatch):
    prepared = {
        "fallback": "stored answer",
        "messages": None,
        "resolved_question": None,
        "sources": [],
    }
    consumed = []
    monkeypatch.setattr(
        main,
        "consume_ai_use",
        lambda user_id: consumed.append(user_id) or 9,
    )
    monkeypatch.setattr(main, "prepare_answer", lambda *_args: prepared)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    result = main.ask(
        main.AskRequest(question="question"),
        _request("POST", "/api/ask", user=_user(), db=object()),
    )

    assert consumed == [ALICE_ID]
    assert result["answer"] == "stored answer"
    assert result["remaining_uses"] == 9

    refunds = []
    monkeypatch.setattr(
        main,
        "prepare_answer",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("search failed")),
    )
    monkeypatch.setattr(
        main,
        "refund_ai_use_after_failure",
        lambda user_id, remaining: refunds.append((user_id, remaining)) or 10,
    )

    with pytest.raises(RuntimeError, match="search failed"):
        main.ask(
            main.AskRequest(question="question"),
            _request("POST", "/api/ask", user=_user(), db=object()),
        )

    assert refunds == [(ALICE_ID, 9)]


def test_update_memory_charges_once_and_returns_balance(monkeypatch):
    db = _MemoryClient([
        _memory(
            "personal-memory",
            scope="personal",
            owner=ALICE_ID,
            creator=ALICE_ID,
            content="old content",
        ),
    ])
    consumed = []
    monkeypatch.setattr(
        main,
        "consume_ai_use",
        lambda user_id: consumed.append(user_id) or 9,
    )
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.1]])
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    result = main.update_memory(
        "personal-memory",
        main.UpdateMemoryRequest(content="new content"),
        _request("PATCH", "/api/memories/personal-memory", user=_user(), db=db),
    )

    assert consumed == [ALICE_ID]
    assert result["content"] == "new content"
    assert result["remaining_uses"] == 9


def test_update_memory_refunds_when_embedding_fails(monkeypatch):
    db = _MemoryClient([
        _memory(
            "personal-memory",
            scope="personal",
            owner=ALICE_ID,
            creator=ALICE_ID,
            content="old content",
        ),
    ])
    refunds = []
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(
        main,
        "refund_ai_use_after_failure",
        lambda user_id, remaining: refunds.append((user_id, remaining)) or 10,
    )
    monkeypatch.setattr(
        main,
        "embed",
        lambda _texts: (_ for _ in ()).throw(RuntimeError("embedding unavailable")),
    )

    with pytest.raises(HTTPException) as raised:
        main.update_memory(
            "personal-memory",
            main.UpdateMemoryRequest(content="new content"),
            _request("PATCH", "/api/memories/personal-memory", user=_user(), db=db),
        )

    assert raised.value.status_code == 502
    assert refunds == [(ALICE_ID, 9)]


def test_stream_error_refunds_and_emits_restored_balance(monkeypatch):
    prepared = {
        "fallback": None,
        "messages": [{"role": "user", "content": "question"}],
        "resolved_question": None,
        "sources": [],
    }
    refunds = []
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "prepare_answer", lambda *_args: prepared)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "refund_ai_use_after_failure",
        lambda user_id, remaining: refunds.append((user_id, remaining)) or 10,
    )

    class FailedCompletions:
        def create(self, **_kwargs):
            raise RuntimeError("stream failed")

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=FailedCompletions())),
    )
    response = main.ask_stream(
        main.AskRequest(question="question"),
        _request("POST", "/api/ask/stream", user=_user(), db=object()),
    )

    async def collect():
        return [part async for part in response.body_iterator]

    events = [
        json.loads(line)
        for part in asyncio.run(collect())
        for line in (part.decode() if isinstance(part, bytes) else part).splitlines()
        if line
    ]

    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[0]["remaining_uses"] == 9
    assert events[1]["remaining_uses"] == 10
    assert refunds == [(ALICE_ID, 9)]


def test_stream_cancellation_closes_generator_and_refunds_once(monkeypatch):
    prepared = {
        "fallback": None,
        "messages": [{"role": "user", "content": "question"}],
        "resolved_question": None,
        "sources": [],
    }
    refunds = []
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "prepare_answer", lambda *_args: prepared)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "refund_ai_use_after_failure",
        lambda user_id, remaining: refunds.append((user_id, remaining)) or 10,
    )

    response = main.ask_stream(
        main.AskRequest(question="question"),
        _request("POST", "/api/ask/stream", user=_user(), db=object()),
    )

    async def read_meta_then_disconnect():
        iterator = response.body_iterator
        first = await anext(iterator)
        await iterator.aclose()
        return first

    raw_meta = asyncio.run(read_meta_then_disconnect())
    meta = json.loads(raw_meta.decode() if isinstance(raw_meta, bytes) else raw_meta)

    assert meta["type"] == "meta"
    assert meta["remaining_uses"] == 9
    assert refunds == [(ALICE_ID, 9)]


def test_admin_api_paths_require_admin_for_every_method():
    for method in ("GET", "POST", "PATCH", "DELETE"):
        assert main.required_action(method, "/api/admin/accounts") == "admin"
        assert main.required_action(
            method,
            f"/api/admin/accounts/{ALICE_ID}/recharge",
        ) == "admin"


def test_admin_recharge_revalidates_role_and_uses_fixed_ten(monkeypatch):
    with pytest.raises(HTTPException) as raised:
        main.recharge_account_ai_uses(
            uuid.UUID(ALICE_ID),
            _request("POST", "/api/admin/accounts/x/recharge", user=_user()),
        )
    assert raised.value.status_code == 403

    client = _RpcClient({"recharge_ai_uses": [{"remaining_uses": 17}]})
    monkeypatch.setattr(main, "admin_sb", client)
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *_args, **_kwargs: pytest.fail(
            "recharge audit must be written atomically by the SQL RPC"
        ),
    )
    admin = _user(BOB_ID, role="admin")

    result = main.recharge_account_ai_uses(
        uuid.UUID(ALICE_ID),
        _request("POST", "/api/admin/accounts/x/recharge", user=admin),
    )

    assert client.calls == [(
        "recharge_ai_uses",
        {
            "target_user_id": ALICE_ID,
            "actor_user_id": BOB_ID,
            "refill_count": 10,
        },
    )]
    assert result["remaining_uses"] == 17
    assert result["recharged"] == 10


def test_admin_accounts_returns_only_expected_account_fields(monkeypatch):
    rows = [{
        "id": ALICE_ID,
        "username": "alice",
        "email": "alice@example.com",
        "remaining_uses": 9,
    }]

    class AccountsQuery:
        def __init__(self):
            self.columns = None

        def select(self, columns):
            self.columns = columns
            return self

        def order(self, column):
            assert column == "username"
            return self

        def execute(self):
            return _result(rows)

    class AccountsClient:
        def __init__(self):
            self.query = AccountsQuery()

        def table(self, name):
            assert name == "account_profiles"
            return self.query

    client = AccountsClient()
    monkeypatch.setattr(main, "admin_sb", client)

    result = main.admin_accounts(
        _request("GET", "/api/admin/accounts", user=_user(role="admin"))
    )

    assert client.query.columns == "id,username,email,remaining_uses"
    assert result == {"accounts": rows}


def test_canonical_session_identity_includes_remaining_uses(monkeypatch):
    auth_user = SimpleNamespace(
        id=ALICE_ID,
        email="alice@example.com",
        app_metadata={},
    )
    monkeypatch.setattr(
        main,
        "account_profile_by_user_id",
        lambda _user_id: {
            "id": ALICE_ID,
            "username": "alice",
            "email": "alice@example.com",
            "remaining_uses": 7,
        },
    )

    identity = main.canonical_auth_user_identity(auth_user)

    assert identity["remaining_uses"] == 7


def test_ai_usage_migration_is_rerunnable_and_service_role_only():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migration_ai_usage_credits.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "where remaining_uses is null" in migration
    assert "profile.remaining_uses > 0" in migration
    assert "raw_app_meta_data ->> 'app_role'" in migration
    assert "refill_count < 1 or refill_count > 1000" in migration
    assert "from public, anon, authenticated" in migration
    assert "to service_role" in migration

    for sql in (migration, (root / "schema.sql").read_text(encoding="utf-8").lower()):
        recharge = sql.split(
            "create or replace function public.recharge_ai_uses", 1
        )[1].split("$$;", 1)[0]
        assert "actor_username text" in recharge
        assert "select profile.username" in recharge
        assert "insert into public.audit_logs" in recharge
        assert "'ai_uses_recharge'" in recharge
        assert (
            recharge.index("update public.account_profiles")
            < recharge.index("insert into public.audit_logs")
            < recharge.index("return query select updated_remaining")
        )


def test_shared_memory_approval_migration_enforces_distinct_two_user_consent():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migration_shared_memory_approvals.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "create table if not exists public.shared_memory_proposals" in migration
    assert (
        "create table if not exists public.shared_memory_proposal_approvals"
        in migration
    )
    assert "required_approvals smallint not null default 2" in migration
    assert "check (required_approvals = 2)" in migration
    assert "primary key (proposal_id, approver_user_id)" in migration

    create_rpc = migration.split(
        "create or replace function public.create_shared_memory_proposal", 1
    )[1].split(
        "create or replace function public.approve_shared_memory_proposal", 1
    )[0]
    assert "creator_user_id" in create_rpc
    assert "'pending'" in create_rpc
    assert "proposal_id, approver_user_id" in create_rpc
    assert "values (requested_proposal_id, creator_user_id)" in create_rpc
    create_grants = migration.split(
        "revoke all on function public.create_shared_memory_proposal", 1
    )[1].split(
        "revoke all on function public.approve_shared_memory_proposal", 1
    )[0]
    assert "from public, anon, authenticated" in create_grants
    assert "to service_role" in create_grants

    approve_rpc = migration.split(
        "create or replace function public.approve_shared_memory_proposal", 1
    )[1].split("-- replace all memory policies", 1)[0]
    assert "approver_id uuid := auth.uid()" in approve_rpc
    assert "for update" in approve_rpc
    assert "on conflict (proposal_id, approver_user_id) do nothing" in approve_rpc
    assert "counted_approvals >= proposal_record.required_approvals" in approve_rpc
    assert "or approver_role = 'admin'" in approve_rpc
    assert "set publication_status = 'published'" in approve_rpc
    assert re.search(
        r"set\s+publication_status\s*=\s*'published'.*proposal_id\s*=\s*null",
        approve_rpc,
        re.DOTALL,
    )
    assert "'shared_memory_proposal_approve'" in approve_rpc
    assert "to authenticated, service_role" in approve_rpc

    # Database policies and vector search independently exclude pending rows,
    # even if an application query accidentally omits its publication filter.
    select_policy = migration.split(
        "create policy memories_authenticated_select", 1
    )[1].split("create policy memories_authenticated_insert", 1)[0]
    assert "scope = 'shared' and publication_status = 'published'" in select_policy
    match_function = migration.split(
        "create function public.match_memories", 1
    )[1].split("revoke all on function public.match_memories", 1)[0]
    assert "where memory.publication_status = 'published'" in match_function


def test_shared_approval_schema_is_included_for_fresh_installations():
    root = Path(__file__).resolve().parents[1]
    schema = (root / "schema.sql").read_text(encoding="utf-8").lower()
    migration = (root / "migration_shared_memory_approvals.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "public.shared_memory_proposals" in schema
    assert "public.shared_memory_proposal_approvals" in schema
    assert "public.create_shared_memory_proposal" in schema
    assert "public.approve_shared_memory_proposal" in schema
    assert "publication_status = 'published'" in schema
    for sql in (migration, schema):
        assert not re.search(r"(?m)^\s*(?:\+--|\*\*\*)", sql)


def test_admin_duplicate_approval_still_audits_actual_publish_transition():
    root = Path(__file__).resolve().parents[1]
    for filename in ("migration_shared_memory_approvals.sql", "schema.sql"):
        sql = (root / filename).read_text(encoding="utf-8").lower()
        approve_rpc = sql.split(
            "create or replace function public.approve_shared_memory_proposal", 1
        )[1].split(
            "alter function public.create_shared_memory_proposal", 1
        )[0]

        # An admin may already own the author's vote, so ON CONFLICT inserts no
        # new approval. Publishing is still a state transition that must be
        # audited independently of approval_inserted.
        assert "on conflict (proposal_id, approver_user_id) do nothing" in approve_rpc
        assert "or approver_role = 'admin'" in approve_rpc
        assert "published_now boolean := false" in approve_rpc
        assert "published_now := true" in approve_rpc
        assert "if approval_inserted > 0 or published_now then" in approve_rpc
        assert "'shared_memory_proposal_approve'" in approve_rpc
        assert "'approval_added', approval_inserted > 0" in approve_rpc
        assert (
            approve_rpc.index("published_now := true")
            < approve_rpc.index("if approval_inserted > 0 or published_now then")
            < approve_rpc.index("'shared_memory_proposal_approve'")
        )


def test_memory_ui_labels_personal_and_shared_content_explicitly():
    html = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'name="memoryScope"[^>]+value="personal"[^>]*checked>개인기억',
        html,
    )
    assert re.search(
        r'name="memoryScope"[^>]+value="shared"[^>]*>모두의 기억',
        html,
    )
    assert 'label: "개인기억"' in html
    assert 'label: "모두의 기억"' in html
    assert "작성자를 포함한 2명이 동의하면 모두에게 공개됩니다." in html
