import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# Importing main constructs SDK clients. Use inert credentials so test collection
# never depends on a developer's .env values or sends requests to real services.
os.environ["SUPABASE_URL"] = "https://example.supabase.co"
os.environ["SUPABASE_SERVICE_KEY"] = "test-service-key"
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["APP_ENV"] = "development"
os.environ["APP_PASSWORD"] = ""
os.environ["APP_USERS_JSON"] = ""
os.environ["APP_SECRET"] = "test-session-secret-at-least-16-chars"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


def _request(method: str, path: str, cookie: str | None = None) -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"ma_session={cookie}".encode()))
    return Request(
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


def _result(data):
    return SimpleNamespace(data=data)


def _assert_security_headers(response):
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['permissions-policy'] == 'camera=(), microphone=(), geolocation=()'
    assert response.headers['content-security-policy']


class _CatalogQuery:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self

    def execute(self):
        return _result(self.rows)


class _CatalogSupabase:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def table(self, name):
        assert name == "memories"
        self.calls += 1
        return _CatalogQuery(self.rows)


def test_session_token_rejects_expiry_and_tampering(monkeypatch):
    monkeypatch.setattr(main, "APP_SECRET", "unit-test-signing-secret")
    monkeypatch.setattr(main, "SESSION_TTL_SECONDS", 60)

    token = main.make_session_token("alice", "editor", now=1_000)
    claims = main.verify_session_token(token, now=1_059)

    assert claims is not None
    assert claims["sub"] == "alice"
    assert claims["role"] == "editor"
    assert main.verify_session_token(token, now=1_060) is None

    payload, signature = token.split(".", 1)
    replacement = "A" if signature[-1] != "A" else "B"
    assert main.verify_session_token(f"{payload}.{signature[:-1]}{replacement}", now=1_001) is None

    decoded = json.loads(main._b64decode(payload))
    decoded["role"] = "admin"
    tampered_payload = main._b64encode(
        json.dumps(decoded, separators=(",", ":")).encode()
    )
    assert main.verify_session_token(f"{tampered_payload}.{signature}", now=1_001) is None


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


def test_auth_middleware_blocks_viewer_write(monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        main,
        "verify_session_token",
        lambda _token: {"sub": "reader", "role": "viewer"},
    )
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    called = False

    async def call_next(_request):
        nonlocal called
        called = True
        return JSONResponse({"ok": True})

    response = asyncio.run(
        main.auth_middleware(
            _request("PATCH", "/api/memories/abc", "valid-token"), call_next
        )
    )

    assert response.status_code == 403
    assert not called
    _assert_security_headers(response)


def test_auth_middleware_attaches_editor_identity(monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        main,
        "verify_session_token",
        lambda _token: {"sub": "writer", "role": "editor"},
    )
    request = _request("POST", "/api/ingest", "valid-token")

    async def call_next(received):
        assert received.state.user == {"username": "writer", "role": "editor"}
        return JSONResponse({"ok": True})

    response = asyncio.run(main.auth_middleware(request, call_next))

    assert response.status_code == 200
    _assert_security_headers(response)


def test_auth_middleware_adds_security_headers_to_unauthorized_response(monkeypatch):
    monkeypatch.setattr(main, 'AUTH_ENABLED', True)
    monkeypatch.setattr(main, 'APP_USERS', {})
    monkeypatch.setattr(main, 'verify_session_token', lambda _token: None)
    called = False

    async def call_next(_request):
        nonlocal called
        called = True
        return JSONResponse({'ok': True})

    response = asyncio.run(
        main.auth_middleware(_request('GET', '/api/memories'), call_next)
    )

    assert response.status_code == 401
    assert not called
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

    payload = main.normalize_parsed_payload({"source": "slack", "records": "bad"}, original)

    assert payload["source"] == "note"
    assert len(payload["records"]) == 3
    assert all(len(record["content"]) <= 6_000 for record in payload["records"])
    assert "".join(record["content"] for record in payload["records"]) == original
    assert all(record["tags"] == [] for record in payload["records"])


def test_password_hash_verification_accepts_only_correct_password():
    encoded = main.make_password_hash("correct-password", iterations=100_000)

    assert main.verify_password("correct-password", {"password_hash": encoded})
    assert not main.verify_password("wrong-password", {"password_hash": encoded})


@pytest.mark.parametrize(
    "encoded",
    [
        "not-a-password-hash",
        "md5$100000$aa$bb",
        "pbkdf2_sha256$99999$aa$bb",
        "pbkdf2_sha256$not-an-int$aa$bb",
        "pbkdf2_sha256$100000$not-hex$deadbeef",
    ],
)
def test_password_hash_verification_rejects_malformed_values(encoded):
    assert not main.verify_password("password", {"password_hash": encoded})


def test_ingest_uses_content_hash_batch_upsert_without_delete_or_rpc(monkeypatch):
    parsed_records = [
        {
            "content": "existing memory",
            "metadata": {"person": "곽진성", "status": "완료"},
            "tags": ["G-core"],
            "expires_at": None,
        },
        {
            "content": "new memory",
            "metadata": {"person": "곽진성", "status": "할 일"},
            "tags": ["ATL"],
            "expires_at": None,
        },
        {
            "content": "existing memory",
            "metadata": {"person": "duplicate input"},
            "tags": [],
            "expires_at": None,
        },
    ]
    parser_result = json.dumps({"source": " SLACK ", "records": parsed_records})

    class FakeChatCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=parser_result))]
            )

    class FakeEmbeddings:
        def __init__(self):
            self.inputs = []

        def create(self, *, model, input):
            assert model == main.EMBED_MODEL
            self.inputs.append(list(input))
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[float(index)]) for index, _ in enumerate(input)]
            )

    fake_chat = FakeChatCompletions()
    fake_embeddings = FakeEmbeddings()
    monkeypatch.setattr(
        main,
        "oai",
        SimpleNamespace(
            chat=SimpleNamespace(completions=fake_chat),
            embeddings=fake_embeddings,
        ),
    )

    existing_hash = main.hashlib.sha256(b"slack\0existing memory").hexdigest()

    class FakeMemoryQuery:
        def __init__(self, store):
            self.store = store
            self.operation = None
            self.hashes = []

        def select(self, columns):
            assert columns == "content_hash"
            self.operation = "select"
            return self

        def in_(self, column, values):
            assert column == "content_hash"
            self.hashes = list(values)
            return self

        def upsert(self, rows, on_conflict):
            self.operation = "upsert"
            self.store.upserts.append((rows, on_conflict))
            return self

        def delete(self):
            self.store.delete_calls += 1
            raise AssertionError("ingest must not delete an existing memory")

        def execute(self):
            if self.operation == "select":
                matches = self.store.existing_hashes.intersection(self.hashes)
                return _result([{"content_hash": value} for value in matches])
            if self.operation == "upsert":
                return _result(self.store.upserts[-1][0])
            raise AssertionError("unexpected Supabase operation")

    class FakeSupabase:
        def __init__(self):
            self.existing_hashes = {existing_hash}
            self.upserts = []
            self.delete_calls = 0
            self.rpc_calls = 0

        def table(self, name):
            assert name == "memories"
            return FakeMemoryQuery(self)

        def rpc(self, *_args, **_kwargs):
            self.rpc_calls += 1
            raise AssertionError("ingest must not use vector RPC for duplicate detection")

    fake_sb = FakeSupabase()
    monkeypatch.setattr(main, "sb", fake_sb)
    monkeypatch.setattr(main, "write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_catalog_cache_time", 123.0)

    result = main.ingest(
        main.IngestRequest(text="input text"),
        _request("POST", "/api/ingest"),
    )

    assert result["source"] == "slack"
    assert result["saved"] == 2
    assert result["replaced"] == 1
    assert fake_chat.calls == 1
    assert fake_embeddings.inputs == [["existing memory", "new memory"]]
    assert fake_sb.rpc_calls == 0
    assert fake_sb.delete_calls == 0
    assert len(fake_sb.upserts) == 1

    rows, conflict = fake_sb.upserts[0]
    assert conflict == "content_hash"
    assert len(rows) == 2
    assert {row["content_hash"] for row in rows} == {
        main.hashlib.sha256(f"slack\0{row['content']}".encode()).hexdigest()
        for row in rows
    }
    assert len({row["metadata"]["batch_id"] for row in rows}) == 1
    assert main._catalog_cache_time == 0.0


def test_memory_catalog_filters_expired_rows_and_caches(monkeypatch):
    rows = [
        {
            "id": "expired",
            "source": "note",
            "content": "old",
            "metadata": {},
            "created_at": "2020-01-01T00:00:00+00:00",
            "expires_at": "2020-01-02T00:00:00+00:00",
        },
        {
            "id": "active",
            "source": "note",
            "content": "current",
            "metadata": {},
            "created_at": "2020-01-01T00:00:00+00:00",
            "expires_at": "2999-01-01T00:00:00+00:00",
        },
        {
            "id": "permanent",
            "source": "note",
            "content": "permanent",
            "metadata": {},
            "created_at": "2020-01-01T00:00:00+00:00",
            "expires_at": None,
        },
    ]
    fake = _CatalogSupabase(rows)
    monkeypatch.setattr(main, "sb", fake)
    monkeypatch.setattr(main, "_catalog_cache", [])
    monkeypatch.setattr(main, "_catalog_cache_time", 0.0)

    first = main.memory_catalog()
    second = main.memory_catalog()

    assert [row["id"] for row in first] == ["active", "permanent"]
    assert second == first
    assert fake.calls == 1


def test_filtered_list_supports_legacy_sender_tags_pagination_and_count(monkeypatch):
    def legacy_row(memory_id, created_at, sender='레거시 담당자', tags=None):
        return {
            'id': memory_id,
            'source': 'slack',
            'content': '진행 중인 레거시 업무',
            'metadata': {
                'sender': sender,
                'tags': tags or ['AI Tech Innovation팀', 'Legacy Project'],
            },
            'created_at': created_at,
            'expires_at': None,
        }

    rows = [
        legacy_row('newest', '2026-08-07T03:00:00+00:00'),
        legacy_row('middle', '2026-08-07T02:00:00+00:00'),
        legacy_row('oldest', '2026-08-07T01:00:00+00:00'),
        legacy_row('other-person', '2026-08-07T00:00:00+00:00', sender='다른 담당자'),
        legacy_row('other-project', '2026-08-06T23:00:00+00:00', tags=['Other Project']),
    ]
    catalog_calls = 0

    def fake_all_memory_catalog():
        nonlocal catalog_calls
        catalog_calls += 1
        return rows

    monkeypatch.setattr(main, 'all_memory_catalog', fake_all_memory_catalog)
    response = Response()

    items = main.list_memories(
        response=response,
        limit=1,
        offset=1,
        person='레거시 담당자',
        project='Legacy Project',
    )

    assert catalog_calls == 1
    assert response.headers['x-total-count'] == '3'
    assert [item['id'] for item in items] == ['middle']
    assert items[0]['metadata']['person'] == '레거시 담당자'
    assert items[0]['metadata']['project'] == 'Legacy Project'


def test_prepare_answer_excludes_old_assistant_and_includes_metadata(monkeypatch):
    hit = {
        "id": "memory-1",
        "source": "slack",
        "content": "출제 에이전트 고도화를 완료했다.",
        "metadata": {
            "person": "곽진성",
            "project": "G-core Quick Win",
            "status": "완료",
            "work_date": "2026-08-07",
            "due_date": "2026-08-14",
            "category": "업무",
            "record_type": "work",
            "tags": ["G-core"],
        },
        "created_at": "2026-08-07T01:00:00+00:00",
        "expires_at": None,
        "similarity": 0.95,
    }

    class FakeRpc:
        def execute(self):
            return _result([hit])

    class FakeSb:
        def rpc(self, name, params):
            assert name == "match_memories"
            assert params["query_embedding"] == [0.1]
            return FakeRpc()

    monkeypatch.setattr(main, "sb", FakeSb())
    monkeypatch.setattr(main, "embed", lambda _texts: [[0.1]])
    monkeypatch.setattr(main, "memory_catalog", lambda: [])
    monkeypatch.setattr(main, "all_tags", lambda: {})
    monkeypatch.setattr(main, "contextualize_search_question", lambda question, _history: question)

    prepared = main.prepare_answer(
        main.AskRequest(
            question="상세 현황을 알려줘",
            history=[
                {"role": "user", "content": "이전 질문"},
                {"role": "assistant", "content": "OLD_HALLUCINATED_ANSWER"},
            ],
        )
    )

    assert all(message["role"] != "assistant" for message in prepared["messages"])
    combined = "\n".join(message["content"] for message in prepared["messages"])
    assert "OLD_HALLUCINATED_ANSWER" not in combined
    for expected in (
        "곽진성",
        "G-core Quick Win",
        "완료",
        "2026-08-07",
        "2026-08-14",
        "업무",
        "work",
    ):
        assert expected in combined


def test_stream_emits_meta_deltas_and_done_without_network(monkeypatch):
    prepared = {
        "fallback": None,
        "messages": [{"role": "user", "content": "question"}],
        "resolved_question": "resolved question",
        "sources": [{"id": "memory-1"}],
    }
    monkeypatch.setattr(main, "prepare_answer", lambda _request: prepared)
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

    fake_oai = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(main, "oai", fake_oai)

    response = main.ask_stream(
        main.AskRequest(question="question"),
        _request("POST", "/api/ask/stream"),
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
