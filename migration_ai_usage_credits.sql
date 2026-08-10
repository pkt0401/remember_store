-- Run after migration_memory_scopes.sql and migration_auth_accounts.sql.
-- This script is safe to rerun:
-- existing balances are preserved and only NULL balances are initialized.

begin;

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

comment on column public.account_profiles.remaining_uses is
  'Remaining chargeable AI operations. New accounts start with 10.';

commit;
