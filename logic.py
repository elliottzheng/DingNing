# -*- coding: utf-8 -*-
"""业务逻辑：学期周次计算、日视图数据组装、待办排序。供 server 与 mailer 共用。"""
import json
import re
from datetime import date, timedelta, datetime

import db

KIND_ORDER = {"holiday": 0, "festival": 1, "exam": 2, "event": 3, "week_note": 4}
KIND_LABEL = {"holiday": "假期", "festival": "节日", "exam": "考试", "event": "校历", "week_note": "本周"}
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_d(s: str) -> date:
    return date.fromisoformat(s)


def schedule_overrides() -> dict:
    """调休映射：{日期: 按周几的课表上课(0=周一..6=周日)}。"""
    try:
        return json.loads(db.get_config("schedule_overrides", "{}") or "{}")
    except Exception:
        return {}


def term_of(d: date):
    for t in db.get_terms():
        if t["start"] <= d.isoformat() <= t["end"]:
            return t
    return None


def week_number(d: date):
    """返回 (term, 第几周) 或 (None, None)。"""
    t = term_of(d)
    if not t:
        return None, None
    start = _parse_d(t["start"])
    return t, (d - start).days // 7 + 1


def day_items(d: date, today: date = None) -> dict:
    """某天的完整视图数据。today 用于"新课表生效"预告的显示窗口判断。"""
    today = today or date.today()
    ov = schedule_overrides().get(d.isoformat())
    eff_weekday = int(ov) if ov is not None else d.weekday()
    lessons, events, cancels = [], [], {}
    with db.conn() as c:
        # 版本化课表：全局取生效日期<=当天的最新一套课表（某天在新版本无课则真的无课）
        latest = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                           (d.isoformat(),)).fetchone()[0]
        if latest:
            lessons = [dict(r) for r in c.execute(
                "SELECT * FROM lessons WHERE effective_from=? AND "
                "((weekday=? AND date IS NULL) OR date=?) ORDER BY start_time",
                (latest, eff_weekday, d.isoformat()))]
        for r in c.execute("SELECT * FROM events WHERE date=? ORDER BY start_time, kind, id",
                           (d.isoformat(),)):
            events.append(dict(r))
        for r in c.execute("SELECT * FROM lesson_cancels WHERE date=?", (d.isoformat(),)):
            cancels[(r["weekday"], r["section"])] = dict(r)
    holiday = [e for e in events if e["kind"] == "holiday"]
    susp = [e for e in events if e.get("suspends")]       # 校历停课事件（校运会等）
    # 横幅只放状态标记（假期/节日/周备注/调休）；事件、考试类校历整合进下方时间轴
    banners = sorted((e for e in events if e["source"] == "calendar"
                      and e["kind"] in ("holiday", "festival", "week_note")),
                     key=lambda e: (KIND_ORDER.get(e["kind"], 9), e["start_time"], e["id"]))
    banners = [{"kind": e["kind"], "label": KIND_LABEL.get(e["kind"], ""),
                "title": e["title"], "time": e["start_time"]} for e in banners]
    if susp:
        banners.append({"kind": "exam", "label": "停课",
                        "title": f"{susp[0]['title']}·当日停课", "time": ""})
    if ov is not None:
        banners.insert(0, {"kind": "event", "label": "调休",
                           "title": f"今日调休，按{WD[int(ov)]}的课表上课", "time": ""})
    # 新课表预告：已导入未来生效的课表版本，且查看的是切换前 31 天内的日期
    if today - timedelta(days=7) <= d < today + timedelta(days=31):
        nxt = db.conn().execute(
            "SELECT MIN(effective_from) FROM lessons WHERE effective_from>? "
            "AND effective_from<=?", (d.isoformat(),
                                      (today + timedelta(days=31)).isoformat())).fetchone()[0]
        if nxt:
            fd = date.fromisoformat(nxt)
            banners.insert(0, {"kind": "event", "label": "新课表",
                               "title": f"{fd.month}月{fd.day}日起执行新课表", "time": ""})
    items = []
    if not holiday:                             # 假期日不排课
        for l in lessons:
            cc = cancels.get((eff_weekday, l["section"])) if not l.get("date") else None
            suspev = susp[0]["title"] if susp else ""
            # 单次课（date 有值）是调课/补课的产物，不受节次停课/停课日影响
            cancelled = (bool(cc) or bool(suspev)) and not l.get("date")
            note = (cc["note"] if cc else
                    (l.get("note") or (f"{suspev}·停课" if suspev else "")))
            items.append({"type": "lesson", "start": l["start_time"], "end": l["end_time"],
                          "title": f"{l['class_name']}{l['subject']}".strip() or l["section"],
                          "sub": l["section"], "section": l["section"],
                          "class_name": l["class_name"],
                          "location": l["location"] or "", "id": None,
                          "weekday": eff_weekday,
                          "cancelled": cancelled,
                          "cancel_id": cc["id"] if cc else None,
                          "note": note})
    subj_words = []
    try:
        subj_words = [x.strip() for x in json.loads(db.get_config("my_subjects", "[]")) if x.strip()]
    except Exception:
        subj_words = []
    disp = (db.get_config("lesson_subject", "") or "").strip()
    if disp:
        subj_words.append(disp)
    for e in events:
        if e["source"] == "user":
            makeup_like = bool(re.search(r"\d{1,4}班", e["title"])) and \
                any(w and w in e["title"] for w in subj_words)
            items.append({"type": "event", "start": e["start_time"], "end": e["end_time"],
                          "title": e["title"], "sub": "", "location": e["location"],
                          "note": e["note"], "id": e["id"], "makeup_like": makeup_like})
        elif e["kind"] in ("event", "exam"):    # 校历中的大会/活动/考试 → 日程
            items.append({"type": "calendar", "start": e["start_time"], "end": e["end_time"],
                          "title": e["title"], "sub": "",
                          "location": e["location"], "note": "", "kind": e["kind"], "id": None})
    items.sort(key=lambda x: (x["start"] == "", x["start"]))
    t, wk = week_number(d)
    return {
        "date": d.isoformat(),
        "weekday": d.weekday(),
        "override": int(ov) if ov is not None else None,
        "term": t["name"] if t else None,
        "week": wk,
        "holiday": [e["title"] for e in holiday],
        "suspends": [e["title"] for e in susp],
        "banners": banners,
        "items": items,
    }


def sort_todos(rows) -> list:
    """未完成在前（逾期→有截止→无截止），已完成沉底。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def key(t):
        done = t["done"]
        due = t["due"] or ""
        if done:
            return (1, "9999", 0, "")            # 完成沉底
        if due and due < now:
            return (0, "0" + due, -t["priority"], "")   # 逾期最前
        if due:
            return (0, "1" + due, -t["priority"], "")
        return (0, "2", -t["priority"], t["created_at"])

    return sorted(rows, key=key)


def todos_snapshot(include_done_today: bool = True) -> list:
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM todos")]
    out = sort_todos(rows)
    today = date.today().isoformat()
    return [t for t in out if not t["done"] or (include_done_today and t["due"].startswith(today))]
