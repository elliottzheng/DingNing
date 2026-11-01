# -*- coding: utf-8 -*-
"""登录/注册/用户管理与配置路由测试。放在独立模块，避免影响其他用例的登录状态。"""
import os

import accounts
import db
import pytest

from conftest import TEST_PASSWORD, TEST_USER


@pytest.fixture()
def auth_restore(client):
    """用例结束后恢复 tester 登录态（会话 Cookie 可能被用例清掉或换人）。"""
    from conftest import _login
    yield
    client.cookies.clear()
    _login(client)


def test_config_roundtrip(client):
    r = client.get("/api/config")
    assert "push_time" in r.json()
    assert client.put("/api/config", json={"push_time": "20:30"}).json()["ok"] is True
    assert client.get("/api/config").json()["push_time"] == "20:30"
    client.put("/api/config", json={"push_time": "21:00"})


def test_me(client):
    r = client.get("/api/me").json()
    assert r["username"] == TEST_USER and r["is_admin"] is True


def test_auth_flow(client, auth_restore):
    # 未登录：API 401，页面 303 → /login；健康检查与登录/注册页始终开放
    client.cookies.clear()
    assert client.get("/api/todos").status_code == 401
    assert client.get("/").status_code == 200                  # 首页开放（前端 401 再跳登录）
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/register").status_code == 200

    # 错误密码 401；正确用户名+密码拿 Cookie 后放行
    assert client.post("/api/login", json={"username": TEST_USER,
                                           "password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
    assert r.status_code == 200
    assert "wb_session" in r.cookies
    assert client.get("/api/todos").status_code == 200


def test_register_requires_invite(client, auth_restore):
    """第二个用户起必须持邀请码；注册成功后数据与 tester 完全隔离。"""
    invite = client.get("/api/admin/users").json()["invite_code"]
    n_before = len(db.all_usernames())
    todo = client.post("/api/todos", json={"title": "测试隔离-交成绩册"}).json()
    todos_before = client.get("/api/todos").json()
    assert todos_before                                       # tester 有待办可做隔离对比

    # 错误邀请码 → 403
    assert client.post("/api/register", json={
        "username": "王老师", "password": "pw-123456", "invite_code": "BAD"}).status_code == 403
    # 非法用户名 / 弱密码 → 400
    assert client.post("/api/register", json={
        "username": "a", "password": "pw-123456", "invite_code": invite}).status_code == 400
    assert client.post("/api/register", json={
        "username": "王老师", "password": "123", "invite_code": invite}).status_code == 400
    # 正确邀请码 → 注册成功并自动登录
    r = client.post("/api/register", json={
        "username": "王老师", "password": "pw-123456", "invite_code": invite})
    assert r.status_code == 200
    assert client.get("/api/me").json()["username"] == "王老师"

    # 数据隔离：新用户没有任何 tester 的待办
    assert client.get("/api/todos").json() == []
    assert len(db.all_usernames()) == n_before + 1
    assert os.path.exists(db.user_db_path("王老师"))

    # tester 的数据仍在自己的库里
    client.cookies.clear()
    client.post("/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
    assert client.get("/api/todos").json() == todos_before
    client.delete(f"/api/todos/{todo['id']}")                 # 清理，不影响其他用例

    # 管理员删除该用户 → 账号与会话、个人库文件一并清除
    r = client.delete("/api/admin/users/王老师")
    assert r.json()["ok"] is True
    assert accounts.get_user("王老师") is None
    assert not os.path.exists(db.user_db_path("王老师"))


def test_admin_guard(client):
    """非管理员访问用户管理接口 → 403；不能删除自己。"""
    invite = client.get("/api/admin/users").json()["invite_code"]
    client.post("/api/register", json={
        "username": "tmp_u1", "password": "pw-123456", "invite_code": invite})
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/invite/reset").status_code == 403
    assert client.delete("/api/admin/users/tmp_u1").status_code == 403
    # 切回管理员
    client.cookies.clear()
    client.post("/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
    assert client.delete(f"/api/admin/users/{TEST_USER}").status_code == 400
    assert client.delete("/api/admin/users/tmp_u1").json()["ok"] is True


def test_invite_reset(client):
    old = client.get("/api/admin/users").json()["invite_code"]
    r = client.post("/api/admin/invite/reset").json()
    assert r["ok"] is True and r["invite_code"] != old
    assert client.get("/api/admin/users").json()["invite_code"] == r["invite_code"]
