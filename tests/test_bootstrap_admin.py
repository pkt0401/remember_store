import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import bootstrap_admin


ADMIN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _auth_user(
    user_id,
    email,
    *,
    username="alice",
    user_metadata=None,
    app_metadata=None,
):
    return SimpleNamespace(
        id=user_id,
        email=email,
        user_metadata=(
            dict(user_metadata)
            if user_metadata is not None
            else {"username": username, "email": email}
        ),
        app_metadata=dict(app_metadata or {}),
    )


class _ProfileQuery:
    def __init__(self, client, operation="select", values=None):
        self.client = client
        self.operation = operation
        self.values = values
        self.filters = []

    def select(self, *_args):
        self.operation = "select"
        return self

    def update(self, values):
        self.operation = "update"
        self.values = dict(values)
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, _count):
        return self

    def execute(self):
        rows = [
            profile
            for profile in self.client.profiles
            if all(profile.get(column) == value for column, value in self.filters)
        ]
        if self.operation == "select":
            return SimpleNamespace(data=[dict(profile) for profile in rows])
        if self.operation == "update":
            self.client.profile_updates.append({
                "values": self.values,
                "filters": list(self.filters),
            })
            if self.client.profile_update_error:
                raise self.client.profile_update_error
            for profile in rows:
                profile.update(self.values)
            return SimpleNamespace(data=[dict(profile) for profile in rows])
        raise AssertionError(f"unexpected profile operation: {self.operation}")


class _FakeAdminAuth:
    def __init__(self, client):
        self.client = client

    def create_user(self, attributes):
        self.client.create_calls.append(dict(attributes))
        if self.client.create_user_error:
            raise self.client.create_user_error
        user = _auth_user(
            ADMIN_ID,
            attributes["email"],
            user_metadata=attributes.get("user_metadata") or {},
            app_metadata=attributes.get("app_metadata") or {},
        )
        self.client.auth_users.append(user)
        self.client.profiles.append({
            "id": ADMIN_ID,
            "username": user.user_metadata.get("username"),
            "email": attributes["email"],
        })
        return SimpleNamespace(user=user)

    def list_users(self, *, page, per_page):
        self.client.list_calls.append((page, per_page))
        if self.client.list_users_error:
            raise self.client.list_users_error
        return list(self.client.auth_users) if page == 1 else []

    def get_user_by_id(self, user_id):
        self.client.get_calls.append(user_id)
        users = [user for user in self.client.auth_users if user.id == user_id]
        return SimpleNamespace(user=users[0] if len(users) == 1 else None)

    def update_user_by_id(self, user_id, attributes):
        self.client.update_calls.append((user_id, dict(attributes)))
        users = [user for user in self.client.auth_users if user.id == user_id]
        user = users[0] if len(users) == 1 else SimpleNamespace(id=user_id)
        if "email" in attributes:
            user.email = attributes["email"]
        for field in ("user_metadata", "app_metadata"):
            if field in attributes:
                current = dict(getattr(user, field, {}) or {})
                current.update(attributes[field])
                setattr(user, field, current)
        return SimpleNamespace(user=user)

    def delete_user(self, user_id):
        self.client.delete_calls.append(user_id)
        if self.client.delete_user_error:
            raise self.client.delete_user_error
        self.client.auth_users = [
            user for user in self.client.auth_users if user.id != user_id
        ]
        self.client.profiles = [
            profile for profile in self.client.profiles
            if profile.get("id") != user_id
        ]


class _FakeClient:
    def __init__(
        self,
        profile=None,
        *,
        auth_users=None,
        profile_update_error=None,
        list_users_error=None,
        create_user_error=None,
        delete_user_error=None,
    ):
        self.profiles = [dict(profile)] if profile else []
        if auth_users is None and profile:
            auth_users = [SimpleNamespace(
                id=str(profile["id"]),
                email=str(profile.get("email") or ""),
                user_metadata={"username": profile.get("username")},
                app_metadata=(
                    {"app_role": "admin"}
                    if profile.get("username") == "admin"
                    else {}
                ),
            )]
        self.auth_users = list(auth_users or [])
        self.profile_update_error = profile_update_error
        self.list_users_error = list_users_error
        self.create_user_error = create_user_error
        self.delete_user_error = delete_user_error
        self.create_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.profile_updates = []
        self.list_calls = []
        self.get_calls = []
        self.auth = SimpleNamespace(admin=_FakeAdminAuth(self))

    def table(self, name):
        assert name == "account_profiles"
        return _ProfileQuery(self)


def _arrange_cli(monkeypatch, client, *, email="Admin@Example.COM", passwords=()):
    password_values = iter(passwords or ("very-secure-password",) * 2)
    monkeypatch.setattr(bootstrap_admin, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bootstrap_admin,
        "parse_args",
        lambda: SimpleNamespace(email=email),
    )
    monkeypatch.setattr(
        bootstrap_admin.getpass,
        "getpass",
        lambda _prompt: next(password_values),
    )
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setattr(
        bootstrap_admin,
        "create_client",
        lambda *_args, **_kwargs: client,
    )


def test_creates_reserved_admin_with_server_controlled_attributes(monkeypatch, capsys):
    client = _FakeClient()
    _arrange_cli(monkeypatch, client, passwords=("Ab1!5678", "Ab1!5678"))
    monkeypatch.setattr(
        bootstrap_admin.secrets,
        "token_hex",
        lambda _size: "0123456789ab",
    )

    bootstrap_admin.main()

    assert client.create_calls == [{
        "email": "admin@example.com",
        "password": "Ab1!5678",
        "email_confirm": True,
        "user_metadata": {
            "username": "admin-bootstrap-0123456789ab",
            "email": "admin@example.com",
        },
    }]
    assert client.update_calls == [(ADMIN_ID, {
        "user_metadata": {
            "username": "admin",
            "email": "admin@example.com",
        },
        "app_metadata": {"app_role": "admin"},
    })]
    assert client.profile_updates == [{
        "values": {"username": "admin", "email": "admin@example.com"},
        "filters": [
            ("id", ADMIN_ID),
            ("username", "admin-bootstrap-0123456789ab"),
        ],
    }]
    assert client.delete_calls == []
    assert (
        capsys.readouterr().out.strip()
        == f"Admin account created: username=admin, email=admin@example.com, id={ADMIN_ID}"
    )


def test_new_admin_promotion_failure_removes_temporary_account(monkeypatch):
    client = _FakeClient(profile_update_error=RuntimeError("profile update failed"))
    _arrange_cli(monkeypatch, client)
    monkeypatch.setattr(
        bootstrap_admin.secrets,
        "token_hex",
        lambda _size: "0123456789ab",
    )

    with pytest.raises(SystemExit, match="임시 계정을 삭제했습니다"):
        bootstrap_admin.main()

    assert len(client.create_calls) == 1
    assert client.update_calls == [
        (ADMIN_ID, {
            "user_metadata": {
                "username": "admin",
                "email": "admin@example.com",
            },
            "app_metadata": {"app_role": "admin"},
        }),
        (ADMIN_ID, {
            "user_metadata": {
                "username": "admin-bootstrap-0123456789ab",
                "email": "admin@example.com",
            },
            "app_metadata": {"app_role": None},
        }),
    ]
    assert client.delete_calls == [ADMIN_ID]
    assert client.auth_users == []
    assert client.profiles == []


def test_updates_existing_admin_auth_and_profile_email(monkeypatch, capsys):
    client = _FakeClient({
        "id": ADMIN_ID,
        "username": "admin",
        "email": "old-admin@example.com",
    })
    _arrange_cli(monkeypatch, client, email="new-admin@example.com")

    bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == [(ADMIN_ID, {
        "email": "new-admin@example.com",
        "password": "very-secure-password",
        "email_confirm": True,
        "user_metadata": {
            "username": "admin",
            "email": "new-admin@example.com",
        },
        "app_metadata": {"app_role": "admin"},
    })]
    assert client.profile_updates == [{
        "values": {"email": "new-admin@example.com"},
        "filters": [("id", ADMIN_ID), ("username", "admin")],
    }]
    assert (
        capsys.readouterr().out.strip()
        == f"Admin account updated: username=admin, email=new-admin@example.com, id={ADMIN_ID}"
    )


def test_promotes_one_exact_existing_auth_email_and_renames_its_profile(
    monkeypatch, capsys
):
    auth_user = _auth_user(
        ADMIN_ID,
        "owner@example.com",
        user_metadata={"username": "owner", "email": "owner@example.com", "theme": "dark"},
        app_metadata={"provider": "email"},
    )
    client = _FakeClient(
        {
            "id": ADMIN_ID,
            "username": "owner",
            "email": "owner@example.com",
        },
        auth_users=[auth_user],
    )
    _arrange_cli(monkeypatch, client, email="OWNER@example.com")

    bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == [
        (ADMIN_ID, {
            "email": "owner@example.com",
            "password": "very-secure-password",
            "email_confirm": True,
        }),
        (ADMIN_ID, {
            "user_metadata": {
                "username": "admin",
                "email": "owner@example.com",
                "theme": "dark",
            },
            "app_metadata": {
                "provider": "email",
                "app_role": "admin",
            },
        }),
    ]
    assert client.profile_updates == [{
        "values": {"username": "admin", "email": "owner@example.com"},
        "filters": [("id", ADMIN_ID), ("username", "owner")],
    }]
    assert client.profiles == [{
        "id": ADMIN_ID,
        "username": "admin",
        "email": "owner@example.com",
    }]
    assert (
        capsys.readouterr().out.strip()
        == f"Admin account promoted: username=admin, email=owner@example.com, id={ADMIN_ID}"
    )


def test_stops_on_ambiguous_exact_email_matches(monkeypatch):
    client = _FakeClient(auth_users=[
        _auth_user(ADMIN_ID, "duplicate@example.com"),
        _auth_user(SECOND_ID, "DUPLICATE@example.com", username="bob"),
    ])
    _arrange_cli(monkeypatch, client, email="duplicate@example.com")

    with pytest.raises(SystemExit, match="정확히 일치하는 Auth 사용자가 둘 이상"):
        bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == []
    assert client.profile_updates == []


def test_existing_auth_email_without_profile_stops_before_promotion(monkeypatch):
    client = _FakeClient(auth_users=[
        _auth_user(ADMIN_ID, "owner@example.com", username="owner"),
    ])
    _arrange_cli(monkeypatch, client, email="owner@example.com")

    with pytest.raises(SystemExit, match="account_profiles 행이 없습니다"):
        bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == []
    assert client.profile_updates == []


def test_existing_admin_does_not_take_email_owned_by_another_auth_user(monkeypatch):
    client = _FakeClient(
        {
            "id": ADMIN_ID,
            "username": "admin",
            "email": "old-admin@example.com",
        },
        auth_users=[
            _auth_user(
                ADMIN_ID,
                "old-admin@example.com",
                username="admin",
                app_metadata={"app_role": "admin"},
            ),
            _auth_user(SECOND_ID, "claimed@example.com", username="bob"),
        ],
    )
    _arrange_cli(monkeypatch, client, email="claimed@example.com")

    with pytest.raises(SystemExit, match="다른 Auth 사용자에게 속해 있습니다"):
        bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == []
    assert client.profile_updates == []


def test_profile_conflict_rolls_back_promoted_metadata(monkeypatch):
    auth_user = _auth_user(
        ADMIN_ID,
        "owner@example.com",
        username="owner",
        app_metadata={"provider": "email"},
    )
    client = _FakeClient(
        {
            "id": ADMIN_ID,
            "username": "owner",
            "email": "owner@example.com",
        },
        auth_users=[auth_user],
        profile_update_error=RuntimeError("unique username conflict"),
    )
    _arrange_cli(monkeypatch, client, email="owner@example.com")

    with pytest.raises(SystemExit, match="metadata를 원래대로"):
        bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == [
        (ADMIN_ID, {
            "email": "owner@example.com",
            "password": "very-secure-password",
            "email_confirm": True,
        }),
        (ADMIN_ID, {
            "user_metadata": {
                "username": "admin",
                "email": "owner@example.com",
            },
            "app_metadata": {
                "provider": "email",
                "app_role": "admin",
            },
        }),
        (ADMIN_ID, {
            "user_metadata": {
                "username": "owner",
                "email": "owner@example.com",
            },
            "app_metadata": {
                "provider": "email",
                "app_role": None,
            },
        }),
    ]


def test_auth_user_list_error_stops_before_any_mutation(monkeypatch):
    client = _FakeClient(list_users_error=RuntimeError("auth unavailable"))
    _arrange_cli(monkeypatch, client)

    with pytest.raises(SystemExit, match="사용자 목록을 안전하게 확인하지 못했습니다"):
        bootstrap_admin.main()

    assert client.create_calls == []
    assert client.update_calls == []
    assert client.profile_updates == []


@pytest.mark.parametrize(
    ("passwords", "message"),
    [
        (("short7!", "short7!"), "Admin password must be 8-128 characters."),
        (("first-password", "second-password"), "Passwords do not match."),
    ],
)
def test_rejects_short_or_mismatched_password_before_connecting(
    monkeypatch, passwords, message
):
    client = _FakeClient()
    _arrange_cli(monkeypatch, client, passwords=passwords)
    connect_attempted = False

    def unexpected_create_client(*_args, **_kwargs):
        nonlocal connect_attempted
        connect_attempted = True
        raise AssertionError("invalid passwords must be rejected before network setup")

    monkeypatch.setattr(bootstrap_admin, "create_client", unexpected_create_client)

    with pytest.raises(SystemExit, match=message):
        bootstrap_admin.main()

    assert connect_attempted is False
    assert client.create_calls == []
    assert client.update_calls == []


def test_reports_non_utf8_password_input_without_traceback(monkeypatch):
    client = _FakeClient()
    _arrange_cli(monkeypatch, client)
    monkeypatch.setattr(
        bootstrap_admin.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
        ),
    )

    with pytest.raises(SystemExit, match="한글을 제외하고 영문, 숫자"):
        bootstrap_admin.main()

    assert client.create_calls == []


class _AdminApiError(Exception):
    def __init__(self, message, *, code="unexpected_failure", status=500):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def test_format_admin_error_gives_actionable_weak_password_guidance():
    error = _AdminApiError(
        "Password should contain at least one character of each type",
        code="weak_password",
        status=422,
    )

    message = bootstrap_admin.format_admin_error(error)

    assert "비밀번호" in message
    assert "정책" in message
    assert "다시" in message
    assert "이메일 중복과 Supabase Auth 설정" not in message


def test_format_admin_error_explains_database_trigger_migrations_and_diagnostics(
    monkeypatch,
):
    service_key = "service-role-secret-that-must-not-leak"
    publishable_key = "publishable-secret-that-must-not-leak"
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", service_key)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", publishable_key)
    error = _AdminApiError(
        "Database error creating new user in trigger "
        "auth_users_require_managed_signup; "
        f"service={service_key}; publishable={publishable_key}; "
        + ("upstream diagnostic " * 100),
        code="unexpected_failure",
        status=500,
    )

    message = bootstrap_admin.format_admin_error(error)

    assert "migration_remove_legacy_signup_guard.sql" in message
    assert "migration_auth_accounts.sql" in message
    assert "500" in message
    assert "unexpected_failure" in message
    assert "database error creating new user" in message.lower()
    assert "trigger" in message.lower()
    assert service_key not in message
    assert publishable_key not in message
    assert message.count("[redacted]") >= 2
    assert len(message) <= 600


def test_main_surfaces_sanitized_admin_create_error(monkeypatch):
    service_key = "bootstrap-service-role-secret"
    error = _AdminApiError(
        "Database error saving new user from create trigger; "
        f"credential={service_key}",
        code="unexpected_failure",
        status=500,
    )
    client = _FakeClient(create_user_error=error)
    _arrange_cli(monkeypatch, client)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", service_key)

    with pytest.raises(SystemExit) as raised:
        bootstrap_admin.main()

    message = str(raised.value)
    assert "migration_remove_legacy_signup_guard.sql" in message
    assert "migration_auth_accounts.sql" in message
    assert "unexpected_failure" in message
    assert service_key not in message
    assert "[redacted]" in message
    assert len(message) <= 600
    assert len(client.create_calls) == 1
    assert client.update_calls == []
