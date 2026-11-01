# -*- coding: utf-8 -*-
"""DeepSeek AI 解析：自然语言 → 日程/待办 JSON。失败返回 None，由规则解析器兜底。"""
import json
import logging
import re
import urllib.request

import db

log = logging.getLogger("workbench")

API_URL = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """你是学校教师的日程解析助手，把用户的一句话解析为一条「日程(event)」或「待办(todo)」。

规则：
1. 日程(event)=到点要发生的事（会议、上课、监考、考试、活动）；待办(todo)=需要完成的事（备课、改卷、交材料），常见“X前/之前/截止”。
2. 相对日期按给出的“今天”推算：“下周X”=下一周的周X；“周X”=最近即将到来的周X（含今天）；“大后天”=今天+3 天。
3. “X前”类待办：截止时间(due)在那一天之内完成；若上下文提到那一天要上课，可用第一节课的开始时间作为截止时间。
4. 时刻换算以口语为准：“下午3点”=15:00，“上午9点”=09:00，“晚上7点半”=19:30；“明晚8点半”=**明天**的 20:30（明晚=明天的晚上，晚上时段一律 12 小时制加 12；“今晚”=今天）。给出的作息表仅用于参考节次与时段，不要把“下午3”这种节次编号当作用户说的时间。
5. 日程有开始时间但没说结束时间时，end_time=开始时间+1小时；明确说了时长/结束时间则按所述。
6. 地点从句中提取（如“在机房”“报告厅”），没有则为空字符串。标题要简短（去掉日期时间描述），保留核心动作与对象。
7. 只输出一个 JSON 对象，不要输出任何其他文字：
{"type":"event或todo","title":"简短标题","date":"YYYY-MM-DD","start_time":"HH:MM或null","end_time":"HH:MM或null","location":"","note":"","priority":1,"due":"YYYY-MM-DD HH:MM或null","remind_min":null}

priority：2=重要/紧急/必须完成，1=普通，0=低。event 的 due 填 null。
remind_min：用户要求提前提醒时提取（"提前20分钟提醒我"→20，"提前1小时提醒"→60，"提前一天提醒"→1440，"开始时提醒我"→0）；没提提醒则填 null。日程按开始时间算，待办按截止时间算。"""


def _s(v):
    return str(v).strip() if v not in (None, "") else ""


def ai_parse(text: str, today):
    """调用 DeepSeek 解析一句话。返回与 nparser.parse 兼容的 dict，失败返回 None。"""
    api_key = db.get_config("ai_api_key")
    model = db.get_config("ai_model", "deepseek-v4-flash")
    if not api_key:
        return None
    try:
        t, wk = __import__("logic").week_number(today)
        week_part = f"{t['name']}第{wk}周" if t else "假期中"
        try:
            times = json.loads(db.get_config("lesson_times", "{}"))
        except Exception:
            times = {}
        slots = ", ".join(f"{v[0]}-{v[1]}" for v in times.values()) or "无"
        wd = "一二三四五六日"[today.weekday()]
        user = (f"今天是 {today.isoformat()} 周{wd}（{week_part}）。\n"
                f"当天课程时段（仅供了解日程背景，与用户所说时刻无关）：{slots}\n\n输入：{text}")

        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},   # v4-flash 默认带高强度思考，简单解析任务关闭
            "max_tokens": 1000,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(API_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read().decode("utf-8"))
        msg = resp["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content.strip():     # 极端情况下答案落在思考区，兜底提取
            content = msg.get("reasoning_content") or ""
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0) if m else content)

        out = {
            "type": "todo" if data.get("type") == "todo" else "event",
            "title": _s(data.get("title")) or text[:30],
            "date": _s(data.get("date")),
            "start_time": _s(data.get("start_time")),
            "end_time": _s(data.get("end_time")),
            "location": _s(data.get("location")),
            "note": _s(data.get("note")),
            "priority": int(data.get("priority") or 1),
            "due": _s(data.get("due")),
        }
        if out["type"] == "todo":
            out["date"] = (out["due"] or out["date"] or today.isoformat())[:10]
            if not out["due"]:
                out["due"] = f"{out['date']} 17:00"
            out["due"] = out["due"][:16]
        else:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", out["date"]):
                return None
        log.info("AI 解析(%s): %s => %s", model, text[:30], out["title"])
        return out
    except Exception:
        log.exception("AI 解析失败，回退规则解析")
        return None


def ai_chat(system: str, user: str, max_tokens: int = 700, temperature: float = 0.3):
    """通用 DeepSeek 对话：返回文本，失败返回 None。"""
    api_key = db.get_config("ai_api_key")
    model = db.get_config("ai_model", "deepseek-v4-flash")
    if not api_key:
        return None
    try:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(API_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        msg = resp["choices"][0]["message"]
        return (msg.get("content") or msg.get("reasoning_content") or "").strip() or None
    except Exception:
        log.exception("DeepSeek 调用失败")
        return None
