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
        return SimpleNamespace(user=SimpleNamespace(id=ADMIN_ID))

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


class _FakeClient:
    def __init__(
        self,
        profile=None,
        *,
        auth_users=None,
        profile_update_error=None,
        list_users_error=None,
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
        self.create_calls = []
        self.update_calls = []
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
    _arrange_cli(monkeypatch, client)

    bootstrap_admin.main()

    assert client.create_calls == [{
        "email": "admin@example.com",
        "password": "very-secure-password",
        "email_confirm": True,
        "user_metadata": {
            "username": "admin",
            "email": "admin@example.com",
        },
        "app_metadata": {"app_role": "admin"},
    }]
    assert client.update_calls == []
    assert client.profile_updates == []
    assert (
        capsys.readouterr().out.strip()
        == f"Admin account created: username=admin, email=admin@example.com, id={ADMIN_ID}"
    )


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
        (("too-short", "too-short"), "Admin password must be 12-128 characters."),
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
