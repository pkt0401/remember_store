-- 기존 프로젝트에 공유/개인 기억, Supabase Auth UUID, RLS를 추가합니다.
-- migration_security.sql 적용 후 실행하세요.
--
-- 기존 기억은 모두 공유 기억으로 전환합니다. 다만 실제 OpenAI API 키와 같은
-- `sk-...` 비밀값이 들어 있는 행은 검색 대상에서 제거합니다. 키 문자열·해시·
-- 임베딩은 폐기하고 마스킹된 기록만 public.quarantined_memories에 보존합니다.
-- 격리 테이블은 service_role만 접근할 수 있으며 애플리케이션의 일반 기억
-- 조회/RPC에서는 사용하지 않아야 합니다.

begin;

create extension if not exists vector;
create extension if not exists pgcrypto;

alter table public.memories
  add column if not exists scope text;
alter table public.memories
  add column if not exists owner_user_id uuid;
alter table public.memories
  add column if not exists created_by_user_id uuid;

-- 기존 행은 명시적으로 모두의 기억으로 backfill합니다.
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
    select 1
    from pg_constraint
    where conrelid = 'public.memories'::regclass
      and conname = 'memories_scope_check'
  ) then
    alter table public.memories
      add constraint memories_scope_check
      check (scope in ('shared', 'personal'));
  end if;

  if not exists (
    select 1
    from pg_constraint
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
    select 1
    from pg_constraint
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
    select 1
    from pg_constraint
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

alter table public.audit_logs
  add column if not exists actor_user_id uuid;

do $migration$
begin
  if not exists (
    select 1
    from pg_constraint
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

create index if not exists audit_logs_actor_user_id_idx
  on public.audit_logs (actor_user_id, created_at desc);

-- 비밀값은 memories에 남겨 두지 않습니다. 실제 키와 그 임베딩은 폐기하고
-- 마스킹된 감사용 사본만 별도 테이블에 보존합니다. 그래야 service_role을
-- 사용하는 서버 코드가 범위 필터를 빠뜨려도 일반 검색 결과에 섞이지 않습니다.
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

-- 이전에 일부 실행된 격리본도 실제 키와 임베딩이 남지 않게 정리합니다.
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

-- 같은 비밀값의 재저장은 아래 트리거가 차단합니다. CHECK 위반은 실패한 행
-- 전체를 DB 로그에 남길 수 있어 키 내용이 없는 명시적 예외를 사용합니다.
alter table public.memories
  drop constraint if exists memories_no_openai_api_key_check;

alter table public.quarantined_memories enable row level security;
alter table public.quarantined_memories force row level security;
revoke all privileges on table public.quarantined_memories
  from public, anon, authenticated;
grant select, insert, delete on table public.quarantined_memories
  to service_role;

-- 중복은 전체 테이블이 아니라 기억 공간별로 판정합니다. PostgreSQL 15의
-- NULLS NOT DISTINCT 덕분에 owner_user_id가 NULL인 공유 기억도 하나의 공간으로
-- 취급됩니다. PostgREST upsert는 다른 작성자의 공유 행을 UPDATE하지 않도록
-- ignore_duplicates/DO NOTHING을 사용해야 합니다.
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

create unique index if not exists memories_scope_owner_content_hash_uidx
  on public.memories (scope, owner_user_id, content_hash) nulls not distinct;

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

create trigger memories_set_derived_fields
before insert or update on public.memories
for each row execute function public.set_memory_derived_fields();

create index if not exists memories_scope_owner_created_at_idx
  on public.memories (scope, owner_user_id, created_at desc, id desc);

drop function if exists public.match_memories(vector, integer, text);
drop function if exists public.match_memories(vector, integer, text, text, uuid);

-- query_scope='shared': 모두의 기억만
-- query_scope='personal': 모두의 기억 + requesting_user_id의 개인기억
-- 기본값을 shared로 두어 구버전 호출이 개인기억을 노출하지 않게 합니다. JWT가
-- 있으면 전달된 UUID보다 auth.uid()를 우선합니다.
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

-- RLS는 authenticated JWT의 auth.uid()를 기준으로 적용됩니다. 쓰기는
-- app_metadata.app_role의 editor/admin만 가능하며 누락된 역할은 editor입니다.
-- admin도 다른 사용자의 개인기억에는 접근하지 못합니다. service_role은 관리
-- 작업용으로 유지되므로 백엔드도 동일한 scope/owner 필터를 적용해야 합니다.
alter table public.memories enable row level security;
alter table public.memories force row level security;

-- RLS 정책은 permissive 정책끼리 OR 결합됩니다. 이전에 남은 광범위한 정책을
-- 제거하고 아래의 범위 정책만 다시 만들어 개인기억 노출을 방지합니다.
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
grant select, insert, update, delete on table public.memories to authenticated;
grant select, insert, update, delete on table public.memories to service_role;

revoke all on function public.match_memories(vector, integer, text, text, uuid)
  from public, anon, authenticated;
grant execute on function public.match_memories(vector, integer, text, text, uuid)
  to authenticated, service_role;

commit;
