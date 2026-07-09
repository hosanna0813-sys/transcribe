-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段二:扣點 RPC 函式
--
-- 前置:先跑過 schema.sql(五張表 + RLS)。
-- 用法:在 Supabase SQL Editor 貼上整份執行一次。可重複執行。
--
-- 這三個函式是「原子扣點 / 結算 / 退款」的核心(規畫書第 8、15 節):
--   * security definer:以擁有者身分執行、繞過 RLS;只授權給後端 service_role。
--   * 扣點以條件式 UPDATE 原子完成,避免併發 double-spend。
--   * 狀態轉換以 status 守門,避免重複結算 / 重複退款。
--   * 餘額、流水帳、usage_log 皆在同一交易內完成。
-- =============================================================

-- ---------- 預扣:原子扣款 + 建立任務 + 寫流水帳 ----------
-- 回傳一列:ok(是否成功)、usage_id、remaining、reason
create or replace function public.reserve_transcription(
  p_user_id     uuid,
  p_cost        integer,
  p_source_type text,
  p_source_name text,
  p_duration    integer
)
returns table (ok boolean, usage_id uuid, remaining integer, reason text)
language plpgsql
security definer set search_path = public
as $$
declare
  v_new_balance integer;
  v_usage_id    uuid;
begin
  if p_cost is null or p_cost <= 0 then
    return query select false, null::uuid, null::integer, 'invalid_cost'; return;
  end if;

  -- 原子條件式扣款:餘額不足時 0 列更新
  update public.credit_balances
     set remaining_credits = remaining_credits - p_cost,
         updated_at = now()
   where user_id = p_user_id
     and remaining_credits >= p_cost
  returning remaining_credits into v_new_balance;

  if not found then
    return query select false, null::uuid,
      (select remaining_credits from public.credit_balances where user_id = p_user_id),
      'insufficient_credits';
    return;
  end if;

  -- 建立任務(status=processing);撞到 one_active_job 唯一索引代表已有進行中任務
  begin
    insert into public.usage_logs
      (user_id, source_type, source_name, duration_seconds, credits_reserved,
       transcription_model, status)
    values
      (p_user_id, p_source_type, p_source_name, p_duration, p_cost,
       'whisper-1', 'processing')
    returning id into v_usage_id;
  exception when unique_violation then
    -- 回滾剛才的扣款(把錢加回去),回報已有進行中任務
    update public.credit_balances
       set remaining_credits = remaining_credits + p_cost, updated_at = now()
     where user_id = p_user_id;
    return query select false, null::uuid,
      (select remaining_credits from public.credit_balances where user_id = p_user_id),
      'active_job_exists';
    return;
  end;

  -- 流水帳:扣點
  insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
  values (p_user_id, 'deduct', -p_cost, 'transcribe_reserve', v_usage_id);

  return query select true, v_usage_id, v_new_balance, null::text;
end;
$$;

-- ---------- 結算:只在 processing 生效,退回多扣的差額 ----------
create or replace function public.complete_transcription(
  p_usage_id       uuid,
  p_user_id        uuid,
  p_actual_seconds integer,
  p_cost_usd       numeric
)
returns table (ok boolean, remaining integer)
language plpgsql
security definer set search_path = public
as $$
declare
  v_reserved integer;
  v_charge   integer;
  v_refund   integer;
  v_balance  integer;
begin
  -- 守門:只處理自己的、且仍在 processing 的任務(防重複結算)
  select credits_reserved into v_reserved
    from public.usage_logs
   where id = p_usage_id and user_id = p_user_id and status = 'processing'
   for update;

  if not found then
    return query select false,
      (select remaining_credits from public.credit_balances where user_id = p_user_id);
    return;
  end if;

  -- 實收 = 實際秒數(但不超過預扣);多扣的退回
  v_charge := least(coalesce(p_actual_seconds, v_reserved), v_reserved);
  v_refund := v_reserved - v_charge;

  update public.usage_logs
     set status = 'completed',
         credits_charged = v_charge,
         estimated_openai_cost_usd = p_cost_usd,
         completed_at = now()
   where id = p_usage_id;

  if v_refund > 0 then
    update public.credit_balances
       set remaining_credits = remaining_credits + v_refund, updated_at = now()
     where user_id = p_user_id;
    insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
    values (p_user_id, 'refund', v_refund, 'transcribe_settle_diff', p_usage_id);
  end if;

  select remaining_credits into v_balance from public.credit_balances where user_id = p_user_id;
  return query select true, v_balance;
end;
$$;

-- ---------- 失敗退款:只在未終態生效,退回全部預扣 ----------
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
  v_balance  integer;
begin
  -- 守門:只退未終態(reserved/processing)的任務(防重複退款)
  select credits_reserved into v_reserved
    from public.usage_logs
   where id = p_usage_id and user_id = p_user_id and status in ('reserved','processing')
   for update;

  if not found then
    return query select false,
      (select remaining_credits from public.credit_balances where user_id = p_user_id);
    return;
  end if;

  update public.usage_logs
     set status = 'failed', error_message = left(coalesce(p_reason,''), 500), completed_at = now()
   where id = p_usage_id;

  update public.credit_balances
     set remaining_credits = remaining_credits + v_reserved, updated_at = now()
   where user_id = p_user_id;

  insert into public.credit_transactions (user_id, type, amount_credits, reason, related_usage_id)
  values (p_user_id, 'refund', v_reserved, 'transcribe_failed', p_usage_id);

  select remaining_credits into v_balance from public.credit_balances where user_id = p_user_id;
  return query select true, v_balance;
end;
$$;

-- ---------- 權限:只給後端 service_role 呼叫,其他角色不可 ----------
revoke all on function public.reserve_transcription(uuid,integer,text,text,integer) from public, anon, authenticated;
revoke all on function public.complete_transcription(uuid,uuid,integer,numeric)      from public, anon, authenticated;
revoke all on function public.fail_transcription(uuid,uuid,text)                     from public, anon, authenticated;
grant execute on function public.reserve_transcription(uuid,integer,text,text,integer) to service_role;
grant execute on function public.complete_transcription(uuid,uuid,integer,numeric)     to service_role;
grant execute on function public.fail_transcription(uuid,uuid,text)                    to service_role;
