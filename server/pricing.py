"""集中式價格設定與成本估算。

原則:模型價格會變動,不硬編死。預設值可由環境變數覆蓋(單位見下),
無法取得用量時回傳的成本標為估算值(estimated=True)。

環境變數(皆選填,單位:美元):
  PRICE_WHISPER_PER_MIN         Whisper 每分鐘(預設 0.006)
  PRICE_<MODEL>_IN_PER_1M       某模型輸入每 1M tokens(MODEL 用大寫、-/. 換成 _)
  PRICE_<MODEL>_OUT_PER_1M      某模型輸出每 1M tokens
例:PRICE_GPT_4O_MINI_IN_PER_1M=0.15
"""
import os

# 內建預設(2026 年中參考價;營運者可用環境變數覆蓋)
_DEFAULT_WHISPER_PER_MIN = 0.006
_DEFAULT_CHAT = {
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "gpt-4o":      {"in": 2.50, "out": 10.00},
}


def _envf(name: str):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def whisper_per_min() -> float:
    return _envf("PRICE_WHISPER_PER_MIN") or _DEFAULT_WHISPER_PER_MIN


def _model_key(model: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (model or "").upper())


def chat_rates(model: str):
    """回傳 (input_per_1M, output_per_1M);未知模型退回 gpt-4o-mini 預設"""
    mk = _model_key(model)
    default = _DEFAULT_CHAT.get(model) or _DEFAULT_CHAT["gpt-4o-mini"]
    in_rate = _envf(f"PRICE_{mk}_IN_PER_1M")
    out_rate = _envf(f"PRICE_{mk}_OUT_PER_1M")
    return (in_rate if in_rate is not None else default["in"],
            out_rate if out_rate is not None else default["out"])


def whisper_cost(duration_sec: float) -> float:
    return round(max(0.0, duration_sec) / 60.0 * whisper_per_min(), 6)


def chat_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_rate, out_rate = chat_rates(model)
    return round((max(0, prompt_tokens) / 1e6) * in_rate
                 + (max(0, completion_tokens) / 1e6) * out_rate, 6)


def estimate_job_cost(duration_sec: float, correction_model: str,
                      prompt_tokens: int, completion_tokens: int,
                      tokens_known: bool):
    """回傳整筆任務的成本明細 dict(供寫入 usage_logs.applied_pricing 快照)"""
    w = whisper_cost(duration_sec)
    g = chat_cost(correction_model, prompt_tokens, completion_tokens)
    in_rate, out_rate = chat_rates(correction_model)
    return {
        "total_usd": round(w + g, 6),
        "whisper_usd": w,
        "correction_usd": g,
        "tokens_known": bool(tokens_known),   # False = 校正成本為估算(沒拿到 usage)
        "pricing": {
            "whisper_per_min": whisper_per_min(),
            "correction_model": correction_model,
            "chat_in_per_1m": in_rate,
            "chat_out_per_1m": out_rate,
        },
    }
