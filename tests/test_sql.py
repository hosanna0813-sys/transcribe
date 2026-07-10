"""SQL migration 正確性(需 Postgres):帳務退款來源、試用 RPC、CHECK 約束。
設 DATABASE_URL 才會執行;CI 以 postgres service 提供。本機無 psycopg 則跳過。"""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DB = os.environ.get("DATABASE_URL")
if not DB:
    pytest.skip("需要 DATABASE_URL", allow_module_level=True)

SCHEMA_FILES = ["schema.sql", "schema_phase2.sql", "schema_phase3.sql",
                "schema_phase4.sql", "schema_phase5.sql", "schema_history.sql",
                "schema_pay.sql", "schema_phase6.sql", "schema_phase7.sql"]
V2 = os.path.join(os.path.dirname(__file__), "..", "v2")

STUB = """
create schema if not exists auth;
create table if not exists auth.users(id uuid primary key, email text);
create or replace function auth.uid() returns uuid language sql stable as
  $$ select nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role; end if;
end $$;
"""


@pytest.fixture(scope="module")
def conn():
    c = psycopg.connect(DB, autocommit=True)
    c.execute(STUB)
    for fn in SCHEMA_FILES:
        with open(os.path.join(V2, fn), encoding="utf-8") as f:
            c.execute(f.read())
    yield c
    c.close()


def _new_user(conn):
    uid = uuid.uuid4()
    conn.execute("insert into auth.users(id,email) values (%s,%s)", (uid, f"{uid}@t.c"))
    conn.execute("""insert into public.credit_balances(user_id,remaining_credits,free_used_today,free_date)
                    values (%s,%s,0,null)
                    on conflict (user_id) do update set remaining_credits=%s,free_used_today=0,free_date=null""",
                 (uid, 1000, 1000))
    return uid


def _reserve(conn, uid, cost, free_limit):
    ok, jid = conn.execute(
        "select ok, usage_id from public.reserve_transcription(%s,%s,'upload','x',%s,%s)",
        (uid, cost, cost, free_limit)).fetchone()
    return ok, jid


def test_refund_all_free_no_paid_created(conn):
    """全免費任務跑短:不得產生付費 credits、免費用量要退回"""
    uid = _new_user(conn)
    conn.execute("update public.credit_balances set remaining_credits=0 where user_id=%s", (uid,))
    ok, jid = _reserve(conn, uid, 300, 600)
    assert ok
    conn.execute("select ok from public.complete_transcription(%s,%s,100,0.01,'t')", (jid, uid))
    bal, free = conn.execute(
        "select remaining_credits, free_used_today from public.credit_balances where user_id=%s", (uid,)).fetchone()
    assert bal == 0, f"付費餘額不應被灌入免費退款,得到 {bal}"
    assert free == 100, f"免費用量應退到 100,得到 {free}"
    n = conn.execute("select count(*) from public.credit_transactions where related_usage_id=%s and type='refund'", (jid,)).fetchone()[0]
    assert n == 0


def test_refund_mixed_free_paid(conn):
    uid = _new_user(conn)   # 餘額 1000
    ok, jid = _reserve(conn, uid, 300, 100)   # 免費100 + 付費200
    assert ok
    assert conn.execute("select remaining_credits from public.credit_balances where user_id=%s", (uid,)).fetchone()[0] == 800
    conn.execute("select ok from public.complete_transcription(%s,%s,150,0.02,'t')", (jid, uid))
    bal, free = conn.execute("select remaining_credits, free_used_today from public.credit_balances where user_id=%s", (uid,)).fetchone()
    assert bal == 950 and free == 100
    assert conn.execute("select charged_free, charged_paid from public.usage_logs where id=%s", (jid,)).fetchone() == (100, 50)


def test_trial_rpcs(conn):
    keys = ["9.8.7.6", "dev:" + str(uuid.uuid4())]
    assert conn.execute("select public.trial_remaining(%s, 600)", (keys,)).fetchone()[0] == 600
    assert conn.execute("select ok, reason from public.trial_reserve(%s, 100, 600, 100000)", (keys,)).fetchone()[0] is True
    assert conn.execute("select public.trial_remaining(%s, 600)", (keys[:1],)).fetchone()[0] == 500
    ok, reason = conn.execute("select ok, reason from public.trial_reserve(%s, 600, 600, 100000)", (keys[:1],)).fetchone()
    assert ok is False and reason == "ip"
    ok, reason = conn.execute("select ok, reason from public.trial_reserve(%s, 100, 600, 150)", (["1.1.1.9"],)).fetchone()
    assert ok is False and reason == "global"
    conn.execute("select public.trial_refund(%s, 100)", (keys,))
    assert conn.execute("select public.trial_remaining(%s, 600)", (keys[:1],)).fetchone()[0] == 600


def test_cost_stats_persisted(conn):
    uid = _new_user(conn)
    ok, jid = _reserve(conn, uid, 300, 0)
    assert ok
    conn.execute("""select ok from public.complete_transcription(
        %s,%s,300,0.05,'t',1200,800,'{"total_usd":0.05}'::jsonb)""", (jid, uid))
    pt, ct, cost = conn.execute(
        "select prompt_tokens, completion_tokens, estimated_openai_cost_usd from public.usage_logs where id=%s",
        (jid,)).fetchone()
    assert pt == 1200 and ct == 800 and float(cost) == 0.05
    # 對帳檢視可讀
    assert conn.execute("select prompt_tokens from public.job_cost_summary where id=%s", (jid,)).fetchone()[0] == 1200


def test_check_constraints(conn):
    uid = _new_user(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update public.profiles set role='superuser' where id=%s", (uid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update public.credit_balances set remaining_credits=-1 where user_id=%s", (uid,))
