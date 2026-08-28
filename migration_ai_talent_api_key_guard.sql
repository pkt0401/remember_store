-- Block both direct OpenAI (`sk-...`) and AI Talent gateway (`atl-...`)
-- credentials even when a client writes to Supabase without using the app API.

begin;

-- Keep writes out while existing gateway credentials are quarantined and the
-- trigger is replaced, so no `atl-...` row can slip through between those steps.
lock table
  public.memories,
  public.shared_memory_proposals,
  public.shared_memory_deletion_proposals
in share row exclusive mode;

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
    '(sk|atl)-[A-Za-z0-9_-]{20,}',
    '[REDACTED_AI_API_KEY]',
    'g'
  ),
  regexp_replace(
    m.content,
    '(sk|atl)-[A-Za-z0-9_-]{20,}',
    '[REDACTED_AI_API_KEY]',
    'g'
  ),
  null::text,
  regexp_replace(
    m.metadata::text,
    '(sk|atl)-[A-Za-z0-9_-]{20,}',
    '[REDACTED_AI_API_KEY]',
    'g'
  )::jsonb,
  null::vector(1536),
  m.expires_at,
  m.created_at,
  m.updated_at,
  m.scope,
  m.owner_user_id,
  m.created_by_user_id,
  'ai_api_key_pattern'
from public.memories as m
where (
  coalesce(m.source, '') || ' ' || coalesce(m.content, '') || ' '
  || coalesce(m.metadata::text, '')
) ~ '(^|[^A-Za-z0-9_-])(sk|atl)-[A-Za-z0-9_-]{20,}'
on conflict (id) do nothing;

-- Scrub every related quarantine row, including rows produced by an earlier
-- version of the OpenAI-only guard.
update public.quarantined_memories
set source = regexp_replace(
      source,
      '(sk|atl)-[A-Za-z0-9_-]{20,}',
      '[REDACTED_AI_API_KEY]',
      'g'
    ),
    content = regexp_replace(
      content,
      '(sk|atl)-[A-Za-z0-9_-]{20,}',
      '[REDACTED_AI_API_KEY]',
      'g'
    ),
    metadata = regexp_replace(
      metadata::text,
      '(sk|atl)-[A-Za-z0-9_-]{20,}',
      '[REDACTED_AI_API_KEY]',
      'g'
    )::jsonb,
    content_hash = null,
    embedding = null
where quarantine_reason in ('openai_api_key_pattern', 'ai_api_key_pattern');

-- Proposal rows can retain a second copy of the original shared-memory text.
-- Remove credential-bearing proposals after the corresponding memory has been
-- copied into quarantine; proposal approvals and linked memories cascade away.
delete from public.shared_memory_proposals as proposal
where (
  coalesce(proposal.source, '') || ' ' || coalesce(proposal.content, '')
) ~ '(^|[^A-Za-z0-9_-])(sk|atl)-[A-Za-z0-9_-]{20,}';

delete from public.memories as m
where (
  coalesce(m.source, '') || ' ' || coalesce(m.content, '') || ' '
  || coalesce(m.metadata::text, '')
) ~ '(^|[^A-Za-z0-9_-])(sk|atl)-[A-Za-z0-9_-]{20,}';

-- Deletion proposals are retained as history, but their snapshots must never
-- retain a credential. Closing them also prevents a stale approval workflow.
update public.shared_memory_deletion_proposals
set source_snapshot = regexp_replace(
      source_snapshot,
      '(sk|atl)-[A-Za-z0-9_-]{20,}',
      '[REDACTED_AI_API_KEY]',
      'g'
    ),
    content_snapshot = regexp_replace(
      content_snapshot,
      '(sk|atl)-[A-Za-z0-9_-]{20,}',
      '[REDACTED_AI_API_KEY]',
      'g'
    ),
    status = 'deleted',
    deleted_at = coalesce(deleted_at, now())
where (
  coalesce(source_snapshot, '') || ' ' || coalesce(content_snapshot, '')
) ~ '(^|[^A-Za-z0-9_-])(sk|atl)-[A-Za-z0-9_-]{20,}';

create or replace function public.set_memory_derived_fields()
returns trigger
language plpgsql
set search_path = public, extensions
as $$
begin
  if (
    coalesce(new.source, '') || ' ' || coalesce(new.content, '') || ' '
    || coalesce(new.metadata::text, '')
  ) ~ '(^|[^A-Za-z0-9_-])(sk|atl)-[A-Za-z0-9_-]{20,}' then
    raise exception using
      errcode = '22023',
      message = 'AI API 키처럼 보이는 값은 기억으로 저장할 수 없습니다.';
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

commit;
