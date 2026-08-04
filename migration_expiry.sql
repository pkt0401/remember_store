-- 기존 프로젝트에 유통기한 기능 추가. Supabase SQL Editor에서 실행하세요.

alter table memories add column if not exists expires_at timestamptz;

-- 만료된 기억은 검색에서 제외하도록 함수 갱신
create or replace function match_memories(
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
language sql stable
as $$
  select
    m.id, m.source, m.content, m.metadata, m.created_at,
    1 - (m.embedding <=> query_embedding) as similarity
  from memories m
  where (filter_source is null or m.source = filter_source)
    and (m.expires_at is null or m.expires_at > now())
  order by m.embedding <=> query_embedding
  limit match_count;
$$;

grant execute on function public.match_memories to service_role;
