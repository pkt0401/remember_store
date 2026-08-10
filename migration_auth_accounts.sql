-- 기존 Supabase 프로젝트의 Auth 계정/아이디 매핑 마이그레이션입니다.
-- Supabase SQL Editor에서 한 번 실행하세요. 재실행해도 기존 프로필은 보존됩니다.

begin;

-- 과거의 BEFORE INSERT 가드는 GoTrue가 auth.users 행을 만드는 시점에
-- 호출 경로를 안정적으로 구분할 수 없어 정상 signUp까지 차단했습니다.
-- auth.users 소유권이나 다른 종속 객체를 건드리지 않고 함수만 중립화합니다.
create or replace function public.require_managed_auth_signup()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  return new;
end;
$$;

alter function public.require_managed_auth_signup() owner to postgres;
revoke all on function public.require_managed_auth_signup()
  from public, anon, authenticated;

-- Supabase Auth의 불변 UUID를 애플리케이션 로그인 ID와 연결합니다.
-- email은 로그인 해석에만 쓰는 비공개 값이며 RLS로 본인과 service_role 외에는
-- 읽을 수 없습니다. username은 소문자로 정규화되고 대소문자를 구분하지 않습니다.
create table if not exists public.account_profiles (
  id uuid primary key
    references auth.users(id) on delete cascade,
  username text not null,
  email text,
  created_at timestamptz not null default now(),
  constraint account_profiles_username_format_check
    check (
      username = lower(btrim(username))
      and username ~ '^[a-z0-9][a-z0-9._-]{2,31}$'
    ),
  constraint account_profiles_username_key unique (username)
);

-- CREATE TABLE IF NOT EXISTS는 기존 owner를 바꾸지 않으므로, SECURITY
-- DEFINER 함수가 FORCE RLS를 우회할 수 있는 Supabase postgres로 복구합니다.
alter table public.account_profiles owner to postgres;

comment on table public.account_profiles is
  'Private mapping from an application username to a Supabase Auth UUID.';
comment on column public.account_profiles.email is
  'Private login email; never grant cross-user or anonymous read access.';

-- 대소문자 변형을 포함한 admin 예약어는 app_metadata.app_role=admin인
-- auth 사용자에게만 허용합니다. raw_user_meta_data는 사용자가 바꿀 수 있으므로
-- 권한 판정에는 절대 사용하지 않습니다.
create or replace function public.validate_account_profile()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  auth_app_role text;
  auth_email text;
begin
  new.username := lower(btrim(new.username));
  new.email := nullif(lower(btrim(new.email)), '');

  if new.username is null
     or new.username !~ '^[a-z0-9][a-z0-9._-]{2,31}$' then
    raise exception using
      errcode = '22023',
      message = '아이디는 영문 소문자나 숫자로 시작하고, 영문 소문자·숫자·점·밑줄·하이픈으로 된 3~32자여야 합니다.';
  end if;

  select
    lower(nullif(btrim(u.raw_app_meta_data ->> 'app_role'), '')),
    nullif(lower(btrim(u.email)), '')
  into auth_app_role, auth_email
  from auth.users as u
  where u.id = new.id;

  if not found then
    raise exception using
      errcode = '23503',
      message = '계정 프로필에 연결할 Supabase Auth 사용자가 없습니다.';
  end if;

  -- user metadata는 사용자가 바꿀 수 있으므로 Auth가 검증한 email을 우선합니다.
  new.email := coalesce(auth_email, new.email);

  if new.username = 'admin' and coalesce(auth_app_role, '') <> 'admin' then
    raise exception using
      errcode = '42501',
      message = 'admin 아이디는 관리자 계정에만 사용할 수 있습니다.';
  end if;

  return new;
end;
$$;

-- CREATE OR REPLACE FUNCTION도 기존 owner를 보존하므로 명시적으로 복구합니다.
alter function public.validate_account_profile() owner to postgres;

drop trigger if exists account_profiles_validate on public.account_profiles;
create trigger account_profiles_validate
before insert or update on public.account_profiles
for each row execute function public.validate_account_profile();

-- 새 Auth 사용자는 가입 요청의 user metadata에서 username/email을 받아
-- 프로필을 만듭니다. username을 보내지 않는 Dashboard/API 생성은 충돌하지 않는
-- UUID 접미사 ID로 안전하게 대체합니다. 첫 관리자 계정은 admin을 기본값으로 씁니다.
create or replace function public.handle_new_auth_user_account_profile()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  requested_username text;
  profile_email text;
  base_username text;
  auth_app_role text;
begin
  requested_username := nullif(
    lower(btrim(new.raw_user_meta_data ->> 'username')),
    ''
  );
  profile_email := nullif(
    lower(btrim(coalesce(
      new.email,
      nullif(new.raw_user_meta_data ->> 'email', '')
    ))),
    ''
  );
  auth_app_role := lower(nullif(
    btrim(new.raw_app_meta_data ->> 'app_role'),
    ''
  ));

  if requested_username is null then
    if auth_app_role = 'admin'
       and not exists (
         select 1
         from public.account_profiles
         where username = 'admin'
       ) then
      requested_username := 'admin';
    else
      base_username := regexp_replace(
        lower(split_part(coalesce(profile_email, ''), '@', 1)),
        '[^a-z0-9._-]+',
        '',
        'g'
      );
      base_username := regexp_replace(base_username, '^[._-]+', '');
      if length(base_username) < 3 then
        base_username := 'user';
      end if;
      requested_username := left(base_username, 23)
        || '-' || substr(replace(new.id::text, '-', ''), 1, 8);
    end if;
  end if;

  insert into public.account_profiles (id, username, email, created_at)
  values (
    new.id,
    requested_username,
    profile_email,
    coalesce(new.created_at, now())
  );

  return new;
end;
$$;

alter function public.handle_new_auth_user_account_profile() owner to postgres;

-- postgres는 auth.users에 TRIGGER 권한은 있지만 relation owner가 아닐 수
-- 있으므로 DROP TRIGGER 대신 기존 트리거를 제자리에서 교체합니다.
create or replace trigger on_auth_user_created_account_profile
after insert on auth.users
for each row execute function public.handle_new_auth_user_account_profile();

-- Auth에서 확인된 email이 변경되면 비공개 로그인 매핑도 함께 갱신합니다.
create or replace function public.sync_auth_user_account_profile_email()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.account_profiles
  set email = nullif(lower(btrim(new.email)), '')
  where id = new.id;

  return new;
end;
$$;

alter function public.sync_auth_user_account_profile_email() owner to postgres;

create or replace trigger auth_users_sync_account_profile_email
after update of email on auth.users
for each row
when (old.email is distinct from new.email)
execute function public.sync_auth_user_account_profile_email();

-- admin 프로필을 가진 사용자의 관리자 권한을 먼저 제거하면 예약어 규칙이
-- 깨지므로 차단합니다. 필요하면 service_role로 username을 먼저 바꾼 뒤 강등합니다.
create or replace function public.protect_admin_account_profile_role()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if exists (
    select 1
    from public.account_profiles as p
    where p.id = new.id
      and p.username = 'admin'
  ) and coalesce(
    lower(nullif(btrim(new.raw_app_meta_data ->> 'app_role'), '')),
    ''
  ) <> 'admin' then
    raise exception using
      errcode = '42501',
      message = 'admin 아이디의 관리자 권한을 제거하기 전에 아이디를 변경해야 합니다.';
  end if;

  return new;
end;
$$;

alter function public.protect_admin_account_profile_role() owner to postgres;

create or replace trigger auth_users_protect_admin_profile_role
before update of raw_app_meta_data on auth.users
for each row execute function public.protect_admin_account_profile_role();

-- 이미 존재하는 Auth 사용자도 빠짐없이 프로필을 갖게 합니다. 첫 admin 역할
-- 사용자는 admin을 받고, 나머지는 기존 metadata/email을 정규화한 뒤 UUID 기반
-- 접미사로 충돌을 해소합니다. 기존 프로필은 절대 덮어쓰지 않습니다.
do $account_backfill$
declare
  auth_user record;
  requested_username text;
  base_username text;
  candidate_username text;
  profile_email text;
  suffix text;
  collision_number integer;
begin
  for auth_user in
    select
      u.id,
      u.email,
      u.raw_user_meta_data,
      u.raw_app_meta_data,
      u.created_at
    from auth.users as u
    left join public.account_profiles as p on p.id = u.id
    where p.id is null
    order by
      case
        when lower(coalesce(u.raw_app_meta_data ->> 'app_role', '')) = 'admin'
          then 0
        else 1
      end,
      u.created_at,
      u.id
  loop
    profile_email := nullif(
      lower(btrim(coalesce(
        auth_user.email,
        nullif(auth_user.raw_user_meta_data ->> 'email', '')
      ))),
      ''
    );
    requested_username := nullif(
      lower(btrim(auth_user.raw_user_meta_data ->> 'username')),
      ''
    );

    if lower(coalesce(auth_user.raw_app_meta_data ->> 'app_role', '')) = 'admin'
       and not exists (
         select 1
         from public.account_profiles
         where username = 'admin'
       ) then
      candidate_username := 'admin';
    elsif requested_username ~ '^[a-z0-9][a-z0-9._-]{2,31}$'
          and requested_username <> 'admin' then
      candidate_username := requested_username;
    else
      candidate_username := null;
    end if;

    base_username := regexp_replace(
      lower(split_part(coalesce(profile_email, ''), '@', 1)),
      '[^a-z0-9._-]+',
      '',
      'g'
    );
    base_username := regexp_replace(base_username, '^[._-]+', '');
    if length(base_username) < 3 then
      base_username := 'user';
    end if;
    base_username := left(base_username, 23);

    if candidate_username is null
       or exists (
         select 1
         from public.account_profiles
         where username = candidate_username
       ) then
      suffix := '-' || substr(replace(auth_user.id::text, '-', ''), 1, 8);
      candidate_username := left(base_username, 32 - length(suffix)) || suffix;
    end if;

    collision_number := 0;
    while exists (
      select 1
      from public.account_profiles
      where username = candidate_username
    ) loop
      collision_number := collision_number + 1;
      suffix := '-' || substr(
        md5(auth_user.id::text || ':' || collision_number::text),
        1,
        8
      );
      candidate_username := left(base_username, 32 - length(suffix)) || suffix;
    end loop;

    insert into public.account_profiles (id, username, email, created_at)
    values (
      auth_user.id,
      candidate_username,
      profile_email,
      coalesce(auth_user.created_at, now())
    )
    on conflict (id) do nothing;
  end loop;
end;
$account_backfill$;

alter table public.account_profiles enable row level security;
alter table public.account_profiles force row level security;

-- 과거의 permissive 정책이 남아 있어도 이메일이나 다른 계정이 노출되지 않게
-- 모든 정책을 제거한 뒤 본인 조회 정책 하나만 만듭니다.
do $account_policies$
declare
  existing_policy record;
begin
  for existing_policy in
    select schemaname, tablename, policyname
    from pg_policies
    where schemaname = 'public'
      and tablename = 'account_profiles'
  loop
    execute format(
      'drop policy %I on %I.%I',
      existing_policy.policyname,
      existing_policy.schemaname,
      existing_policy.tablename
    );
  end loop;
end;
$account_policies$;

create policy account_profiles_own_select
on public.account_profiles
for select
to authenticated
using (id = (select auth.uid()));

revoke all privileges on table public.account_profiles
  from public, anon, authenticated;
grant select on table public.account_profiles to authenticated;
grant select, insert, update, delete on table public.account_profiles
  to service_role;

revoke all on function public.validate_account_profile()
  from public, anon, authenticated;
revoke all on function public.handle_new_auth_user_account_profile()
  from public, anon, authenticated;
revoke all on function public.sync_auth_user_account_profile_email()
  from public, anon, authenticated;
revoke all on function public.protect_admin_account_profile_role()
  from public, anon, authenticated;

commit;
