# -*- coding: utf-8 -*-
"""课表 / 调课 / 日报预览 路由测试。"""
from datetime import date

import db


def test_day_and_week(client):
    r = client.get("/api/day?d=2026-09-14")     # 周一，9/14 版课表生效
    assert r.status_code == 200
    data = r.json()
    assert data["date"] == "2026-09-14" and data["weekday"] == 0
    assert any(it["type"] == "lesson" and it["class_name"] == "17班" for it in data["items"])
    # 课程条目必须带 class_name（课堂打分入口依赖它）
    lesson = next(it for it in data["items"] if it["type"] == "lesson")
    assert lesson.get("class_name") and lesson.get("section")

    r = client.get("/api/week?d=2026-09-14")
    days = r.json()["days"]
    assert len(days) == 7 and days[0]["date"] == "2026-09-14"


def test_lesson_cancel_and_restore(client):
    # 周一 20班 有课（9/14 版）
    r = client.post("/api/lesson_cancel", json={"date": "2026-09-14", "weekday": 0,
                                                "section": "上午1", "note": "测试停课"})
    assert r.status_code == 200
    cid = r.json()["id"]
    data = client.get("/api/day?d=2026-09-14").json()
    lesson = next(it for it in data["items"]
                  if it["type"] == "lesson" and it["class_name"] == "17班")
    assert lesson["cancelled"] is True and lesson["cancel_id"] == cid
    # 恢复
    assert client.delete(f"/api/lesson_cancel/{cid}").json()["deleted"] == 1
    data = client.get("/api/day?d=2026-09-14").json()
    lesson = next(it for it in data["items"]
                  if it["type"] == "lesson" and it["class_name"] == "17班")
    assert lesson["cancelled"] is False


def test_lesson_move(client):
    r = client.post("/api/lesson_move", json={
        "date": "2026-09-15", "weekday": 1, "section": "下午1",
        "new_date": "2026-09-17", "new_start": "16:15", "new_end": "17:00",
        "note": "测试换课"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 9/17 应出现换到的单次课（type=lesson，蓝色课样式）
    data = client.get("/api/day?d=2026-09-17").json()
    assert any(it["type"] == "lesson" and it.get("date") == "2026-09-17"
               and it["start"] == "16:15" for it in data["items"])


def test_week_items_carry_date(client):
    """周视图条目必须带 date（周视图调课/停课/打分都依赖它）。"""
    d = date(2026, 9, 22)                                  # 周二，17班 下午2 有课
    w = client.get(f"/api/week?d={d.isoformat()}").json()
    tue = w["days"][1]
    assert tue["date"] == "2026-09-22"
    lessons = [it for it in tue["items"] if it["type"] == "lesson"]
    assert lessons and all(it.get("date") == "2026-09-22" for it in lessons)


def test_lesson_move_from_week_item(client):
    """用周视图条目的字段直接提交换课 → 200（回归：曾因缺 date 报 Field required）。"""
    d = date(2026, 9, 22)
    w = client.get(f"/api/week?d={d.isoformat()}").json()
    it = next(x for day in w["days"] for x in day["items"]
              if x["type"] == "lesson" and x.get("class_name") == "17班")
    r = client.post("/api/lesson_move", json={
        "date": it["date"], "weekday": it["weekday"], "section": it["section"],
        "new_date": "2026-09-23", "new_start": "16:15", "new_end": "17:00", "note": "测试"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 清理：删掉本次产生的 cancel 与 event
    with db.conn() as c:
        c.execute("DELETE FROM lesson_cancels WHERE note LIKE '换至 2026-09-23%'")
        c.execute("DELETE FROM events WHERE date='2026-09-23' AND note LIKE '换课：原 2026-09-22%'")


def test_lesson_move_same_day_time_change(client):
    """同一天内调整某节起止时间 → 原节标取消，新时段出现该班的单次课。"""
    r = client.post("/api/lesson_move", json={
        "date": "2026-09-14", "weekday": 0, "section": "上午1",
        "new_date": "2026-09-14", "new_start": "09:30", "new_end": "10:00",
        "note": "家长会提前"})
    assert r.status_code == 200
    data = client.get("/api/day?d=2026-09-14").json()
    moved = [it for it in data["items"] if it["type"] == "lesson"
             and it["class_name"] == "17班" and it["section"] == "上午1"]
    assert len(moved) == 1                                   # 同键替换，不重复
    assert moved[0]["start"] == "09:30" and moved[0]["end"] == "10:00"
    assert moved[0]["date"] == "2026-09-14"                  # 单次课，仅此日期
    with __import__("db").conn() as c:
        n = c.execute("SELECT COUNT(*) FROM lessons WHERE date='2026-09-14' "
                      "AND class_name='17班' AND section='上午1'").fetchone()[0]
        assert n == 1
