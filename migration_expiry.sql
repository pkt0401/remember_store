-- 레거시 마이그레이션: 기존 프로젝트에 유통기한 기능만 추가합니다.
-- 보안, 중복 방지, 감사 로그까지 적용하려면 이어서 migration_security.sql을
-- 실행하세요. 새 프로젝트는 schema.sql만 실행하면 됩니다.

begin;

alter table public.memories add column if not exists expires_at timestamptz;

create index if not exists memories_expires_at_idx
  on public.memories (expires_at)
  where expires_at is not null;

-- 만료된 기억은 검색에서 제외하도록 함수 갱신
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

revoke all on function public.match_memories(vector, integer, text)
  from public, anon, authenticated;
grant execute on function public.match_memories(vector, integer, text)
  to service_role;

commit;
