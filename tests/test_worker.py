"""背景 Worker 端對端(需 Postgres):enqueue → claim → 處理 → 結算 / 取消。
把 main._sb_rpc 導到本機資料庫(真跑 SQL RPC),並 mock probe/relay/whisper/切段。
無 DATABASE_URL 或 psycopg 則跳過。"""
import decimal
import os
import uuid

import threading

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb
DB = os.environ.get("DATABASE_URL")
if not DB:
    pytest.skip("需要 DATABASE_URL", allow_module_level=True)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

SCHEMA_FILES = ["schema.sql", "schema_phase2.sql", "schema_phase3.sql",
                "schema_phase4.sql", "schema_phase5.sql", "schema_history.sql",
                "schema_pay.sql", "schema_phase6.sql", "schema_phase7.sql", "schema_phase8.sql", "schema_phase9.sql"]
V2 = os.path.join(os.path.dirname(__file__), "..", "v2")
STUB = """
create schema if not exists auth;
create table if not exists auth.users(id uuid primary key, email text);
create or replace function auth.uid() returns uuid language sql stable as
  $$ select nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
do $$ begin
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role; end if;
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
end $$;
"""

SCALAR_FUNCS = {"job_heartbeat", "job_retry_or_fail", "sweep_jobs", "free_remaining",
                "expire_results", "trial_remaining"}


@pytest.fixture()
def dbmain(monkeypatch):
    import main
    conn = psycopg.connect(DB, autocommit=True)
    conn.execute(STUB)
    for fn in SCHEMA_FILES:
        conn.execute(open(os.path.join(V2, fn), encoding="utf-8").read())
    conn.execute("delete from public.usage_logs")   # 每個測試乾淨起步(claim 是全域取最舊 queued)
    lock = threading.Lock()   # 一條連線,跨執行緒(心跳)存取要序列化

    def _adapt(v):
        if isinstance(v, dict):
            return Jsonb(v)                 # dict → jsonb(生產走 JSON)
        if isinstance(v, float):
            return decimal.Decimal(str(v))  # float → numeric(對上 p_cost_usd 型別)
        return v

    def sb_rpc(fn, payload):
        # 生產環境 _sb_rpc 走 HTTP+JSON;此處模擬型別對應(jsonb / numeric)
        p = {k: _adapt(v) for k, v in payload.items()}
        args = ", ".join(f"{k} => %({k})s" for k in payload)
        with lock:
            cur = conn.execute(f"select * from public.{fn}({args})", p)
            rows = cur.fetchall()
            cols = [d.name for d in cur.description] if cur.description else []
        if fn in SCALAR_FUNCS:
            return rows[0][0] if rows else None
        return [dict(zip(cols, r)) for r in rows]

    def sb_balance(uid):
        with lock:
            r = conn.execute("select remaining_credits from public.credit_balances where user_id=%s", (uid,)).fetchone()
        return r[0] if r else 0

    monkeypatch.setattr(main, "_sb_rpc", sb_rpc)
    monkeypatch.setattr(main, "_sb_balance", sb_balance)
    monkeypatch.setattr(main, "_verify_jwt", lambda a: getattr(main, "_TEST_UID", None))
    monkeypatch.setattr(main, "_user_rate_check", lambda uid: None)
    monkeypatch.setattr(main, "rate_check", lambda b, r: None)
    monkeypatch.setattr(main, "_content_length_precheck", lambda r: None)
    monkeypatch.setattr(main, "_relay_info", lambda url: {"duration": 600, "too_long": False})
    monkeypatch.setattr(main, "_reject_live", lambda meta: None)
    monkeypatch.setattr(main, "_relay_fetch_audio", lambda url, s, e, dest: open(dest, "wb").write(b"x"))
    monkeypatch.setattr(main, "_transcode_and_segment",
                        lambda src, tmp, normalized=False: ["seg0", "seg1"])
    monkeypatch.setattr(main, "_ffprobe_seconds", lambda p: 600.0)
    monkeypatch.setattr(main, "_whisper_transcribe", lambda p, k: "逐字稿片段")
    monkeypatch.setattr(main, "_gpt_correct",
                        lambda t, k, remove_fillers=False, speakers=False, usage=None,
                        on_progress=None: t + "（校正）")
    yield main, conn
    conn.close()


def _mk_user(conn, credits=100000):
    uid = uuid.uuid4()
    conn.execute("insert into auth.users(id,email) values (%s,%s)", (uid, f"{uid}@t.c"))
    conn.execute("""insert into public.credit_balances(user_id,remaining_credits) values (%s,%s)
                    on conflict (user_id) do update set remaining_credits=%s,free_used_today=0,free_date=null""",
                 (uid, credits, credits))
    return uid


def _client(main):
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_enqueue_worker_complete(dbmain):
    main, conn = dbmain
    uid = _mk_user(conn)
    main._TEST_UID = str(uid)
    c = _client(main)
    # 建立 YouTube 任務 → 入列
    r = c.post("/api/jobs", data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "correct": "true"},
               headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert conn.execute("select status from public.usage_logs where id=%s", (jid,)).fetchone()[0] == "queued"
    # 餘額已預扣 600
    assert conn.execute("select remaining_credits from public.credit_balances where user_id=%s", (uid,)).fetchone()[0] == 100000 - 600
    # worker 認領 + 處理(同步跑一次)
    row = main._rpc1("claim_next_job", {"p_stale_sec": 180, "p_max_retry": 2})
    main._process_job(row["usage_id"], row["user_id"], row, "sk-test")
    # 完成:狀態 completed、有結果、成本已記
    st, txt, cost = conn.execute(
        "select status, result_text, estimated_openai_cost_usd from public.usage_logs where id=%s", (jid,)).fetchone()
    assert st == "completed" and txt and "校正" in txt and cost is not None
    # 輪詢回 done
    got = c.get(f"/api/jobs/{jid}", headers={"Authorization": "Bearer x"}).json()
    assert got["status"] == "done" and "校正" in got["text"]


def test_cancel_refunds(dbmain):
    main, conn = dbmain
    uid = _mk_user(conn)
    main._TEST_UID = str(uid)
    c = _client(main)
    r = c.post("/api/jobs", data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ"},
               headers={"Authorization": "Bearer x"})
    jid = r.json()["job_id"]
    # 取消 → 退款、狀態 cancelled
    rc = c.post(f"/api/jobs/{jid}/cancel", headers={"Authorization": "Bearer x"})
    assert rc.status_code == 200 and rc.json()["remaining_credits"] == 100000
    assert conn.execute("select status from public.usage_logs where id=%s", (jid,)).fetchone()[0] == "cancelled"


def test_options_carried_through_queue(dbmain, monkeypatch):
    """phase9:選項寫入佇列、claim 帶回、worker 依選項處理(關時間戳、開去贅詞)"""
    main, conn = dbmain
    uid = _mk_user(conn)
    main._TEST_UID = str(uid)
    c = _client(main)
    got_corr = {}

    def fake_correct(t, k, remove_fillers=False, speakers=False, usage=None, on_progress=None):
        got_corr.update({"remove_fillers": remove_fillers, "speakers": speakers})
        return t + "（校正）"
    monkeypatch.setattr(main, "_gpt_correct", fake_correct)

    r = c.post("/api/jobs", data={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "correct": "true",
                                  "timestamps": "false", "remove_fillers": "true", "speakers": "false"},
               headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    # 選項已入列
    opts = conn.execute("select options from public.usage_logs where id=%s", (jid,)).fetchone()[0]
    assert opts == {"timestamps": False, "remove_fillers": True, "speakers": False}
    # claim 帶回選項;worker 依選項:結果無 [00:00:00] 前綴、校正收到 remove_fillers=True
    row = main._rpc1("claim_next_job", {"p_stale_sec": 180, "p_max_retry": 2})
    assert row["options"] == opts
    main._process_job(row["usage_id"], row["user_id"], row, "sk-test")
    txt = conn.execute("select result_text from public.usage_logs where id=%s", (jid,)).fetchone()[0]
    assert txt and "[00:00:00]" not in txt and "校正" in txt
    assert got_corr == {"remove_fillers": True, "speakers": False}


def test_upload_lost_fails_and_refunds(dbmain):
    main, conn = dbmain
    uid = _mk_user(conn)
    # 直接入列一筆 upload 任務,upload_path 指向不存在的檔 → worker 應失敗退款
    row = main._rpc1("enqueue_transcription", {
        "p_user_id": str(uid), "p_cost": 300, "p_source_type": "upload", "p_source_name": "a",
        "p_duration": 300, "p_upload_path": "/no/such/file", "p_correct": False})
    jid = row["usage_id"]
    claimed = main._rpc1("claim_next_job", {"p_stale_sec": 180, "p_max_retry": 2})
    main._process_job(claimed["usage_id"], claimed["user_id"], claimed, "sk-test")
    st, err = conn.execute("select status, error_code is not null from public.usage_logs where id=%s", (jid,)).fetchone()
    assert st == "failed"
    assert conn.execute("select remaining_credits from public.credit_balances where user_id=%s", (uid,)).fetchone()[0] == 100000
