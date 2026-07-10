"""綠界回呼:正確付款、簽章錯誤、MerchantID 錯誤、金額不符、訂單不存在、
重複回呼冪等、RtnCode!=1 不入帳、production fail-closed"""
import pytest


STATE = {}


@pytest.fixture(autouse=True)
def mock_db(m, monkeypatch):
    STATE.clear()
    STATE.update({"payments": {}, "credited": []})

    def fake_by_mtn(mtn):
        return STATE["payments"].get(mtn)

    def fake_rpc(fn, p):
        assert fn == "credit_payment"
        pid = p["p_payment_id"]
        if pid in STATE["credited"]:
            return [{"ok": True, "reason": "duplicate"}]
        STATE["credited"].append(pid)
        return [{"ok": True, "reason": None}]

    monkeypatch.setattr(m, "_sb_payment_by_mtn", fake_by_mtn)
    monkeypatch.setattr(m, "_sb_rpc", fake_rpc)


def put_order(mtn, amount=100, status="pending", provider="ecpay"):
    STATE["payments"][mtn] = {"id": "pay-" + mtn, "status": status,
                              "credits_added": 3600, "amount": amount,
                              "provider": provider}


def cb_data(m, mtn, amt="100", rtn="1", mid=None):
    mid_conf, _, _, _ = m._ecpay_conf()
    d = {"MerchantID": mid if mid is not None else mid_conf,
         "MerchantTradeNo": mtn, "RtnCode": rtn, "RtnMsg": "ok",
         "TradeNo": "EC1", "TradeAmt": amt, "PaymentType": "Credit_CreditCard"}
    d["CheckMacValue"] = m._ecpay_checkmac(d)
    return d


def test_valid_payment_credits(client, m):
    put_order("T1")
    r = client.post("/api/pay/ecpay/callback", data=cb_data(m, "T1"))
    assert r.text == "1|OK" and STATE["credited"] == ["pay-T1"]


def test_bad_mac_rejected(client, m):
    put_order("T2")
    d = cb_data(m, "T2")
    d["TradeAmt"] = "999999"   # 竄改後簽章不符
    r = client.post("/api/pay/ecpay/callback", data=d)
    assert r.status_code == 400 and not STATE["credited"]


def test_wrong_merchant_id_rejected(client, m):
    put_order("T3")
    d = cb_data(m, "T3", mid="9999999")
    r = client.post("/api/pay/ecpay/callback", data=d)
    assert r.status_code == 400 and not STATE["credited"]


def test_amount_mismatch_rejected(client, m):
    put_order("T4", amount=500)          # 本地訂單 500,回呼只付 100
    r = client.post("/api/pay/ecpay/callback", data=cb_data(m, "T4", amt="100"))
    assert r.status_code == 400 and not STATE["credited"]


def test_order_not_found(client, m):
    r = client.post("/api/pay/ecpay/callback", data=cb_data(m, "NOPE"))
    assert r.status_code == 400 and not STATE["credited"]


def test_duplicate_callback_idempotent(client, m):
    put_order("T5")
    d = cb_data(m, "T5")
    assert client.post("/api/pay/ecpay/callback", data=d).text == "1|OK"
    STATE["payments"]["T5"]["status"] = "paid"
    assert client.post("/api/pay/ecpay/callback", data=d).text == "1|OK"
    assert STATE["credited"] == ["pay-T5"]   # 只入帳一次


def test_failed_rtncode_no_credit(client, m):
    put_order("T6")
    r = client.post("/api/pay/ecpay/callback", data=cb_data(m, "T6", rtn="10100058"))
    assert r.text == "1|OK" and not STATE["credited"]


def test_cancelled_order_rejected(client, m):
    put_order("T7", status="cancelled")
    r = client.post("/api/pay/ecpay/callback", data=cb_data(m, "T7"))
    assert r.status_code == 400 and not STATE["credited"]


def test_production_requires_real_keys(m, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")   # ecpay 於呼叫時讀環境
    for k in ("ECPAY_MERCHANT_ID", "ECPAY_HASH_KEY", "ECPAY_HASH_IV"):
        monkeypatch.delenv(k, raising=False)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._ecpay_conf()
    assert e.value.status_code == 503   # 不退回測試金鑰,fail closed
