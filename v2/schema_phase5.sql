-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段五:每日免費額度 + 退款支援
--
-- 前置:先跑過 schema.sql、schema_phase2.sql、schema_phase3.sql、schema_phase4.sql。
-- 用法:在 Supabase SQL Editor 貼上整份執行一次。可重複執行。
--
-- 免費額度模型:每個帳號每天有 N 分鐘免費(用不完隔天重置,以台灣時間午夜為界)。
-- 轉錄成本先由「今日免費剩餘」抵扣,不足的部分才扣付費 credits。
-- 免費用量只是計數(free_used_today),不是付費 credits,因此不進 credit_transactions。
-- =============================================================

-- 免費額度計數欄位(掛在 credit_balances)
alter table public.credit_balances add column if not exists free_used_today integer not null default 0;
alter table public.credit_balances add column if not exists free_date date;
-- 記錄每筆任務用掉多少免費額度,失敗退款時要還回去
alter table public.usage_logs add column if not exists free_credits integer not null default 0;

-- ---------- 改寫預扣:先用免費額度、不足再扣付費 ----------
-- p_free_limit = 今日免費上限(credits/秒);由後端依 FREE_DAILY_MINUTES 帶入。
create or replace function public.reserve_transcription(
  p_user_id     uuid,
  p_cost        integer,
  p_source_type text,
  p_source_name text,
  p_duration    integer,
  p_free_limit  integer default 0
)
returns table (ok boolean, usage_id uuid, remaining integer, reason text)
language plpgsql
security definer set search_path = public
as $$
declare
  v_today     date := (now() at time zone 'Asia/Taipei')::date;
  v_free_used integer;
  v_free_date date;
  v_free_avail integer;
  v_free_use  integer;
  v_paid_need integer;
  v_new_balance integer;
  v_usage_id  uuid;
begin
  if p_cost is null or p_cost <= 0 then
    return query select false, null::uuid, null::integer, 'invalid_cost'; return;
  end if;

  -- 讀取(並鎖定)免費計數;跨日則歸零
  select free_used_today, free_date into v_free_used, v_free_date
    from public.credit_balances where user_id = p_user_id for update;
  if not found then
    insert into public.credit_balances (user_id, remaining_credits) values (p_user_id, 0)
      on conflict (user_id) do nothing;
    v_free_used := 0; v_free_date := null;
  end if;
  if v_free_date is distinct from v_today then
    v_free_used := 0;
  end if;

  v_free_avail := greatest(0, coalesce(p_free_limit, 0) - v_free_used);
  v_free_use   := least(v_free_avail, p_cost);
  v_paid_need  := p_cost - v_free_use;

  -- 原子扣付費(不足即 0 列),同時更新今日免費用量
  update public.credit_balances
     set remaining_credits = remaining_credits - v_paid_need,
         free_used_today = v_free_used + v_free_use,
         free_date = v_today,
         updated_at = now()
   where user_id = p_user_id
     and remaining_credits >= v_paid_need
  returning remaining_credits into v_new_balance;

  if not found then
    return query select false, null::uuid,
      (select remaining_credits from public.credit_balances where user_id = p_user_id),
      'insufficient_credits';
    return;
  end if;

  -- 建立任務;撞 one_active_job 唯一索引代表已有進行中任務 → 回滾
  begin
    insert into public.usage_logs
      (user_id, source_type, source_name, duration_seconds, credits_reserved,
       free_credits, transcription_model, status)
    values
      (p_user_id, p_source_type, p_source_name, p_duration, p_cost,
       v_free_use, 'whisper-1', 'processing')
    returning id into v_usage_id;
  exception when unique_violation then
    update public.credit_balances
       set remaining_credits = remaining_credits + v_paid_need,
           free_used_today = v_free_used,
           updated_at = now()
     where user_id = p_user_id;
    return query select false, null::uuid,
      (select remaining_credits from public.credit_balances where user_id = p_user_id),
      'active_job_exists';
    return;
  end;

  -- 流水帳:只記付費扣款部分(免費部分不是付費 credits)
  if v_paid_need > 0 then
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
    values (p_user_id, 'deduct', -v_paid_need, 'transcribe_reserve', v_usage_id);
  end if;

  return query select true, v_usage_id, v_new_balance, null::text;
end;
$$;

-- ---------- 改寫失敗退款:付費退回餘額、免費退回今日計數 ----------
create or replace function public.fail_transcription(
  p_usage_id uuid,
  p_user_id  uuid,
  p_reason   text
)
returns table (ok boolean, remaining integer)
language plpgsql
security definer set search_path = public
as $$
declare
  v_reserved integer;
  v_free     integer;
  v_paid     integer;
  v_balance  integer;
begin
  select credits_reserved, free_credits into v_reserved, v_free
    from public.usage_logs
   where id = p_usage_id and user_id = p_user_id and status in ('reserved','processing')
   for update;

  if not found then
    return query select false,
      (select remaining_credits from public.credit_balances where user_id = p_user_id);
    return;
  end if;

  v_paid := v_reserved - coalesce(v_free, 0);

  update public.usage_logs
     set status = 'failed', error_message = left(coalesce(p_reason,''), 500), completed_at = now()
   where id = p_usage_id;

  update public.credit_balances
     set remaining_credits = remaining_credits + v_paid,
         free_used_today = greatest(0, free_used_today - coalesce(v_free, 0)),
         updated_at = now()
   where user_id = p_user_id;

  if v_paid > 0 then
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
    values (p_user_id, 'refund', v_paid, 'transcribe_failed', p_usage_id);
  end if;

  select remaining_credits into v_balance from public.credit_balances where user_id = p_user_id;
  return query select true, v_balance;
end;
$$;

-- ---------- 讀取今日免費剩餘(給 /api/me 顯示) ----------
create or replace function public.free_remaining(p_user_id uuid, p_free_limit integer)
returns integer
language plpgsql
security definer set search_path = public
as $$
declare
  v_today date := (now() at time zone 'Asia/Taipei')::date;
  v_used integer; v_date date;
begin
  select free_used_today, free_date into v_used, v_date
    from public.credit_balances where user_id = p_user_id;
  if not found then return coalesce(p_free_limit,0); end if;
  if v_date is distinct from v_today then v_used := 0; end if;
  return greatest(0, coalesce(p_free_limit,0) - coalesce(v_used,0));
end;
$$;

-- 權限:新簽章沿用只給 service_role
revoke all on function public.reserve_transcription(uuid,integer,text,text,integer,integer) from public, anon, authenticated;
grant  execute on function public.reserve_transcription(uuid,integer,text,text,integer,integer) to service_role;
grant  execute on function public.free_remaining(uuid,integer) to service_role;
-- 移除舊的 5 參數 reserve(避免 PostgREST 呼叫時多載歧義)
drop function if exists public.reserve_transcription(uuid,integer,text,text,integer);
