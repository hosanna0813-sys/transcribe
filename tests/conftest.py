import os
import sys

# 測試環境變數(必須在 import main 前設定;全部為假值)
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("SUPABASE_URL", "https://demo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "svc-test")
os.environ.setdefault("TRIAL_MINUTES", "10")
os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import main  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def reset_trial_state():
    with main._trial_lock:
        main._trial_ip.clear()
        main._trial_global.update({"date": None, "sec": 0})
    with main._relay_nonce_lock:
        main._relay_nonces.clear()
    yield


@pytest.fixture()
def m():
    return main
