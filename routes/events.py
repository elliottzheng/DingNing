# -*- coding: utf-8 -*-
"""用户日程路由（校历事件不可删）。"""
from datetime import date

from fastapi import APIRouter, HTTPException

import db
from common import duplicate_event_exists
from models import EventIn, EventPatch

router = APIRouter()


@router.post("/api/events")
def api_add_event(e: EventIn):
    if not e.title.strip():
        raise HTTPException(400, "标题不能为空")
    try:
        date.fromisoformat(e.date)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    if duplicate_event_exists(e.title.strip(), e.date, e.start_time, e.end_time):
        raise HTTPException(409, "已存在完全相同的日程（同标题、日期、起止时间），未重复添加")
    with db.conn() as c:
        cur = c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind,remind_min) "
                        "VALUES(?,?,?,?,?,?, 'user','event',?)",
                        (e.date, e.start_time, e.end_time, e.title.strip(), e.location, e.note,
                         e.remind_min))
        eid = cur.lastrowid
    r = db.conn().execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    return dict(r)


@router.patch("/api/events/{event_id}")
def api_patch_event(event_id: int, p: EventPatch):
    with db.conn() as c:
        r = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "日程不存在")
        vals = {k: r[k] for k in ("title", "date", "start_time", "end_time", "location", "note", "remind_min")}
        vals.update(p.model_dump(exclude_none=True))
        if vals.get("remind_min") == -1:
            vals["remind_min"] = None                     # -1 = 清除提醒
        c.execute("UPDATE events SET title=?,date=?,start_time=?,end_time=?,location=?,note=?,remind_min=? WHERE id=?",
                  (vals["title"], vals["date"], vals["start_time"], vals["end_time"],
                   vals["location"], vals["note"], vals["remind_min"], event_id))
    return dict(db.conn().execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())


@router.delete("/api/events/{event_id}")
def api_del_event(event_id: int):
    with db.conn() as c:
        n = c.execute("DELETE FROM events WHERE id=? AND source='user'", (event_id,)).rowcount
    if n == 0:
        raise HTTPException(404, "日程不存在（校历事件不可删除）")
    return {"deleted": n}
