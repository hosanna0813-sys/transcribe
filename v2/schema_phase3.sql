-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段三:長音檔背景任務所需的資料庫調整
--
-- 前置:先跑過 schema.sql、schema_phase2.sql(、schema_phase4.sql)。
-- 用法:在 Supabase SQL Editor 貼上整份執行一次。可重複執行。
-- =============================================================

-- 逐字稿暫存欄位:背景任務完成後把結果暫放這裡,前端取走後即清空(NULL),
-- 維持零保存原則;也讓 Render 萬一重啟後剛完成的結果不致遺失。
alter table public.usage_logs add column if not exists result_text text;

-- ---------- 改寫 complete_transcription:多收 result_text ----------
-- 先移除階段二的 4 參數版本,改成 5 參數(result_text 有預設值,
-- 階段二的 /api/transcribe 仍可用 4 個參數呼叫,不會破壞)。
drop function if exists public.complete_transcription(uuid, uuid, integer, numeric);

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

  v_charge := least(coalesce(p_actual_seconds, v_reserved), v_reserved);
  v_refund := v_reserved - v_charge;

  update public.usage_logs
     set status = 'completed',
         credits_charged = v_charge,
         estimated_openai_cost_usd = p_cost_usd,
         result_text = p_result_text,
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

revoke all on function public.complete_transcription(uuid,uuid,integer,numeric,text) from public, anon, authenticated;
grant execute on function public.complete_transcription(uuid,uuid,integer,numeric,text) to service_role;

-- ---------- 取走並清空逐字稿(前端拿到結果後呼叫,去內容化) ----------
create or replace function public.claim_result(p_usage_id uuid, p_user_id uuid)
returns table (result_text text, status text, credits_charged integer)
language plpgsql
security definer set search_path = public
as $$
declare
  v_text text;
  v_status text;
  v_charged integer;
begin
  select u.result_text, u.status, u.credits_charged
    into v_text, v_status, v_charged
    from public.usage_logs u
   where u.id = p_usage_id and u.user_id = p_user_id
   for update;
  if not found then
    return;
  end if;
  -- 讀到就清空內容欄位(帳務欄位保留)
  if v_text is not null then
    update public.usage_logs set result_text = null where id = p_usage_id;
  end if;
  return query select v_text, v_status, v_charged;
end;
$$;

revoke all on function public.claim_result(uuid,uuid) from public, anon, authenticated;
grant execute on function public.claim_result(uuid,uuid) to service_role;
