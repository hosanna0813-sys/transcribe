"""集中式價格設定與成本估算"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import pricing


def test_whisper_cost_default():
    # 預設 0.006/分:600 秒 = 10 分 = 0.06
    assert pricing.whisper_cost(600) == round(10 * 0.006, 6)


def test_chat_cost_default_mini():
    # gpt-4o-mini 預設 in 0.15 / out 0.60(每 1M)
    c = pricing.chat_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert c == round(0.15 + 0.60, 6)


def test_unknown_model_falls_back_to_mini():
    assert pricing.chat_rates("some-future-model") == pricing.chat_rates("gpt-4o-mini")


def test_env_override(monkeypatch):
    monkeypatch.setenv("PRICE_WHISPER_PER_MIN", "0.01")
    monkeypatch.setenv("PRICE_GPT_4O_MINI_IN_PER_1M", "0.30")
    importlib.reload(pricing)
    try:
        assert pricing.whisper_per_min() == 0.01
        assert pricing.chat_rates("gpt-4o-mini")[0] == 0.30
    finally:
        monkeypatch.delenv("PRICE_WHISPER_PER_MIN", raising=False)
        monkeypatch.delenv("PRICE_GPT_4O_MINI_IN_PER_1M", raising=False)
        importlib.reload(pricing)


def test_estimate_job_cost_shape():
    est = pricing.estimate_job_cost(120, "gpt-4o-mini", 500, 300, tokens_known=True)
    assert est["total_usd"] == round(est["whisper_usd"] + est["correction_usd"], 6)
    assert est["tokens_known"] is True
    assert est["pricing"]["correction_model"] == "gpt-4o-mini"


def test_estimate_marks_unknown_tokens():
    est = pricing.estimate_job_cost(120, "gpt-4o-mini", 0, 0, tokens_known=False)
    assert est["tokens_known"] is False
