import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
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
os.environ["AI_TALENT_API_KEY"] = ""
os.environ["AI_TALENT_ENDPOINT"] = ""
os.environ["AI_TALENT_API_VERSION"] = "2024-12-01-preview"
os.environ["CHAT_MODEL"] = "gpt-4o-mini"
os.environ["EMBED_MODEL"] = "text-embedding-3-small"
os.environ["APP_ENV"] = "development"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


ALICE_ID = "11111111-1111-4111-8111-111111111111"
BOB_ID = "22222222-2222-4222-8222-222222222222"


def test_chat_model_uses_luna_when_gateway_is_configured():
    assert main.resolve_chat_model(
        "https://skax.ai-talentlab.com",
        "gpt-4o-mini",
    ) == "gpt-5.6-luna"


def test_chat_model_preserves_direct_openai_fallback_without_gateway():
    assert main.resolve_chat_model("", "gpt-4o-mini") == "gpt-4o-mini"
    assert main.CHAT_MODEL == "gpt-4o-mini"


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\ufeff2024-12-01-preview", "2024-12-01-preview"),
        ("  \ufeffgateway-test-key\r\n", "gateway-test-key"),
        (" https://skax.ai-talentlab.com/ ", "https://skax.ai-talentlab.com/"),
    ],
)
def test_normalize_ai_talent_env_value_removes_bom_and_whitespace(raw, expected):
    assert main.normalize_ai_talent_env_value(raw) == expected


def test_create_ai_client_prefers_ai_talent_gateway(monkeypatch):
    captured = {}
    expected_client = object()

    def fake_azure_openai(**kwargs):
        captured.update(kwargs)
        return expected_client

    monkeypatch.setattr(main, "AzureOpenAI", fake_azure_openai)

    client = main.create_ai_client(
        openai_api_key="direct-test-key",
        ai_talent_api_key="gateway-test-key",
        ai_talent_endpoint="https://skax.ai-talentlab.com",
        ai_talent_api_version="2024-12-01-preview",
    )

    assert client is expected_client
    assert captured == {
        "api_key": "gateway-test-key",
        "azure_endpoint": "https://skax.ai-talentlab.com",
        "api_version": "2024-12-01-preview",
        "timeout": 30.0,
        "max_retries": 2,
    }


def test_create_ai_client_keeps_direct_openai_fallback(monkeypatch):
    captured = {}
    expected_client = object()

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return expected_client

    monkeypatch.setattr(main, "OpenAI", fake_openai)

    client = main.create_ai_client(
        openai_api_key="direct-test-key",
        ai_talent_api_key="",
        ai_talent_endpoint="",
        ai_talent_api_version="2024-12-01-preview",
    )

    assert client is expected_client
    assert captured == {
        "api_key": "direct-test-key",
        "timeout": 30.0,
        "max_retries": 2,
    }


@pytest.mark.parametrize(
    ("gateway_key", "gateway_endpoint"),
    [
        ("gateway-test-key", ""),
        ("", "https://skax.ai-talentlab.com"),
    ],
)
def test_create_ai_client_rejects_partial_gateway_config(
    gateway_key,
    gateway_endpoint,
):
    with pytest.raises(RuntimeError, match="함께 설정"):
        main.create_ai_client(
            openai_api_key="direct-test-key",
            ai_talent_api_key=gateway_key,
            ai_talent_endpoint=gateway_endpoint,
            ai_talent_api_version="2024-12-01-preview",
        )


def test_create_ai_client_rejects_missing_gateway_version():
    with pytest.raises(RuntimeError, match="AI_TALENT_API_VERSION"):
        main.create_ai_client(
            openai_api_key="",
            ai_talent_api_key="gateway-test-key",
            ai_talent_endpoint="https://skax.ai-talentlab.com",
            ai_talent_api_version="",
        )


def test_create_ai_client_rejects_missing_all_credentials():
    with pytest.raises(RuntimeError, match="API_KEY"):
        main.create_ai_client(
            openai_api_key="",
            ai_talent_api_key="",
            ai_talent_endpoint="",
            ai_talent_api_version="2024-12-01-preview",
        )


def test_azure_sdk_serializes_deployment_path_and_gpt5_limit():
    requests = []

    def handle_request(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5.6-luna",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle_request)) as http_client:
        with main.AzureOpenAI(
            api_key="gateway-test-key",
            azure_endpoint="https://skax.ai-talentlab.com",
            api_version="2024-12-01-preview",
            http_client=http_client,
        ) as client:
            response = client.chat.completions.create(
                model="gpt-5.6-luna",
                messages=[{"role": "user", "content": "안녕"}],
                max_completion_tokens=32,
            )

    assert response.choices[0].message.content == "ok"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == (
        "/openai/deployments/gpt-5.6-luna/chat/completions"
    )
    assert request.url.params["api-version"] == "2024-12-01-preview"
    assert request.headers["api-key"] == "gateway-test-key"
    assert json.loads(request.content)["max_completion_tokens"] == 32


def test_azure_sdk_serializes_embedding_deployment_and_dimensions():
    requests = []

    def handle_request(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "object": "list",
                "data": [{
                    "object": "embedding",
                    "index": 0,
                    "embedding": [0.1, 0.2],
                }],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle_request)) as http_client:
        with main.AzureOpenAI(
            api_key="gateway-test-key",
            azure_endpoint="https://skax.ai-talentlab.com",
            api_version="2024-12-01-preview",
            http_client=http_client,
        ) as client:
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=["기억"],
                dimensions=1536,
            )

    assert response.data[0].embedding == [0.1, 0.2]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == (
        "/openai/deployments/text-embedding-3-small/embeddings"
    )
    assert request.url.params["api-version"] == "2024-12-01-preview"
    assert request.headers["api-key"] == "gateway-test-key"
    assert json.loads(request.content)["dimensions"] == 1536


def test_azure_sdk_parses_streaming_chat_response():
    requests = []

    def handle_request(request):
        requests.append(request)
        body = (
            'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
            '"created":0,"model":"gpt-5.6-luna","choices":[{"index":0,'
            '"delta":{"content":"안녕"},"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
            '"created":0,"model":"gpt-5.6-luna","choices":[{"index":0,'
            '"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    with httpx.Client(transport=httpx.MockTransport(handle_request)) as http_client:
        with main.AzureOpenAI(
            api_key="gateway-test-key",
            azure_endpoint="https://skax.ai-talentlab.com",
            api_version="2024-12-01-preview",
            http_client=http_client,
        ) as client:
            chunks = list(client.chat.completions.create(
                model="gpt-5.6-luna",
                messages=[{"role": "user", "content": "안녕"}],
                max_completion_tokens=32,
                stream=True,
            ))

    content = "".join(
        choice.delta.content or ""
        for chunk in chunks
        for choice in chunk.choices
    )
    assert content == "안녕"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == (
        "/openai/deployments/gpt-5.6-luna/chat/completions"
    )
    assert json.loads(request.content)["stream"] is True


def test_embed_requests_schema_compatible_dimensions(monkeypatch):
    captured = {}

    class FakeEmbeddings:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(embeddings=FakeEmbeddings()),
    )
    monkeypatch.setattr(main, "EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(main, "EMBED_DIMENSIONS", 2)

    assert main.embed(["기억"]) == [[0.1, 0.2]]
    assert captured == {
        "model": "text-embedding-3-small",
        "input": ["기억"],
        "dimensions": 2,
    }


def test_embed_rejects_wrong_provider_dimension(monkeypatch):
    class WrongSizedEmbeddings:
        def create(self, **_kwargs):
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1])])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(embeddings=WrongSizedEmbeddings()),
    )
    monkeypatch.setattr(main, "EMBED_DIMENSIONS", 2)

    with pytest.raises(RuntimeError, match="임베딩 차원"):
        main.embed(["기억"])


def test_embed_rejects_missing_provider_result(monkeypatch):
    class MissingEmbeddings:
        def create(self, **_kwargs):
            return SimpleNamespace(data=[])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(embeddings=MissingEmbeddings()),
    )
    monkeypatch.setattr(main, "EMBED_DIMENSIONS", 1536)

    with pytest.raises(RuntimeError, match="응답 수"):
        main.embed(["기억"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("key: sk-" + "x" * 32, True),
        ("key: atl-" + "x" * 32, True),
        ("프로젝트 ATL-123", False),
    ],
)
def test_contains_openai_api_key_supports_gateway_prefix(value, expected):
    assert main.contains_openai_api_key(value) is expected


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


def test_public_config_is_open_and_reports_signup_availability(monkeypatch):
    assert "/api/config" in main.OPEN_PATHS
    monkeypatch.setattr(main, "SIGNUP_ENABLED", False)

    assert main.public_config() == {"signup_enabled": False}


def test_vercel_environment_enables_production_cookie_policy(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")

    assert main.is_production_environment("development") is True

    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    response = Response()
    main.set_auth_cookies(response, "access-token", "refresh-token")
    assert all("Secure" in value for value in response.headers.getlist("set-cookie"))


def test_index_path_is_independent_of_current_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    response = main.index()

    assert Path(response.path) == main.BASE_DIR / "static" / "index.html"


def test_signup_ui_stays_hidden_until_public_config_enables_it():
    html = (main.BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")

    assert re.search(r'id="signupTab"[^>]*\shidden(?:\s|>)', html)
    assert '$("signupTab").hidden = !signupEnabled;' in html
    assert '$("signupClosedNote").hidden = signupEnabled;' in html
    assert "await loadPublicConfig();" in html


def test_signup_is_rejected_before_database_access_when_disabled(monkeypatch):
    monkeypatch.setattr(main, "SIGNUP_ENABLED", False)
    monkeypatch.setattr(
        main,
        "account_profile_by_username",
        lambda *_args: pytest.fail("disabled signup must not access the database"),
    )

    with pytest.raises(HTTPException) as exc_info:
        main.signup(
            main.SignupRequest(
                username="new-user",
                email="new-user@example.com",
                password="password123",
            ),
            _request("POST", "/api/signup"),
            Response(),
        )

    assert exc_info.value.status_code == 403
    assert "관리자에게 발급받은 계정" in exc_info.value.detail


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


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "[주소] http://10.250.182.156:8848/",
            "http://10.250.182.156:8848/",
        ),
        (
            "[URL]\nhttps://hub.noah-ai.dev/vibe-studio/",
            "https://hub.noah-ai.dev/vibe-studio/",
        ),
        (
            "주소: https://example.com/path?tab=course",
            "https://example.com/path?tab=course",
        ),
        (
            "참고 링크: https://example.com/docs).",
            "https://example.com/docs",
        ),
    ],
)
def test_normalize_metadata_extracts_http_url(content, expected):
    metadata = main.normalize_metadata({"content": content, "metadata": {}})

    assert metadata["url"] == expected


def test_effective_metadata_backfills_url_for_existing_memory():
    metadata = main.effective_metadata({
        "content": "[주소] https://hub.noah-ai.dev/vibe-studio/",
        "metadata": {"record_type": "system"},
        "created_at": "2026-08-14T00:00:00+00:00",
    })

    assert metadata["url"] == "https://hub.noah-ai.dev/vibe-studio/"


def test_infer_record_type_recognizes_labeled_business_system():
    assert main.infer_record_type(
        "[유형] 업무 시스템\n[제목] ATL\n[주소] http://10.0.0.1:8848/"
    ) == "system"


def test_find_similar_memories_returns_allowlisted_preview(monkeypatch):
    class SimilarityClient:
        def __init__(self):
            self.calls = []

        def rpc(self, name, params):
            self.calls.append((name, params))
            return SimpleNamespace(execute=lambda: _result([
                {
                    "id": "similar-1",
                    "source": "note",
                    "content": "<script>alert('x')</script> ATL AI 도구 비용 처리",
                    "metadata": {
                        "project": "ATL",
                        "person": "권민정",
                        "work_date": "2026-08-25",
                        "tags": ["ATL", "비용", 123],
                        "credentials": "must-not-leak",
                    },
                    "created_at": "2026-08-25T01:00:00+00:00",
                    "similarity": 0.91,
                    "scope": "personal",
                    "owner_user_id": ALICE_ID,
                },
                {
                    "id": "below-threshold",
                    "source": "note",
                    "content": "weak match",
                    "metadata": {},
                    "created_at": "2026-08-25T01:00:00+00:00",
                    "similarity": main.SIMILAR_MEMORY_THRESHOLD - 0.01,
                    "scope": "shared",
                    "owner_user_id": None,
                },
            ]))

    db = SimilarityClient()
    monkeypatch.setattr(main, "embed", lambda texts: [[0.1, 0.2]])

    matches = main.find_similar_memories(
        "ATL AI 도구 비용 처리",
        "personal",
        db,
        _user(),
    )

    assert db.calls == [("match_memories", {
        "query_embedding": [0.1, 0.2],
        "match_count": 10,
        "filter_source": None,
        "query_scope": "personal",
        "requesting_user_id": ALICE_ID,
    })]
    assert matches == [{
        "id": "similar-1",
        "scope": "personal",
        "source": "note",
        "snippet": "<script>alert('x')</script> ATL AI 도구 비용 처리",
        "similarity": 0.91,
        "created_at": "2026-08-25T01:00:00+00:00",
        "metadata": {
            "person": "권민정",
            "project": "ATL",
            "work_date": "2026-08-25",
            "tags": ["ATL", "비용"],
        },
    }]


def test_ingest_warns_before_quota_or_database_write(monkeypatch):
    candidate = {
        "id": "similar-1",
        "scope": "shared",
        "source": "note",
        "snippet": "기존 ATL 비용 처리 안내",
        "similarity": 0.9,
        "created_at": "2026-08-01T00:00:00+00:00",
        "metadata": {},
    }
    monkeypatch.setattr(
        main,
        "find_similar_memories",
        lambda text, scope, db, user: [candidate],
    )
    monkeypatch.setattr(
        main,
        "consume_ai_use",
        lambda _user_id: pytest.fail("similarity warning must not consume quota"),
    )
    monkeypatch.setattr(
        main,
        "ingest_with_consumed_use",
        lambda *_args: pytest.fail("similarity warning must not write"),
    )

    with pytest.raises(HTTPException) as raised:
        main.ingest(
            main.IngestRequest(
                text="이번 달 ATL AI 도구 사용료",
                scope="personal",
                allow_similar=False,
            ),
            _request("POST", "/api/ingest", user=_user(), db=object()),
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "similar_memories_found",
        "message": "유사한 기억이 있습니다. 그래도 저장하시겠습니까?",
        "similar_memories": [candidate],
    }


def test_confirmed_similar_ingest_skips_preflight(monkeypatch):
    monkeypatch.setattr(
        main,
        "find_similar_memories",
        lambda *_args: pytest.fail("confirmed save must skip similarity preflight"),
    )
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(
        main,
        "ingest_with_consumed_use",
        lambda req, user, db: {"saved": 1, "scope": req.scope},
    )

    result = main.ingest(
        main.IngestRequest(
            text="이번 달 ATL AI 도구 사용료",
            scope="personal",
            allow_similar=True,
        ),
        _request("POST", "/api/ingest", user=_user(), db=object()),
    )

    assert result == {"saved": 1, "scope": "personal", "remaining_uses": 9}


@pytest.mark.parametrize("question", ["ATL 주소 알려줘", "ATL URL 알려줘", "ATL 링크 알려줘"])
def test_lexical_link_question_synonyms_find_same_memory(question):
    rows = [{
        "id": "atl-link",
        "content": "ATL 지식 문의 http://10.250.182.156:8848/",
        "metadata": {"project": "ATL", "tags": ["지식 문의"]},
    }]

    assert [row["id"] for row in main.lexical_memory_hits(question, rows)] == [
        "atl-link"
    ]


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


def test_parse_pasted_text_preserves_structured_fields_and_explicit_tags(
    monkeypatch,
):
    monkeypatch.setattr(main, "CHAT_MODEL", "gpt-5.6-luna")
    text = """[기록 유형] 업무
[카테고리] 비용 처리 가이드
[제목] 2026년 8월 ATL AI 도구 사용료 처리

[적용월] 2026년 8월
[확인자] 권민정 매니저
[프로젝트 코드] 41000069-001
[프로젝트명] 26년 AI Talent Lab 운영
[비용 계정] CL/AI

[내용]
이번 달 ATL 관련 AI 도구 사용료는 위 프로젝트와 비용 계정으로 처리합니다.

[키워드] ATL, AI 도구 사용료, 비용 처리, 41000069-001, CL/AI"""
    model_payload = {
        "source": "note",
        "records": [{
            # Reproduce the lossy model response that originally discarded the
            # structured project code and cost account fields.
            "content": (
                "이번 달 ATL 관련 AI 도구 사용료는 위 프로젝트와 "
                "비용 계정으로 처리합니다."
            ),
            "metadata": {
                "record_type": "work",
                "category": "비용 처리 가이드",
                "project": "26년 AI Talent Lab 운영",
            },
            "expires_at": None,
            "tags": ["ATL"],
        }],
    }

    model_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            model_calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(model_payload))
            )])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    parsed = main.parse_pasted_text(text)

    assert parsed["source"] == "note"
    assert len(parsed["records"]) == 1
    record = parsed["records"][0]
    assert record["content"] == text
    assert "[프로젝트 코드] 41000069-001" in record["content"]
    assert "[비용 계정] CL/AI" in record["content"]
    assert record["metadata"]["project_code"] == "41000069-001"
    assert record["metadata"]["expense_account"] == "CL/AI"
    assert record["metadata"]["applicable_month"] == "2026년 8월"
    assert record["tags"] == [
        "ATL",
        "AI 도구 사용료",
        "비용 처리",
        "41000069-001",
        "CL/AI",
    ]
    assert model_calls[0]["model"] == "gpt-5.6-luna"
    assert model_calls[0]["max_completion_tokens"] == 4000
    assert "max_tokens" not in model_calls[0]


def test_structured_field_value_changes_ingest_content_hash(monkeypatch):
    base_text = """[기록 유형] 업무
[제목] 2026년 8월 ATL AI 도구 사용료 처리
[프로젝트 코드] 41000069-001
[비용 계정] {cost_account}
[내용]
이번 달 ATL 관련 AI 도구 사용료를 지정 계정으로 처리합니다.
[키워드] ATL, AI 도구 사용료, 비용 처리, 41000069-001, {cost_account}"""
    model_payload = {
        "source": "note",
        "records": [{
            "content": "이번 달 ATL 관련 AI 도구 사용료를 지정 계정으로 처리합니다.",
            "metadata": {"record_type": "work"},
            "expires_at": None,
            "tags": ["ATL"],
        }],
    }

    class FakeCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(model_payload))
            )])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )
    monkeypatch.setattr(main, "embed", lambda texts: [[0.1] for _ in texts])
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    rows = []
    for cost_account in ("CL/AI", "CL/OPEX"):
        db = _MemoryClient()
        main.ingest_with_consumed_use(
            main.IngestRequest(
                text=base_text.format(cost_account=cost_account),
                scope="personal",
                allow_similar=True,
            ),
            _user(),
            db,
        )
        rows.append(db.upserts[0]["rows"][0])

    assert rows[0]["content_hash"] != rows[1]["content_hash"]
    assert "[비용 계정] CL/AI" in rows[0]["content"]
    assert "[비용 계정] CL/OPEX" in rows[1]["content"]
    assert rows[0]["metadata"]["tags"] == [
        "ATL",
        "AI 도구 사용료",
        "비용 처리",
        "41000069-001",
        "CL/AI",
    ]


def test_ingest_keeps_at_most_eight_tags(monkeypatch):
    db = _MemoryClient()
    monkeypatch.setattr(
        main,
        "parse_pasted_text",
        lambda _text: {
            "source": "note",
            "records": [{
                "content": "tag limit memory",
                "metadata": {},
                "tags": [f"tag-{index}" for index in range(1, 10)],
                "expires_at": None,
            }],
        },
    )
    monkeypatch.setattr(main, "embed", lambda texts: [[0.1] for _ in texts])
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    main.ingest_with_consumed_use(
        main.IngestRequest(
            text="tag limit memory",
            scope="personal",
            allow_similar=True,
        ),
        _user(),
        db,
    )

    assert db.upserts[0]["rows"][0]["metadata"]["tags"] == [
        "tag-1", "tag-2", "tag-3", "tag-4",
        "tag-5", "tag-6", "tag-7", "tag-8",
    ]


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
@pytest.mark.parametrize(
    "api_key",
    [
        "sk-proj-" + "x" * 32,
        "atl-" + "x" * 40,
    ],
)
def test_ingest_rejects_openai_api_key_before_parser_or_storage(
    monkeypatch,
    scope,
    api_key,
):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("OpenAI key input must be rejected before external work")

    monkeypatch.setattr(main, "parse_pasted_text", unexpected)
    monkeypatch.setattr(main, "embed", unexpected)

    with pytest.raises(HTTPException) as exc_info:
        main.ingest(
            main.IngestRequest(
                text=f"API key: {api_key}",
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
    assert by_id["shared"]["can_delete"] is False
    assert by_id["shared"]["can_request_delete"] is True
    assert by_id["alice-private"]["scope"] == "personal"
    assert by_id["alice-private"]["can_edit"] is True
    assert by_id["alice-private"]["can_delete"] is True
    assert by_id["alice-private"]["can_request_delete"] is False
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


def test_shared_author_cannot_patch_published_memory(monkeypatch):
    memory_id = "77777777-7777-4777-8777-777777777777"
    db = _MemoryClient([
        _memory(
            memory_id,
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
            memory_id,
            main.UpdateMemoryRequest(content="작성자가 바꾸려는 내용"),
            _request(
                "PATCH",
                f"/api/memories/{memory_id}",
                user=_user(),
                db=db,
            ),
        )

    assert patch_error.value.status_code == 404
    assert db.rows[0]["content"] == "공개 후에는 관리자만 변경"


def test_admin_can_still_patch_published_shared_memory(monkeypatch):
    memory_id = "77777777-7777-4777-8777-777777777777"
    db = _MemoryClient([
        _memory(
            memory_id,
            scope="shared",
            creator=ALICE_ID,
            content="관리자 수정 전",
        ),
    ])
    monkeypatch.setattr(main, "admin_sb", db)
    monkeypatch.setattr(main, "consume_ai_use", lambda _user_id: 9)
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.4]])
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    result = main.update_memory(
        memory_id,
        main.UpdateMemoryRequest(content="관리자 수정 후"),
        _request(
            "PATCH",
            f"/api/memories/{memory_id}",
            user=_user(BOB_ID, role="admin"),
            db=db,
        ),
    )

    assert result["content"] == "관리자 수정 후"
    assert db.rows[0]["content"] == "관리자 수정 후"


def test_personal_memory_owner_deletes_immediately_and_other_users_stay_isolated(
    monkeypatch,
):
    memory_id = "88888888-8888-4888-8888-888888888888"
    db = _MemoryClient([
        _memory(
            memory_id,
            scope="personal",
            owner=ALICE_ID,
            creator=ALICE_ID,
            content="앨리스 개인 메모",
        ),
    ])
    audits = []
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *args, **kwargs: audits.append((args, kwargs)),
    )

    result = main.delete_memory(
        memory_id,
        _request(
            "DELETE",
            f"/api/memories/{memory_id}",
            user=_user(),
            db=db,
        ),
    )

    assert result == {
        "status": "deleted",
        "deleted": True,
        "pending_approval": False,
        "proposal_id": None,
        "approval_count": 0,
        "required_approvals": 0,
    }
    assert db.rows == []
    assert db.executed_operations == ["select", "delete"]
    assert audits[0][0][2] == "memory_delete"


def test_shared_editor_delete_is_pending_and_same_user_retry_does_not_add_vote(
    monkeypatch,
):
    memory_id = "77777777-7777-4777-8777-777777777777"
    proposal_id = "99999999-9999-4999-8999-999999999999"

    def handler(name, params, _client):
        assert name == "request_shared_memory_deletion"
        assert params == {"target_memory_id": memory_id}
        return [{
            "proposal_id": proposal_id,
            "proposal_status": "pending",
            "deleted": False,
            "approval_count": 1,
            "required_approvals": 2,
        }]

    db = _MemoryRpcClient([
        _memory(memory_id, scope="shared", creator=ALICE_ID),
    ], handler)
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *_args, **_kwargs: pytest.fail(
            "shared deletion audit must be atomic inside the SQL RPC"
        ),
    )

    first = main.delete_memory(
        memory_id,
        _request(
            "DELETE",
            f"/api/memories/{memory_id}",
            user=_user(),
            db=db,
        ),
    )
    repeated = main.delete_memory(
        memory_id,
        _request(
            "DELETE",
            f"/api/memories/{memory_id}",
            user=_user(),
            db=db,
        ),
    )

    expected = {
        "status": "pending",
        "deleted": False,
        "pending_approval": True,
        "proposal_id": proposal_id,
        "approval_count": 1,
        "required_approvals": 2,
    }
    assert first == expected
    assert repeated == expected
    assert len(db.rpc_calls) == 2
    assert len(db.rows) == 1


def test_different_editor_delete_removes_shared_memory_after_second_vote(monkeypatch):
    memory_id = "77777777-7777-4777-8777-777777777777"
    proposal_id = "99999999-9999-4999-8999-999999999999"
    rpc_count = 0

    def handler(_name, _params, client):
        nonlocal rpc_count
        rpc_count += 1
        if rpc_count == 1:
            return [{
                "proposal_id": proposal_id,
                "proposal_status": "pending",
                "deleted": False,
                "approval_count": 1,
                "required_approvals": 2,
            }]
        client.rows = []
        return [{
            "proposal_id": proposal_id,
            "proposal_status": "deleted",
            "deleted": True,
            "approval_count": 2,
            "required_approvals": 2,
        }]

    db = _MemoryRpcClient([
        _memory(
            memory_id,
            scope="shared",
            creator=ALICE_ID,
            content="삭제 승인 전에도 검색 가능한 공유 기억",
        ),
    ], handler)
    monkeypatch.setattr(main, "_catalog_cache", {})
    monkeypatch.setattr(main, "_management_catalog_cache", {})
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)

    first = main.delete_memory(
        memory_id,
        _request(
            "DELETE",
            f"/api/memories/{memory_id}",
            user=_user(),
            db=db,
        ),
    )
    visible_after_first_vote = main.memory_catalog(db, BOB_ID)
    listed_after_first_vote = main.list_memories(
        request=_request("GET", "/api/memories", user=_user(BOB_ID), db=db),
        response=Response(),
    )
    searchable_after_first_vote = main.lexical_memory_hits(
        "삭제 승인 전에도 검색 가능한 공유 기억",
        visible_after_first_vote,
    )

    second = main.delete_memory(
        memory_id,
        _request(
            "DELETE",
            f"/api/memories/{memory_id}",
            user=_user(BOB_ID),
            db=db,
        ),
    )
    visible_after_second_vote = main.memory_catalog(db, ALICE_ID)

    assert first["status"] == "pending"
    assert first["approval_count"] == 1
    assert {row["id"] for row in visible_after_first_vote} == {memory_id}
    assert {row["id"] for row in listed_after_first_vote} == {memory_id}
    assert {row["id"] for row in searchable_after_first_vote} == {memory_id}
    assert second == {
        "status": "deleted",
        "deleted": True,
        "pending_approval": False,
        "proposal_id": proposal_id,
        "approval_count": 2,
        "required_approvals": 2,
    }
    assert visible_after_second_vote == []
    assert db.rows == []


def test_viewer_cannot_request_shared_delete_but_can_cast_second_approval(monkeypatch):
    memory_id = "77777777-7777-4777-8777-777777777777"
    proposal_id = "99999999-9999-4999-8999-999999999999"

    def unexpected_request(*_args):
        raise AssertionError("viewer delete request must not reach the RPC")

    request_db = _MemoryRpcClient([
        _memory(memory_id, scope="shared", creator=ALICE_ID),
    ], unexpected_request)
    with pytest.raises(HTTPException) as denied:
        main.delete_memory(
            memory_id,
            _request(
                "DELETE",
                f"/api/memories/{memory_id}",
                user=_user(BOB_ID, role="viewer"),
                db=request_db,
            ),
        )

    approval_db = _MemoryRpcClient([
        _memory(memory_id, scope="shared", creator=ALICE_ID),
    ], lambda name, params, client: (
        client.rows.clear()
        or [{
            "proposal_id": proposal_id,
            "proposal_status": "deleted",
            "deleted": True,
            "approval_count": 2,
            "required_approvals": 2,
        }]
    ))
    approved = main.approve_shared_memory_deletion_proposal(
        proposal_id,
        _request(
            "POST",
            f"/api/shared-memory-deletion-proposals/{proposal_id}/approve",
            user=_user(BOB_ID, role="viewer"),
            db=approval_db,
        ),
    )

    assert denied.value.status_code == 403
    assert request_db.rpc_calls == []
    assert approval_db.rpc_calls == [(
        "approve_shared_memory_deletion_proposal",
        {"target_proposal_id": proposal_id},
    )]
    assert approved["status"] == "deleted"
    assert approved["approval_count"] == 2
    assert approval_db.rows == []


def test_admin_deletes_shared_memory_immediately_with_one_vote(monkeypatch):
    memory_id = "77777777-7777-4777-8777-777777777777"
    proposal_id = "99999999-9999-4999-8999-999999999999"

    def handler(_name, _params, client):
        client.rows = []
        return [{
            "proposal_id": proposal_id,
            "proposal_status": "deleted",
            "deleted": True,
            "approval_count": 1,
            "required_approvals": 2,
        }]

    db = _MemoryRpcClient([
        _memory(memory_id, scope="shared", creator=ALICE_ID),
    ], handler)

    result = main.delete_memory(
        memory_id,
        _request(
            "DELETE",
            f"/api/memories/{memory_id}",
            user=_user(BOB_ID, role="admin"),
            db=db,
        ),
    )

    assert result["status"] == "deleted"
    assert result["deleted"] is True
    assert result["approval_count"] == 1
    assert db.rows == []
    assert db.rpc_calls == [(
        "request_shared_memory_deletion",
        {"target_memory_id": memory_id},
    )]


def test_shared_deletion_rpc_missing_migration_returns_guidance(monkeypatch):
    db = _RpcClient({
        "request_shared_memory_deletion": RuntimeError(
            "Could not find the function public.request_shared_memory_deletion"
        ),
    })

    with pytest.raises(HTTPException) as raised:
        main.run_shared_memory_deletion_rpc(
            db,
            "request_shared_memory_deletion",
            "target_memory_id",
            "77777777-7777-4777-8777-777777777777",
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == main.SHARED_DELETION_APPROVAL_MIGRATION_MESSAGE


def test_shared_deletion_rpc_ambiguous_legacy_function_returns_guidance(monkeypatch):
    db = _RpcClient({
        "approve_shared_memory_deletion_proposal": RuntimeError(
            '42702 column reference "proposal_id" is ambiguous'
        ),
    })

    with pytest.raises(HTTPException) as raised:
        main.run_shared_memory_deletion_rpc(
            db,
            "approve_shared_memory_deletion_proposal",
            "target_proposal_id",
            "99999999-9999-4999-8999-999999999999",
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == main.SHARED_DELETION_APPROVAL_MIGRATION_MESSAGE


def test_shared_deletion_proposal_list_exposes_snapshot_and_vote_eligibility(
    monkeypatch,
):
    proposal_id = "99999999-9999-4999-8999-999999999999"
    memory_id = "77777777-7777-4777-8777-777777777777"
    tables = {
        "shared_memory_deletion_proposals": [{
            "id": proposal_id,
            "memory_id": memory_id,
            "source_snapshot": "note",
            "content_snapshot": "삭제 승인 대상 원문 snapshot",
            "requested_by_user_id": ALICE_ID,
            "status": "pending",
            "required_approvals": 2,
            "created_at": "2026-08-11T01:00:00+00:00",
            "deleted_at": None,
        }],
        "shared_memory_deletion_proposal_approvals": [{
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

        def limit(self, _count):
            return self

        def range(self, start, end):
            self.bounds = (start, end)
            return self

        def execute(self):
            rows = [
                row for row in self.rows
                if all(row.get(column) == value for column, value in self.equal_filters)
                and all(row.get(column) in values for column, values in self.in_filters)
            ]
            if self.bounds:
                start, end = self.bounds
                rows = rows[start:end + 1]
            return _result([dict(row) for row in rows])

    class ProposalClient:
        def table(self, name):
            return Query(tables[name])

    monkeypatch.setattr(main, "admin_sb", ProposalClient())

    requester = main.load_shared_memory_deletion_proposals(_user())[0]
    viewer = main.load_shared_memory_deletion_proposals(
        _user(BOB_ID, role="viewer")
    )[0]

    assert requester["memory_id"] == memory_id
    assert requester["source"] == "note"
    assert requester["content"] == "삭제 승인 대상 원문 snapshot"
    assert requester["requested_by_username"] == "alice"
    assert requester["approval_count"] == 1
    assert requester["required_approvals"] == 2
    assert requester["approved_by_me"] is True
    assert requester["can_approve"] is False
    assert viewer["approved_by_me"] is False
    assert viewer["can_approve"] is True


@pytest.mark.parametrize(
    ("loaded_count", "expected_count", "has_more", "next_offset"),
    [
        (3, 2, True, 6),
        (2, 2, False, None),
        (1, 1, False, None),
    ],
)
def test_shared_deletion_proposal_pagination_contract(
    monkeypatch,
    loaded_count,
    expected_count,
    has_more,
    next_offset,
):
    rows = [{"id": f"deletion-{index}"} for index in range(loaded_count)]
    calls = []

    def fake_load(user, proposal_id=None, *, page_size=100, offset=0):
        calls.append((user["id"], proposal_id, page_size, offset))
        return rows

    monkeypatch.setattr(main, "load_shared_memory_deletion_proposals", fake_load)

    result = main.list_shared_memory_deletion_proposals(
        _request(
            "GET",
            "/api/shared-memory-deletion-proposals",
            user=_user(),
        ),
        limit=2,
        offset=4,
    )

    assert calls == [(ALICE_ID, None, 3, 4)]
    assert result == {
        "proposals": rows[:expected_count],
        "has_more": has_more,
        "next_offset": next_offset,
    }


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (101, 0), (10, -1)])
def test_shared_deletion_proposal_rejects_invalid_pagination(
    monkeypatch,
    limit,
    offset,
):
    monkeypatch.setattr(
        main,
        "load_shared_memory_deletion_proposals",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid pagination must be rejected before database access"
        ),
    )

    with pytest.raises(HTTPException) as raised:
        main.list_shared_memory_deletion_proposals(
            _request(
                "GET",
                "/api/shared-memory-deletion-proposals",
                user=_user(),
            ),
            limit=limit,
            offset=offset,
        )

    assert raised.value.status_code == 422


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
    memory_id = "88888888-8888-4888-8888-888888888888"
    db = _MemoryClient([
        _memory(memory_id, scope="personal", owner=BOB_ID, creator=BOB_ID),
    ])
    request = _request(
        "PATCH" if operation == "update" else "DELETE",
        f"/api/memories/{memory_id}",
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
                memory_id,
                main.UpdateMemoryRequest(content="attempted overwrite"),
                request,
            )
        else:
            main.delete_memory(memory_id, request)

    assert exc_info.value.status_code == 404
    assert db.executed_operations == ["select"]
    assert db.rows[0]["content"] == f"{memory_id} content"
    assert ALICE_ID in db.visibility_expressions[0]


@pytest.mark.parametrize(
    "api_key",
    [
        "sk-proj-" + "x" * 32,
        "atl-" + "x" * 40,
    ],
)
def test_update_rejects_openai_api_key_before_lookup_or_embedding(api_key):
    with pytest.raises(HTTPException) as exc_info:
        main.update_memory(
            "memory-id",
            main.UpdateMemoryRequest(
                content=api_key
            ),
            _request("PATCH", "/api/memories/memory-id"),
        )

    assert exc_info.value.status_code == 400
    assert "API 키" in exc_info.value.detail


@pytest.mark.parametrize(
    "api_key",
    [
        "sk-proj-" + "x" * 32,
        "atl-" + "x" * 40,
    ],
)
def test_update_rejects_openai_api_key_nested_in_metadata_before_lookup(api_key):
    with pytest.raises(HTTPException) as exc_info:
        main.update_memory(
            "memory-id",
            main.UpdateMemoryRequest(
                content="otherwise safe content",
                metadata={
                    "subject": {
                        "credentials": api_key
                    }
                },
            ),
            _request("PATCH", "/api/memories/memory-id"),
        )

    assert exc_info.value.status_code == 400
    assert "API 키" in exc_info.value.detail


def test_contextualize_search_question_rewrites_generic_detail_followup(monkeypatch):
    monkeypatch.setattr(main, "CHAT_MODEL", "gpt-5.6-luna")
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="ATL AI 도구 사용료의 프로젝트 코드와 비용 계정을 구체적으로 알려줘"
                )
            )])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )

    resolved = main.contextualize_search_question(
        "구체적으로 알려줘",
        [
            {"role": "user", "content": "AI 도구 사용처리 어떻게 해?"},
            {"role": "assistant", "content": "저장된 내용을 바탕으로 처리합니다."},
        ],
    )

    assert resolved == (
        "ATL AI 도구 사용료의 프로젝트 코드와 비용 계정을 구체적으로 알려줘"
    )
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-5.6-luna"
    assert calls[0]["max_completion_tokens"] == 512
    assert "max_tokens" not in calls[0]
    assert "temperature" not in calls[0]
    assert calls[0]["messages"][-1] == {
        "role": "user",
        "content": "구체적으로 알려줘",
    }
    assert {
        "role": "user",
        "content": "AI 도구 사용처리 어떻게 해?",
    } in calls[0]["messages"]


def test_contextualize_search_question_uses_prior_question_when_rewrite_fails(
    monkeypatch,
):
    class FailedCompletions:
        def create(self, **_kwargs):
            raise RuntimeError("rewrite unavailable")

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=FailedCompletions())),
    )

    resolved = main.contextualize_search_question(
        "구체적으로 알려줘",
        [{"role": "user", "content": "AI 도구 사용처리 어떻게 해?"}],
    )

    assert resolved == (
        "AI 도구 사용처리 어떻게 해? / 후속 요청: 구체적으로 알려줘"
    )


def test_contextualize_search_question_does_not_rewrite_independent_question(
    monkeypatch,
):
    class UnexpectedCompletions:
        def create(self, **_kwargs):
            pytest.fail("an independent question must not invoke the rewrite model")

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=UnexpectedCompletions())),
    )
    question = "ATL AI 도구 사용료의 프로젝트 코드와 비용 계정을 구체적으로 알려줘"

    resolved = main.contextualize_search_question(
        question,
        [{"role": "user", "content": "법인카드 사용법 알려줘"}],
    )

    assert resolved == question


def test_prepare_answer_uses_resolved_followup_for_search_and_answer_prompt(
    monkeypatch,
):
    original_question = "구체적으로 알려줘"
    resolved_question = (
        "ATL AI 도구 사용료의 프로젝트 코드와 비용 계정을 구체적으로 알려줘"
    )
    memory = _memory(
        "atl-ai-tool-expense",
        content=(
            "이번 달 ATL 관련 AI 도구 사용료는 프로젝트 "
            "41000069-001/26년 AI Talent Lab 운영, 계정 CL/AI로 처리합니다."
        ),
    )

    monkeypatch.setattr(
        main,
        "contextualize_search_question",
        lambda question, history: (
            resolved_question
            if question == original_question and history
            else pytest.fail("the follow-up history was not forwarded")
        ),
    )
    monkeypatch.setattr(main, "memory_catalog", lambda _db, _user_id: [memory])
    monkeypatch.setattr(
        main,
        "embed",
        lambda _texts: pytest.fail("lexical follow-up retrieval should be sufficient"),
    )

    prepared = main.prepare_answer(
        main.AskRequest(
            question=original_question,
            history=[
                {"role": "user", "content": "AI 도구 사용처리 어떻게 해?"},
                {"role": "assistant", "content": "저장된 기준으로 처리합니다."},
            ],
        ),
        object(),
        _user(),
    )

    assert prepared["resolved_question"] == resolved_question
    assert [source["id"] for source in prepared["sources"]] == [
        "atl-ai-tool-expense"
    ]
    final_prompt = prepared["messages"][-1]["content"]
    assert f"질문: {resolved_question}" in final_prompt
    assert f"질문: {original_question}" not in final_prompt
    assert "41000069-001/26년 AI Talent Lab 운영" in final_prompt
    assert "CL/AI" in final_prompt


def test_lexical_search_normalizes_whitespace_for_exact_expense_query():
    expense_memory = _memory(
        "atl-ai-tool-expense",
        content="""[카테고리] 비용 처리 가이드
[제목] 2026년 8월 ATL AI 도구 사용료 처리
[프로젝트 코드] 41000069-001
[프로젝트명] 26년 AI Talent Lab 운영
[비용 계정] CL/AI
[내용]
ATL AI 도구 사용료는 지정 프로젝트와 비용 계정으로 처리합니다.""",
    )
    distractor = _memory(
        "ai-course",
        content="AI Bootcamp에서 OpenAI 도구 에이전트 활용법을 학습합니다.",
    )

    hits = main.lexical_memory_hits(
        "AI도구 비용처리 방법",
        [distractor, expense_memory],
    )

    assert [hit["id"] for hit in hits] == ["atl-ai-tool-expense", "ai-course"]
    assert [hit["_lexical_score"] for hit in hits] == [3, 2]


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("식대에서", "식대"),
        ("식대에서는", "식대"),
        ("법인카드는", "법인카드"),
        ("법인카드로", "법인카드"),
        ("법카로", "법카"),
        ("식비가", "식비"),
        ("식사비로", "식사비"),
        ("회계과", "회계과"),
        ("정산", "정산"),
    ],
)
def test_strip_korean_query_particle_is_narrow(token, expected):
    assert main._strip_korean_query_particle(token) == expected


def test_lexical_query_concepts_normalize_meal_card_wording_once():
    assert main._lexical_query_concepts(
        "식대에서 법인카드 처리 어떻게 해?"
    ) == [
        frozenset({"식대", "식사비", "식비"}),
        frozenset({"법인카드", "법카"}),
        frozenset({"처리", "정산", "결제"}),
    ]
    assert len(main._lexical_query_concepts(
        "식대 식사비 식비 법인카드 법카 처리 정산 결제"
    )) == 3


def _meal_card_search_rows():
    registration_guide = _memory(
        "registration-guide",
        content="""[유형] 비용 관리 가이드
[제목] 파트 의욕관리비 관련 구글 시트
[목적]
법인카드 사용 내역을 등록하고 정산하는 방법을 안내합니다.
[검색 키워드]
파트 의욕관리비, 비용 관리, 법인카드, 정산, 구글 시트, 사용 내역
[검색 질문 예시]
의욕관리비 사용 후 어떻게 정산해?""",
    )
    meal_card_guide = _memory(
        "meal-card-guide",
        content="""제목: 법인카드 사용 및 정산 가이드
구분: 비용 및 법인카드 운영
적용 대상: AI Tech Innovation 파트 구성원
배정 금액: 인당 월 80,000원

사용 기준:
- 개인 식사비: 월 40,000원
- 파트 공동 식사비: 월 40,000원

파트 공동 식사비 사용 방법:
- 월 1회 사용
- 파트 구성원 전체가 함께하는 점심 또는 저녁 식사에 사용

정산 방법:
- 적요 형식: [AI Talent] 내용
- 정산 예시: [AI Talent] 파트 점심식사""",
    )
    meal_card_guide["metadata"]["subject"] = "법인카드 사용 및 정산 가이드"
    return registration_guide, meal_card_guide


def test_lexical_meal_card_query_selects_policy_not_registration_guide():
    registration_guide, meal_card_guide = _meal_card_search_rows()

    hits = main.lexical_memory_hits(
        "식대에서 법인카드 처리 어떻게 해?",
        [registration_guide, meal_card_guide],
    )

    assert [(hit["id"], hit["_lexical_score"]) for hit in hits] == [
        ("meal-card-guide", 3),
    ]


@pytest.mark.parametrize(
    ("question", "expected_score"),
    [
        ("식대에서 법인카드는 어떻게 사용하고 정산하면 돼?", 3),
        ("식비가 법인카드로 처리해도 돼?", 3),
        ("식사비로 법카로 정산 방법 알려줘", 4),
        ("식대에서 법인 카드로 결제 어떻게 해?", 3),
        ("식사비가 법인 카드는 정산 어떻게 해?", 3),
    ],
)
def test_lexical_meal_card_query_handles_particles_and_action_inflections(
    question,
    expected_score,
):
    registration_guide, meal_card_guide = _meal_card_search_rows()

    hits = main.lexical_memory_hits(
        question,
        [registration_guide, meal_card_guide],
    )

    assert [(hit["id"], hit["_lexical_score"]) for hit in hits] == [
        ("meal-card-guide", expected_score),
    ]


def test_lexical_non_policy_query_preserves_two_term_matching():
    rows = [_memory("deployment", content="ATL 배포는 수요일에 진행합니다.")]

    hits = main.lexical_memory_hits("ATL 배포 일정", rows)

    assert [(hit["id"], hit["_lexical_score"]) for hit in hits] == [
        ("deployment", 2),
    ]


def test_lexical_search_preserves_meaningful_method_term():
    rows = [_memory("deployment", content="ATL 배포는 수요일에 진행합니다.")]

    assert main.lexical_memory_hits("배포 방법", rows) == []


def test_lexical_alias_matches_at_start_of_compound_not_inside_word():
    rows = [
        _memory("card-guide", content="법인카드사용법 안내"),
        _memory("unrelated-card", content="불법카드 사용법 안내"),
        _memory("card-company", content="법인카드사 사용법 안내"),
        _memory("cafe", content="법카페 사용법 안내"),
    ]

    hits = main.lexical_memory_hits("법인카드 사용법 알려줘", rows)

    assert [hit["id"] for hit in hits] == ["card-guide"]


def test_finance_synonyms_do_not_expand_outside_finance_context():
    rows = [_memory("civil-service", content="민원 정산 일정과 접수 창구 안내")]

    assert main.lexical_memory_hits("민원 처리 어떻게 해?", rows) == []


def test_meal_card_synonyms_do_not_match_inside_unrelated_words():
    rows = [_memory(
        "unrelated-compounds",
        content="주식대금, 불법카드, 간식비 처리 내역",
    )]

    assert main.lexical_memory_hits(
        "식대에서 법인카드 처리 어떻게 해?",
        rows,
    ) == []


def test_prepare_answer_meal_card_query_uses_actual_policy_only(monkeypatch):
    registration_guide, meal_card_guide = _meal_card_search_rows()
    monkeypatch.setattr(
        main,
        "memory_catalog",
        lambda _db, _user_id: [registration_guide, meal_card_guide],
    )
    monkeypatch.setattr(
        main,
        "embed",
        lambda _texts: pytest.fail("full lexical match must avoid vector search"),
    )

    prepared = main.prepare_answer(
        main.AskRequest(question="식대에서 법인카드 처리 어떻게 해?"),
        object(),
        _user(),
    )

    assert [source["id"] for source in prepared["sources"]] == ["meal-card-guide"]
    prompt = prepared["messages"][-1]["content"]
    assert "인당 월 80,000원" in prompt
    assert "개인 식사비: 월 40,000원" in prompt
    assert "파트 공동 식사비: 월 40,000원" in prompt
    assert "[AI Talent] 내용" in prompt
    assert "파트 의욕관리비 관련 구글 시트" not in prompt


def test_prepare_answer_exact_expense_query_keeps_best_hit_and_grounded_fields(
    monkeypatch,
):
    expense_memory = _memory(
        "atl-ai-tool-expense",
        content="""[기록 유형] 업무
[카테고리] 비용 처리 가이드
[제목] 2026년 8월 ATL AI 도구 사용료 처리
[적용월] 2026년 8월
[확인자] 권민정 매니저
[프로젝트 코드] 41000069-001
[프로젝트명] 26년 AI Talent Lab 운영
[비용 계정] CL/AI
[내용]
이번 달 ATL 관련 AI 도구 사용료는 위 프로젝트와 비용 계정으로 처리합니다.
[키워드] ATL, AI 도구 사용료, 비용 처리, 41000069-001, CL/AI""",
    )
    distractor = _memory(
        "ai-course",
        content="""과정명: AI Bootcamp
모듈명: Azure OpenAI & LangChain
강의 목록: 도구 에이전트와 챗봇 실습""",
    )
    monkeypatch.setattr(
        main,
        "memory_catalog",
        lambda _db, _user_id: [distractor, expense_memory],
    )
    monkeypatch.setattr(
        main,
        "embed",
        lambda _texts: pytest.fail("normalized lexical retrieval should be sufficient"),
    )

    prepared = main.prepare_answer(
        main.AskRequest(question="AI도구 비용처리 방법"),
        object(),
        _user(),
    )

    assert [source["id"] for source in prepared["sources"]] == [
        "atl-ai-tool-expense"
    ]
    grounded = prepared["grounded_fallback"]
    assert grounded is not None
    assert "41000069-001" in grounded
    assert "26년 AI Talent Lab 운영" in grounded
    assert "CL/AI" in grounded
    assert "2026년 8월" in grounded
    assert "권민정 매니저" in grounded
    assert "Azure OpenAI" not in prepared["messages"][-1]["content"]


def test_prepare_answer_does_not_prefer_weaker_structured_expense_hit(
    monkeypatch,
):
    strong_plain = _memory(
        "strong-plain-expense",
        content=(
            "AI 도구 비용처리 방법은 사내 결제 가이드의 최신 절차를 따릅니다."
        ),
    )
    weak_structured = _memory(
        "weak-structured-expense",
        content="""[제목] 다른 프로젝트 비용 안내
[프로젝트 코드] WEAK-001
[비용 계정] OTHER/AI
[내용]
AI 도구 참고 자료입니다.""",
    )
    monkeypatch.setattr(
        main,
        "memory_catalog",
        lambda _db, _user_id: [weak_structured, strong_plain],
    )
    monkeypatch.setattr(
        main,
        "embed",
        lambda _texts: pytest.fail("strong lexical retrieval should be sufficient"),
    )

    prepared = main.prepare_answer(
        main.AskRequest(question="AI도구 비용처리 방법"),
        object(),
        _user(),
    )

    assert prepared["sources"][0]["id"] == "strong-plain-expense"
    assert prepared["grounded_fallback"] is None
    assert "최신 절차" in prepared["messages"][-1]["content"]


def test_prepare_answer_without_related_memory_preserves_no_result_fallback(
    monkeypatch,
):
    class EmptyRpc:
        def execute(self):
            return _result([])

    class EmptySearchClient:
        def rpc(self, name, params):
            assert name == "match_memories"
            assert params["query_scope"] == "personal"
            return EmptyRpc()

    monkeypatch.setattr(main, "memory_catalog", lambda _db, _user_id: [])
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.1]])

    prepared = main.prepare_answer(
        main.AskRequest(question="AI도구 비용처리 방법"),
        EmptySearchClient(),
        _user(),
    )

    assert prepared["fallback"] == (
        "저장된 정보에서 관련 내용을 찾지 못했어요. "
        "먼저 관련 메시지를 붙여넣어 저장해 주세요."
    )
    assert prepared["messages"] is None
    assert prepared["sources"] == []
    assert prepared["grounded_fallback"] is None


def test_grounded_answer_requires_relevant_intent_and_prefers_edited_metadata():
    memory = _memory(
        "atl-ai-tool-expense",
        content="""[제목] 이전 AI 도구 비용 처리
[프로젝트 코드] OLD-001
[프로젝트명] 이전 프로젝트
[비용 계정] OLD/AI
[적용월] 2026년 8월
[확인자] 이전 확인자
[내용]
AI 도구 사용료는 지정된 프로젝트와 비용 계정으로 처리합니다.""",
    )
    memory["_lexical_score"] = 3
    memory["metadata"].update({
        "subject": "수정된 AI 도구 비용 처리",
        "project_code": "NEW-001",
        "project": "수정된 프로젝트",
        "expense_account": "NEW/AI",
        "applicable_month": "2026년 9월",
        "confirmed_by": "새 확인자",
    })

    assert main.structured_grounded_answer(
        "권민정 매니저 연락처 알려줘",
        [memory],
    ) is None

    grounded = main.structured_grounded_answer("AI도구 비용처리 방법", [memory])

    assert grounded is not None
    assert "수정된 AI 도구 비용 처리" in grounded
    assert "NEW-001 / 수정된 프로젝트" in grounded
    assert "NEW/AI" in grounded
    assert "2026년 9월" in grounded
    assert "새 확인자" in grounded
    assert "이전 프로젝트" not in grounded
    assert "이전 확인자" not in grounded


def test_no_information_detector_rejects_partial_grounded_answers():
    assert main.is_no_information_answer("저장된 정보에서 찾지 못했다.")
    assert main.is_no_information_answer(
        "저장된 정보에서 관련 내용을 찾지 못했어요. 먼저 저장해 주세요."
    )
    assert not main.is_no_information_answer(
        "저장된 정보에서 프로젝트 코드는 41000069-001이지만 "
        "확인자는 찾지 못했습니다."
    )


def test_nonstream_replaces_short_no_information_answer_with_grounded_answer(
    monkeypatch,
):
    monkeypatch.setattr(main, "CHAT_MODEL", "gpt-5.6-luna")
    grounded = (
        "**2026년 8월 ATL AI 도구 사용료 처리**\n"
        "- 프로젝트: 41000069-001 / 26년 AI Talent Lab 운영\n"
        "- 비용 계정: CL/AI\n"
        "- 적용월: 2026년 8월\n"
        "- 확인자: 권민정 매니저"
    )
    prepared = {
        "fallback": None,
        "grounded_fallback": grounded,
        "messages": [{"role": "user", "content": "grounded question"}],
        "resolved_question": None,
        "sources": [{"id": "atl-ai-tool-expense"}],
    }
    events = []
    monkeypatch.setattr(
        main,
        "consume_ai_use",
        lambda user_id: events.append(("consume", user_id)) or 9,
    )
    monkeypatch.setattr(
        main,
        "prepare_answer",
        lambda *_args: events.append(("prepare", None)) or prepared,
    )
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *args, **kwargs: events.append(
            ("audit", args[2], kwargs["source_count"], kwargs["streaming"])
        ),
    )

    class FalseNegativeCompletions:
        def create(self, **kwargs):
            assert kwargs.get("stream") is not True
            assert kwargs["model"] == "gpt-5.6-luna"
            assert kwargs["max_completion_tokens"] == 1500
            assert "max_tokens" not in kwargs
            events.append(("model", False))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="저장된 정보에서 찾지 못했다.")
            )])

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(
            chat=SimpleNamespace(completions=FalseNegativeCompletions())
        ),
    )

    result = main.ask(
        main.AskRequest(question="AI도구 비용처리 방법"),
        _request("POST", "/api/ask", user=_user(), db=object()),
    )

    assert result["answer"] == grounded
    assert "찾지 못했다" not in result["answer"]
    assert result["sources"] == [{"id": "atl-ai-tool-expense"}]
    assert result["remaining_uses"] == 9
    assert events == [
        ("consume", ALICE_ID),
        ("prepare", None),
        ("model", False),
        ("audit", "memory_ask", 1, False),
    ]


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
    monkeypatch.setattr(main, "CHAT_MODEL", "gpt-5.6-luna")
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
            assert kwargs["model"] == "gpt-5.6-luna"
            assert kwargs["max_completion_tokens"] == 1500
            assert "max_tokens" not in kwargs
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


def test_stream_replaces_chunked_no_information_answer_with_grounded_answer(
    monkeypatch,
):
    grounded = (
        "**2026년 8월 ATL AI 도구 사용료 처리**\n"
        "- 프로젝트: 41000069-001 / 26년 AI Talent Lab 운영\n"
        "- 비용 계정: CL/AI\n"
        "- 적용월: 2026년 8월\n"
        "- 확인자: 권민정 매니저"
    )
    prepared = {
        "fallback": None,
        "grounded_fallback": grounded,
        "messages": [{"role": "user", "content": "grounded question"}],
        "resolved_question": None,
        "sources": [{"id": "atl-ai-tool-expense"}],
    }
    request_db = object()
    call_order = []
    monkeypatch.setattr(
        main,
        "consume_ai_use",
        lambda user_id: call_order.append(("consume", user_id)) or 9,
    )
    monkeypatch.setattr(
        main,
        "prepare_answer",
        lambda *_args: call_order.append(("prepare", None)) or prepared,
    )
    monkeypatch.setattr(
        main,
        "write_audit",
        lambda *args, **kwargs: call_order.append(
            ("audit", args[2], kwargs["source_count"], kwargs["streaming"])
        ),
    )

    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="저장된 정보에서 ")
        )]),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="찾지 못했다.")
        )]),
    ]

    class FalseNegativeStreamCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            call_order.append(("model", True))
            return iter(chunks)

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(
            chat=SimpleNamespace(completions=FalseNegativeStreamCompletions())
        ),
    )

    response = main.ask_stream(
        main.AskRequest(question="AI도구 비용처리 방법"),
        _request(
            "POST",
            "/api/ask/stream",
            user=_user(),
            db=request_db,
        ),
    )

    async def collect():
        return [part async for part in response.body_iterator]

    events = [
        json.loads(line)
        for part in asyncio.run(collect())
        for line in (part.decode() if isinstance(part, bytes) else part).splitlines()
        if line
    ]

    assert [event["type"] for event in events] == [
        "meta", "progress", "progress", "delta", "done",
    ]
    rendered = "".join(
        event.get("content", "") for event in events if event["type"] == "delta"
    )
    assert rendered == grounded
    assert "찾지 못했다" not in rendered
    assert call_order == [
        ("consume", ALICE_ID),
        ("prepare", None),
        ("audit", "memory_ask", 1, True),
        ("model", True),
    ]


def test_buffered_stream_cancellation_closes_upstream_and_refunds_once(monkeypatch):
    prepared = {
        "fallback": None,
        "grounded_fallback": "근거가 있는 비용 처리 답변",
        "messages": [{"role": "user", "content": "grounded question"}],
        "resolved_question": None,
        "sources": [{"id": "atl-ai-tool-expense"}],
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

    class TrackingStream:
        def __init__(self):
            self.sent = False
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.sent:
                raise AssertionError("disconnect should stop upstream iteration")
            self.sent = True
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="저장된 정보에서 ")
            )])

        def close(self):
            self.closed = True

    upstream = TrackingStream()

    class BufferedCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return upstream

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=BufferedCompletions())),
    )
    response = main.ask_stream(
        main.AskRequest(question="AI도구 비용처리 방법"),
        _request("POST", "/api/ask/stream", user=_user(), db=object()),
    )

    async def read_progress_then_disconnect():
        iterator = response.body_iterator
        parts = [await anext(iterator), await anext(iterator)]
        await iterator.aclose()
        return parts

    events = [
        json.loads(part.decode() if isinstance(part, bytes) else part)
        for part in asyncio.run(read_progress_then_disconnect())
    ]

    assert [event["type"] for event in events] == ["meta", "progress"]
    assert all(event["type"] != "delta" for event in events)
    assert upstream.closed is True
    assert refunds == [(ALICE_ID, 9)]


def test_buffered_stream_error_emits_no_partial_answer_and_refunds_once(monkeypatch):
    prepared = {
        "fallback": None,
        "grounded_fallback": "근거가 있는 비용 처리 답변",
        "messages": [{"role": "user", "content": "grounded question"}],
        "resolved_question": None,
        "sources": [{"id": "atl-ai-tool-expense"}],
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

    class FailingStream:
        def __init__(self):
            self.calls = 0
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="저장된 정보에서 ")
                )])
            raise RuntimeError("stream failed after a buffered chunk")

        def close(self):
            self.closed = True

    upstream = FailingStream()

    class BufferedCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return upstream

    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(chat=SimpleNamespace(completions=BufferedCompletions())),
    )
    response = main.ask_stream(
        main.AskRequest(question="AI도구 비용처리 방법"),
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

    assert [event["type"] for event in events] == ["meta", "progress", "error"]
    assert all(event["type"] != "delta" for event in events)
    assert events[-1]["remaining_uses"] == 10
    assert upstream.closed is True
    assert refunds == [(ALICE_ID, 9)]


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


class _MemoryRpcClient(_MemoryClient):
    def __init__(self, rows, handler):
        super().__init__(rows)
        self.handler = handler
        self.rpc_calls = []

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return _RpcResult(self.handler(name, params, self))


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


def test_ai_talent_key_guard_is_present_in_schema_and_upgrade_migration():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migration_ai_talent_api_key_guard.sql").read_text(
        encoding="utf-8"
    )
    sql_documents = [
        (root / "schema.sql").read_text(encoding="utf-8"),
        migration,
    ]

    for sql in sql_documents:
        assert "create or replace function public.set_memory_derived_fields" in sql
        assert "(sk|atl)-[A-Za-z0-9_-]{20,}" in sql
        assert "AI API 키처럼 보이는 값" in sql

    assert re.search(r"lock table\s+public\.memories", migration)
    assert "insert into public.quarantined_memories" in migration
    assert "[REDACTED_AI_API_KEY]" in migration
    assert "delete from public.memories" in migration
    assert "delete from public.shared_memory_proposals" in migration
    assert "update public.shared_memory_deletion_proposals" in migration
    assert "source_snapshot = regexp_replace" in migration
    assert "content_snapshot = regexp_replace" in migration
    assert "embedding = null" in migration

    schema_trigger = sql_documents[0].split(
        "create or replace function public.set_memory_derived_fields()", 1
    )[1].split("$$;", 1)[0]
    migration_trigger = migration.split(
        "create or replace function public.set_memory_derived_fields()", 1
    )[1].split("$$;", 1)[0]
    def executable_sql(body):
        without_line_comments = "\n".join(
            line.split("--", 1)[0]
            for line in body.splitlines()
        )
        return " ".join(without_line_comments.split())

    assert executable_sql(schema_trigger) == executable_sql(migration_trigger)


def test_embedding_configuration_matches_supabase_vector_schema():
    root = Path(__file__).resolve().parents[1]
    schema = (root / "schema.sql").read_text(encoding="utf-8").lower()

    assert main.EMBED_MODEL == "text-embedding-3-small"
    assert main.EMBED_DIMENSIONS == 1536
    assert "embedding vector(1536)" in schema


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
    assert (
        "on conflict on constraint shared_memory_proposal_approvals_pkey"
        in approve_rpc
    )
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


def test_unhandled_shared_approval_schema_error_returns_migration_guidance():
    request = _request("GET", "/api/memories")
    error = RuntimeError("column memories.publication_status does not exist")

    response = asyncio.run(main.unhandled_exception_handler(request, error))

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "detail": main.SHARED_APPROVAL_MIGRATION_MESSAGE,
    }
    assert response.headers["cache-control"] == "private, no-store"


def test_unhandled_memory_scope_error_returns_base_migration_guidance():
    request = _request("GET", "/api/memories")
    error = RuntimeError("column memories.scope does not exist")

    response = asyncio.run(main.unhandled_exception_handler(request, error))

    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": main.MEMORY_MIGRATION_MESSAGE}


def test_unhandled_unknown_error_keeps_generic_message():
    request = _request("GET", "/api/memories")

    response = asyncio.run(
        main.unhandled_exception_handler(request, RuntimeError("unexpected"))
    )

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "detail": "서버 처리 중 오류가 발생했습니다.",
    }


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
        assert (
            "on conflict on constraint shared_memory_proposal_approvals_pkey"
            in approve_rpc
        )
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


def test_shared_memory_deletion_migration_enforces_two_distinct_voters_and_history():
    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "migration_shared_memory_deletion_approvals.sql"
    ).read_text(encoding="utf-8").lower()

    proposal_table = migration.split(
        "create table if not exists public.shared_memory_deletion_proposals", 1
    )[1].split(
        "create table if not exists public.shared_memory_deletion_proposal_approvals",
        1,
    )[0]
    assert "memory_id uuid not null" in proposal_table
    assert "source_snapshot text not null" in proposal_table
    assert "content_snapshot text not null" in proposal_table
    assert "references public.memories" not in proposal_table
    assert "required_approvals smallint not null default 2" in proposal_table
    assert "check (required_approvals = 2)" in proposal_table
    assert "check (status in ('pending', 'deleted'))" in proposal_table
    assert "primary key (proposal_id, approver_user_id)" in migration
    assert re.search(
        r"create unique index[^;]+\(memory_id\)\s+where status = 'pending'",
        migration,
        re.DOTALL,
    )

    request_rpc = migration.split(
        "create or replace function public.request_shared_memory_deletion", 1
    )[1].split(
        "create or replace function public.approve_shared_memory_deletion_proposal",
        1,
    )[0]
    assert "actor_id uuid := auth.uid()" in request_rpc
    assert "actor_role not in ('editor', 'admin')" in request_rpc
    assert "for update" in request_rpc
    assert "source_snapshot" in request_rpc
    assert "content_snapshot" in request_rpc
    assert (
        "on conflict on constraint "
        "shared_memory_deletion_proposal_approvals_pkey" in request_rpc
    )
    assert "counted_approvals >= proposal_record.required_approvals" in request_rpc
    assert "actor_role = 'admin'" in request_rpc
    assert "delete from public.memories" in request_rpc
    assert "'shared_memory_deletion_proposal_create'" in request_rpc
    assert "'shared_memory_deletion_proposal_approve'" in request_rpc
    assert "'shared_memory_delete'" in request_rpc

    approve_rpc = migration.split(
        "create or replace function public.approve_shared_memory_deletion_proposal",
        1,
    )[1].split(
        "alter function public.request_shared_memory_deletion", 1
    )[0]
    assert "actor_id uuid := auth.uid()" in approve_rpc
    assert "actor_role not in ('viewer', 'editor', 'admin')" in approve_rpc
    assert "for update" in approve_rpc
    assert (
        "on conflict on constraint "
        "shared_memory_deletion_proposal_approvals_pkey" in approve_rpc
    )
    assert "counted_approvals >= proposal_record.required_approvals" in approve_rpc
    assert "actor_role = 'admin'" in approve_rpc
    assert "delete from public.memories" in approve_rpc
    assert "'shared_memory_deletion_proposal_approve'" in approve_rpc
    assert "'shared_memory_delete'" in approve_rpc
    assert "memory_exists boolean := false" in approve_rpc
    assert "memory_exists := found" in approve_rpc
    assert "or not memory_exists" in approve_rpc
    assert "set status = 'deleted'" in approve_rpc

    trigger_sql = migration.split(
        "create or replace function public.close_shared_memory_deletion_proposal",
        1,
    )[1].split(
        "create or replace function public.request_shared_memory_deletion", 1
    )[0]
    assert "update public.shared_memory_deletion_proposals" in trigger_sql
    assert "where proposal.memory_id = old.id" in trigger_sql
    assert "proposal.status = 'pending'" in trigger_sql
    assert "after delete on public.memories" in trigger_sql

    assert re.search(
        r"revoke all privileges on table public\.shared_memory_deletion_proposals"
        r"\s+from public, anon, authenticated",
        migration,
    )
    assert re.search(
        r"revoke all privileges on table public\.shared_memory_deletion_proposal_approvals"
        r"\s+from public, anon, authenticated",
        migration,
    )
    assert re.search(
        r"grant execute on function public\.request_shared_memory_deletion\(uuid\)"
        r"\s+to authenticated, service_role",
        migration,
    )
    assert re.search(
        r"grant execute on function public\.approve_shared_memory_deletion_proposal\(uuid\)"
        r"\s+to authenticated, service_role",
        migration,
    )


def test_shared_memory_deletion_schema_mirrors_migration_for_fresh_installs():
    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "migration_shared_memory_deletion_approvals.sql"
    ).read_text(encoding="utf-8").lower()
    schema = (root / "schema.sql").read_text(encoding="utf-8").lower()

    required_fragments = (
        "public.shared_memory_deletion_proposals",
        "public.shared_memory_deletion_proposal_approvals",
        "public.request_shared_memory_deletion",
        "public.approve_shared_memory_deletion_proposal",
        "public.close_shared_memory_deletion_proposal",
        "shared_memory_deletion_one_pending_uidx",
        "source_snapshot text not null",
        "content_snapshot text not null",
        "after delete on public.memories",
    )
    for fragment in required_fragments:
        assert fragment in migration
        assert fragment in schema
    for sql in (migration, schema):
        assert not re.search(r"(?m)^\s*(?:\+--|\*\*\*)", sql)


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


def test_access_cards_support_address_labels_and_omit_empty_credentials():
    html = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert '["URL", "주소", "링크", "호스트"]' in html
    assert "extractHttpUrl(meta.url)" in html
    assert 'if (!url && !id && !password) return "";' in html
    assert 'meta.record_type === "credential" || id || password' in html
    assert "const accessBody = isAccess ? accessDetails(m, meta)" in html


def test_ingest_ui_confirms_similar_memories_before_saving():
    html = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="similarMemoryOverlay"' in html
    assert "유사한 기억이 있습니다" in html
    assert "그래도 저장하시겠습니까?" in html
    assert 'id="similarMemoryCancelBtn"' in html
    assert 'id="similarMemoryConfirmBtn"' in html
    assert "allow_similar: allowSimilar" in html
    assert 'conflict.code === "similar_memories_found"' in html
    assert "content.textContent = String(memory.snippet" in html
    assert "let ingestBusy = false" in html
    assert "if (!canWrite() || ingestBusy) return" in html


def test_shared_memory_deletion_ui_labels_and_keeps_meal_expense_examples():
    html = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="sharedDeletionApprovalPanel"' in html
    assert "모두의 기억 삭제 승인" in html
    assert "요청자를 포함한 2명이 동의하면 모두의 기억에서 삭제됩니다." in html
    assert 'aria-label="모두의 기억 삭제 요청"' in html
    assert ">삭제 요청</button>" in html
    assert 'appendProposalBadge(labels, "scope-badge deletion", "삭제 승인")' in html
    assert "/api/shared-memory-deletion-proposals" in html
    deletion_approval_helper = html.split(
        "function deletionProposalApprovalInfo", 1
    )[1].split("function setSharedDeletionApprovalFeedback", 1)[0]
    assert re.search(
        r"const canApprove = isAdmin\(\)\s*"
        r"\? explicitCanApprove !== false\s*"
        r": !approvedByMe",
        deletion_approval_helper,
    )
    assert "예) 식대에서 법인카드 사용 방식" in html
    assert "예: 식대에서 법인카드는 어떻게 사용하고 정산하면 돼?" in html
