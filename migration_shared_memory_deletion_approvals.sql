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
