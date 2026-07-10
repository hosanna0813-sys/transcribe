-- =============================================================
-- 語音轉逐字稿 計費版 v2 — 階段七:成本統計(記錄 tokens 與費率快照)
--
-- 前置:先跑過 schema.sql ~ schema_phase6。用法:SQL Editor 貼上執行(可重複)。
--
-- 擴充 complete_transcription:多收 GPT 校正的 prompt/completion tokens 與費率快照,
-- 寫入 usage_logs 既有欄位(prompt_tokens/completion_tokens/estimated_openai_cost_usd/
-- applied_pricing)。退款來源邏輯與階段六完全相同,只是多存成本明細。
-- =============================================================

-- 先移除階段六的 5 參數版,改成 8 參數(避免 PostgREST 多載歧義)
drop function if exists public.complete_transcription(uuid, uuid, integer, numeric, text);

create or replace function public.complete_transcription(
  p_usage_id          uuid,
  p_user_id           uuid,
  p_actual_seconds    integer,
  p_cost_usd          numeric,
  p_result_text       text    default null,
  p_prompt_tokens     integer default 0,
  p_completion_tokens integer default 0,
  p_pricing           jsonb   default null
)
returns table (ok boolean, remaining integer)
language plpgsql
security definer set search_path = public
as $$
declare
  v_reserved     integer;
  v_free_res     integer;
  v_paid_res     integer;
  v_charge       integer;
  v_charge_free  integer;
  v_charge_paid  integer;
  v_refund_free  integer;
  v_refund_paid  integer;
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
         prompt_tokens = p_prompt_tokens,
         completion_tokens = p_completion_tokens,
         applied_pricing = p_pricing,
         result_text = p_result_text,
         completed_at = now(),
         expires_at = now() + make_interval(hours => v_ttl_hours)
   where id = p_usage_id;

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

revoke all on function public.complete_transcription(uuid,uuid,integer,numeric,text,integer,integer,jsonb)
  from public, anon, authenticated;
grant execute on function public.complete_transcription(uuid,uuid,integer,numeric,text,integer,integer,jsonb)
  to service_role;

-- 對帳輔助檢視:每筆完成任務的成本與收費(僅 service_role 用;RLS 由底層表控管)
create or replace view public.job_cost_summary as
select id, user_id, created_at, completed_at, status,
       source_type, duration_seconds,
       credits_charged, charged_free, charged_paid,
       prompt_tokens, completion_tokens,
       estimated_openai_cost_usd,
       applied_pricing
  from public.usage_logs
 where status in ('completed','failed');
