#!/usr/bin/env python3
"""Create or rotate the reserved ``admin`` Supabase Auth account safely."""

import argparse
import getpass
import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client
from supabase.client import ClientOptions


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BootstrapSafetyError(RuntimeError):
    """A deliberate stop whose message should be shown to the operator."""


def format_admin_error(exc):
    """Return an actionable Supabase error without exposing configured keys."""
    def finish(message):
        return message[:600]

    code = str(getattr(exc, "code", "") or "").strip()
    status = str(getattr(exc, "status", "") or "").strip()
    detail = str(getattr(exc, "message", "") or str(exc) or "").strip()
    detail = re.sub(r"[\r\n\t]+", " ", detail)
    detail = re.sub(r"\s{2,}", " ", detail)
    for name in ("SUPABASE_SERVICE_KEY", "SUPABASE_PUBLISHABLE_KEY"):
        secret = os.getenv(name, "").strip()
        if secret:
            detail = detail.replace(secret, "[redacted]")
    detail = re.sub(
        r"(?i)(bearer\s+)[a-z0-9._~-]{20,}",
        r"\1[redacted]",
        detail,
    )[:600]

    fingerprint = " ".join((code, status, detail)).lower()
    context = ", ".join(
        part for part in (
            f"code={code}" if code else "",
            f"status={status}" if status else "",
            detail,
        )
        if part
    )
    suffix = f" ({context})" if context else ""

    if any(
        marker in fingerprint
        for marker in (
            "weak_password",
            "weak password",
            "password should",
            "password must",
            "password is too",
        )
    ):
        return finish(
            "관리자 비밀번호가 Supabase 비밀번호 보안 정책을 충족하지 않습니다. "
            "8자 이상으로 대·소문자, 숫자, 특수문자를 조합해 다시 실행하세요."
            f"{suffix}"
        )

    if any(
        marker in fingerprint
        for marker in (
            "email_exists",
            "already registered",
            "already been registered",
            "user already exists",
        )
    ):
        return finish(
            "해당 이메일을 사용하는 Supabase Auth 계정이 이미 있습니다. "
            "계정 목록과 account_profiles 연결 상태를 확인하세요."
            f"{suffix}"
        )

    if status == "500" or any(
        marker in fingerprint
        for marker in (
            "database error",
            "db error",
            "trigger",
            "unexpected_failure",
        )
    ):
        return finish(
            "Supabase Auth 데이터베이스 트리거 오류로 관리자 생성에 실패했습니다. "
            "SQL Editor에서 migration_remove_legacy_signup_guard.sql과 "
            "migration_auth_accounts.sql을 순서대로 실행한 뒤 다시 시도하세요."
            f"{suffix}"
        )

    return finish(
        "관리자 생성/갱신에 실패했습니다. Supabase Auth 설정과 서버 로그를 "
        f"확인하세요.{suffix}"
    )


def admin_profile(client):
    result = (
        client.table("account_profiles")
        .select("id,username,email")
        .eq("username", "admin")
        .limit(2)
        .execute()
    )
    rows = result.data or []
    if len(rows) > 1:
        raise RuntimeError("admin 프로필이 둘 이상입니다.")
    return rows[0] if rows else None


def account_profile_by_id(client, user_id):
    result = (
        client.table("account_profiles")
        .select("id,username,email")
        .eq("id", user_id)
        .limit(2)
        .execute()
    )
    rows = result.data or []
    if len(rows) > 1:
        raise RuntimeError("하나의 Auth 사용자에 프로필이 둘 이상입니다.")
    return rows[0] if rows else None


def auth_users_by_email(client, email):
    """Return distinct Auth users whose normalized email exactly matches email."""
    matches = {}
    seen_pages = set()
    page = 1
    per_page = 1000

    while True:
        users = list(client.auth.admin.list_users(page=page, per_page=per_page) or [])
        if not users:
            break

        page_ids = tuple(str(getattr(user, "id", "") or "") for user in users)
        if page_ids in seen_pages:
            raise RuntimeError("Auth 사용자 목록 페이지가 진행되지 않습니다.")
        seen_pages.add(page_ids)

        for user in users:
            user_email = str(getattr(user, "email", "") or "").strip().lower()
            if user_email != email:
                continue
            user_id = str(getattr(user, "id", "") or "")
            if not user_id:
                raise RuntimeError("이메일이 일치하는 Auth 사용자에게 UUID가 없습니다.")
            matches[user_id] = user
        page += 1

    return list(matches.values())


def auth_user_by_id(client, user_id):
    result = client.auth.admin.get_user_by_id(user_id)
    user = getattr(result, "user", None)
    returned_id = str(getattr(user, "id", "") or "")
    if not user or returned_id != user_id:
        raise RuntimeError("admin 프로필에 연결된 Auth 사용자를 확인하지 못했습니다.")
    return user


def metadata_dict(user, field):
    value = getattr(user, field, None)
    return dict(value) if isinstance(value, dict) else {}


def admin_attributes(email, password, *, existing_user=None):
    attributes = admin_metadata_attributes(email, existing_user=existing_user)
    attributes.update({
        "email": email,
        "password": password,
        "email_confirm": True,
    })
    return attributes


def admin_metadata_attributes(email, *, existing_user=None):
    user_metadata = metadata_dict(existing_user, "user_metadata")
    app_metadata = metadata_dict(existing_user, "app_metadata")
    user_metadata.update({"username": "admin", "email": email})
    app_metadata["app_role"] = "admin"
    return {
        "user_metadata": user_metadata,
        "app_metadata": app_metadata,
    }


def temporary_admin_username():
    return f"admin-bootstrap-{secrets.token_hex(6)}"


def temporary_admin_attributes(email, password, username):
    # The profile trigger can safely create this non-reserved identity before
    # the trusted admin app_metadata is persisted by GoTrue.
    return {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"username": username, "email": email},
    }


def update_profile(client, user_id, original_username, values):
    result = (
        client.table("account_profiles")
        .update(values)
        .eq("id", user_id)
        .eq("username", original_username)
        .execute()
    )
    rows = result.data or []
    if len(rows) != 1 or str(rows[0].get("id", "") or "") != user_id:
        raise RuntimeError("대상 account_profiles 행을 정확히 하나 갱신하지 못했습니다.")


def rollback_promoted_metadata(client, user_id, original_user_metadata, original_app_metadata):
    # Auth metadata updates may merge JSON. Explicit nulls remove the effective
    # application role/username even when those keys were absent originally.
    user_metadata = dict(original_user_metadata)
    app_metadata = dict(original_app_metadata)
    user_metadata.setdefault("username", None)
    user_metadata.setdefault("email", None)
    app_metadata.setdefault("app_role", None)
    client.auth.admin.update_user_by_id(user_id, {
        "user_metadata": user_metadata,
        "app_metadata": app_metadata,
    })


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create or update the reserved Memory Agent admin account."
    )
    parser.add_argument(
        "--email",
        help="Admin recovery/login email. The visible application ID remains 'admin'.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    email = (args.email or input("Admin email: ")).strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise SystemExit("Enter a valid admin email address.")

    try:
        password = getpass.getpass("Admin password (8-128 characters): ")
        confirmation = getpass.getpass("Confirm admin password: ")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            "비밀번호 입력 인코딩을 읽지 못했습니다. 한글을 제외하고 영문, 숫자, "
            "ASCII 특수문자로 직접 입력하세요."
        ) from exc
    if not 8 <= len(password) <= 128:
        raise SystemExit("Admin password must be 8-128 characters.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not service_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required in .env.")

    client = create_client(
        url,
        service_key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )
    try:
        existing = admin_profile(client)
        email_matches = auth_users_by_email(client, email)
    except Exception as exc:
        raise SystemExit(
            "계정 프로필/Auth 사용자 목록을 안전하게 확인하지 못했습니다. "
            "먼저 migration_auth_accounts.sql과 Supabase 연결을 확인하세요."
        ) from exc

    if len(email_matches) > 1:
        raise SystemExit(
            "입력한 이메일과 정확히 일치하는 Auth 사용자가 둘 이상이라 중단합니다."
        )
    email_user = email_matches[0] if email_matches else None

    try:
        if existing:
            user_id = str(existing["id"])
            existing_user = auth_user_by_id(client, user_id)
            email_user_id = str(getattr(email_user, "id", "") or "")
            if email_user and email_user_id != user_id:
                raise BootstrapSafetyError(
                    "입력한 이메일이 현재 admin과 다른 Auth 사용자에게 속해 있습니다."
                )
            attributes = admin_attributes(
                email,
                password,
                existing_user=existing_user,
            )
            result = client.auth.admin.update_user_by_id(user_id, attributes)
            update_profile(client, user_id, "admin", {"email": email})
            action = "updated"
        elif email_user:
            user_id = str(getattr(email_user, "id", "") or "")
            profile = account_profile_by_id(client, user_id)
            if not profile:
                raise BootstrapSafetyError(
                    "기존 Auth 사용자에 연결된 account_profiles 행이 없습니다."
                )

            original_username = str(profile.get("username", "") or "")
            original_user_metadata = metadata_dict(email_user, "user_metadata")
            original_app_metadata = metadata_dict(email_user, "app_metadata")

            # 먼저 비밀번호를 교체해 기존 자격 증명을 차단한 후 역할을 승격합니다.
            client.auth.admin.update_user_by_id(user_id, {
                "email": email,
                "password": password,
                "email_confirm": True,
            })
            promoted_attributes = admin_attributes(
                email,
                password,
                existing_user=email_user,
            )
            client.auth.admin.update_user_by_id(user_id, {
                "user_metadata": promoted_attributes["user_metadata"],
                "app_metadata": promoted_attributes["app_metadata"],
            })
            try:
                update_profile(
                    client,
                    user_id,
                    original_username,
                    {"username": "admin", "email": email},
                )
            except Exception as profile_exc:
                try:
                    rollback_promoted_metadata(
                        client,
                        user_id,
                        original_user_metadata,
                        original_app_metadata,
                    )
                except Exception as rollback_exc:
                    raise BootstrapSafetyError(
                        "프로필 승격과 권한 롤백이 모두 실패했습니다. "
                        "Supabase에서 해당 사용자의 app_role을 즉시 확인하세요."
                    ) from rollback_exc
                raise BootstrapSafetyError(
                    "프로필 승격에 실패해 app_role과 사용자 metadata를 원래대로 "
                    "돌렸습니다. 보안을 위해 비밀번호는 새 값으로 유지됩니다."
                ) from profile_exc
            action = "promoted"
        else:
            temporary_username = temporary_admin_username()
            attributes = temporary_admin_attributes(
                email,
                password,
                temporary_username,
            )
            result = client.auth.admin.create_user(attributes)
            user_id = str(getattr(result.user, "id", "") or "")
            if not user_id:
                raise BootstrapSafetyError(
                    "Supabase가 생성된 임시 관리자 계정의 UUID를 반환하지 않았습니다."
                )

            created_user = result.user
            original_user_metadata = metadata_dict(created_user, "user_metadata")
            original_app_metadata = metadata_dict(created_user, "app_metadata")
            try:
                client.auth.admin.update_user_by_id(
                    user_id,
                    admin_metadata_attributes(email, existing_user=created_user),
                )
                update_profile(
                    client,
                    user_id,
                    temporary_username,
                    {"username": "admin", "email": email},
                )
            except Exception as promotion_exc:
                rollback_error = None
                try:
                    rollback_promoted_metadata(
                        client,
                        user_id,
                        original_user_metadata,
                        original_app_metadata,
                    )
                except Exception as exc:
                    rollback_error = exc

                try:
                    client.auth.admin.delete_user(user_id)
                except Exception as delete_exc:
                    if rollback_error:
                        raise BootstrapSafetyError(
                            "관리자 승격, 권한 롤백, 임시 계정 삭제가 모두 실패했습니다. "
                            f"Supabase에서 사용자 {user_id}를 즉시 확인하세요."
                        ) from delete_exc
                    raise BootstrapSafetyError(
                        "관리자 승격에 실패했고 권한은 회수했지만 임시 계정을 "
                        f"삭제하지 못했습니다. Supabase에서 사용자 {user_id}를 "
                        "삭제하세요."
                    ) from delete_exc

                raise BootstrapSafetyError(
                    "관리자 2단계 승격에 실패해 생성된 임시 계정을 삭제했습니다. "
                    f"{format_admin_error(promotion_exc)}"
                ) from promotion_exc
            action = "created"
    except BootstrapSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        raise SystemExit(format_admin_error(exc)) from exc

    if not user_id:
        raise SystemExit("Supabase가 관리자 UUID를 반환하지 않았습니다.")
    print(f"Admin account {action}: username=admin, email={email}, id={user_id}")


if __name__ == "__main__":
    main()
