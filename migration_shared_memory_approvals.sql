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
    on conflict (proposal_id, approver_user_id) do nothing;
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
