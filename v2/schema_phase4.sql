-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段四:管理員加值函式
--
-- 前置:先跑過 schema.sql 與 schema_phase2.sql。
-- 用法:在 Supabase SQL Editor 貼上整份執行一次。可重複執行。
-- =============================================================

-- ---------- 管理員加值(security definer,只給後端 service_role) ----------
-- 安全:函式內再次確認呼叫者是 admin(縱深防禦);冪等:同一 idempotency_key
-- 不重複加值;以 email 找目標使用者。
create or replace function public.admin_add_credits(
  p_caller uuid,
  p_email  text,
  p_amount integer,
  p_reason text,
  p_idem   text
)
returns table (ok boolean, remaining integer, target_email text, reason text)
language plpgsql
security definer set search_path = public
as $$
declare
  v_caller_role text;
  v_target      uuid;
  v_balance     integer;
  v_type        text;
begin
  if p_amount is null or p_amount = 0 then
    return query select false, null::integer, null::text, 'invalid_amount'; return;
  end if;

  -- 呼叫者必須是 admin
  select role into v_caller_role from public.profiles where id = p_caller;
  if v_caller_role is distinct from 'admin' then
    return query select false, null::integer, null::text, 'not_admin'; return;
  end if;

  -- 冪等:同一 idempotency_key 已處理過就直接回現況(防連點/重試重複加值)
  if p_idem is not null and p_idem <> '' then
    if exists (select 1 from public.credit_transactions where idempotency_key = p_idem) then
      select ct.user_id into v_target from public.credit_transactions ct
        where ct.idempotency_key = p_idem limit 1;
      select remaining_credits into v_balance from public.credit_balances where user_id = v_target;
      return query select true, v_balance,
        (select email from auth.users where id = v_target), 'duplicate';
      return;
    end if;
  end if;

  -- 以 email 找目標使用者(需對方已登入過至少一次)
  select id into v_target from auth.users where lower(email) = lower(trim(p_email));
  if v_target is null then
    return query select false, null::integer, null::text, 'user_not_found'; return;
  end if;

  -- 確保有餘額列後加值
  insert into public.credit_balances (user_id, remaining_credits)
    values (v_target, 0) on conflict (user_id) do nothing;
  update public.credit_balances
     set remaining_credits = remaining_credits + p_amount, updated_at = now()
   where user_id = v_target
  returning remaining_credits into v_balance;

  v_type := case when p_amount > 0 then 'add' else 'adjust' end;
  begin
    insert into public.credit_transactions (user_id, type, amount_credits, reason, idempotency_key)
    values (v_target, v_type, p_amount,
            coalesce(nullif(trim(p_reason), ''), 'admin_add'),
            nullif(p_idem, ''));
  exception when unique_violation then
    -- 極少數併發:另一請求已用同一 key 加過,退回本次多加的量
    update public.credit_balances
       set remaining_credits = remaining_credits - p_amount, updated_at = now()
     where user_id = v_target
    returning remaining_credits into v_balance;
    return query select true, v_balance,
      (select email from auth.users where id = v_target), 'duplicate';
    return;
  end;

  return query select true, v_balance,
    (select email from auth.users where id = v_target), null::text;
end;
$$;

revoke all on function public.admin_add_credits(uuid,text,integer,text,text) from public, anon, authenticated;
grant execute on function public.admin_add_credits(uuid,text,integer,text,text) to service_role;

-- =============================================================
-- 【設定管理員】把自己設為 admin(加值介面才會出現)。
-- 把 email 換成你的登入信箱,取消註解執行一次。
-- =============================================================
-- update public.profiles set role = 'admin', updated_at = now()
--   where id = (select id from auth.users where email = 'YOUR_EMAIL@example.com');
