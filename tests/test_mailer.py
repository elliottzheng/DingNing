# -*- coding: utf-8 -*-
"""mailer 测试：摘要内容 + 推送补发调度（mock 网络与时间）。"""
from datetime import date, datetime, timedelta

import db
import mailer


def test_build_digest_content(client):
    """找接下来 7 天内第一个有课的日子，验证摘要含课程与待办区。"""
    import logic
    target = None
    for i in range(7):
        d = date.today() + timedelta(days=i)
        if any(it["type"] == "lesson" for it in logic.day_items(d)["items"]):
            target = d
            break
    if target is None:
        import pytest
        pytest.skip("未来 7 天都没有课")
    subject, html = mailer.build_digest(target)
    assert f"{target.month}/{target.day}" in subject
    lessons = [it for it in logic.day_items(target)["items"] if it["type"] == "lesson"]
    assert lessons and all(it["title"] in html for it in lessons)   # 课程行（班级+科目，科目可为空）
    assert "待办" in html                                      # 待办区


def test_build_digest_holiday(client):
    """假期日：标题与正文标注停课。"""
    with db.conn() as c:
        c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind) "
                  "VALUES('2027-03-08','','','测试假日','','','calendar','holiday')")
    subject, html = mailer.build_digest(date(2027, 3, 8))
    assert "假期" in subject
    assert "假期" in subject or "假期" in html


def test_remaining_text():
    from datetime import timedelta
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    assert "还剩" in mailer._remaining(future)
    past = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    assert "已逾期" in mailer._remaining(past)


def test_digest_job_marks_date(client, monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_pushplus", lambda t, h: sent.append(t) or True)
    ok = mailer.digest_job(date(2026, 9, 15))
    assert ok is True and len(sent) == 1
    assert db.get_config("last_digest_date") == date.today().isoformat()
    db.set_config("last_digest_date", "")


def test_catchup_skips_when_already_sent(client, monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_pushplus", lambda t, h: sent.append(t) or True)
    db.set_config("last_digest_date", date.today().isoformat())
    mailer.catchup()
    assert sent == []                                          # 今天已发，不重复
    db.set_config("last_digest_date", "")


def test_scheduler_has_both_jobs(client):
    """后台定时任务都在：21:00 明日摘要 + ClawBot 轮询 + 推送看门狗。"""
    sched = mailer.setup_scheduler()
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert "nightly_digest" in jobs and "clawbot_poll" in jobs and "push_watchdog" in jobs
    finally:
        sched.shutdown(wait=False)


def test_send_pushplus_retries_then_succeeds(client, monkeypatch):
    """第一次连接失败、第二次成功 → 退避重试后返回 True。"""
    calls = {"n": 0, "sleeps": []}
    def fake_send_once(title, html):
        calls["n"] += 1
        return (False, "connection refused") if calls["n"] == 1 else (True, "")
    monkeypatch.setattr(mailer, "_send_pushplus_once", fake_send_once)
    monkeypatch.setattr(mailer.time, "sleep", lambda s: calls["sleeps"].append(s))
    assert mailer.send_pushplus("测试", "<p>hi</p>") is True
    assert calls["n"] == 2 and calls["sleeps"] == [5]        # 5s 退避后重试成功


def test_send_pushplus_gives_up_after_retries(client, monkeypatch):
    """一直失败 → 3 次尝试、2 次退避，返回 False。"""
    calls = {"n": 0, "sleeps": []}
    monkeypatch.setattr(mailer, "_send_pushplus_once",
                        lambda t, h: (calls.__setitem__("n", calls["n"] + 1), (False, "timeout"))[1])
    monkeypatch.setattr(mailer.time, "sleep", lambda s: calls["sleeps"].append(s))
    assert mailer.send_pushplus("测试", "<p>hi</p>") is False
    assert calls["n"] == 3 and calls["sleeps"] == [5, 10]


class _EveningDateTime(datetime):
    """固定"现在"= 今天 22:00（必过 21:00 摘要时刻）。"""
    @classmethod
    def now(cls):
        return datetime.combine(date.today(), datetime.min.time()).replace(hour=22)


def test_watchdog_backfills_missed_digest(client, monkeypatch):
    """时刻已过、今天没发 → 看门狗补发；发过/已尝试过则不重复。"""
    db.set_config("last_digest_date", "")
    db.set_config("last_report_date", date.today().isoformat())   # 压制日报分支，只验证摘要
    sent = []
    monkeypatch.setattr(mailer, "send_pushplus", lambda t, h: sent.append(t) or True)
    monkeypatch.setattr(mailer, "datetime", _EveningDateTime)
    mailer._watch_attempts.clear()

    mailer._watchdog()
    assert len(sent) == 1                                     # 补发一次
    assert db.get_config("last_digest_date") == date.today().isoformat()
    mailer._watchdog()
    assert len(sent) == 1                                     # 已标记，不再发
    db.set_config("last_digest_date", "")


def test_watchdog_noop_when_not_yet_time(client, monkeypatch):
    """未到推送时刻 → 看门狗不动。"""
    class _Morning(datetime):
        @classmethod
        def now(cls):
            return datetime.combine(date.today(), datetime.min.time()).replace(hour=8)
    db.set_config("last_digest_date", "")
    sent = []
    monkeypatch.setattr(mailer, "send_pushplus", lambda t, h: sent.append(t) or True)
    monkeypatch.setattr(mailer, "datetime", _Morning)
    mailer._watch_attempts.clear()
    mailer._watchdog()
    assert sent == []
    db.set_config("last_digest_date", "")


def test_ring_time_adjust_clash(client):
    """响铃撞课 → 挪到该节课开始前 5 分钟；课间/无课日不动。"""
    import common
    d = date(2026, 11, 4)                                     # 周三（conftest 有示例课）
    # conftest 周三课：上午1 8:00-8:45、上午2 9:00-9:45、下午1 14:15-15:00、下午2 15:15-16:00
    assert common.ring_time_adjust(d, "08:30") == "07:55"     # 撞上午1
    assert common.ring_time_adjust(d, "09:30") == "08:55"     # 撞上午2
    assert common.ring_time_adjust(d, "09:50") == "09:50"     # 课间不挪
    assert common.ring_time_adjust(d, "14:30") == "14:10"     # 撞下午1（H:MM 存法）
    assert common.ring_time_adjust(d, "07:00") == "07:00"     # 早于第一节课不挪
    assert common.ring_time_adjust(d, "18:00") == "18:00"     # 晚间无课不挪


def test_ring_time_adjust_single_day_lesson(client):
    """单次课行（调课补课）同样算上课时段。"""
    import common
    d = date(2026, 11, 4)
    with db.conn() as c:
        ver = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                        (d.isoformat(),)).fetchone()[0]
        c.execute("INSERT INTO lessons(weekday,section,start_time,end_time,class_name,"
                  "subject,location,weeks,date,effective_from) "
                  "VALUES(3,'下午3','16:15','17:00','20班','信息','','all',?,?)",
                  (d.isoformat(), ver))
    assert common.ring_time_adjust(d, "16:30") == "16:10"     # 撞单次课
    assert common.ring_time_adjust(date(2026, 11, 5), "16:30") == "16:30"  # 别的日子无此课


def test_ring_time_adjust_cancel_and_suspend(client):
    """该节调走 / 全天停课：不算上课，不调整。"""
    import common
    d = date(2026, 11, 4)
    with db.conn() as c:
        c.execute("INSERT INTO lesson_cancels(date,weekday,section,note,created_at) "
                  "VALUES(?,2,'下午1','',?)", (d.isoformat(), d.isoformat()))
    assert common.ring_time_adjust(d, "14:30") == "14:30"     # 下午1已调走
    with db.conn() as c:
        c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,"
                  "source,kind,suspends) VALUES(?,'','','校运会','','','calendar','event',1)",
                  (d.isoformat(),))
    assert common.ring_time_adjust(d, "09:30") == "09:30"     # 停课日全天不撞课


def test_ring_time_adjust_chain(client):
    """挪出的时刻仍撞课（连堂/重叠）→ 继续前移。"""
    import common
    d = date(2026, 11, 3)                                     # 周二（独立日期，防用例间污染）
    with db.conn() as c:
        ver = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                        (d.isoformat(),)).fetchone()[0]
        c.execute("INSERT INTO lessons(weekday,section,start_time,end_time,class_name,"
                  "subject,location,weeks,effective_from) "
                  "VALUES(1,'上午2','07:50','09:40','20班','信息','','all',?)", (ver,))
    assert common.ring_time_adjust(d, "09:00") == "07:45"     # 撞9:00课→08:55仍撞→07:45
