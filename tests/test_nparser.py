# -*- coding: utf-8 -*-
"""nparser 规则解析器测试（离线、确定性）。"""
from datetime import date

import nparser

# parse(text, today) 支持注入"今天"，日期断言完全确定


def test_todo_deadline_weekday():
    p = nparser.parse("周五前交成绩册", today=date(2026, 9, 14))   # 周一
    assert p["type"] == "todo"
    assert p["due"].startswith("2026-09-")
    assert "成绩册" in p["title"]


def test_todo_before_relative_week():
    p = nparser.parse("下周二前完成新课备课", today=date(2026, 9, 14))   # 下周二 = 9/22
    assert p["type"] == "todo"
    assert p["due"].startswith("2026-09-22")


def test_event_afternoon_time():
    p = nparser.parse("周三下午3点机房教研会议", today=date(2026, 9, 14))
    assert p["type"] == "event"
    assert p["start_time"] == "15:00"
    assert "教研" in p["title"]


def test_event_time_range():
    """文档示例：时间段解析。"""
    p = nparser.parse("明天下午2点到4点初二监考", today=date(2026, 9, 14))
    assert p["type"] == "event"
    assert p["start_time"] == "14:00" and p["end_time"] == "16:00"
    assert "监考" in p["title"]


def test_todo_priority_urgent():
    """紧急词提升优先级。"""
    p = nparser.parse("紧急：今天要交教案", today=date(2026, 9, 14))
    assert p["type"] == "todo"
    assert p["priority"] == 2
