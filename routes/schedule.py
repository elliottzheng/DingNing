# -*- coding: utf-8 -*-
"""课程/日程/调课路由。"""
from datetime import date, timedelta

from fastapi import APIRouter, Form, HTTPException, UploadFile

import db
import excel_import
import logic
from common import date_param, eff_weekday, lesson_at, now_str
from models import LessonCancelIn, LessonMoveIn

router = APIRouter()


@router.get("/api/day")
def api_day(d: str | None = None):
    from datetime import datetime
    day = date_param(d)
    info = logic.day_items(day)
    now = datetime.now()
    now_t = now.strftime("%H:%M")
    if info["date"] == date.today().isoformat():
        for it in info["items"]:
            it["ongoing"] = bool(it["start"] and it["start"] <= now_t <= (it["end"] or "23:59"))
            if it["end"]:
                it["past"] = it["end"] < now_t
            elif it["start"]:
                h, m = map(int, it["start"].split(":"))
                it["past"] = h * 60 + m + 60 < now.hour * 60 + now.minute   # 无结束时间默认1小时
            else:
                it["past"] = False
    todos = logic.todos_snapshot()
    info["todos"] = todos
    with db.conn() as c:
        info["due_todos"] = [dict(r) for r in c.execute(
            "SELECT id, title, due, priority, note FROM todos "
            "WHERE done=0 AND due LIKE ? ORDER BY due", (info["date"] + "%",))]
    for it in info["items"]:
        it.setdefault("date", info["date"])
    return info


@router.get("/api/week")
def api_week(d: str | None = None):
    import json
    day = date_param(d)
    monday = day - timedelta(days=day.weekday())
    days = []
    for i in range(7):
        info = logic.day_items(monday + timedelta(days=i))
        with db.conn() as c:
            info["due_todos"] = [dict(r) for r in c.execute(
                "SELECT id, title, due, priority, note FROM todos "
                "WHERE done=0 AND due LIKE ? ORDER BY due", (info["date"] + "%",))]
        # 与 /api/day 一致：给条目补上所属日期（周视图的调课/打分依赖它）
        for it in info["items"]:
            it.setdefault("date", info["date"])
        days.append(info)
    sections = []                       # 完整节次时间表（含当日无课的节次）
    lt = db.get_config("lesson_times")
    if lt:
        try:
            for name, se in json.loads(lt).items():
                if se and len(se) == 2:
                    sections.append({"name": name, "start": se[0], "end": se[1]})
            sections.sort(key=lambda s: s["start"])
        except ValueError:
            sections = []
    return {"week_start": monday.isoformat(), "days": days, "sections": sections,
            "term": days[0]["term"], "week": days[0]["week"]}


@router.post("/api/lesson_cancel")
def api_lesson_cancel(b: LessonCancelIn):
    with db.conn() as c:
        cur = c.execute("INSERT INTO lesson_cancels(date, weekday, section, note, created_at) VALUES(?,?,?,?,?)",
                        (b.date, b.weekday, b.section, b.note, now_str()))
        cid = cur.lastrowid
    return {"id": cid}


@router.delete("/api/lesson_cancel/{cid}")
def api_lesson_cancel_del(cid: int):
    with db.conn() as c:
        n = c.execute("DELETE FROM lesson_cancels WHERE id=?", (cid,)).rowcount
    if n == 0:
        raise HTTPException(404, "调课记录不存在")
    return {"deleted": n}


@router.post("/api/lesson_move")
def api_lesson_move(b: LessonMoveIn):
    lesson = lesson_at(date_param(b.date), b.section)
    if not lesson:
        raise HTTPException(404, "该节次没有课程")
    label = f"{lesson['class_name']}{lesson['subject']}"
    new_wd = date.fromisoformat(b.new_date).weekday()
    with db.conn() as c:
        c.execute("INSERT INTO lesson_cancels(date, weekday, section, note, created_at) VALUES(?,?,?,?,?)",
                  (b.date, b.weekday, b.section, f"换至 {b.new_date} {b.new_start}", now_str()))
        ver = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                        (b.new_date,)).fetchone()[0] or lesson["effective_from"]
        # 新位置上同班同节次的循环课让位（避免重复）
        c.execute("DELETE FROM lessons WHERE effective_from=? AND weekday=? AND section=? "
                  "AND class_name=? AND date IS NULL",
                  (ver, new_wd, lesson["section"], lesson["class_name"]))
        c.execute("INSERT INTO lessons(weekday, section, start_time, end_time, class_name, subject, "
                  "location, effective_from, date, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (new_wd, lesson["section"], b.new_start, b.new_end, lesson["class_name"],
                   lesson["subject"], lesson["location"], ver, b.new_date,
                   f"原 {b.date[5:]} {b.section} 调至本节"))
    return {"ok": True, "moved": label, "to": f"{b.new_date} {b.new_start}-{b.new_end}"}


MAX_UPLOAD = 5 * 1024 * 1024          # 5MB


@router.post("/api/import/text")
def api_import_text(b: dict):
    """明文课表导入：一行=一节课；有错全不导，返回全部错误行。"""
    text = str(b.get("text") or "")
    eff = str(b.get("effective_from") or "") or None
    replace = bool(b.get("replace", True))
    if not text.strip():
        raise HTTPException(400, "课表内容为空")
    try:
        return excel_import.import_schedule_text(text, effective_from=eff, replace=replace)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/import/calendar-text")
def api_import_calendar_text(b: dict):
    """明文校历导入：替换全部日历来源事件；有错全不导。"""
    text = str(b.get("text") or "")
    year = b.get("year")
    if not text.strip():
        raise HTTPException(400, "校历内容为空")
    try:
        return excel_import.import_calendar_text(
            text, this_year=int(year) if year else None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/import/upload")
async def api_import_upload(file: UploadFile, effective_from: str = Form("")):
    """上传课表/教学安排 Excel（multipart，.xlsx/.xls，≤5MB）并导入当前用户库。

    effective_from：课表文件的生效日（YYYY-MM-DD，默认今天）——学期中换课表时
    设为未来日期，则切换日之前照旧课表、当天起自动执行新课表。"""
    import os
    from datetime import datetime as _dt
    name = os.path.basename(file.filename or "")
    if not name.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "仅支持 .xlsx / .xls 文件")
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, "文件超过 5MB 上限")
    os.makedirs(db.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(db.UPLOAD_DIR, f"{_dt.now():%Y%m%d%H%M%S}_{name}")
    with open(path, "wb") as f:
        f.write(data)
    try:
        if any(k in name for k in ("教学安排", "校历")):
            report = excel_import.import_arrangement(path)
        else:
            report = excel_import.import_schedule(path, effective_from=effective_from or None)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"导入失败：{e}")
    return {"ok": True, "report": report}


@router.get("/api/events/search")
def api_events_search(kw: str = "", limit: int = 8):
    """按名称搜日程：语义优先（bge 向量），离线回退两字按序匹配。未来优先，不足补最近过期。"""
    import eventsearch
    r = eventsearch.find(kw, date.today(), max(1, min(limit, 20)))
    return {"results": r["results"], "past": r["past"], "engine": r["engine"]}
