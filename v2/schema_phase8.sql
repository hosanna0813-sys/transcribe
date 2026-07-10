-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段八:背景任務佇列(資料庫佇列 + 就地 Worker)
--
-- 前置:先跑過 schema.sql ~ schema_phase7。用法:SQL Editor 貼上執行(可重複)。
--
-- Render 免費方案沒有獨立 Worker/Cron 服務,因此 Worker 以「單一 Web 實例內的
-- 專責執行緒」實作;可靠性靠資料庫佇列 + 心跳 + 重試 + 取消,而非另一個行程。
--
-- 任務生命週期:queued → processing →(completed / failed / cancelled)。
--  * enqueue_transcription:預扣額度 + 建立 queued 任務(帶輸入:YT 網址/起訖、上傳暫存路徑、是否校正)
--  * claim_next_job:原子認領最舊 queued → processing(設 started_at/heartbeat);
--                   並把「心跳過期且未逾重試上限」的 processing 退回 queued(retry_count+1)
--  * job_heartbeat:更新心跳,回傳目前狀態(worker 用來偵測被取消)
--  * cancel_job:使用者取消 → 退款 + 標 cancelled
--  * sweep_jobs:心跳過期且逾重試上限的 processing → 失敗退款;並清過期逐字稿
-- 退款一律「按來源」:免費退今日免費計數、付費退付費餘額(與階段六/七一致)。
-- =============================================================

-- ---------- 任務生命週期欄位 ----------
alter table public.usage_logs add column if not exists started_at   timestamptz;
alter table public.usage_logs add column if not exists heartbeat_at timestamptz;
alter table public.usage_logs add column if not exists retry_count  integer not null default 0;
alter table public.usage_logs add column if not exists error_code   text;
alter table public.usage_logs add column if not exists upload_path  text;      -- 上傳檔暫存路徑(worker 讀;跨重啟會失效)
alter table public.usage_logs add column if not exists correction_requested boolean not null default false;

-- status 允許值加入 'queued'(階段六的 CHECK 未含)
alter table public.usage_logs drop constraint if exists usage_status_chk;
alter table public.usage_logs add constraint usage_status_chk
  check (status in ('reserved','queued','processing','completed','failed','refunded','cancelled','expired'));

-- 「同帳號同時只一個進行中任務」也要涵蓋 queued
drop index if exists one_active_job;
create unique index one_active_job
  on public.usage_logs (user_id)
  where status in ('reserved', 'queued', 'processing');

create index if not exists usage_logs_queue on public.usage_logs (status, created_at)
  where status in ('queued', 'processing');

-- ---------- 共用退款(免費/付費按來源退回);內部函式 ----------
create or replace function public._refund_job(p_usage_id uuid)
returns void
language plpgsql
security definer set search_path = public
as $$
declare
  v_uid uuid; v_reserved int; v_free int; v_paid int;
begin
  select user_id, credits_reserved, coalesce(free_credits,0)
    into v_uid, v_reserved, v_free
    from public.usage_logs where id = p_usage_id;
  if not found then return; end if;
  v_paid := coalesce(v_reserved,0) - coalesce(v_free,0);
  if v_paid > 0 then
    update public.credit_balances set remaining_credits = remaining_credits + v_paid, updated_at = now()
      where user_id = v_uid;
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
      values (v_uid, 'refund', v_paid, 'transcribe_refund', p_usage_id);
  end if;
  if coalesce(v_free,0) > 0 then
    update public.credit_balances set free_used_today = greatest(0, free_used_today - v_free), updated_at = now()
      where user_id = v_uid;
  end if;
end;
$$;

-- ---------- enqueue:預扣額度 + 建立 queued 任務 ----------
create or replace function public.enqueue_transcription(
  p_user_id     uuid,
  p_cost        integer,
  p_source_type text,
  p_source_name text,
  p_duration    integer,
  p_free_limit  integer default 0,
  p_youtube_url text default null,
  p_start       integer default null,
  p_end         integer default null,
  p_upload_path text default null,
  p_correct     boolean default false
)
returns table (ok boolean, usage_id uuid, remaining integer, reason text)
language plpgsql
security definer set search_path = public
as $$
declare
  v_today      date := (now() at time zone 'Asia/Taipei')::date;
  v_free_used  integer; v_free_date date;
  v_free_avail integer; v_free_use integer; v_paid_need integer;
  v_new_balance integer; v_usage_id uuid;
begin
  if p_cost is null or p_cost <= 0 then
    return query select false, null::uuid, null::integer, 'invalid_cost'; return;
  end if;

  select free_used_today, free_date into v_free_used, v_free_date
    from public.credit_balances where user_id = p_user_id for update;
  if not found then
    insert into public.credit_balances (user_id, remaining_credits) values (p_user_id, 0)
      on conflict (user_id) do nothing;
    v_free_used := 0; v_free_date := null;
  end if;
  if v_free_date is distinct from v_today then v_free_used := 0; end if;

  v_free_avail := greatest(0, coalesce(p_free_limit,0) - v_free_used);
  v_free_use   := least(v_free_avail, p_cost);
  v_paid_need  := p_cost - v_free_use;

  update public.credit_balances
     set remaining_credits = remaining_credits - v_paid_need,
         free_used_today = v_free_used + v_free_use, free_date = v_today, updated_at = now()
   where user_id = p_user_id and remaining_credits >= v_paid_need
  returning remaining_credits into v_new_balance;
  if not found then
    return query select false, null::uuid,
      (select remaining_credits from public.credit_balances where user_id = p_user_id),
      'insufficient_credits';
    return;
  end if;

  begin
    insert into public.usage_logs
      (user_id, source_type, source_name, youtube_url, start_seconds, end_seconds,
       duration_seconds, credits_reserved, free_credits, transcription_model,
       upload_path, correction_requested, status)
    values
      (p_user_id, p_source_type, p_source_name, p_youtube_url, p_start, p_end,
       p_duration, p_cost, v_free_use, 'whisper-1',
       p_upload_path, coalesce(p_correct,false), 'queued')
    returning id into v_usage_id;
  exception when unique_violation then
    update public.credit_balances
       set remaining_credits = remaining_credits + v_paid_need, free_used_today = v_free_used, updated_at = now()
     where user_id = p_user_id;
    return query select false, null::uuid,
      (select remaining_credits from public.credit_balances where user_id = p_user_id),
      'active_job_exists';
    return;
  end;

  if v_paid_need > 0 then
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
      values (p_user_id, 'deduct', -v_paid_need, 'transcribe_reserve', v_usage_id);
  end if;
  return query select true, v_usage_id, v_new_balance, null::text;
end;
$$;

-- ---------- claim:認領一筆 queued(先把可重試的心跳過期任務退回佇列) ----------
create or replace function public.claim_next_job(p_stale_sec integer, p_max_retry integer)
returns table (usage_id uuid, user_id uuid, source_type text, youtube_url text,
               start_seconds integer, end_seconds integer, upload_path text,
               correction_requested boolean, duration_seconds integer, retry_count integer)
language plpgsql
security definer set search_path = public
as $$
begin
  -- 心跳過期且未逾重試上限的 processing → 退回 queued(視為一次重試)
  update public.usage_logs ul
     set status = 'queued', retry_count = ul.retry_count + 1, started_at = null, heartbeat_at = null
   where ul.status = 'processing'
     and ul.heartbeat_at is not null
     and ul.heartbeat_at < now() - make_interval(secs => p_stale_sec)
     and ul.retry_count < p_max_retry;

  return query
  with pick as (
    select id from public.usage_logs
     where status = 'queued'
     order by created_at asc
     for update skip locked
     limit 1
  )
  update public.usage_logs u
     set status = 'processing', started_at = now(), heartbeat_at = now()
    from pick
   where u.id = pick.id
  returning u.id, u.user_id, u.source_type, u.youtube_url, u.start_seconds, u.end_seconds,
            u.upload_path, u.correction_requested, u.duration_seconds, u.retry_count;
end;
$$;

-- ---------- heartbeat:更新心跳,回傳目前狀態(worker 據此偵測取消) ----------
create or replace function public.job_heartbeat(p_usage_id uuid)
returns text
language plpgsql
security definer set search_path = public
as $$
declare v_status text;
begin
  update public.usage_logs set heartbeat_at = now()
   where id = p_usage_id and status = 'processing'
  returning status into v_status;
  if not found then
    select status into v_status from public.usage_logs where id = p_usage_id;
  end if;
  return v_status;
end;
$$;

-- ---------- cancel:使用者取消 → 退款 + 標 cancelled ----------
create or replace function public.cancel_job(p_usage_id uuid, p_user_id uuid)
returns table (ok boolean, remaining integer)
language plpgsql
security definer set search_path = public
as $$
declare v_bal integer;
begin
  perform 1 from public.usage_logs
    where id = p_usage_id and user_id = p_user_id and status in ('queued','processing') for update;
  if not found then
    return query select false, (select remaining_credits from public.credit_balances where user_id = p_user_id);
    return;
  end if;
  perform public._refund_job(p_usage_id);
  update public.usage_logs
     set status = 'cancelled', error_code = 'user_cancelled', completed_at = now()
   where id = p_usage_id;
  select remaining_credits into v_bal from public.credit_balances where user_id = p_user_id;
  return query select true, v_bal;
end;
$$;

-- ---------- worker 出錯時:未逾重試上限 → 退回佇列;否則失敗退款 ----------
create or replace function public.job_retry_or_fail(p_usage_id uuid, p_max_retry integer, p_error text)
returns text
language plpgsql
security definer set search_path = public
as $$
declare v_rc integer; v_status text;
begin
  select retry_count, status into v_rc, v_status
    from public.usage_logs where id = p_usage_id for update;
  if not found or v_status <> 'processing' then
    return coalesce(v_status, 'gone');
  end if;
  if v_rc < p_max_retry then
    update public.usage_logs
       set status = 'queued', retry_count = retry_count + 1, started_at = null, heartbeat_at = null,
           error_code = left(coalesce(p_error,''), 50)
     where id = p_usage_id;
    return 'requeued';
  end if;
  perform public._refund_job(p_usage_id);
  update public.usage_logs
     set status = 'failed', error_code = 'max_retry', error_message = left(coalesce(p_error,''), 500),
         completed_at = now()
   where id = p_usage_id;
  return 'failed';
end;
$$;

-- ---------- sweep:心跳過期且逾重試上限 → 失敗退款;並清過期逐字稿 ----------
create or replace function public.sweep_jobs(p_stale_sec integer, p_max_retry integer)
returns integer
language plpgsql
security definer set search_path = public
as $$
declare r record; v_n integer := 0;
begin
  for r in
    select id from public.usage_logs
     where status = 'processing'
       and heartbeat_at is not null
       and heartbeat_at < now() - make_interval(secs => p_stale_sec)
       and retry_count >= p_max_retry
     for update skip locked
  loop
    perform public._refund_job(r.id);
    update public.usage_logs set status = 'failed', error_code = 'stale_timeout',
           error_message = 'worker heartbeat timeout', completed_at = now()
      where id = r.id;
    v_n := v_n + 1;
  end loop;
  -- 也把「從未開始跑」的孤兒 queued(建立後很久沒被認領,通常是長時間沒有 worker)退款失敗
  for r in
    select id from public.usage_logs
     where status = 'queued'
       and created_at < now() - make_interval(secs => p_stale_sec * (p_max_retry + 2))
     for update skip locked
  loop
    perform public._refund_job(r.id);
    update public.usage_logs set status = 'failed', error_code = 'queue_timeout',
           error_message = 'queued too long', completed_at = now()
      where id = r.id;
    v_n := v_n + 1;
  end loop;
  perform public.expire_results();
  return v_n;
end;
$$;

-- 權限:一律只給後端 service_role
revoke all on function public._refund_job(uuid) from public, anon, authenticated;
revoke all on function public.enqueue_transcription(uuid,integer,text,text,integer,integer,text,integer,integer,text,boolean) from public, anon, authenticated;
revoke all on function public.claim_next_job(integer,integer) from public, anon, authenticated;
revoke all on function public.job_heartbeat(uuid) from public, anon, authenticated;
revoke all on function public.cancel_job(uuid,uuid) from public, anon, authenticated;
revoke all on function public.sweep_jobs(integer,integer) from public, anon, authenticated;
revoke all on function public.job_retry_or_fail(uuid,integer,text) from public, anon, authenticated;
grant execute on function public.enqueue_transcription(uuid,integer,text,text,integer,integer,text,integer,integer,text,boolean) to service_role;
grant execute on function public.claim_next_job(integer,integer) to service_role;
grant execute on function public.job_heartbeat(uuid) to service_role;
grant execute on function public.cancel_job(uuid,uuid) to service_role;
grant execute on function public.sweep_jobs(integer,integer) to service_role;
grant execute on function public.job_retry_or_fail(uuid,integer,text) to service_role;
