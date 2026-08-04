-- Supabase SQL Editor에서 한 번 실행하세요.

create extension if not exists vector;

create table if not exists memories (
  id uuid primary key default gen_random_uuid(),
  source text not null default 'note',        -- 'slack' | 'email' | 'note'
  content text not null,                       -- 검색/답변에 쓰이는 본문 (청크 단위)
  metadata jsonb not null default '{}'::jsonb, -- sender, channel, subject, msg_date, batch_id 등
  embedding vector(1536),
  created_at timestamptz not null default now()
);

create index if not exists memories_embedding_idx
  on memories using hnsw (embedding vector_cosine_ops);

create index if not exists memories_source_idx on memories (source);

-- 벡터 유사도 검색 함수 (코사인)
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
  where filter_source is null or m.source = filter_source
  order by m.embedding <=> query_embedding
  limit match_count;
$$;
