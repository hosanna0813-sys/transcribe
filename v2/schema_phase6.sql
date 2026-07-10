-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段六:帳務退款來源修正 + 約束 + 試用持久化 + 結果 TTL
--
-- 前置:先跑過 schema.sql ~ schema_phase5、schema_history、schema_pay。
-- 用法:Supabase SQL Editor 貼上整份執行一次。可重複執行(idempotent)。
--
-- 本檔修正一個帳務 bug 並補強一致性:
--  (1) complete_transcription 結算退差額時,未區分免費/付費來源,會把免費額度
--      「退成付費 credits」、且免費用量未退回 → 憑空產生付費餘額。改為按來源分別退回。
--  (2) 補記帳欄位:每筆任務記錄實際扣掉的免費/付費 credits。
--  (3) 補 CHECK 約束與索引,防止不合法狀態進資料庫。
--  (4) 免費試用改為資料庫持久化(重啟不歸零、多實例一致),取代後端記憶體字典。
--  (5) 逐字稿結果 TTL:提供清理函式,過期自動去內容化(帳務保留)。
-- =============================================================

-- ---------- (2) 記帳欄位:實際扣掉的免費/付費 ----------
alter table public.usage_logs add column if not exists charged_free integer;
alter table public.usage_logs add column if not exists charged_paid integer;
alter table public.usage_logs add column if not exists expires_at timestamptz;

-- ---------- (1) 修正 complete_transcription:免費退免費、付費退付費 ----------
-- 簽章與階段三相同(5 參數),後端呼叫方式不變;只改內部退款邏輯。
create or replace function public.complete_transcription(
  p_usage_id       uuid,
  p_user_id        uuid,
  p_actual_seconds integer,
  p_cost_usd       numeric,
  p_result_text    text default null
)
returns table (ok boolean, remaining integer)
language plpgsql
security definer set search_path = public
as $$
declare
  v_reserved     integer;
  v_free_res     integer;   -- 預留時用掉的免費
  v_paid_res     integer;   -- 預留時用掉的付費
  v_charge       integer;   -- 實際計費(不超過預留)
  v_charge_free  integer;   -- 實際計費中來自免費的部分
  v_charge_paid  integer;   -- 實際計費中來自付費的部分
  v_refund_free  integer;   -- 應退回免費計數
  v_refund_paid  integer;   -- 應退回付費餘額
  v_ttl_hours    integer := coalesce(nullif(current_setting('app.result_ttl_hours', true),'')::int, 24);
  v_balance      integer;
begin
  select credits_reserved, coalesce(free_credits, 0)
    into v_reserved, v_free_res
    from public.usage_logs
   where id = p_usage_id and user_id = p_user_id and status = 'processing'
   for update;

  if not found then
    return query select false,
      (select remaining_credits from public.credit_balances where user_id = p_user_id);
    return;
  end if;

  v_paid_res := v_reserved - v_free_res;
  v_charge := least(coalesce(p_actual_seconds, v_reserved), v_reserved);
  -- 免費「先用」:實際計費先從免費額度扣,超出才算付費(與預扣一致)
  v_charge_free := least(v_charge, v_free_res);
  v_charge_paid := v_charge - v_charge_free;
  v_refund_free := v_free_res - v_charge_free;
  v_refund_paid := v_paid_res - v_charge_paid;

  update public.usage_logs
     set status = 'completed',
         credits_charged = v_charge,
         charged_free = v_charge_free,
         charged_paid = v_charge_paid,
         estimated_openai_cost_usd = p_cost_usd,
         result_text = p_result_text,
         completed_at = now(),
         expires_at = now() + make_interval(hours => v_ttl_hours)
   where id = p_usage_id;

  -- 付費差額退回餘額(且僅此進流水帳);免費差額退回今日免費計數
  if v_refund_paid > 0 then
    update public.credit_balances
       set remaining_credits = remaining_credits + v_refund_paid, updated_at = now()
     where user_id = p_user_id;
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
    values (p_user_id, 'refund', v_refund_paid, 'transcribe_settle_diff', p_usage_id);
  end if;
  if v_refund_free > 0 then
    update public.credit_balances
       set free_used_today = greatest(0, free_used_today - v_refund_free), updated_at = now()
     where user_id = p_user_id;
  end if;

  select remaining_credits into v_balance from public.credit_balances where user_id = p_user_id;
  return query select true, v_balance;
end;
$$;

revoke all on function public.complete_transcription(uuid,uuid,integer,numeric,text) from public, anon, authenticated;
grant execute on function public.complete_transcription(uuid,uuid,integer,numeric,text) to service_role;

-- ---------- (3) CHECK 約束 + 索引(用 DO 區塊,重跑不報錯) ----------
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'profiles_role_chk') then
    alter table public.profiles add constraint profiles_role_chk check (role in ('user','admin'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'balances_nonneg_chk') then
    alter table public.credit_balances add constraint balances_nonneg_chk
      check (remaining_credits >= 0 and free_used_today >= 0);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'usage_status_chk') then
    alter table public.usage_logs add constraint usage_status_chk
      check (status in ('reserved','processing','completed','failed','refunded','cancelled','expired'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'usage_source_chk') then
    alter table public.usage_logs add constraint usage_source_chk
      check (source_type is null or source_type in ('upload','youtube'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'payments_status_chk') then
    alter table public.payments add constraint payments_status_chk
      check (status in ('pending','paid','failed','refunded'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'payments_credits_pos_chk') then
    alter table public.payments add constraint payments_credits_pos_chk
      check (credits_added is null or credits_added > 0);
  end if;
end $$;

create index if not exists usage_logs_user_created on public.usage_logs (user_id, created_at desc);
create index if not exists usage_logs_expires on public.usage_logs (expires_at) where result_text is not null;
create index if not exists credit_tx_user_created on public.credit_transactions (user_id, created_at desc);

-- handle_new_user 是 auth.users 觸發器(非 PostgREST 端點),仍收斂執行權限
revoke all on function public.handle_new_user() from public, anon, authenticated;

-- =============================================================
-- (4) 免費試用持久化(取代後端記憶體字典;重啟不歸零、多實例一致)
-- 計量鍵 key = 來源 IP 或 'dev:<uuid>';每個 key 每日一列。
-- =============================================================
create table if not exists public.trial_usage (
  key          text not null,
  day          date not null,
  used_seconds integer not null default 0,
  updated_at   timestamptz not null default now(),
  primary key (key, day),
  constraint trial_used_nonneg check (used_seconds >= 0)
);
create table if not exists public.trial_global (
  day          date primary key,
  used_seconds integer not null default 0,
  constraint trial_global_nonneg check (used_seconds >= 0)
);
alter table public.trial_usage  enable row level security;   -- 無政策 = 前端完全不可讀寫
alter table public.trial_global enable row level security;

-- 查詢剩餘(取所有 key 中最小剩餘)
create or replace function public.trial_remaining(p_keys text[], p_per_key_limit integer)
returns integer
language plpgsql
security definer set search_path = public
as $$
declare
  v_today date := (now() at time zone 'Asia/Taipei')::date;
  v_max_used integer;
begin
  select coalesce(max(used_seconds), 0) into v_max_used
    from public.trial_usage where key = any(p_keys) and day = v_today;
  return greatest(0, coalesce(p_per_key_limit,0) - v_max_used);
end;
$$;

-- 預留:每 key 與全站都要足夠才記入;不足回 (false, 'ip'/'global')
create or replace function public.trial_reserve(
  p_keys text[], p_cost integer, p_per_key_limit integer, p_total_limit integer)
returns table (ok boolean, reason text)
language plpgsql
security definer set search_path = public
as $$
declare
  v_today date := (now() at time zone 'Asia/Taipei')::date;
  k text;
  v_used integer;
  v_global integer;
begin
  if p_cost is null or p_cost <= 0 then
    return query select false, 'invalid_cost'; return;
  end if;
  -- 鎖定全站當日列(序列化同日並發),順手清掉舊日資料
  delete from public.trial_usage  where day < v_today;
  delete from public.trial_global where day < v_today;
  insert into public.trial_global(day, used_seconds) values (v_today, 0)
    on conflict (day) do nothing;
  select used_seconds into v_global from public.trial_global where day = v_today for update;

  foreach k in array p_keys loop
    select used_seconds into v_used from public.trial_usage
      where key = k and day = v_today for update;
    if coalesce(v_used, 0) + p_cost > p_per_key_limit then
      return query select false, 'ip'; return;
    end if;
  end loop;
  if p_total_limit > 0 and v_global + p_cost > p_total_limit then
    return query select false, 'global'; return;
  end if;

  foreach k in array p_keys loop
    insert into public.trial_usage(key, day, used_seconds) values (k, v_today, p_cost)
      on conflict (key, day) do update set used_seconds = public.trial_usage.used_seconds + p_cost,
                                           updated_at = now();
  end loop;
  update public.trial_global set used_seconds = used_seconds + p_cost where day = v_today;
  return query select true, null::text;
end;
$$;

-- 退款(轉錄失敗)
create or replace function public.trial_refund(p_keys text[], p_cost integer)
returns void
language plpgsql
security definer set search_path = public
as $$
declare
  v_today date := (now() at time zone 'Asia/Taipei')::date;
  k text;
begin
  if p_cost is null or p_cost <= 0 then return; end if;
  foreach k in array p_keys loop
    update public.trial_usage set used_seconds = greatest(0, used_seconds - p_cost), updated_at = now()
      where key = k and day = v_today;
  end loop;
  update public.trial_global set used_seconds = greatest(0, used_seconds - p_cost) where day = v_today;
end;
$$;

revoke all on function public.trial_remaining(text[],integer) from public, anon, authenticated;
revoke all on function public.trial_reserve(text[],integer,integer,integer) from public, anon, authenticated;
revoke all on function public.trial_refund(text[],integer) from public, anon, authenticated;
grant execute on function public.trial_remaining(text[],integer) to service_role;
grant execute on function public.trial_reserve(text[],integer,integer,integer) to service_role;
grant execute on function public.trial_refund(text[],integer) to service_role;

-- =============================================================
-- (5) 逐字稿結果 TTL:去內容化過期結果(帳務欄位保留),回傳清除筆數
-- 由後端定時呼叫;不刪列,只清 result_text(與「刪除紀錄內容」一致)。
-- =============================================================
create or replace function public.expire_results()
returns integer
language plpgsql
security definer set search_path = public
as $$
declare v_n integer;
begin
  update public.usage_logs
     set result_text = null
   where result_text is not null and expires_at is not null and expires_at < now();
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;
revoke all on function public.expire_results() from public, anon, authenticated;
grant execute on function public.expire_results() to service_role;
