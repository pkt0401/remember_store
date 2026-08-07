-- 기존 Supabase 프로젝트용 보안/무결성 마이그레이션입니다.
-- 반복 실행할 수 있으며 기존 중복 레코드를 삭제하지 않습니다.

begin;

create extension if not exists pgcrypto;

alter table public.memories add column if not exists expires_at timestamptz;
alter table public.memories add column if not exists content_hash text;
alter table public.memories add column if not exists updated_at timestamptz default now();

update public.memories
set updated_at = coalesce(created_at, now())
where updated_at is null;

alter table public.memories alter column updated_at set default now();
alter table public.memories alter column updated_at set not null;

-- 기존 중복 본문은 보존합니다. 가장 오래된 레코드 하나만 표준 해시를 사용하고
-- 나머지 레코드는 고유한 보조 해시를 사용합니다.
drop trigger if exists memories_set_derived_fields on public.memories;

with ranked as (
  select
    id,
    encode(digest(
      convert_to(source, 'UTF8') || decode('00', 'hex') || convert_to(content, 'UTF8'),
      'sha256'
    ), 'hex') as base_hash,
    row_number() over (
      partition by encode(digest(
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

create unique index if not exists memories_content_hash_uidx
  on public.memories (content_hash);

create index if not exists memories_created_at_id_idx
  on public.memories (created_at desc, id desc);

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
  if tg_op = 'INSERT' then
    new.content_hash := encode(
      digest(
        convert_to(new.source, 'UTF8') || decode('00', 'hex') || convert_to(new.content, 'UTF8'),
        'sha256'
      ),
      'hex'
    );
  elsif new.source is distinct from old.source
        or new.content is distinct from old.content then
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
  role text not null,
  action text not null,
  memory_id uuid,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- 삭제된 기억의 ID도 감사 로그에 남길 수 있도록 외래 키를 두지 않습니다.
alter table public.audit_logs
  drop constraint if exists audit_logs_memory_id_fkey;

create index if not exists audit_logs_memory_id_idx
  on public.audit_logs (memory_id);

create index if not exists audit_logs_created_at_idx
  on public.audit_logs (created_at desc);

create or replace function public.match_memories(
  query_embedding vector(1536),
  match_count int default 8,
  filter_source text default null
)
returns table (
  id uuid,
  source text,
  content text,
  metadata jsonb,
  created_at timestamptz,
  similarity float
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
    1 - (m.embedding <=> query_embedding) as similarity
  from public.memories as m
  where (filter_source is null or m.source = filter_source)
    and (m.expires_at is null or m.expires_at > now())
    and m.embedding is not null
  order by m.embedding <=> query_embedding
  limit greatest(least(coalesce(match_count, 8), 100), 1);
$$;

alter table public.memories enable row level security;
alter table public.memories force row level security;
alter table public.audit_logs enable row level security;
alter table public.audit_logs force row level security;

revoke all privileges on table public.memories from public, anon, authenticated;
revoke all privileges on table public.audit_logs from public, anon, authenticated;
grant select, insert, update, delete on table public.memories to service_role;
grant select, insert on table public.audit_logs to service_role;

revoke all on function public.set_memory_derived_fields()
  from public, anon, authenticated;
grant execute on function public.set_memory_derived_fields()
  to service_role;

revoke all on function public.match_memories(vector, integer, text)
  from public, anon, authenticated;
grant execute on function public.match_memories(vector, integer, text)
  to service_role;

commit;
