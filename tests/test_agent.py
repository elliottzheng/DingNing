# -*- coding: utf-8 -*-
"""agent.py 测试：计划→校验→原子落库→撤销回滚（LLM 全部打桩）。"""
import json
from datetime import date, timedelta

import agent
import common
import db


def _msg(name, args):
    return {"role": "assistant", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _scripted(monkeypatch, script):
    """按顺序回放假模型响应；script 元素 = message dict 或 (name, args) 快捷元组。"""
    queue = list(script)
    def fake_chat(messages, model, **kw):
        item = queue.pop(0)
        if isinstance(item, tuple):
            return _msg(*item)
        return item
    monkeypatch.setattr(agent, "_chat", fake_chat)


def setup_function(_):
    # 课表夹具：直接写临时库。effective_from 用今天——_lessons_of 取 MAX(<=今天)，
    # conftest 的真实导入可能已用今天作版本号，夹具必须同版本才查得到。
    # 测试库是 session 级共享：改动必须在这里快照、teardown 恢复，否则污染 test_schedule。
    with db.conn() as c:
        agent._saved_lessons = [dict(r) for r in
                                c.execute("SELECT * FROM lessons WHERE weekday=1")]
        c.execute("DELETE FROM lessons WHERE weekday=1")
        c.executemany("INSERT INTO lessons(weekday, section, start_time, end_time, class_name, subject, effective_from) "
                      "VALUES(1,?, '14:15','15:00',?,'信息',?)",
                      [(sec, cls, ver) for ver in ("2026-09-01", date.today().isoformat())
                       for sec, cls in (("下午1", "21班"), ("下午2", "17班"), ("下午3", "23班"))])
        c.execute("DELETE FROM agent_batches")
        # 防幻觉班级校验改为：班号须出现在已导入课表/我的班级设置里（无需班级表）


def teardown_function(_):
    with db.conn() as c:
        c.execute("DELETE FROM lessons WHERE weekday=1")
        for l in agent._saved_lessons:
            c.execute("INSERT INTO lessons(id, weekday, section, start_time, end_time, class_name, "
                      "subject, location, weeks, effective_from) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (l["id"], l["weekday"], l["section"], l["start_time"], l["end_time"],
                       l["class_name"], l["subject"], l["location"], l["weeks"],
                       l["effective_from"]))



def test_happy_path_two_step_apply(client, monkeypatch):
    """查课表 → 停课+补课两步，校验通过，落库并登记批次。"""
    # list_lessons 是只读工具，真实执行；只桩 _chat 的两个模型轮次
    _scripted(monkeypatch, [
        _msg("list_lessons", {"weekday": 1}),                     # step1
        _msg("cancel_lesson", {"date": "2026-09-29", "section": "下午1",
                               "note": "调至第五节"}),
        _msg("create_event", {"date": "2026-09-29", "start_time": "11:20",
                              "end_time": "12:05", "title": "21班信息"}),
        _msg("finish", {"summary": "已完成"}),
    ])
    out = agent.handle("把下周二的课调一下", today=date(2026, 9, 25), source="test")
    assert "已执行 2 项操作" in out
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM lesson_cancels WHERE date='2026-09-29'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM events WHERE date='2026-09-29' "
                         "AND title LIKE '%21班信息%'").fetchone()[0] == 1
        batch = c.execute("SELECT id, status FROM agent_batches").fetchone()
    # 撤销回滚
    undo_out = agent.undo_last()
    assert "已撤销" in undo_out
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM lesson_cancels WHERE date='2026-09-29'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM events WHERE date='2026-09-29' "
                         "AND title LIKE '%21班信息%'").fetchone()[0] == 0
        assert c.execute("SELECT status FROM agent_batches WHERE id=?", (batch["id"],)).fetchone()[0] == "undone"


def test_validation_rejects_cancel_of_nonexistent_section(client, monkeypatch):
    """取消当天课表里不存在的节次 → 两轮都错 → 驳回不落库。"""
    _scripted(monkeypatch, [
        _msg("cancel_lesson", {"date": "2026-09-29", "section": "上午1", "note": "x"}),
        _msg("finish", {"summary": ""}),
        _msg("cancel_lesson", {"date": "2026-09-29", "section": "上午1", "note": "x"}),  # 自纠轮仍错
        _msg("finish", {"summary": ""}),
    ])
    out = agent.handle("乱来一条", today=date(2026, 9, 25), source="test")
    assert "整批未执行" in out and "上午1" in out
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM lesson_cancels WHERE date='2026-09-29'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM agent_batches").fetchone()[0] == 0


def test_unknown_class_rejected(client, monkeypatch):
    """日程标题里的班号不在已导入课表 → 两轮都错 → 驳回（防幻觉班级）。"""
    _scripted(monkeypatch, [
        _msg("create_event", {"date": "2026-09-29", "start_time": "08:00",
                              "end_time": "09:00", "title": "99班信息"}),
        _msg("finish", {"summary": ""}),
        _msg("create_event", {"date": "2026-09-29", "start_time": "08:00",
                              "end_time": "09:00", "title": "99班信息"}),
        _msg("finish", {"summary": ""}),
    ])
    out = agent.handle("给99班排课", today=date(2026, 9, 25), source="test")
    assert "整批未执行" in out and "99班" in out


def test_disabled_agent_short_circuits(client, monkeypatch):
    """agent_enabled=0 → chatcmd 不走 agent（信号再强也不进）。"""
    db.set_config("agent_enabled", "0")
    try:
        import chatcmd
        saved = chatcmd.classify
        chatcmd.classify = lambda text: ("add", None, "rule")
        try:
            out = chatcmd.handle("把下周三的教研会挪到周五，8点9点10点", today=date(2026, 9, 25))
            assert "多步" not in out                      # 没进 agent（失败也走的是单步 add）
        finally:
            chatcmd.classify = saved
    finally:
        db.set_config("agent_enabled", "")


def test_should_use_agent_signals():
    assert agent._should_use_agent("8:00-8:40第一节 8:50-9:30第二节 9:40-10:20第三节")
    assert agent._should_use_agent("把下周三的教研会挪到周五")
    assert agent._should_use_agent("校区下周二作息调整通知，详细如下……")
    assert not agent._should_use_agent("周五前交成绩册")
    assert not agent._should_use_agent("周三下午3点机房教研会议")


# ---------------- 工具白名单与预选 ----------------

def test_tools_whitelist_complete():
    """TOOLS 必须恰好包含 11 个工具（防程序化重建时截断/遗漏——踩过的坑）。"""
    expected = {"add_todo", "cancel_event", "cancel_lesson", "change_event",
                "create_event", "finish", "list_day", "list_lessons",
                "list_todos", "search_events", "update_todo"}
    got = {t["function"]["name"] for t in agent.TOOLS}
    assert got == expected
    # 模式过滤同样完整
    for mode in ("schedule", "general"):
        visible = {t["function"]["name"] for t in agent.TOOLS
                   if t["function"]["name"] in agent.MODE_TOOLS[mode]}
        assert {"cancel_lesson", "create_event", "finish"} <= visible
    assert agent.OP_TYPES == {"cancel_lesson", "create_event", "update_todo",
                              "change_event", "cancel_event", "add_todo"}
    # 三份清单必须一致（历史上 OP_TYPES 漏 add_todo 导致建待办全部报 unknown tool）
    assert agent.OP_TYPES == set(agent.MODE_OPS["schedule"]) | set(agent.MODE_OPS["general"])
    for op in agent.OP_TYPES:
        assert op in agent.TOOL_BUNDLES, f"{op} 未登记工具包（unknown tool 风险）"


def test_tool_bundle_registry_covers_all():
    """每个工具都必须登记所属包（新工具忘登记 → 预选永远不含它）。"""
    assert {t["function"]["name"] for t in agent.TOOLS} == set(agent.TOOL_BUNDLES)


def test_select_bundles_always_full():
    """无高置信信号 → 一律全量（None = 不收窄）。"""
    assert agent._select_bundles("把下周二的课调一下") is None
    assert agent._select_bundles("周五前交成绩册") is None
    assert agent._select_bundles("明天下午2点测试例会") is None
    assert agent._select_bundles("你好呀") is None


# ---------------- 按课表查询 ----------------

def _fixture_lesson(class_no, weekday):
    """给某班排任意星期的课；双版本（9/14 覆盖历史测试日期，今天覆盖现实查询）。"""
    with db.conn() as c:
        c.execute("DELETE FROM lessons WHERE class_name=? AND weekday=?",
                  (f"{class_no}班", weekday))
        c.executemany("INSERT INTO lessons(weekday, section, start_time, end_time, class_name, subject, effective_from) "
                      "VALUES(?,?,?,?,?,?,?)",
                      [(weekday, f"测{weekday}", "14:15", "15:00", f"{class_no}班", "信息", ver)
                       for ver in ("2026-09-01", date.today().isoformat())])


def _cleanup_lesson(class_no, weekday):
    with db.conn() as c:
        c.execute("DELETE FROM lessons WHERE class_name=? AND weekday=? AND section LIKE '测%'",
                  (f"{class_no}班", weekday))


def test_list_lessons_read(client):
    out = json.loads(agent._exec_read("list_lessons", {"weekday": 1}))
    secs = [i["section"] for i in out]
    assert "下午2" in secs and any("17班" in i["cls"] for i in out)


def test_list_day_shows_cancelled_flag(client):
    """list_day 必须把停课标记透传给模型（此前被剥掉导致模型说'没取消'）。"""
    _fixture_lesson(17, 1)                    # 17班 周二有课
    d = date.today() + timedelta(days=7)      # 至少一周后，避开校历假期/事件
    while d.weekday() != 1:
        d += timedelta(days=1)
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO lesson_cancels(date, weekday, section, note, created_at) "
                  "VALUES(?,?,?,?,datetime('now','localtime'))",
                  (d.isoformat(), 1, "测1", "教师停课"))
    try:
        out = json.loads(agent._exec_read("list_day", {"date": d.isoformat()}))
        mine = [i for i in out if "17班" in i["title"]]
        assert mine and mine[0]["cancelled"] is True
        assert "停课" in mine[0]["note"]
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM lesson_cancels WHERE date=?", (d.isoformat(),))
        _cleanup_lesson(17, 1)


# ---------------- 待办闭环 + 日程变更 ----------------

def test_list_todos_read(client):
    with db.conn() as c:
        c.execute("INSERT INTO todos(title, due, priority, done, created_at) "
                  "VALUES('测试待办清单', '2026-10-02 17:00', 1, 0, datetime('now','localtime'))")
    try:
        out = json.loads(agent._exec_read("list_todos", {}))
        assert any(t["title"] == "测试待办清单" for t in out)
        assert all(not t.get("done") for t in out)
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM todos WHERE title='测试待办清单'")


def test_update_todo_roundtrip_and_undo(client, monkeypatch):
    with db.conn() as c:
        c.execute("INSERT INTO todos(title, due, priority, done, created_at) "
                  "VALUES('交成绩册', '2026-10-02 17:00', 1, 0, datetime('now','localtime'))")
        tid = c.execute("SELECT id FROM todos WHERE title='交成绩册'").fetchone()["id"]
    try:
        monkeypatch.setattr(agent, "plan_ops", lambda text, today, mode="general": ([
            {"op": "update_todo", "title_match": "成绩册", "due": "2026-10-09 17:00"},
        ], "已改期", ""))
        out = agent.handle("把交成绩册改到下周五", today=date(2026, 9, 29), source="test",
                           mode="general")
        assert "已执行" in out
        with db.conn() as c:
            got = c.execute("SELECT due FROM todos WHERE id=?", (tid,)).fetchone()["due"]
        assert got == "2026-10-09 17:00"
        assert "已撤销" in agent.undo_last()
        with db.conn() as c:
            got = c.execute("SELECT due FROM todos WHERE id=?", (tid,)).fetchone()["due"]
        assert got == "2026-10-02 17:00"
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM todos WHERE title='交成绩册'")


def test_update_todo_ambiguous_rejected(client, monkeypatch):
    with db.conn() as c:
        c.executemany("INSERT INTO todos(title, due, priority, done, created_at) "
                      "VALUES(?,?,1,0,datetime('now','localtime'))",
                      [("交成绩册A", "2026-10-02 17:00"), ("交成绩册B", "2026-10-03 17:00")])
    try:
        monkeypatch.setattr(agent, "plan_ops", lambda text, today, mode="general": ([
            {"op": "update_todo", "title_match": "成绩册", "due": "2026-10-09 17:00"},
        ], "", ""))
        out = agent.handle("改成绩册截止", today=date(2026, 9, 29), source="test",
                           mode="general")
        assert "整批未执行" in out and "2 条待办" in out
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM todos WHERE title LIKE '交成绩册%'")


def _seed_user_event(date_s="2026-10-16", title="测试教研会", source="user"):
    with db.conn() as c:
        cur = c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind) "
                        "VALUES(?,?,?,?,?,?,?,'event')",
                        (date_s, "15:00", "16:00", title, "测试地点", "", source))
        return cur.lastrowid


def test_change_event_roundtrip_and_undo(client, monkeypatch):
    eid = _seed_user_event()
    try:
        monkeypatch.setattr(agent, "plan_ops", lambda text, today, mode="general": ([
            {"op": "change_event", "event_id": eid, "date": "2026-10-17"},
        ], "已改期", ""))
        out = agent.handle("把测试教研会改到10月17日", today=date(2026, 9, 29),
                           source="test", mode="general")
        assert "已执行" in out
        with db.conn() as c:
            got = c.execute("SELECT date FROM events WHERE id=?", (eid,)).fetchone()["date"]
        assert got == "2026-10-17"
        assert "已撤销" in agent.undo_last()
        with db.conn() as c:
            got = c.execute("SELECT date FROM events WHERE id=?", (eid,)).fetchone()["date"]
        assert got == "2026-10-16"
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM events WHERE id=?", (eid,))


def test_change_calendar_event_rejected(client, monkeypatch):
    eid = _seed_user_event(source="calendar", title="测试校历考试")
    try:
        monkeypatch.setattr(agent, "plan_ops", lambda text, today, mode="general": ([
            {"op": "change_event", "event_id": eid, "date": "2026-10-20"},
        ], "", ""))
        out = agent.handle("把校历考试改期", today=date(2026, 9, 29), source="test",
                           mode="general")
        assert "整批未执行" in out and "校历" in out
        with db.conn() as c:
            got = c.execute("SELECT date FROM events WHERE id=?", (eid,)).fetchone()["date"]
        assert got == "2026-10-16"          # 未被动过
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM events WHERE id=?", (eid,))


def test_cancel_event_roundtrip_undo_restores_row(client, monkeypatch):
    eid = _seed_user_event(title="测试博饼聚餐")
    try:
        monkeypatch.setattr(agent, "plan_ops", lambda text, today, mode="general": ([
            {"op": "cancel_event", "event_id": eid},
        ], "已取消", ""))
        out = agent.handle("测试博饼聚餐取消了", today=date(2026, 9, 29), source="test",
                           mode="general")
        assert "已执行" in out
        with db.conn() as c:
            assert not c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()
        assert "已撤销" in agent.undo_last()
        with db.conn() as c:
            row = c.execute("SELECT title, date FROM events WHERE id=?", (eid,)).fetchone()
        assert row and row["title"] == "测试博饼聚餐"
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM events WHERE id=?", (eid,))


# ---------------- 课表版本与上课日推算（common 层） ----------------

def test_class_sections_on_respects_schedule_version(client):
    """旧版课表的同名班不能串日（22班旧版周五/新版周三 → 周五应无课）。"""
    with db.conn() as c:
        cid = 90                                   # 新语义：class_sections_on 直接收班号
        c.execute("DELETE FROM lessons WHERE class_name='90班'")
        c.execute("INSERT INTO lessons(weekday,section,start_time,end_time,class_name,"
                  "subject,effective_from) VALUES(4,'旧周五','14:15','15:00','90班','信息','2026-09-01')")
        c.execute("INSERT INTO lessons(weekday,section,start_time,end_time,class_name,"
                  "subject,effective_from) VALUES(2,'新周三','14:15','15:00','90班','信息',?)",
                  (date.today().isoformat(),))
    try:
        # 探测日动态计算：下一个未到的周五（新版本无周五课 → 旧版不串日）
        fri = date.today()
        while fri.weekday() != 4 or fri <= date.today():
            fri += timedelta(days=1)
        assert common.class_sections_on(cid, fri) == []
        # 旧版生效期内的周五（固定历史日期，语义稳定）
        assert common.class_sections_on(cid, date(2026, 9, 11)) == ["旧周五"]
        assert common.class_sections_on(cid, fri + timedelta(days=5)) == ["新周三"]
        assert common.class_sections_on(cid, date(2026, 9, 10)) == []   # 9/10 旧版无周三课
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM lessons WHERE class_name='90班'")


def _seed_class(class_no):
    return class_no                                # 新语义：直接用班号


def test_next_meeting_date_skips_holidays(client):
    """下一个上课日要跳过校历假期（补班 override 例外）。"""
    cid = _seed_class(86)
    _fixture_lesson(86, 2)                            # 周三有课（weekday=2）
    try:
        with db.conn() as c:
            c.execute("DELETE FROM events WHERE kind='holiday' AND date LIKE '2026-10%'")
            for i in range(1, 8):                     # 国庆假 10/1–10/7
                c.execute("INSERT INTO events(title,date,source,kind) "
                          "VALUES('国庆假',?, 'calendar','holiday')",
                          (f"2026-10-0{i}",))
        # 2026-09-30（周三）之后：10/7 周三是假期 → 跳到 10/14 周三
        nd = common.next_meeting_date(cid, date(2026, 9, 30))
        assert nd is not None and nd.isoformat() == "2026-10-14", nd
        # 补班日不跳过：override 10/10 按周六=周三课表（eff_weekday=2）→ 有课
        import logic
        with db.conn() as c:
            c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('schedule_overrides','{\"2026-10-10\":2}')")
            logic.schedule_overrides.cache_clear() if hasattr(logic.schedule_overrides, "cache_clear") else None
        nd2 = common.next_meeting_date(cid, date(2026, 10, 8))
        assert nd2 is not None and nd2.isoformat() == "2026-10-10", nd2
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM events WHERE kind='holiday' AND date LIKE '2026-10%'")
            c.execute("DELETE FROM config WHERE key='schedule_overrides'")
        _cleanup_lesson(86, 2)

def test_noop_failure_summary_falls_back(monkeypatch):
    """模型零操作却自称失败：绝不把借口当答案，必须抛错让 chatcmd 走规则兜底。"""
    import agent, pytest
    monkeypatch.setattr(agent, "plan_ops",
                        lambda text, today, mode="general":
                        ([], "【新建待办未成功】· 建待办的接口报错", None))
    with pytest.raises(RuntimeError, match="零操作"):
        agent.handle("晚上还有一个待办，是把所有的材料收集齐。9 点截止。", today=__import__("datetime").date(2031, 3, 5))


def test_noop_query_summary_still_answered(monkeypatch):
    """纯查询零操作：模型答案照常返回，不受守卫影响。"""
    import agent
    monkeypatch.setattr(agent, "plan_ops",
                        lambda text, today, mode="general":
                        ([], "今天没有课程与日程安排。", None))
    r = agent.handle("今天有什么安排", today=__import__("datetime").date(2031, 3, 5))
    assert "没有课程" in r

def test_noop_add_intent_even_neutral_summary_falls_back(monkeypatch):
    """写入意图 + 零操作：即使总结措辞中立（没自称失败），也必须兜底真做。"""
    import agent, pytest
    monkeypatch.setattr(agent, "plan_ops",
                        lambda text, today, mode="general":
                        ([], "已了解，这是一条关于今晚安排的记录需求。", None))
    with pytest.raises(RuntimeError, match="零操作"):
        agent.handle("帮我记一下，晚上要准备教研组例会发言。", today=__import__("datetime").date(2031, 3, 5))
