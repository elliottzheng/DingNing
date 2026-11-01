# -*- coding: utf-8 -*-
"""安全加固测试：密码哈希/会话/限流/安全头/改密码验证。"""
import common
import accounts
import db

from conftest import TEST_PASSWORD, TEST_USER


def test_password_hash_roundtrip():
    h = common.hash_password("pw123456")
    assert h.startswith("pbkdf2_sha256$") and "pw123456" not in h
    assert common.verify_password("pw123456", h) is True
    assert common.verify_password("wrong", h) is False
    assert common.verify_password("x", "garbage") is False


def test_session_lifecycle(client):
    t = accounts.create_session(TEST_USER)
    assert accounts.check_session(t) == TEST_USER
    accounts.destroy_session(t)
    assert accounts.check_session(t) is None
    assert accounts.check_session("") is None
    assert accounts.check_session("nonexistent") is None


def test_login_rate_limit_lockout(client):
    """连续 5 次错误 → 锁定 15 分钟，第 6 次（即使密码正确）也 429。"""
    common._LOGIN_FAILS.clear()
    try:
        for _ in range(5):
            assert client.post("/api/login", json={"username": TEST_USER,
                                                   "password": "bad"}).status_code == 401
        r = client.post("/api/login", json={"username": TEST_USER, "password": "bad"})
        assert r.status_code == 429                        # 已锁定
        assert client.post("/api/login", json={"username": TEST_USER,
                                               "password": TEST_PASSWORD}).status_code == 429
    finally:
        common._LOGIN_FAILS.clear()


def test_change_password_requires_current(client):
    """改密码必须带正确当前密码；旧会话全部失效（只影响被改密码的用户）。"""
    common._LOGIN_FAILS.clear()
    invite = client.get("/api/admin/users").json()["invite_code"]
    # 专门建一个用户来改密码，避免影响共享的 tester 账号
    assert client.post("/api/register", json={
        "username": "pw_user", "password": "pw-a-old", "invite_code": invite}).status_code == 200
    r = client.post("/api/login", json={"username": "pw_user", "password": "pw-a-old"})
    old_cookie = r.cookies.get("wb_session")
    assert old_cookie
    client.cookies.clear()      # 只带 old_cookie，排除 jar 里新会话的干扰
    old = {"Cookie": f"wb_session={old_cookie}"}
    assert client.get("/api/todos", headers=old).status_code == 200
    # 错误当前密码 → 403
    assert client.put("/api/config", json={"login_password": "pw-b-new",
                                           "current_password": "wrong"},
                      headers=old).status_code == 403
    # 正确当前密码 → 200，旧会话失效
    assert client.put("/api/config", json={"login_password": "pw-b-new",
                                           "current_password": "pw-a-old"},
                      headers=old).json()["ok"] is True
    assert client.get("/api/todos", headers=old).status_code == 401
    # 新密码可登录
    assert client.post("/api/login", json={"username": "pw_user",
                                           "password": "pw-b-new"}).status_code == 200
    # 清理临时用户（管理员）
    client.cookies.clear()
    client.post("/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
    assert client.delete("/api/admin/users/pw_user").json()["ok"] is True


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


def test_token_masked_in_config(client):
    db.set_config("pushplus_token", "641e3d0b371a484db18070e4f8200369")
    cfg = client.get("/api/config").json()
    assert "641e3d0b" not in str(cfg)                       # 完整 token 不外泄
    assert cfg["pushplus_token_masked"].endswith("0369")
    # 空值 = 不修改
    client.put("/api/config", json={"pushplus_token": ""})
    assert db.get_config("pushplus_token") == "641e3d0b371a484db18070e4f8200369"
    db.set_config("pushplus_token", "")
