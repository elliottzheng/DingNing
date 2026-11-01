# -*- coding: utf-8 -*-
"""待办 + 自然语言快速添加路由。"""
from datetime import date

from fastapi import APIRouter, HTTPException

import db
import ai_parse
import logic
import nparser
from common import duplicate_event_exists, now_str, todo_dict
from models import TodoIn, TodoPatch, QuickAdd

router = APIRouter()


@router.get("/api/todos")
def api_todos():
    rows = list(db.conn().execute("SELECT * FROM todos"))
    return [todo_dict(r) for r in logic.sort_todos(rows)]


@router.post("/api/todos")
def api_add_todo(t: TodoIn):
    if not t.title.strip():
        raise HTTPException(400, "标题不能为空")
    with db.conn() as c:
        cur = c.execute("INSERT INTO todos(title, due, priority, note, done, created_at, remind_min) VALUES(?,?,?,?,?,?,?)",
                        (t.title.strip(), t.due, t.priority, t.note, int(t.done), now_str(), t.remind_min))
        tid = cur.lastrowid
    r = db.conn().execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
    return todo_dict(r)


@router.patch("/api/todos/{todo_id}")
def api_patch_todo(todo_id: int, p: TodoPatch):
    with db.conn() as c:
        r = c.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "待办不存在")
        vals = {"title": r["title"], "due": r["due"], "priority": r["priority"],
                "note": r["note"], "done": r["done"], "remind_min": r["remind_min"]}
        if p.title is not None: vals["title"] = p.title.strip()
        if p.due is not None: vals["due"] = p.due
        if p.priority is not None: vals["priority"] = p.priority
        if p.note is not None: vals["note"] = p.note
        if p.done is not None: vals["done"] = int(p.done)
        if p.remind_min is not None:
            vals["remind_min"] = None if p.remind_min == -1 else p.remind_min   # -1 = 清除提醒
        c.execute("UPDATE todos SET title=?,due=?,priority=?,note=?,done=?,remind_min=? WHERE id=?",
                  (vals["title"], vals["due"], vals["priority"], vals["note"], vals["done"],
                   vals["remind_min"], todo_id))
    r = db.conn().execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    return todo_dict(r)


@router.delete("/api/todos/{todo_id}")
def api_del_todo(todo_id: int):
    with db.conn() as c:
        n = c.execute("DELETE FROM todos WHERE id=?", (todo_id,)).rowcount
    if n == 0:
        raise HTTPException(404, "待办不存在")
    return {"deleted": n}


@router.post("/api/quick_add")
def api_quick_add(q: QuickAdd):
    text = q.text.strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    p, src = None, "rule"
    if q.use_ai:                          # 界面输入优先用 AI 解析，失败回退规则
        p = ai_parse.ai_parse(text, date.today())
        if p:
            src = "ai"
    if p is None:
        p = nparser.parse(text)
    p.setdefault("date", date.today().isoformat())
    if p["type"] == "todo":
        due = p["due"] or f"{p['date']} 17:00"
        # 只给了日期（默认 17:00 截止）时，若当天有课（周期课或当日日程），截止提前到最早开始时间
        if due.endswith("17:00"):
            try:
                wd = date.fromisoformat(due[:10]).weekday()
                with db.conn() as c:
                    row = c.execute("SELECT MIN(start_time) AS s FROM lessons WHERE weekday=?",
                                    (wd,)).fetchone()
                    row2 = c.execute("SELECT MIN(start_time) AS s FROM events "
                                     "WHERE date=? AND source='user' AND start_time!=''",
                                     (due[:10],)).fetchone()
                cands = [x for x in (row["s"] if row else None,
                                     row2["s"] if row2 else None) if x]
                if cands:
                    due = f"{due[:10]} {min(cands)}"
            except ValueError:
                pass
        with db.conn() as c:
            cur = c.execute("INSERT INTO todos(title, due, priority, note, created_at, remind_min) VALUES(?,?,?,?,?,?)",
                            (p["title"], due, p["priority"], p.get("note", ""), now_str(),
                             p.get("remind_min")))
            tid = cur.lastrowid
        r = db.conn().execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
        return {"type": "todo", "source": src, "item": todo_dict(r), "parsed": p}
    if not p.get("start_time"):
        p["start_time"], p["end_time"] = "", ""
    elif not p.get("end_time"):                    # 无结束时间默认 1 小时
        h, m = map(int, p["start_time"].split(":"))
        e = h * 60 + m + 60
        p["end_time"] = f"{e // 60 % 24:02d}:{e % 60:02d}"
    if duplicate_event_exists(p["title"], p["date"], p["start_time"], p["end_time"]):
        raise HTTPException(409, "已存在完全相同的日程，未重复添加")
    with db.conn() as c:
        cur = c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind,remind_min) "
                        "VALUES(?,?,?,?,?,?,'user','event',?)",
                        (p["date"], p["start_time"], p["end_time"], p["title"],
                         p.get("location", ""), p.get("note", ""), p.get("remind_min")))
        eid = cur.lastrowid
    r = db.conn().execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    return {"type": "event", "source": src, "item": dict(r), "parsed": p}
