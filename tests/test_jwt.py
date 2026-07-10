"""JWT 驗證:正確 Token、過期、alg=none、HS256 降級、錯 audience/issuer"""
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
from fastapi import HTTPException

SUPABASE_URL = "https://demo.supabase.co"
KID = "test-key-1"

_priv = generate_private_key(SECP256R1())
_pub_jwk = json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(_priv.public_key()))
_pub_jwk.update({"kid": KID, "alg": "ES256", "use": "sig"})


@pytest.fixture(autouse=True)
def stub_jwks(m, monkeypatch):
    monkeypatch.setattr(m, "_get_jwks", lambda url: [_pub_jwk])
    with m._jwks_lock:
        m._jwks_cache["keys"] = None


def make_token(claims_override=None, alg="ES256", key=None, headers=None):
    now = int(time.time())
    claims = {"sub": "user-123", "aud": "authenticated",
              "iss": f"{SUPABASE_URL}/auth/v1", "exp": now + 600, "iat": now}
    claims.update(claims_override or {})
    h = {"kid": KID}
    h.update(headers or {})
    return pyjwt.encode(claims, key if key is not None else _priv, algorithm=alg, headers=h)


def test_valid_token(m):
    tok = make_token()
    assert m._verify_jwt("Bearer " + tok) == "user-123"


def test_expired_token(m):
    tok = make_token({"exp": int(time.time()) - 10})
    with pytest.raises(HTTPException) as e:
        m._verify_jwt("Bearer " + tok)
    assert e.value.status_code == 401


def test_alg_none_rejected(m):
    tok = pyjwt.encode({"sub": "user-123", "aud": "authenticated"}, None,
                       algorithm="none", headers={"kid": KID})
    with pytest.raises(HTTPException) as e:
        m._verify_jwt("Bearer " + tok)
    assert e.value.status_code == 401


def test_hs256_downgrade_rejected(m):
    # 用公鑰內容當 HMAC 密鑰的經典降級攻擊:白名單必須直接擋掉
    tok = pyjwt.encode({"sub": "user-123", "aud": "authenticated",
                        "iss": f"{SUPABASE_URL}/auth/v1",
                        "exp": int(time.time()) + 600},
                       "some-secret", algorithm="HS256", headers={"kid": KID})
    with pytest.raises(HTTPException) as e:
        m._verify_jwt("Bearer " + tok)
    assert e.value.status_code == 401


def test_wrong_audience(m):
    tok = make_token({"aud": "evil"})
    with pytest.raises(HTTPException) as e:
        m._verify_jwt("Bearer " + tok)
    assert e.value.status_code == 401


def test_wrong_issuer(m):
    tok = make_token({"iss": "https://evil.example/auth/v1"})
    with pytest.raises(HTTPException) as e:
        m._verify_jwt("Bearer " + tok)
    assert e.value.status_code == 401


def test_missing_sub(m):
    now = int(time.time())
    tok = pyjwt.encode({"aud": "authenticated", "iss": f"{SUPABASE_URL}/auth/v1",
                        "exp": now + 600}, _priv, algorithm="ES256", headers={"kid": KID})
    with pytest.raises(HTTPException) as e:
        m._verify_jwt("Bearer " + tok)
    assert e.value.status_code == 401


def test_no_header(m):
    with pytest.raises(HTTPException) as e:
        m._verify_jwt(None)
    assert e.value.status_code == 401
