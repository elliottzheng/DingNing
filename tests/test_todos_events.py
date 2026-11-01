# -*- coding: utf-8 -*-
"""待办 / 日程 / 快速添加 路由测试。"""

import db


def test_todo_crud(client):
    r = client.post("/api/todos", json={"title": "交成绩册", "due": "2026-10-09 17:00", "priority": 2})
    assert r.status_code == 200
    t = r.json()
    assert t["title"] == "交成绩册" and t["done"] is False

    r = client.patch(f"/api/todos/{t['id']}", json={"done": True, "note": "已交"})
    assert r.json()["done"] is True and r.json()["note"] == "已交"

    r = client.get("/api/todos")
    assert any(x["id"] == t["id"] for x in r.json())

    r = client.delete(f"/api/todos/{t['id']}")
    assert r.json()["deleted"] == 1
    assert client.get("/api/todos").json() == [] or all(
        x["id"] != t["id"] for x in client.get("/api/todos").json())


def test_todo_title_required(client):
    assert client.post("/api/todos", json={"title": "  "}).status_code == 400


def test_quick_add_rule_todo(client):
    """规则解析（不用 AI）：“周X前交...”应归为待办。"""
    r = client.post("/api/quick_add", json={"text": "周五前交校本课程教案", "use_ai": False})
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "todo"
    assert data["item"]["title"].endswith("教案") or "教案" in data["item"]["title"]
    assert data["item"]["due"]


def test_quick_add_rule_event(client):
    r = client.post("/api/quick_add", json={"text": "周三下午3点机房教研会议", "use_ai": False})
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "event"
    assert data["item"]["start_time"] == "15:00"


def test_event_crud_and_calendar_protect(client):
    r = client.post("/api/events", json={"title": "监考", "date": "2026-11-05",
                                         "start_time": "08:30", "end_time": "11:30"})
    ev = r.json()
    assert ev["id"]
    r = client.patch(f"/api/events/{ev['id']}", json={"location": "第3考场"})
    assert r.json()["location"] == "第3考场"
    assert client.delete(f"/api/events/{ev['id']}").json()["deleted"] == 1


def test_duplicate_event_rejected(client):
    """同标题+日期+起止的日程不允许重复添加（409）。"""
    body = {"title": "防重测试会", "date": "2026-11-11",
            "start_time": "10:00", "end_time": "11:00"}
    assert client.post("/api/events", json=body).status_code == 200
    r = client.post("/api/events", json=body)
    assert r.status_code == 409 and "相同" in r.json()["detail"]
    # 不同时间/日期的同名日程允许
    r2 = client.post("/api/events", json={**body, "start_time": "14:00", "end_time": "15:00"})
    assert r2.status_code == 200
    # 清理
    with db.conn() as c:
        c.execute("DELETE FROM events WHERE title='防重测试会'")
