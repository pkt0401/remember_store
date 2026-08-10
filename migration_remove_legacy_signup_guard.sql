-- 기존 프로젝트에서 정상 회원가입을 차단하는 레거시 Auth 가드를 중립화합니다.
-- auth.users의 소유권이나 함수의 다른 종속 객체를 건드리지 않도록 트리거를
-- 삭제하지 않고, 소유권이 확인된 함수 본문만 통과 동작으로 교체합니다.

begin;

create or replace function public.require_managed_auth_signup()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  return new;
end;
$$;

alter function public.require_managed_auth_signup() owner to postgres;
revoke all on function public.require_managed_auth_signup()
  from public, anon, authenticated;

commit;
