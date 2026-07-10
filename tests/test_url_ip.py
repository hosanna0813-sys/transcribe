"""URL 白名單與可信來源 IP"""
import pytest
from fastapi import HTTPException


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeReq:
    def __init__(self, xff=None, host="203.0.113.9"):
        self.headers = {}
        if xff is not None:
            self.headers["x-forwarded-for"] = xff
        self.client = FakeClient(host)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=abc12345678",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
])
def test_url_valid(m, url):
    m.check_url(url)   # 不應丟例外


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.tw/watch?v=x",          # 惡意相似網域
    "https://notyoutube.com/watch?v=x",
    "http://localhost/watch?v=x",
    "https://127.0.0.1/watch?v=x",
    "https://192.168.1.1/watch?v=x",
    "https://user:pass@youtube.com/watch?v=x",        # 帶帳密
    "ftp://youtube.com/watch?v=x",
    "",
])
def test_url_invalid(m, url):
    with pytest.raises(HTTPException):
        m.check_url(url)


def test_ip_plain_connection(m):
    assert m.get_trusted_client_ip(FakeReq()) == "203.0.113.9"


def test_ip_takes_rightmost_xff(m):
    # 使用者偽造左值,只有代理附加的最右值可信
    assert m.get_trusted_client_ip(FakeReq(xff="6.6.6.6, 7.7.7.7, 198.51.100.4")) == "198.51.100.4"


def test_ip_invalid_xff_falls_back(m):
    # 不合法的 header 不可當成合法身分
    assert m.get_trusted_client_ip(FakeReq(xff="not-an-ip")) == "203.0.113.9"
    assert m.get_trusted_client_ip(FakeReq(xff="1.2.3.4, <script>")) == "203.0.113.9"


def test_ip_ipv6_ok(m):
    assert m.get_trusted_client_ip(FakeReq(xff="2001:db8::1")) == "2001:db8::1"
