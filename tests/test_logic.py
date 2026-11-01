# -*- coding: utf-8 -*-
"""logic 层测试：学期周次 / 调休 / 假期停课 / 日视图组装。"""
from datetime import date, timedelta

import db
import logic


def test_term_of():
    assert logic.term_of(date(2026, 9, 14))["name"] == "第一学期"
    assert logic.term_of(date(2027, 3, 1))["name"] == "第二学期"
    assert logic.term_of(date(2027, 8, 1)) is None            # 暑假


def test_week_number():
    t, wk = logic.week_number(date(2026, 8, 31))              # 学期第一天 = 第 1 周
    assert t["name"] == "第一学期" and wk == 1
    _, wk2 = logic.week_number(date(2026, 9, 14))
    assert wk2 == 3
    assert logic.week_number(date(2027, 8, 1)) == (None, None)


def test_schedule_overrides(monkeypatch):
    db.set_config("schedule_overrides", '{"2026-10-10": 0}')   # 10/10 周六按周一课表
    try:
        assert logic.schedule_overrides() == {"2026-10-10": 0}
        info = logic.day_items(date(2026, 10, 10))
        # 调休日按周一课表上 20班 上午4
        assert any(it["type"] == "lesson" and it["class_name"] == "17班" for it in info["items"])
        assert info["items"][0]["weekday"] == 0                 # 有效星期被换算
        assert any(b["label"] == "调休" for b in info["banners"])
    finally:
        db.set_config("schedule_overrides", "{}")


def test_holiday_no_lessons(client):
    """插入一条假期校历事件 → 当天停课且横幅出现。"""
    with db.conn() as c:
        c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind) "
                  "VALUES('2027-03-08','','','测试假日','','','calendar','holiday')")
    info = logic.day_items(date(2027, 3, 8))
    assert info["holiday"] == ["测试假日"]
    assert info["items"] == []                                  # 假期不排课
    assert any(b["kind"] == "holiday" for b in info["banners"])


def test_day_items_calendar_exam_in_timeline(client):
    """考试类校历事件整合进时间轴（不是横幅）。"""
    with db.conn() as c:
        c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind) "
                  "VALUES('2027-03-09','09:00','10:30','期中考试','','','calendar','exam')")
    info = logic.day_items(date(2027, 3, 9))
    assert any(it["type"] == "calendar" and it["kind"] == "exam" and "期中" in it["title"]
               for it in info["items"])
    assert not any(b["kind"] == "exam" for b in info["banners"])  # 考试不进横幅


def test_sort_todos_order(client):
    now = "2026-09-14 12:00:00"
    with db.conn() as c:
        c.execute("INSERT INTO todos(title, due, priority, done, created_at) VALUES('低优先无截止','',0,0,?)", (now,))
        c.execute("INSERT INTO todos(title, due, priority, done, created_at) VALUES('明天截止','2026-09-15 17:00',1,0,?)", (now,))
        c.execute("INSERT INTO todos(title, due, priority, done, created_at) VALUES('已完成','',1,1,?)", (now,))
    rows = [dict(r) for r in db.conn().execute(
        "SELECT * FROM todos WHERE title IN ('低优先无截止','明天截止','已完成')")]
    titles = [t["title"] for t in logic.sort_todos(rows)]
    assert titles[-1] == "已完成"                               # 已完成沉底
    assert titles[0] == "明天截止"                              # 有截止在前
    with db.conn() as c:
        c.execute("DELETE FROM todos WHERE title IN ('低优先无截止','明天截止','已完成')")


def test_new_schedule_banner_window(client):
    """学期中换课表：导入未来生效的新版本 → 切换前 31 天内的日视图出现预告横幅；
    切换日之后、以及久远的历史日期，不出现。"""
    import db as dbmod
    future = date.today() + timedelta(days=10)
    with dbmod.conn() as c:
        # 新版本（未来生效）：周三 30班 上午1（区别于现行示例课表）
        c.execute("INSERT INTO lessons(weekday,section,start_time,end_time,class_name,subject,effective_from) "
                  "VALUES(2,'上午1','08:00','08:45','30班','信息',?)", (future.isoformat(),))
    try:
        today = date.today()
        info = logic.day_items(today)
        assert any(b["label"] == "新课表" and str(future.month) in b["title"]
                   for b in info["banners"])
        # 切换日当天起：已在执行，无预告
        info2 = logic.day_items(future)
        assert not any(b["label"] == "新课表" for b in info2["banners"])
        # 久远历史日期：不出现预告
        info3 = logic.day_items(today - timedelta(days=60))
        assert not any(b["label"] == "新课表" for b in info3["banners"])
    finally:
        with dbmod.conn() as c:
            c.execute("DELETE FROM lessons WHERE class_name='30班'")


def test_suspends_calendar_event_cancels_lessons(client):
    """校历停课事件：当日课程自动标记停课；课堂归属日计算跳过该日。"""
    import db, logic, common
    from datetime import date
    d = date(2031, 3, 5)                                    # 周三（课表有 23班 上午1）
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO events(date,title,source,kind,suspends) "
                  "VALUES(?,?,?,?,1)", (d.isoformat(), "校运动会", "calendar", "event"))
    info = logic.day_items(d, today=date(2031, 3, 1))
    assert info["suspends"] == ["校运动会"]
    lessons = [i for i in info["items"] if i["type"] == "lesson"]
    assert lessons and all(i["cancelled"] for i in lessons)
    assert all("校运动会" in i["note"] for i in lessons)
    assert any(b["label"] == "停课" for b in info["banners"])
    assert common.class_sections_on(23, d) == []            # 课堂归属跳过停课日
