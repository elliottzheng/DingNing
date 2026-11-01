# -*- coding: utf-8 -*-
"""chatcmd 测试：统一 agent 链路（确定性查询不进 LLM；写操作走 plan→validate→apply）。

所有会触发 LLM 的路径都通过 monkeypatch agent.plan_ops 打桩；
确定性分支（帮助/待办/课表/搜索/撤销）直接调用即可。
"""
from datetime import date, timedelta

import agent
import chatcmd
import db


def _script_ops(monkeypatch, ops, summary="已完成", err=""):
    """把 agent.plan_ops 钉死为返回给定操作计划。"""
    monkeypatch.setattr(agent, "plan_ops",
                        lambda text, today, mode="general": (ops, summary, err))


def setup_function(_):
    with db.conn() as c:
        c.execute("DELETE FROM agent_batches")


# ---------------- 帮助 / 空消息 / 快捷 ----------------

def test_help_lists_commands():
    for kw in ("帮助", "help", "?"):
        assert "课表" in chatcmd.handle(kw) and "待办" in chatcmd.handle(kw)


def test_empty_message_hint():
    assert "帮助" in chatcmd.handle("   ")


def test_undo_shortcut(client):
    out = chatcmd.handle("撤销")
    assert "撤销" in out or "没有" in out


def test_todos_shortcut(client):
    assert "待办" in chatcmd.handle("待办")


# ---------------- 查询（统一智能体作答；规则仅作降级兜底） ----------------

_QUERY_PHRASES = ("课表", "明天", "明日课程", "课表 2026-09-01", "下周课程安排",
                  "校运会啥时候", "明天上啥课")


def test_queries_all_routed_to_agent(client, monkeypatch):
    """所有查询不再走规则猜测：智能体的 summary 就是回复，且绝不写库。"""
    _script_ops(monkeypatch, [], summary="【答案】3节课：20、28、19班信息。")
    with db.conn() as c:
        ev0 = c.execute("SELECT COUNT(*) FROM events WHERE source='user'").fetchone()[0]
        td0 = c.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    for q in _QUERY_PHRASES:
        out = chatcmd.handle(q, today=date(2026, 9, 25))
        assert "【答案】" in out, (q, out)
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE source='user'").fetchone()[0] == ev0
        assert c.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == td0


def test_agent_answers_query_without_ops(client, monkeypatch):
    """模型查完没产出写入操作、只给 summary → summary 就是回复，不算失败。"""
    _script_ops(monkeypatch, [], summary="明天周一有3节课：20、28、19班。")
    out = chatcmd.handle("明天上啥课", today=date(2026, 9, 27))
    assert "20" in out and "没能理解" not in out


def test_agent_noops_no_summary_still_fails(client, monkeypatch):
    """没操作也没答案 → 维持'没能理解'。"""
    _script_ops(monkeypatch, [], summary="")
    out = chatcmd.handle("喵喵喵", today=date(2026, 9, 27))
    assert "没能理解" in out


def test_agent_down_falls_back_to_rules(client, monkeypatch):
    """智能体异常 → 自动降级到确定性规则路径（可用性兜底）。"""
    def boom(text, today, mode="general"):
        raise RuntimeError("DeepSeek 调用失败：HTTP 503")
    monkeypatch.setattr(agent, "handle", boom)
    out = chatcmd.handle("明日课程", today=date(2026, 9, 27))
    assert "9/28" in out                                   # 日视图
    out2 = chatcmd.handle("校运会啥时候", today=date(2026, 9, 25))
    assert "校运会" in out2 and "11/5" in out2              # 事件名搜索
    out3 = chatcmd.handle("银河系巡回讲座啥时候", today=date(2026, 9, 25))
    assert "没找到" in out3
    out4 = chatcmd.handle("周五前交成绩册", today=date(2026, 9, 24))
    try:
        assert "已记待办" in out4                           # quick_add 兜底
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM todos WHERE title LIKE '%成绩册%'")


def test_todos_list(client):
    assert "待办" in chatcmd.handle("待办")


def test_todos_empty_text(client):
    with db.conn() as c:
        c.execute("DELETE FROM todos")
    assert "没有未完成" in chatcmd.handle("待办")


# ---------------- 写入（agent 计划 → 校验 → 落库） ----------------

def test_add_todo_via_agent_plan(client, monkeypatch):
    _script_ops(monkeypatch, [
        {"op": "add_todo", "title": "交成绩册", "due": "2026-10-02 17:00"},
    ])
    out = chatcmd.handle("周五前交成绩册", today=date(2026, 9, 25))
    assert "已记待办" in out or "已执行" in out
    with db.conn() as c:
        row = c.execute("SELECT title, done FROM todos WHERE title LIKE '%成绩册%'").fetchone()
    try:
        assert row and row["done"] == 0
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM todos WHERE title LIKE '%成绩册%'")


def test_add_event_via_agent_plan(client, monkeypatch):
    _script_ops(monkeypatch, [
        {"op": "create_event", "title": "机房教研会议", "date": "2026-10-01",
         "start_time": "15:00", "end_time": "16:00"},
    ])
    out = chatcmd.handle("周三下午3点机房教研会议", today=date(2026, 9, 25))
    assert "已记日程" in out or "已执行" in out
    with db.conn() as c:
        row = c.execute("SELECT title, date FROM events WHERE title LIKE '%教研会议%' "
                        "AND source='user'").fetchone()
    try:
        assert row
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM events WHERE title LIKE '%教研会议%' AND source='user'")


def test_duplicate_event_rejected(client, monkeypatch):
    _script_ops(monkeypatch, [
        {"op": "create_event", "title": "测试例会", "date": "2026-09-25",
         "start_time": "14:00", "end_time": "15:00"},
    ])
    out1 = chatcmd.handle("明天下午2点测试例会", today=date(2026, 9, 24))
    out2 = chatcmd.handle("明天下午2点测试例会", today=date(2026, 9, 24))
    assert "已经在课表里" in out2 or "未重复添加" in out2
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE title LIKE '%测试例会%'").fetchone()[0]
        assert n == 1
    with db.conn() as c:
        c.execute("DELETE FROM events WHERE title LIKE '%测试例会%'")


# ---------------- agent 关闭时的兜底 ----------------

def test_legacy_fallback_query_when_disabled(client, monkeypatch):
    import db as _db
    _db.set_config("agent_enabled", "0")
    try:
        out = chatcmd.handle("明天", today=date.today())
        assert "日程" in out
        with db.conn() as c:
            assert c.execute("SELECT COUNT(*) FROM events WHERE title='明天'").fetchone()[0] == 0
    finally:
        _db.set_config("agent_enabled", "1")


def test_legacy_fallback_add_when_disabled(client, monkeypatch):
    import db as _db
    _db.set_config("agent_enabled", "0")
    try:
        out = chatcmd.handle("周五前交成绩册", today=date(2026, 9, 24))
        assert "已记待办" in out
    finally:
        _db.set_config("agent_enabled", "1")
        with db.conn() as c:
            c.execute("DELETE FROM todos WHERE title LIKE '%成绩册%'")
