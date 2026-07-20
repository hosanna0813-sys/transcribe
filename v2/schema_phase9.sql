-- =============================================================
-- 階段九:轉錄選項隨任務入列(時間戳 / 去贅詞 / 講者標記)
-- 冪等,可重複執行。前置:已跑過 schema_phase8.sql。
--
-- 目的:計費版前端功能對齊免費首頁 —— 選項需跟著任務進佇列,
-- worker 重啟/重試後仍讀得到(不靠記憶體)。
--  * usage_logs.options jsonb:{"timestamps":bool,"remove_fillers":bool,"speakers":bool}
--  * enqueue_transcription 加 p_options(舊簽名移除;簽名變更無法 create or replace)
--  * claim_next_job 回傳多帶 options(回傳型別變更,同樣需先 drop)
-- 未跑本檔前,後端會自動退回不帶 p_options 的舊簽名(選項採預設值),不影響建立任務。
-- =============================================================

alter table public.usage_logs add column if not exists options jsonb not null default '{}';

-- ---------- enqueue:預扣額度 + 建立 queued 任務(帶選項) ----------
drop function if exists public.enqueue_transcription(
  uuid, integer, text, text, integer, integer, text, integer, integer, text, boolean);

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
  p_correct     boolean default false,
  p_options     jsonb default '{}'
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
       upload_path, correction_requested, options, status)
    values
      (p_user_id, p_source_type, p_source_name, p_youtube_url, p_start, p_end,
       p_duration, p_cost, v_free_use, 'whisper-1',
       p_upload_path, coalesce(p_correct,false), coalesce(p_options,'{}'::jsonb), 'queued')
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

-- ---------- claim:認領一筆 queued(回傳多帶 options) ----------
drop function if exists public.claim_next_job(integer, integer);

create function public.claim_next_job(p_stale_sec integer, p_max_retry integer)
returns table (usage_id uuid, user_id uuid, source_type text, youtube_url text,
               start_seconds integer, end_seconds integer, upload_path text,
               correction_requested boolean, duration_seconds integer, retry_count integer,
               options jsonb)
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
            u.upload_path, u.correction_requested, u.duration_seconds, u.retry_count, u.options;
end;
$$;

-- ---------- 權限:僅 service_role 可執行(與階段八一致) ----------
revoke all on function public.enqueue_transcription(
  uuid,integer,text,text,integer,integer,text,integer,integer,text,boolean,jsonb)
  from public, anon, authenticated;
revoke all on function public.claim_next_job(integer,integer) from public, anon, authenticated;
grant execute on function public.enqueue_transcription(
  uuid,integer,text,text,integer,integer,text,integer,integer,text,boolean,jsonb) to service_role;
grant execute on function public.claim_next_job(integer,integer) to service_role;
