-- Supabase SQL Editor에서 신규 프로젝트에 한 번 실행하세요.

begin;

create extension if not exists vector;
create extension if not exists pgcrypto;

-- Supabase Auth의 불변 UUID를 애플리케이션 로그인 ID와 연결합니다.
-- email은 로그인 해석에만 쓰는 비공개 값이며 RLS로 본인과 service_role 외에는
-- 읽을 수 없습니다. username은 소문자로 정규화되고 대소문자를 구분하지 않습니다.
create table if not exists public.account_profiles (
  id uuid primary key
    references auth.users(id) on delete cascade,
  username text not null,
  email text,
  remaining_uses integer not null default 10,
  created_at timestamptz not null default now(),
  constraint account_profiles_username_format_check
    check (
      username = lower(btrim(username))
      and username ~ '^[a-z0-9][a-z0-9._-]{2,31}$'
    ),
  constraint account_profiles_username_key unique (username),
  constraint account_profiles_remaining_uses_check
    check (remaining_uses >= 0)
);

-- CREATE TABLE IF NOT EXISTS는 기존 owner를 바꾸지 않으므로, SECURITY
-- DEFINER 함수가 FORCE RLS를 우회할 수 있는 Supabase postgres로 복구합니다.
alter table public.account_profiles owner to postgres;

-- Existing projects need the new column too. Only NULL values are initialized,
-- so rerunning the schema does not reset a user's current balance.
alter table public.account_profiles
  add column if not exists remaining_uses integer;

update public.account_profiles
set remaining_uses = 10
where remaining_uses is null;

alter table public.account_profiles
  alter column remaining_uses set default 10,
  alter column remaining_uses set not null;

do $migration$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.account_profiles'::regclass
      and conname = 'account_profiles_remaining_uses_check'
  ) then
    alter table public.account_profiles
      add constraint account_profiles_remaining_uses_check
      check (remaining_uses >= 0);
  end if;
end;
$migration$;

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

create or replace function public.consume_ai_use(target_user_id uuid)
returns table (remaining_uses integer)
language plpgsql
security definer
set search_path = ''
as $$
declare
  updated_remaining integer;
begin
  update public.account_profiles as profile
  set remaining_uses = profile.remaining_uses - 1
  where profile.id = target_user_id
    and profile.remaining_uses > 0
  returning profile.remaining_uses into updated_remaining;

  if not found then
    return;
  end if;

  return query select updated_remaining;
end;
$$;

create or replace function public.refund_ai_use(target_user_id uuid)
returns table (remaining_uses integer)
language plpgsql
security definer
set search_path = ''
as $$
declare
  updated_remaining integer;
begin
  update public.account_profiles as profile
  set remaining_uses = profile.remaining_uses + 1
  where profile.id = target_user_id
  returning profile.remaining_uses into updated_remaining;

  if not found then
    return;
  end if;

  return query select updated_remaining;
end;
$$;

create or replace function public.recharge_ai_uses(
  target_user_id uuid,
  actor_user_id uuid,
  refill_count integer default 10
)
returns table (remaining_uses integer)
language plpgsql
security definer
set search_path = ''
as $$
declare
  updated_remaining integer;
  actor_username text;
begin
  if refill_count is null or refill_count < 1 or refill_count > 1000 then
    raise exception using
      errcode = '22023',
      message = '충전 횟수는 1~1000 사이여야 합니다.';
  end if;

  if not exists (
    select 1
    from auth.users as auth_user
    where auth_user.id = actor_user_id
      and lower(coalesce(
        auth_user.raw_app_meta_data ->> 'app_role',
        ''
      )) = 'admin'
  ) then
    raise exception using
      errcode = '42501',
      message = '관리자 권한이 필요합니다.';
  end if;

  select profile.username
  into actor_username
  from public.account_profiles as profile
  where profile.id = actor_user_id;

  if actor_username is null then
    raise exception using
      errcode = '42501',
      message = '관리자 계정 프로필을 확인하지 못했습니다.';
  end if;

  update public.account_profiles as profile
  set remaining_uses = profile.remaining_uses + refill_count
  where profile.id = target_user_id
  returning profile.remaining_uses into updated_remaining;

  if not found then
    return;
  end if;

  insert into public.audit_logs (
    actor,
    actor_user_id,
    role,
    action,
    details
  ) values (
    actor_username,
    actor_user_id,
    'admin',
    'ai_uses_recharge',
    jsonb_build_object(
      'target_user_id', target_user_id,
      'refill_count', refill_count,
      'remaining_uses', updated_remaining
    )
  );

  return query select updated_remaining;
end;
$$;

alter function public.consume_ai_use(uuid) owner to postgres;
alter function public.refund_ai_use(uuid) owner to postgres;
alter function public.recharge_ai_uses(uuid, uuid, integer) owner to postgres;

revoke all on function public.consume_ai_use(uuid)
  from public, anon, authenticated;
revoke all on function public.refund_ai_use(uuid)
  from public, anon, authenticated;
revoke all on function public.recharge_ai_uses(uuid, uuid, integer)
  from public, anon, authenticated;

grant execute on function public.consume_ai_use(uuid) to service_role;
grant execute on function public.refund_ai_use(uuid) to service_role;
grant execute on function public.recharge_ai_uses(uuid, uuid, integer)
  to service_role;

revoke all on function public.validate_account_profile()
  from public, anon, authenticated;
revoke all on function public.handle_new_auth_user_account_profile()
  from public, anon, authenticated;
revoke all on function public.sync_auth_user_account_profile_email()
  from public, anon, authenticated;
revoke all on function public.protect_admin_account_profile_role()
  from public, anon, authenticated;

create table if not exists public.memories (
  id uuid primary key default gen_random_uuid(),
  source text not null default 'note',
  content text not null,
  content_hash text,
  metadata jsonb not null default '{}'::jsonb,
  embedding vector(1536),
  expires_at timestamptz,
  scope text not null default 'shared',
  owner_user_id uuid references auth.users(id) on delete cascade,
  created_by_user_id uuid default auth.uid()
    references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint memories_scope_check
    check (scope in ('shared', 'personal')),
  constraint memories_scope_owner_check
    check (
      (scope = 'shared' and owner_user_id is null)
      or
      (scope = 'personal' and owner_user_id is not null)
    )
);

-- 이미 일부 스키마가 생성된 프로젝트에서도 최종 컬럼 구성을 보장합니다.
alter table public.memories add column if not exists content_hash text;
alter table public.memories add column if not exists expires_at timestamptz;
alter table public.memories add column if not exists updated_at timestamptz default now();
alter table public.memories add column if not exists scope text;
alter table public.memories add column if not exists owner_user_id uuid;
alter table public.memories add column if not exists created_by_user_id uuid;

-- 기존 행은 모두의 기억으로 전환합니다. 이미 범위가 지정된 행은 보존합니다.
update public.memories
set scope = 'shared',
    owner_user_id = null
where scope is null;

alter table public.memories alter column scope set default 'shared';
alter table public.memories alter column scope set not null;
alter table public.memories
  alter column created_by_user_id set default auth.uid();

do $migration$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_scope_check'
  ) then
    alter table public.memories
      add constraint memories_scope_check
      check (scope in ('shared', 'personal'));
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_scope_owner_check'
  ) then
    alter table public.memories
      add constraint memories_scope_owner_check
      check (
        (scope = 'shared' and owner_user_id is null)
        or
        (scope = 'personal' and owner_user_id is not null)
      );
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_owner_user_id_fkey'
  ) then
    alter table public.memories
      add constraint memories_owner_user_id_fkey
      foreign key (owner_user_id)
      references auth.users(id)
      on delete cascade;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_created_by_user_id_fkey'
  ) then
    alter table public.memories
      add constraint memories_created_by_user_id_fkey
      foreign key (created_by_user_id)
      references auth.users(id)
      on delete set null;
  end if;
end;
$migration$;

update public.memories
set updated_at = coalesce(created_at, now())
where updated_at is null;

alter table public.memories alter column updated_at set default now();
alter table public.memories alter column updated_at set not null;

-- OpenAI API 키처럼 보이는 기존 기억은 일반 검색 대상과 물리적으로 분리합니다.
-- 실제 키와 그 임베딩은 남기지 않고 마스킹된 감사용 사본만 보존합니다.
-- 이 테이블은 아래에서 service_role 전용으로 잠급니다.
create table if not exists public.quarantined_memories (
  id uuid primary key,
  source text not null,
  content text not null,
  content_hash text,
  metadata jsonb not null default '{}'::jsonb,
  embedding vector(1536),
  expires_at timestamptz,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  original_scope text not null,
  original_owner_user_id uuid,
  original_created_by_user_id uuid,
  quarantine_reason text not null,
  quarantined_at timestamptz not null default now()
);

insert into public.quarantined_memories (
  id,
  source,
  content,
  content_hash,
  metadata,
  embedding,
  expires_at,
  created_at,
  updated_at,
  original_scope,
  original_owner_user_id,
  original_created_by_user_id,
  quarantine_reason
)
select
  m.id,
  regexp_replace(
    m.source,
    'sk-[A-Za-z0-9_-]{20,}',
    '[REDACTED_OPENAI_API_KEY]',
    'g'
  ),
  regexp_replace(
    m.content,
    'sk-[A-Za-z0-9_-]{20,}',
    '[REDACTED_OPENAI_API_KEY]',
    'g'
  ),
  null::text,
  regexp_replace(
    m.metadata::text,
    'sk-[A-Za-z0-9_-]{20,}',
    '[REDACTED_OPENAI_API_KEY]',
    'g'
  )::jsonb,
  null::vector(1536),
  m.expires_at,
  m.created_at,
  m.updated_at,
  m.scope,
  m.owner_user_id,
  m.created_by_user_id,
  'openai_api_key_pattern'
from public.memories as m
where (
  coalesce(m.source, '') || ' ' || coalesce(m.content, '') || ' '
  || coalesce(m.metadata::text, '')
) ~ '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}'
on conflict (id) do nothing;

-- 이전에 이 스크립트가 일부 실행된 경우에도 격리본에서 실제 키와 임베딩을
-- 제거합니다. 원본 키가 이미 노출됐다면 별도로 키를 회전해야 합니다.
update public.quarantined_memories
set source = regexp_replace(
      source,
      'sk-[A-Za-z0-9_-]{20,}',
      '[REDACTED_OPENAI_API_KEY]',
      'g'
    ),
    content = regexp_replace(
      content,
      'sk-[A-Za-z0-9_-]{20,}',
      '[REDACTED_OPENAI_API_KEY]',
      'g'
    ),
    metadata = regexp_replace(
      metadata::text,
      'sk-[A-Za-z0-9_-]{20,}',
      '[REDACTED_OPENAI_API_KEY]',
      'g'
    )::jsonb,
    content_hash = null,
    embedding = null
where quarantine_reason = 'openai_api_key_pattern';

delete from public.memories as m
using public.quarantined_memories as q
where q.id = m.id
  and (
    coalesce(m.source, '') || ' ' || coalesce(m.content, '') || ' '
    || coalesce(m.metadata::text, '')
  ) ~ '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}';

-- 이후 저장은 아래의 파생 필드 트리거에서 같은 형태의 키를 차단합니다.
-- CHECK 위반은 실패한 행 전체를 DB 로그에 남길 수 있어 명시적 예외를 사용합니다.
alter table public.memories
  drop constraint if exists memories_no_openai_api_key_check;

-- 동일 본문의 첫 레코드는 표준 해시를 사용합니다. 기존 중복 레코드는 보존하되
-- 같은 기억 공간 안의 중복에만 고유한 보조 해시를 부여합니다.
drop trigger if exists memories_set_derived_fields on public.memories;
drop index if exists public.memories_content_hash_uidx;

with ranked as (
  select
    id,
    encode(digest(
      convert_to(source, 'UTF8') || decode('00', 'hex') || convert_to(content, 'UTF8'),
      'sha256'
    ), 'hex') as base_hash,
    row_number() over (
      partition by
        scope,
        owner_user_id,
        encode(digest(
          convert_to(source, 'UTF8') || decode('00', 'hex') || convert_to(content, 'UTF8'),
          'sha256'
        ), 'hex')
      order by created_at, id
    ) as duplicate_number
  from public.memories
), resolved as (
  select
    id,
    case
      when duplicate_number = 1 then base_hash
      else encode(digest(convert_to(base_hash || ':' || id::text, 'UTF8'), 'sha256'), 'hex')
    end as desired_hash
  from ranked
)
update public.memories as memories
set content_hash = resolved.desired_hash
from resolved
where memories.id = resolved.id
  and memories.content_hash is distinct from resolved.desired_hash;

alter table public.memories alter column content_hash set not null;

-- 같은 본문은 공유 공간과 각 사용자의 개인 공간에 각각 저장할 수 있습니다.
-- NULLS NOT DISTINCT를 사용해 owner가 NULL인 공유 공간도 하나의 공간으로 봅니다.
-- 앱 upsert는 다른 작성자의 공유 행을 UPDATE하지 않도록 DO NOTHING을 사용합니다.
create unique index if not exists memories_scope_owner_content_hash_uidx
  on public.memories (scope, owner_user_id, content_hash) nulls not distinct;

create index if not exists memories_embedding_idx
  on public.memories using hnsw (embedding vector_cosine_ops);

create index if not exists memories_source_idx
  on public.memories (source);

create index if not exists memories_created_at_id_idx
  on public.memories (created_at desc, id desc);

create index if not exists memories_scope_owner_created_at_idx
  on public.memories (scope, owner_user_id, created_at desc, id desc);

create index if not exists memories_expires_at_idx
  on public.memories (expires_at)
  where expires_at is not null;

-- PostgREST JSON 경로 필터와 향후 DB 직접 필터링을 위한 표현식 인덱스입니다.
create index if not exists memories_metadata_person_idx
  on public.memories ((metadata ->> 'person'));

create index if not exists memories_metadata_sender_idx
  on public.memories ((metadata ->> 'sender'));

create index if not exists memories_metadata_project_idx
  on public.memories ((metadata ->> 'project'));

create index if not exists memories_metadata_status_idx
  on public.memories ((metadata ->> 'status'));

create index if not exists memories_metadata_work_date_idx
  on public.memories ((metadata ->> 'work_date'));

create index if not exists memories_metadata_record_type_idx
  on public.memories ((metadata ->> 'record_type'));

create index if not exists memories_metadata_tags_gin_idx
  on public.memories using gin ((metadata -> 'tags'));

create or replace function public.set_memory_derived_fields()
returns trigger
language plpgsql
set search_path = public, extensions
as $$
begin
  if (
    coalesce(new.source, '') || ' ' || coalesce(new.content, '') || ' '
    || coalesce(new.metadata::text, '')
  ) ~ '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}' then
    raise exception using
      errcode = '22023',
      message = 'OpenAI API 키처럼 보이는 값은 기억으로 저장할 수 없습니다.';
  end if;

  if tg_op = 'INSERT' then
    new.content_hash := encode(
      digest(
        convert_to(new.source, 'UTF8') || decode('00', 'hex') || convert_to(new.content, 'UTF8'),
        'sha256'
      ),
      'hex'
    );
  elsif new.source is distinct from old.source
        or new.content is distinct from old.content
        or new.scope is distinct from old.scope
        or new.owner_user_id is distinct from old.owner_user_id then
    new.content_hash := encode(
      digest(
        convert_to(new.source, 'UTF8') || decode('00', 'hex') || convert_to(new.content, 'UTF8'),
        'sha256'
      ),
      'hex'
    );
  else
    -- 메타데이터만 수정할 때는 마이그레이션된 중복 행의 보조 해시를 보존합니다.
    new.content_hash := old.content_hash;
  end if;

  if tg_op = 'UPDATE' then
    new.updated_at := now();
  else
    new.updated_at := coalesce(new.updated_at, now());
  end if;

  return new;
end;
$$;

drop trigger if exists memories_set_derived_fields on public.memories;
create trigger memories_set_derived_fields
before insert or update on public.memories
for each row execute function public.set_memory_derived_fields();

create table if not exists public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  actor text not null,
  actor_user_id uuid references auth.users(id) on delete set null,
  role text not null,
  action text not null,
  memory_id uuid,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.audit_logs add column if not exists actor_user_id uuid;

do $migration$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.audit_logs'::regclass
      and conname = 'audit_logs_actor_user_id_fkey'
  ) then
    alter table public.audit_logs
      add constraint audit_logs_actor_user_id_fkey
      foreign key (actor_user_id)
      references auth.users(id)
      on delete set null;
  end if;
end;
$migration$;

-- 삭제된 기억의 ID도 감사 로그에 남길 수 있도록 외래 키를 두지 않습니다.
alter table public.audit_logs
  drop constraint if exists audit_logs_memory_id_fkey;

create index if not exists audit_logs_memory_id_idx
  on public.audit_logs (memory_id);

create index if not exists audit_logs_created_at_idx
  on public.audit_logs (created_at desc);

create index if not exists audit_logs_actor_user_id_idx
  on public.audit_logs (actor_user_id, created_at desc);

-- 벡터 유사도 검색 함수 (코사인). 만료된 기억은 검색하지 않습니다.
-- shared 모드는 공유 기억만, personal 모드는 공유 + 현재 사용자 개인기억을
-- 반환합니다. 기본값은 개인기억을 노출하지 않는 shared입니다. JWT가 있으면
-- 전달된 UUID보다 auth.uid()를 우선해 다른 사용자의 UUID를 가장할 수 없습니다.
drop function if exists public.match_memories(vector, integer, text);
drop function if exists public.match_memories(vector, integer, text, text, uuid);

create function public.match_memories(
  query_embedding vector(1536),
  match_count int default 8,
  filter_source text default null,
  query_scope text default 'shared',
  requesting_user_id uuid default auth.uid()
)
returns table (
  id uuid,
  source text,
  content text,
  metadata jsonb,
  created_at timestamptz,
  similarity float,
  scope text,
  owner_user_id uuid
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
  select
    m.id,
    m.source,
    m.content,
    m.metadata,
    m.created_at,
    1 - (m.embedding <=> query_embedding) as similarity,
    m.scope,
    m.owner_user_id
  from public.memories as m
  where (filter_source is null or m.source = filter_source)
    and (m.expires_at is null or m.expires_at > now())
    and m.embedding is not null
    and (
      (coalesce(query_scope, 'shared') = 'shared' and m.scope = 'shared')
      or
      (
        query_scope = 'personal'
        and (
          m.scope = 'shared'
          or (
            coalesce((select auth.uid()), requesting_user_id) is not null
            and m.scope = 'personal'
            and m.owner_user_id = coalesce((select auth.uid()), requesting_user_id)
          )
        )
      )
    )
  order by m.embedding <=> query_embedding
  limit greatest(least(coalesce(match_count, 8), 100), 1);
$$;

-- 로그인 사용자는 공유 기억과 자신의 개인기억만 볼 수 있습니다. 쓰기는
-- editor/admin만 가능하고, admin도 다른 사용자의 개인기억은 수정/삭제할 수
-- 없습니다. app_role이 없는 일반 가입자는 애플리케이션과 동일하게 editor입니다.
-- service_role은 RLS를 우회하므로 백엔드 코드에서도 반드시 같은 필터를 적용합니다.
alter table public.memories enable row level security;
alter table public.memories force row level security;
alter table public.audit_logs enable row level security;
alter table public.audit_logs force row level security;
alter table public.quarantined_memories enable row level security;
alter table public.quarantined_memories force row level security;

-- 이전 설치에서 남은 permissive 정책이 OR 결합되어 개인기억을 노출하지 않도록
-- memories와 격리 테이블의 기존 정책을 제거한 뒤 허용 정책을 다시 만듭니다.
do $policies$
declare
  existing_policy record;
begin
  for existing_policy in
    select schemaname, tablename, policyname
    from pg_policies
    where schemaname = 'public'
      and tablename in ('memories', 'quarantined_memories')
  loop
    execute format(
      'drop policy %I on %I.%I',
      existing_policy.policyname,
      existing_policy.schemaname,
      existing_policy.tablename
    );
  end loop;
end;
$policies$;

drop policy if exists memories_authenticated_select on public.memories;
create policy memories_authenticated_select
on public.memories
for select
to authenticated
using (
  scope = 'shared'
  or (scope = 'personal' and owner_user_id = (select auth.uid()))
);

drop policy if exists memories_authenticated_insert on public.memories;
create policy memories_authenticated_insert
on public.memories
for insert
to authenticated
with check (
  (
    select coalesce(
      nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
      'editor'
    )
  ) in ('editor', 'admin')
  and created_by_user_id = (select auth.uid())
  and (
    (scope = 'shared' and owner_user_id is null)
    or
    (scope = 'personal' and owner_user_id = (select auth.uid()))
  )
);

drop policy if exists memories_creator_update on public.memories;
create policy memories_creator_update
on public.memories
for update
to authenticated
using (
  (
    select coalesce(
      nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
      'editor'
    )
  ) in ('editor', 'admin')
  and (
    (
      scope = 'shared'
      and (
        created_by_user_id = (select auth.uid())
        or (
          select coalesce(
            nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
            'editor'
          )
        ) = 'admin'
      )
    )
    or (
      scope = 'personal'
      and owner_user_id = (select auth.uid())
    )
  )
)
with check (
  (
    select coalesce(
      nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
      'editor'
    )
  ) in ('editor', 'admin')
  and (
    (
      scope = 'shared'
      and owner_user_id is null
      and (
        created_by_user_id = (select auth.uid())
        or (
          select coalesce(
            nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
            'editor'
          )
        ) = 'admin'
      )
    )
    or (
      scope = 'personal'
      and owner_user_id = (select auth.uid())
      and created_by_user_id = (select auth.uid())
    )
  )
);

drop policy if exists memories_creator_delete on public.memories;
create policy memories_creator_delete
on public.memories
for delete
to authenticated
using (
  (
    select coalesce(
      nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
      'editor'
    )
  ) in ('editor', 'admin')
  and (
    (
      scope = 'shared'
      and (
        created_by_user_id = (select auth.uid())
        or (
          select coalesce(
            nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
            'editor'
          )
        ) = 'admin'
      )
    )
    or (
      scope = 'personal'
      and owner_user_id = (select auth.uid())
    )
  )
);

revoke all privileges on table public.memories from public, anon, authenticated;
revoke all privileges on table public.audit_logs from public, anon, authenticated;
revoke all privileges on table public.quarantined_memories
  from public, anon, authenticated;
grant select, insert, update, delete on table public.memories to authenticated;
grant select, insert, update, delete on table public.memories to service_role;
grant select, insert on table public.audit_logs to service_role;
grant select, insert, delete on table public.quarantined_memories to service_role;

revoke all on function public.set_memory_derived_fields()
  from public, anon, authenticated;
grant execute on function public.set_memory_derived_fields()
  to service_role;

revoke all on function public.match_memories(vector, integer, text, text, uuid)
  from public, anon, authenticated;
grant execute on function public.match_memories(vector, integer, text, text, uuid)
  to authenticated, service_role;

commit;
-- Require two distinct users (including the author) before a shared memory is
-- published. Run after migration_security.sql, migration_memory_scopes.sql,
-- and migration_auth_accounts.sql.

begin;

create extension if not exists vector;
create extension if not exists pgcrypto;

create table if not exists public.shared_memory_proposals (
  id uuid primary key default gen_random_uuid(),
  content text not null,
  source text not null default 'note',
  created_by_user_id uuid not null
    references auth.users(id) on delete cascade,
  status text not null default 'pending',
  required_approvals smallint not null default 2,
  created_at timestamptz not null default now(),
  published_at timestamptz,
  constraint shared_memory_proposals_status_check
    check (status in ('pending', 'published')),
  constraint shared_memory_proposals_required_approvals_check
    check (required_approvals = 2),
  constraint shared_memory_proposals_published_at_check
    check (
      (status = 'pending' and published_at is null)
      or (status = 'published' and published_at is not null)
    )
);

create table if not exists public.shared_memory_proposal_approvals (
  proposal_id uuid not null
    references public.shared_memory_proposals(id) on delete cascade,
  approver_user_id uuid not null
    references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (proposal_id, approver_user_id)
);

alter table public.memories
  add column if not exists publication_status text;
alter table public.memories
  add column if not exists proposal_id uuid;
alter table public.memories
  add column if not exists approved_at timestamptz;

-- Existing shared memories were already public before this migration. Personal
-- memories are never subject to shared-memory approval.
update public.memories
set publication_status = 'published',
    approved_at = coalesce(approved_at, created_at, now())
where publication_status is null;

alter table public.memories
  alter column publication_status set default 'published';
alter table public.memories
  alter column publication_status set not null;

do $migration$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_publication_status_check'
  ) then
    alter table public.memories
      add constraint memories_publication_status_check
      check (publication_status in ('pending', 'published'));
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_proposal_id_fkey'
  ) then
    alter table public.memories
      add constraint memories_proposal_id_fkey
      foreign key (proposal_id)
      references public.shared_memory_proposals(id)
      on delete cascade;
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_approval_state_check'
  ) then
    alter table public.memories
      add constraint memories_approval_state_check
      check (
        (
          scope = 'personal'
          and publication_status = 'published'
          and proposal_id is null
          and approved_at is not null
        )
        or (
          scope = 'shared'
          and publication_status = 'pending'
          and proposal_id is not null
          and approved_at is null
        )
        or (
          scope = 'shared'
          and publication_status = 'published'
          and approved_at is not null
        )
      );
  end if;
end;
$migration$;

create index if not exists shared_memory_proposals_pending_created_idx
  on public.shared_memory_proposals (created_at desc, id desc)
  where status = 'pending';
create index if not exists shared_memory_proposal_approvals_user_idx
  on public.shared_memory_proposal_approvals (approver_user_id, created_at desc);
create index if not exists memories_publication_scope_created_idx
  on public.memories (publication_status, scope, created_at desc, id desc);
create index if not exists memories_proposal_id_idx
  on public.memories (proposal_id)
  where proposal_id is not null;

-- The application service creates the proposal, the author's first approval,
-- and every pending memory in one transaction. Authenticated clients cannot
-- call this function directly.
create or replace function public.create_shared_memory_proposal(
  requested_proposal_id uuid,
  creator_user_id uuid,
  proposal_content text,
  proposal_source text,
  proposal_records jsonb
)
returns table (
  proposal_id uuid,
  inserted_count integer
)
language plpgsql
security definer
set search_path = pg_catalog, extensions, public
as $$
declare
  creator_role text;
  creator_username text;
  inserted_rows integer := 0;
begin
  if requested_proposal_id is null
     or nullif(btrim(coalesce(proposal_content, '')), '') is null
     or jsonb_typeof(proposal_records) <> 'array'
     or jsonb_array_length(proposal_records) = 0 then
    raise exception using
      errcode = '22023',
      message = 'Invalid shared-memory proposal.';
  end if;

  select
    lower(coalesce(u.raw_app_meta_data ->> 'app_role', 'editor')),
    profile.username
  into creator_role, creator_username
  from auth.users as u
  join public.account_profiles as profile on profile.id = u.id
  where u.id = creator_user_id;

  if creator_role is null or creator_role not in ('editor', 'admin') then
    raise exception using
      errcode = '42501',
      message = 'This account cannot create shared-memory proposals.';
  end if;

  insert into public.shared_memory_proposals (
    id, content, source, created_by_user_id, status,
    required_approvals, published_at
  ) values (
    requested_proposal_id,
    proposal_content,
    coalesce(nullif(btrim(proposal_source), ''), 'note'),
    creator_user_id,
    'pending',
    2,
    null
  );

  insert into public.shared_memory_proposal_approvals (
    proposal_id, approver_user_id
  ) values (requested_proposal_id, creator_user_id);

  with inserted as (
    insert into public.memories (
      source,
      content,
      content_hash,
      metadata,
      embedding,
      expires_at,
      scope,
      owner_user_id,
      created_by_user_id,
      updated_at,
      publication_status,
      proposal_id,
      approved_at
    )
    select
      coalesce(nullif(btrim(record ->> 'source'), ''), proposal_source, 'note'),
      record ->> 'content',
      record ->> 'content_hash',
      coalesce(record -> 'metadata', '{}'::jsonb),
      case
        when record ? 'embedding' and record -> 'embedding' <> 'null'::jsonb
          then (record ->> 'embedding')::vector(1536)
        else null
      end,
      nullif(record ->> 'expires_at', '')::timestamptz,
      'shared',
      null,
      creator_user_id,
      coalesce(nullif(record ->> 'updated_at', '')::timestamptz, now()),
      'pending',
      requested_proposal_id,
      null
    from jsonb_array_elements(proposal_records) as record
    where nullif(btrim(record ->> 'content'), '') is not null
    on conflict (scope, owner_user_id, content_hash) do nothing
    returning 1
  )
  select count(*)::integer into inserted_rows from inserted;

  if inserted_rows = 0 then
    delete from public.shared_memory_proposals
    where id = requested_proposal_id;
  else
    insert into public.audit_logs (
      actor, actor_user_id, role, action, details
    ) values (
      creator_username,
      creator_user_id,
      creator_role,
      'shared_memory_proposal_create',
      jsonb_build_object(
        'proposal_id', requested_proposal_id,
        'inserted_count', inserted_rows,
        'approval_count', 1,
        'required_approvals', 2
      )
    );
  end if;

  proposal_id := case when inserted_rows > 0 then requested_proposal_id else null end;
  inserted_count := inserted_rows;
  return next;
end;
$$;

-- A row lock serializes concurrent approvals. The composite primary key makes
-- repeat clicks idempotent, so only distinct users contribute to the count.
create or replace function public.approve_shared_memory_proposal(
  target_proposal_id uuid
)
returns table (
  proposal_id uuid,
  proposal_status text,
  approval_count integer,
  required_approvals integer,
  published boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  approver_id uuid := auth.uid();
  approver_role text;
  approver_username text;
  proposal_record public.shared_memory_proposals%rowtype;
  counted_approvals integer;
  approval_inserted integer := 0;
  published_now boolean := false;
begin
  if approver_id is null then
    raise exception using
      errcode = '28000',
      message = 'Authentication is required.';
  end if;

  select
    lower(coalesce(u.raw_app_meta_data ->> 'app_role', 'editor')),
    profile.username
  into approver_role, approver_username
  from auth.users as u
  join public.account_profiles as profile on profile.id = u.id
  where u.id = approver_id;

  if approver_role is null or approver_role not in ('viewer', 'editor', 'admin') then
    raise exception using
      errcode = '42501',
      message = 'This account cannot approve shared-memory proposals.';
  end if;

  select proposal.*
  into proposal_record
  from public.shared_memory_proposals as proposal
  where proposal.id = target_proposal_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'Shared-memory proposal was not found.';
  end if;

  if proposal_record.status = 'pending' then
    insert into public.shared_memory_proposal_approvals (
      proposal_id, approver_user_id
    ) values (target_proposal_id, approver_id)
    on conflict on constraint shared_memory_proposal_approvals_pkey
      do nothing;
    get diagnostics approval_inserted = row_count;
  end if;

  select count(*)::integer
  into counted_approvals
  from public.shared_memory_proposal_approvals as approval
  where approval.proposal_id = target_proposal_id;

  if proposal_record.status = 'pending'
     and (
       counted_approvals >= proposal_record.required_approvals
       or approver_role = 'admin'
     ) then
    update public.shared_memory_proposals as proposal
    set status = 'published',
        published_at = now()
    where proposal.id = target_proposal_id;

    update public.memories as memory
    set publication_status = 'published',
        approved_at = now(),
        updated_at = now(),
        proposal_id = null
    where memory.proposal_id = target_proposal_id
      and memory.publication_status = 'pending';

    proposal_record.status := 'published';
    published_now := true;
  end if;

  if approval_inserted > 0 or published_now then
    insert into public.audit_logs (
      actor, actor_user_id, role, action, details
    ) values (
      approver_username,
      approver_id,
      approver_role,
      'shared_memory_proposal_approve',
      jsonb_build_object(
        'proposal_id', target_proposal_id,
        'approval_count', counted_approvals,
        'required_approvals', proposal_record.required_approvals,
        'approval_added', approval_inserted > 0,
        'published', proposal_record.status = 'published'
      )
    );
  end if;

  proposal_id := target_proposal_id;
  proposal_status := proposal_record.status;
  approval_count := counted_approvals;
  required_approvals := proposal_record.required_approvals;
  published := proposal_record.status = 'published';
  return next;
end;
$$;

alter function public.create_shared_memory_proposal(
  uuid, uuid, text, text, jsonb
) owner to postgres;
alter function public.approve_shared_memory_proposal(uuid) owner to postgres;

alter table public.shared_memory_proposals enable row level security;
alter table public.shared_memory_proposals force row level security;
alter table public.shared_memory_proposal_approvals enable row level security;
alter table public.shared_memory_proposal_approvals force row level security;

revoke all privileges on table public.shared_memory_proposals
  from public, anon, authenticated;
revoke all privileges on table public.shared_memory_proposal_approvals
  from public, anon, authenticated;
grant select, insert, update, delete on table public.shared_memory_proposals
  to service_role;
grant select, insert, update, delete on table public.shared_memory_proposal_approvals
  to service_role;

revoke all on function public.create_shared_memory_proposal(
  uuid, uuid, text, text, jsonb
) from public, anon, authenticated;
grant execute on function public.create_shared_memory_proposal(
  uuid, uuid, text, text, jsonb
) to service_role;

revoke all on function public.approve_shared_memory_proposal(uuid)
  from public, anon;
grant execute on function public.approve_shared_memory_proposal(uuid)
  to authenticated, service_role;

-- Replace all memory policies so pending rows cannot become visible through a
-- permissive legacy policy. Only admins may edit/delete published shared data.
do $policies$
declare
  existing_policy record;
begin
  for existing_policy in
    select policyname
    from pg_policies
    where schemaname = 'public' and tablename = 'memories'
  loop
    execute format('drop policy %I on public.memories', existing_policy.policyname);
  end loop;
end;
$policies$;

create policy memories_authenticated_select
on public.memories
for select
to authenticated
using (
  (scope = 'shared' and publication_status = 'published')
  or (
    scope = 'personal'
    and publication_status = 'published'
    and owner_user_id = (select auth.uid())
  )
);

create policy memories_authenticated_insert
on public.memories
for insert
to authenticated
with check (
  created_by_user_id = (select auth.uid())
  and publication_status = 'published'
  and approved_at is not null
  and proposal_id is null
  and (
    (
      scope = 'personal'
      and owner_user_id = (select auth.uid())
      and (
        select coalesce(
          nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
          'editor'
        )
      ) in ('editor', 'admin')
    )
    or (
      scope = 'shared'
      and owner_user_id is null
      and (
        select lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role'))
      ) = 'admin'
    )
  )
);

create policy memories_authenticated_update
on public.memories
for update
to authenticated
using (
  (
    scope = 'personal'
    and owner_user_id = (select auth.uid())
    and (
      select coalesce(
        nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
        'editor'
      )
    ) in ('editor', 'admin')
  )
  or (
    scope = 'shared'
    and publication_status = 'published'
    and (
      select lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role'))
    ) = 'admin'
  )
)
with check (
  publication_status = 'published'
  and approved_at is not null
  and (
    (
      scope = 'personal'
      and owner_user_id = (select auth.uid())
      and proposal_id is null
      and created_by_user_id = (select auth.uid())
    )
    or (
      scope = 'shared'
      and owner_user_id is null
      and (
        select lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role'))
      ) = 'admin'
    )
  )
);

create policy memories_authenticated_delete
on public.memories
for delete
to authenticated
using (
  (
    scope = 'personal'
    and owner_user_id = (select auth.uid())
    and (
      select coalesce(
        nullif(lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role')), ''),
        'editor'
      )
    ) in ('editor', 'admin')
  )
  or (
    scope = 'shared'
    and (
      select lower(btrim(auth.jwt() -> 'app_metadata' ->> 'app_role'))
    ) = 'admin'
  )
);

-- Recreate vector search so unpublished proposal rows are never candidates.
drop function if exists public.match_memories(vector, integer, text, text, uuid);

create function public.match_memories(
  query_embedding vector(1536),
  match_count int default 8,
  filter_source text default null,
  query_scope text default 'shared',
  requesting_user_id uuid default auth.uid()
)
returns table (
  id uuid,
  source text,
  content text,
  metadata jsonb,
  created_at timestamptz,
  similarity float,
  scope text,
  owner_user_id uuid
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
  select
    memory.id,
    memory.source,
    memory.content,
    memory.metadata,
    memory.created_at,
    1 - (memory.embedding <=> query_embedding) as similarity,
    memory.scope,
    memory.owner_user_id
  from public.memories as memory
  where memory.publication_status = 'published'
    and (filter_source is null or memory.source = filter_source)
    and (memory.expires_at is null or memory.expires_at > now())
    and memory.embedding is not null
    and (
      (coalesce(query_scope, 'shared') = 'shared' and memory.scope = 'shared')
      or (
        query_scope = 'personal'
        and (
          memory.scope = 'shared'
          or (
            coalesce((select auth.uid()), requesting_user_id) is not null
            and memory.scope = 'personal'
            and memory.owner_user_id = coalesce(
              (select auth.uid()), requesting_user_id
            )
          )
        )
      )
    )
  order by memory.embedding <=> query_embedding
  limit greatest(least(coalesce(match_count, 8), 100), 1);
$$;

revoke all on function public.match_memories(vector, integer, text, text, uuid)
  from public, anon, authenticated;
grant execute on function public.match_memories(vector, integer, text, text, uuid)
  to authenticated, service_role;

commit;
-- Require two distinct users (including the requester) before deleting a
-- published shared memory. Run after migration_shared_memory_approvals.sql.

begin;

create extension if not exists pgcrypto;

create table if not exists public.shared_memory_deletion_proposals (
  id uuid primary key default gen_random_uuid(),
  memory_id uuid not null,
  source_snapshot text not null,
  content_snapshot text not null,
  requested_by_user_id uuid
    references auth.users(id) on delete set null,
  status text not null default 'pending',
  required_approvals smallint not null default 2,
  created_at timestamptz not null default now(),
  deleted_at timestamptz,
  constraint shared_memory_deletion_proposals_status_check
    check (status in ('pending', 'deleted')),
  constraint shared_memory_deletion_proposals_required_check
    check (required_approvals = 2),
  constraint shared_memory_deletion_proposals_deleted_at_check
    check (
      (status = 'pending' and deleted_at is null)
      or (status = 'deleted' and deleted_at is not null)
    )
);

create table if not exists public.shared_memory_deletion_proposal_approvals (
  proposal_id uuid not null
    references public.shared_memory_deletion_proposals(id) on delete cascade,
  approver_user_id uuid not null
    references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (proposal_id, approver_user_id)
);

create unique index if not exists shared_memory_deletion_one_pending_uidx
  on public.shared_memory_deletion_proposals (memory_id)
  where status = 'pending';
create index if not exists shared_memory_deletion_pending_created_idx
  on public.shared_memory_deletion_proposals (created_at desc, id desc)
  where status = 'pending';
create index if not exists shared_memory_deletion_approver_idx
  on public.shared_memory_deletion_proposal_approvals (
    approver_user_id, created_at desc
  );

-- Memory rows intentionally are not referenced by a foreign key. The proposal
-- and its snapshots remain available after the target memory has been deleted.
create or replace function public.close_shared_memory_deletion_proposal()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if old.scope = 'shared' then
    update public.shared_memory_deletion_proposals as proposal
    set status = 'deleted',
        deleted_at = coalesce(proposal.deleted_at, now())
    where proposal.memory_id = old.id
      and proposal.status = 'pending';
  end if;
  return old;
end;
$$;

alter function public.close_shared_memory_deletion_proposal() owner to postgres;

drop trigger if exists memories_close_deletion_proposal on public.memories;
create trigger memories_close_deletion_proposal
after delete on public.memories
for each row execute function public.close_shared_memory_deletion_proposal();

-- Editors create a pending proposal and cast their own first vote. A second
-- distinct editor request is also an approval. Admins delete immediately, but
-- still create a durable proposal/snapshot and audit history.
create or replace function public.request_shared_memory_deletion(
  target_memory_id uuid
)
returns table (
  proposal_id uuid,
  proposal_status text,
  deleted boolean,
  approval_count integer,
  required_approvals integer
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor_id uuid := auth.uid();
  actor_role text;
  actor_username text;
  target_memory record;
  proposal_record public.shared_memory_deletion_proposals%rowtype;
  counted_approvals integer := 0;
  approval_inserted integer := 0;
  deletion_count integer := 0;
  proposal_created boolean := false;
  deleted_now boolean := false;
begin
  if actor_id is null then
    raise exception using
      errcode = '28000',
      message = 'Authentication is required.';
  end if;

  select
    lower(coalesce(u.raw_app_meta_data ->> 'app_role', 'editor')),
    profile.username
  into actor_role, actor_username
  from auth.users as u
  join public.account_profiles as profile on profile.id = u.id
  where u.id = actor_id;

  if actor_role is null or actor_role not in ('editor', 'admin') then
    raise exception using
      errcode = '42501',
      message = 'This account cannot request shared-memory deletion.';
  end if;

  select
    memory.id,
    memory.source,
    memory.content,
    memory.scope,
    memory.publication_status
  into target_memory
  from public.memories as memory
  where memory.id = target_memory_id
  for update;

  if not found then
    select proposal.*
    into proposal_record
    from public.shared_memory_deletion_proposals as proposal
    where proposal.memory_id = target_memory_id
    order by proposal.created_at desc, proposal.id desc
    limit 1
    for update;

    if not found then
      raise exception using
        errcode = 'P0002',
        message = 'Shared memory was not found.';
    end if;

    select count(*)::integer
    into counted_approvals
    from public.shared_memory_deletion_proposal_approvals as approval
    where approval.proposal_id = proposal_record.id;

    proposal_id := proposal_record.id;
    proposal_status := proposal_record.status;
    deleted := proposal_record.status = 'deleted';
    approval_count := counted_approvals;
    required_approvals := proposal_record.required_approvals;
    return next;
    return;
  end if;

  if target_memory.scope <> 'shared'
     or target_memory.publication_status <> 'published' then
    raise exception using
      errcode = '42501',
      message = 'Only published shared memories use deletion approval.';
  end if;

  select proposal.*
  into proposal_record
  from public.shared_memory_deletion_proposals as proposal
  where proposal.memory_id = target_memory_id
    and proposal.status = 'pending'
  for update;

  if not found then
    insert into public.shared_memory_deletion_proposals (
      memory_id,
      source_snapshot,
      content_snapshot,
      requested_by_user_id,
      status,
      required_approvals,
      deleted_at
    ) values (
      target_memory_id,
      target_memory.source,
      target_memory.content,
      actor_id,
      'pending',
      2,
      null
    )
    returning * into proposal_record;
    proposal_created := true;
  end if;

  insert into public.shared_memory_deletion_proposal_approvals (
    proposal_id, approver_user_id
  ) values (proposal_record.id, actor_id)
  on conflict on constraint shared_memory_deletion_proposal_approvals_pkey
    do nothing;
  get diagnostics approval_inserted = row_count;

  select count(*)::integer
  into counted_approvals
  from public.shared_memory_deletion_proposal_approvals as approval
  where approval.proposal_id = proposal_record.id;

  if proposal_created then
    insert into public.audit_logs (
      actor, actor_user_id, role, action, memory_id, details
    ) values (
      actor_username,
      actor_id,
      actor_role,
      'shared_memory_deletion_proposal_create',
      target_memory_id,
      jsonb_build_object(
        'proposal_id', proposal_record.id,
        'approval_count', counted_approvals,
        'required_approvals', proposal_record.required_approvals
      )
    );
  end if;

  if approval_inserted > 0 then
    insert into public.audit_logs (
      actor, actor_user_id, role, action, memory_id, details
    ) values (
      actor_username,
      actor_id,
      actor_role,
      'shared_memory_deletion_proposal_approve',
      target_memory_id,
      jsonb_build_object(
        'proposal_id', proposal_record.id,
        'approval_count', counted_approvals,
        'required_approvals', proposal_record.required_approvals
      )
    );
  end if;

  if actor_role = 'admin'
     or counted_approvals >= proposal_record.required_approvals then
    delete from public.memories as memory
    where memory.id = target_memory_id
      and memory.scope = 'shared'
      and memory.publication_status = 'published';
    get diagnostics deletion_count = row_count;
    deleted_now := deletion_count > 0;

    if deleted_now then
      insert into public.audit_logs (
        actor, actor_user_id, role, action, memory_id, details
      ) values (
        actor_username,
        actor_id,
        actor_role,
        'shared_memory_delete',
        target_memory_id,
        jsonb_build_object(
          'proposal_id', proposal_record.id,
          'approval_count', counted_approvals,
          'required_approvals', proposal_record.required_approvals
        )
      );
    end if;
  end if;

  proposal_id := proposal_record.id;
  proposal_status := case when deleted_now then 'deleted' else 'pending' end;
  deleted := deleted_now;
  approval_count := counted_approvals;
  required_approvals := proposal_record.required_approvals;
  return next;
end;
$$;

-- Approval looks up the memory id first, then locks memory -> proposal. This
-- matches expiry/admin deletion lock order and avoids a proposal/memory deadlock.
create or replace function public.approve_shared_memory_deletion_proposal(
  target_proposal_id uuid
)
returns table (
  proposal_id uuid,
  proposal_status text,
  deleted boolean,
  approval_count integer,
  required_approvals integer
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor_id uuid := auth.uid();
  actor_role text;
  actor_username text;
  target_memory_id uuid;
  target_memory record;
  proposal_record public.shared_memory_deletion_proposals%rowtype;
  counted_approvals integer := 0;
  approval_inserted integer := 0;
  deletion_count integer := 0;
  deleted_now boolean := false;
  memory_exists boolean := false;
begin
  if actor_id is null then
    raise exception using
      errcode = '28000',
      message = 'Authentication is required.';
  end if;

  select
    lower(coalesce(u.raw_app_meta_data ->> 'app_role', 'editor')),
    profile.username
  into actor_role, actor_username
  from auth.users as u
  join public.account_profiles as profile on profile.id = u.id
  where u.id = actor_id;

  if actor_role is null
     or actor_role not in ('viewer', 'editor', 'admin') then
    raise exception using
      errcode = '42501',
      message = 'This account cannot approve shared-memory deletion.';
  end if;

  select proposal.memory_id
  into target_memory_id
  from public.shared_memory_deletion_proposals as proposal
  where proposal.id = target_proposal_id;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'Shared-memory deletion proposal was not found.';
  end if;

  select memory.id, memory.scope, memory.publication_status
  into target_memory
  from public.memories as memory
  where memory.id = target_memory_id
  for update;
  memory_exists := found;

  select proposal.*
  into proposal_record
  from public.shared_memory_deletion_proposals as proposal
  where proposal.id = target_proposal_id
  for update;

  if proposal_record.status = 'deleted' or not memory_exists then
    if proposal_record.status = 'pending' then
      update public.shared_memory_deletion_proposals as proposal
      set status = 'deleted',
          deleted_at = coalesce(proposal.deleted_at, now())
      where proposal.id = target_proposal_id;
      proposal_record.status := 'deleted';
    end if;

    select count(*)::integer
    into counted_approvals
    from public.shared_memory_deletion_proposal_approvals as approval
    where approval.proposal_id = target_proposal_id;

    proposal_id := proposal_record.id;
    proposal_status := proposal_record.status;
    deleted := proposal_record.status = 'deleted';
    approval_count := counted_approvals;
    required_approvals := proposal_record.required_approvals;
    return next;
    return;
  end if;

  if target_memory.scope <> 'shared'
     or target_memory.publication_status <> 'published' then
    raise exception using
      errcode = '42501',
      message = 'Only published shared memories use deletion approval.';
  end if;

  insert into public.shared_memory_deletion_proposal_approvals (
    proposal_id, approver_user_id
  ) values (target_proposal_id, actor_id)
  on conflict on constraint shared_memory_deletion_proposal_approvals_pkey
    do nothing;
  get diagnostics approval_inserted = row_count;

  select count(*)::integer
  into counted_approvals
  from public.shared_memory_deletion_proposal_approvals as approval
  where approval.proposal_id = target_proposal_id;

  if approval_inserted > 0 then
    insert into public.audit_logs (
      actor, actor_user_id, role, action, memory_id, details
    ) values (
      actor_username,
      actor_id,
      actor_role,
      'shared_memory_deletion_proposal_approve',
      target_memory_id,
      jsonb_build_object(
        'proposal_id', target_proposal_id,
        'approval_count', counted_approvals,
        'required_approvals', proposal_record.required_approvals
      )
    );
  end if;

  if actor_role = 'admin'
     or counted_approvals >= proposal_record.required_approvals then
    delete from public.memories as memory
    where memory.id = target_memory_id
      and memory.scope = 'shared'
      and memory.publication_status = 'published';
    get diagnostics deletion_count = row_count;
    deleted_now := deletion_count > 0;

    if deleted_now then
      insert into public.audit_logs (
        actor, actor_user_id, role, action, memory_id, details
      ) values (
        actor_username,
        actor_id,
        actor_role,
        'shared_memory_delete',
        target_memory_id,
        jsonb_build_object(
          'proposal_id', target_proposal_id,
          'approval_count', counted_approvals,
          'required_approvals', proposal_record.required_approvals
        )
      );
    end if;
  end if;

  proposal_id := proposal_record.id;
  proposal_status := case when deleted_now then 'deleted' else 'pending' end;
  deleted := deleted_now;
  approval_count := counted_approvals;
  required_approvals := proposal_record.required_approvals;
  return next;
end;
$$;

alter function public.request_shared_memory_deletion(uuid) owner to postgres;
alter function public.approve_shared_memory_deletion_proposal(uuid)
  owner to postgres;

alter table public.shared_memory_deletion_proposals enable row level security;
alter table public.shared_memory_deletion_proposals force row level security;
alter table public.shared_memory_deletion_proposal_approvals
  enable row level security;
alter table public.shared_memory_deletion_proposal_approvals
  force row level security;

revoke all privileges on table public.shared_memory_deletion_proposals
  from public, anon, authenticated;
revoke all privileges on table public.shared_memory_deletion_proposal_approvals
  from public, anon, authenticated;
grant select, insert, update, delete
  on table public.shared_memory_deletion_proposals to service_role;
grant select, insert, update, delete
  on table public.shared_memory_deletion_proposal_approvals to service_role;

revoke all on function public.close_shared_memory_deletion_proposal()
  from public, anon, authenticated;
revoke all on function public.request_shared_memory_deletion(uuid)
  from public, anon;
revoke all on function public.approve_shared_memory_deletion_proposal(uuid)
  from public, anon;
grant execute on function public.request_shared_memory_deletion(uuid)
  to authenticated, service_role;
grant execute on function public.approve_shared_memory_deletion_proposal(uuid)
  to authenticated, service_role;

commit;
