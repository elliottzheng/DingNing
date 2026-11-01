# -*- coding: utf-8 -*-
"""Agent 执行器：单指令 → 模型自主多步调工具 → 确定性校验 → 原子落库（可整批撤销）。

与原型的差别：
  1. 工具执行器接**真实数据**（lessons / events / lesson_cancels）；
  2. 两阶段执行：模型先产出"操作计划"，校验闸门全过才落库——不会写一半；
  3. 每次执行登记为 agent_batches 批次，微信发「撤销」可整批回滚。

安全边界：
  - 模型只产出计划，不直接写库；写库由 validate+apply 代码完成；
  - 校验不通过 → 带错误回喂一次让模型自纠；仍不过 → 整批拒绝，不落库；
  - 工具白名单只有读/调课/建日程/待办。
"""
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import date, timedelta

import common
import db
import logic

log = logging.getLogger("workbench")

API_URL = "https://api.deepseek.com/chat/completions"
MAX_STEPS = 10

SYSTEM_COMMON = ("你是叮咛（教师智能日程助手）的执行助手，把用户的一条指令拆解成工具调用并执行，全部完成后调 finish。"
          "规则：调课/补课前先 list_lessons 确认该节次是哪门课；日期按今天推算，"
          "下周X=下一周的周X。"
          "【时刻与日期】晚上/今晚/明晚的时刻一律按 12 小时制换算：晚上8点半=20:30、"
          "晚上9点=21:00；明晚=明天的晚上（不是今天上午）。"
          "【标题】去掉「帮我加个日程」之类请求性语句，只留核心事项（如「教研组会议」）。"
          "所有操作会先经过校验，不合法会被驳回，请按驳回原因修正。"
          "【纯查询也是正常任务】问课表/日程/待办/上课安排等，先调查询工具"
          "（list_day/list_lessons/search_events）拿到事实，再把结果整理进 finish 的 summary "
          "直接回答用户——这类指令不需要产出任何写入操作，回答完就结束。"
          "【改/删必须先拿 id】修改或取消待办：先 list_todos 拿 todo_id（或给唯一"
          "title_match）；修改或取消日程：先 list_day/search_events 拿 event_id——"
          "只能动 user 来源的日程，校历事件（假期/考试等）不可改删。"
          "【只陈述工具返回的事实】list_day 结果里 cancelled:true 表示该节已停课/已调走，"
          "回答某天上什么课时要明确说明这些课已停、不要当成正常上课；"
          "绝不编造'我核对过/系统内没有记录'之类没做过的检查结论——"
          "工具没返回的事实就直说没查到，不要断言不存在。"
          "【禁止虚构工具故障】除非某个工具调用真的返回了错误，否则不得在 summary 里"
          "说'工具不可用/无法使用/回报失败'之类的话；你选择某种操作类型就是你的判断，"
          "如实的说法是'按日程记录'或'按待办记录'，不要附加虚构的理由。"
          "【提醒】用户说「提前X分钟提醒」时：日程用 create_event/update 事件带上 remind_min=X；"
          "待办用 add_todo/update_todo 带 remind_min=X。「开始时提醒」= remind_min 0。"
          "【回复排版】finish 的 summary 是纯文本渠道，禁止 markdown 符号（**、#、|、`）——"
          "会原样露出。排版规范：用【小标题】分组；每行一条，以 · 或 - 开头；"
          "同类多条合并写；紧急/重要的放最前；结尾不要客套话。")

SYSTEM_MODE = {
    "schedule": "你当前处于【调课模式】：只能调课、停课、建日程（含跨天补课），"
                "不能做其他写入操作。",
    "general": "你可以使用全部工具：记待办/日程、调课、搜日程。",
}


def _system_for(mode: str) -> str:
    return SYSTEM_COMMON + (SYSTEM_MODE.get(mode) or SYSTEM_MODE["general"])


TOOLS = [
 {
  "type": "function",
  "function": {
   "name": "list_lessons",
   "description": "查询周期课表：某周几的全部课程（节次/班级/时间）",
   "parameters": {
    "type": "object",
    "properties": {
     "weekday": {
      "type": "integer",
      "description": "0=周一..4=周五"
     }
    },
    "required": [
     "weekday"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "list_day",
   "description": "查询某日期已有的日程（含周期课与一次性日程）",
   "parameters": {
    "type": "object",
    "properties": {
     "date": {
      "type": "string",
      "description": "YYYY-MM-DD"
     }
    },
    "required": [
     "date"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "list_todos",
   "description": "查未完成待办（含逾期与无截止的），按 due 排序",
   "parameters": {
    "type": "object",
    "properties": {
     "within_days": {
      "type": "integer",
      "description": "只看未来 N 天内的截止，默认 30"
     }
    }
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "update_todo",
   "description": "计划：修改一条待办（改期/改标题/改优先级/标记完成）。先用 list_todos 拿 todo_id，或给 title_match 唯一匹配",
   "parameters": {
    "type": "object",
    "properties": {
     "todo_id": {
      "type": "integer",
      "description": "待办 id（list_todos 返回的）"
     },
     "title_match": {
      "type": "string",
      "description": "标题关键词（必须唯一匹配，否则驳回）"
     },
     "due": {
      "type": "string",
      "description": "新截止 YYYY-MM-DD HH:MM"
     },
     "title": {
      "type": "string",
      "description": "新标题"
     },
     "priority": {
      "type": "integer",
      "description": "0低/1中/2高"
     },
     "done": {
      "type": "boolean",
      "description": "true=标记完成"
     }
    }
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "change_event",
   "description": "计划：修改已有日程的日期/时间/标题（只能动 user 来源，校历不可改）。先用 list_day 或 search_events 拿 event_id",
   "parameters": {
    "type": "object",
    "properties": {
     "event_id": {
      "type": "integer",
      "description": "日程 id"
     },
     "date": {
      "type": "string",
      "description": "新日期 YYYY-MM-DD"
     },
     "start_time": {
      "type": "string",
      "description": "新开始 HH:MM"
     },
     "end_time": {
      "type": "string",
      "description": "新结束 HH:MM"
     },
     "title": {
      "type": "string",
      "description": "新标题"
     }
    },
    "required": [
     "event_id"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "cancel_event",
   "description": "计划：删除一条 user 来源的日程（活动取消等；校历事件不可删，可撤销）",
   "parameters": {
    "type": "object",
    "properties": {
     "event_id": {
      "type": "integer",
      "description": "日程 id（list_day/search_events 返回的）"
     }
    },
    "required": [
     "event_id"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "cancel_lesson",
   "description": "计划：取消某日期的某节课（原课停上），note 写明去向",
   "parameters": {
    "type": "object",
    "properties": {
     "date": {
      "type": "string",
      "description": "YYYY-MM-DD"
     },
     "section": {
      "type": "string",
      "description": "节次，如 下午1"
     },
     "note": {
      "type": "string",
      "description": "去向说明"
     }
    },
    "required": [
     "date",
     "section",
     "note"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "create_event",
   "description": "计划：在某日期创建日程（补课/调课后的课/调研/活动都算）",
   "parameters": {
    "type": "object",
    "properties": {
     "date": {
      "type": "string",
      "description": "YYYY-MM-DD"
     },
     "start_time": {
      "type": "string",
      "description": "HH:MM"
     },
     "end_time": {
      "type": "string",
      "description": "HH:MM"
     },
     "remind_min": {
      "type": "integer",
      "description": "开始前提前提醒的分钟数；用户说了「提前X分钟提醒」才填，否则不传"
     },
     "title": {
      "type": "string",
      "description": "标题，含班级时写成如 23班信息"
     }
    },
    "required": [
     "date",
     "start_time",
     "end_time",
     "title"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "finish",
   "description": "操作计划完成后调用",
   "parameters": {
    "type": "object",
    "properties": {
     "summary": {
      "type": "string"
     }
    },
    "required": [
     "summary"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "search_events",
   "description": "按名称搜日程/事件（未来优先，含最近过期）",
   "parameters": {
    "type": "object",
    "properties": {
     "kw": {
      "type": "string",
      "description": "关键词，如 校运会/月考/沙龙"
     }
    },
    "required": [
     "kw"
    ]
   }
  }
 },
 {
  "type": "function",
  "function": {
   "name": "add_todo",
   "description": "计划：新建一条待办（用户说要做什么/提醒做什么时用）",
   "parameters": {
    "type": "object",
    "properties": {
     "title": {
      "type": "string",
      "description": "待办标题"
     },
     "due": {
      "type": "string",
      "description": "截止 YYYY-MM-DD HH:MM，可选"
     },
     "remind_min": {
      "type": "integer",
      "description": "距截止提前提醒的分钟数；用户说了「提前X分钟提醒」才填，否则不传"
     },
     "priority": {
      "type": "integer",
      "description": "2=重要 1=普通 0=低，缺省 1"
     },
     "note": {
      "type": "string",
      "description": "备注，可空"
     }
    },
    "required": [
     "title"
    ]
   }
  }
 }
]
# 各模式可见的工具与允许的操作（模式隔离：调课模式不允许其他写入操作）
MODE_TOOLS = {
    "schedule": ["list_lessons", "list_day", "search_events", "list_todos",
                 "cancel_lesson", "create_event", "add_todo", "update_todo",
                 "change_event", "cancel_event", "finish"],
    "general": ["list_lessons", "list_day", "search_events", "list_todos",
                "cancel_lesson", "create_event", "add_todo", "update_todo",
                "change_event", "cancel_event", "finish"],
}
MODE_OPS = {
    "schedule": {"cancel_lesson", "create_event", "add_todo", "update_todo",
                 "change_event", "cancel_event"},
    "general": {"cancel_lesson", "create_event", "add_todo", "update_todo",
                "change_event", "cancel_event"},
}
# 计划里允许出现的操作：直接从各模式白名单派生，杜绝两处清单漂移
# （历史上这份清单漏了 add_todo，导致所有建待办指令报 unknown tool——模型只能
#  如实转述「接口报错」或自行改用日程补救）
OP_TYPES = set(MODE_OPS["schedule"]) | set(MODE_OPS["general"])

# ---------------- 工具打包预选（收窄是优化不是决策，猜错可自愈） ----------------
# 每个工具所属的包；预选只对 general 模式生效（schedule 的模式隔离是安全边界）
TOOL_BUNDLES = {
    "list_lessons": {"read"},
    "list_day": {"read"},
    "search_events": {"read"},
    "cancel_lesson": {"scheduling"},
    "create_event": {"scheduling"},
    "change_event": {"scheduling"},
    "cancel_event": {"scheduling"},
    "add_todo": {"todo"},
    "list_todos": {"todo", "read"},
    "update_todo": {"todo"},
    "finish": {"meta"},
}

# 无单一高置信信号可收窄，保持全量
_BUNDLE_SIGNALS = []


def _select_bundles(text: str):
    """单一高置信信号 → 收窄到该包+read+meta；其余一律全量。

    单词信号太弱（"例会""交"歧义大），不收窄——收窄是优化不是决策。
    """
    hits = set()
    for pat, bundle in _BUNDLE_SIGNALS:
        if pat.search(text):
            hits |= bundle
    if len(hits) != 1:
        return None
    # 包之外还有写入/调度意图词 → 混合意图，给全量
    if re.search(r"(建个?|加个?|日程|待办|提醒|会议|教研|调课|停课|排课|改期)", text):
        return None
    return hits | {"read", "meta"}


# ---------------- LLM 循环：产出操作计划 ----------------

def _chat(messages, model, tools=None):
    # max_tokens 4000：大通知一条响应里可能有 30+ 个工具调用，1200 会截断，
    # 尾部 tool_calls 的 arguments 变成残缺 JSON（实测教训）
    body = {"model": model, "messages": messages, "temperature": 0.1,
            "tools": tools or TOOLS, "tool_choice": "auto",
            "thinking": {"type": "enabled", "budget_tokens": 512}, "max_tokens": 4000}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(3):                     # 瞬时 400/5xx/超时退避重试（实测发生过）
        req = urllib.request.Request(
            API_URL, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {db.get_config('ai_api_key')}"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode("utf-8"))
            msg = resp["choices"][0]["message"]
            msg["finish_reason"] = resp["choices"][0]["finish_reason"]
            return msg
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:120]
            last_err = f"HTTP {e.code}: {detail}"
            if e.code not in (400, 401, 408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
        except Exception as e:
            last_err = repr(e)
            if attempt == 2:
                raise
        import time as _t
        _t.sleep(3 + attempt * 5)
    raise RuntimeError(f"DeepSeek 调用失败：{last_err}")


def _lessons_of(weekday: int, d: date | None = None) -> list:
    """某星期的课（按 d 当日生效的课表版本；判断过去用过去的课表）。"""
    as_of = (d or date.today()).isoformat()
    latest = db.conn().execute(
        "SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
        (as_of,)).fetchone()[0]
    if not latest:
        return []
    return [dict(r) for r in db.conn().execute(
        "SELECT section, start_time, end_time, class_name, subject, location "
        "FROM lessons WHERE weekday=? AND effective_from=? ORDER BY start_time",
        (weekday, latest))]


def _exec_read(name, args):
    """只读工具，直接查真实库。"""
    if name == "list_lessons":
        wd = int(args["weekday"])
        wd_name = logic.WD[wd] if 0 <= wd <= 6 else str(wd)
        out = [{"weekday_name": wd_name,
                "section": l["section"], "time": f"{l['start_time']}-{l['end_time']}",
                "cls": f"{l['class_name']}{l['subject']}"} for l in _lessons_of(wd)]
        return json.dumps(out, ensure_ascii=False)
    if name == "list_day":
        info = logic.day_items(date.fromisoformat(args["date"]))
        out = []
        for i in info["items"]:
            it = {"time": f"{i['start']}-{i['end'] or ''}", "title": i["title"]}
            if i.get("cancelled"):
                it["cancelled"] = True
                it["note"] = i.get("note", "")
            out.append(it)
        return json.dumps(out, ensure_ascii=False)
    if name == "search_events":
        kw = str(args.get("kw") or "").strip()
        if not kw:
            return json.dumps({"error": "kw 不能为空"}, ensure_ascii=False)
        rows = db.conn().execute(
            "SELECT id, date, start_time, end_time, title, location, source FROM events "
            "WHERE title LIKE ? AND date>=? ORDER BY date, start_time LIMIT 20",
            (f"%{kw}%", (date.today() - timedelta(days=30)).isoformat())).fetchall()
        return json.dumps([dict(r) for r in rows], ensure_ascii=False)
    if name == "list_todos":
        days = max(1, min(int(args.get("within_days") or 30), 180))
        horizon = (date.today() + timedelta(days=days)).isoformat()
        rows = db.conn().execute(
            "SELECT id, title, due, priority FROM todos WHERE done=0 ORDER BY due, id"
        ).fetchall()
        out = [dict(r) for r in rows if not r["due"] or r["due"][:10] <= horizon]
        return json.dumps(out, ensure_ascii=False)
    return json.dumps({"error": f"unknown {name}"})


def plan_ops(text: str, today: date, mode: str = "general") -> tuple:
    """跑 LLM 循环，产出操作计划。返回 (ops, summary, error)；error 非空=模型侧失败。"""
    wd = "一二三四五六日"[today.weekday()]
    messages = [{"role": "system", "content": _system_for(mode)},
                {"role": "user", "content": f"今天是 {today.isoformat()} 周{wd}。指令：{text}"}]
    ops, summary = [], ""
    malformed = []
    truncated = False
    model = db.get_config("ai_model", "deepseek-v4-flash")
    finished = False
    all_tools = [t for t in TOOLS if t["function"]["name"] in MODE_TOOLS[mode]]
    tools = all_tools
    provided = {t["function"]["name"] for t in tools}
    if mode == "general":
        bundles = _select_bundles(text)
        if bundles:
            tools = [t for t in all_tools
                     if TOOL_BUNDLES.get(t["function"]["name"], set()) & bundles]
            provided = {t["function"]["name"] for t in tools}
    for _ in range(MAX_STEPS):
        msg = _chat(messages, model, tools=tools)
        truncated = msg.get("finish_reason") == "length"
        calls = msg.get("tool_calls") or []
        if not calls:
            summary = msg.get("content") or ""
            break
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            if name not in provided and name in TOOL_BUNDLES:
                # 预选收窄漏了它：模型仍然要用 → 照常执行，并把工具集扩展回全量。
                # 收窄只是优化，绝不能因此让指令失败。
                log.info("工具预选遗漏，扩展为全量：%s（指令：%s）", name, text[:30])
                tools = all_tools
                provided = {t["function"]["name"] for t in tools}
            try:
                args = json.loads(c["function"]["arguments"])
            except Exception:
                # 残缺参数：响应被截断的尾部调用。不产出空 op，
                # 记为错误，让 handle 明确告诉用户"响应不完整"
                malformed.append(name)
                continue
            if name == "finish":
                summary = args.get("summary", "")
                finished = True
                break
            if name in OP_TYPES:
                args["_cls"] = None
                ops.append({"op": name, **args})
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps({"planned": True, "pending_validation": True})})
            else:
                result = _exec_read(name, args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if finished:
            break
    if truncated:
        malformed.append("finish_reason=length（响应被截断）")
    err = ""
    if malformed:
        err = f"模型响应不完整（{';'.join(map(str, malformed))}），部分操作缺失"
    return ops, summary, err


# ---------------- 确定性校验（代码逻辑，不信模型） ----------------

def _valid_remind(v) -> bool:
    if v is None:
        return True
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate(ops, today: date, mode: str = "schedule") -> tuple:
    """确定性校验，逐操作判定：返回 (valid_ops, invalid)。

    invalid = [{"op":…, "reason":…}]。任一无效 → 调用方整批拒绝（设计原则）。
    模式隔离：调课模式只允许调课/建日程/待办类写入。
    """
    valid, invalid = [], []
    seen_cancels = set()
    allowed = MODE_OPS[mode]
    for op in ops:
        reasons = []
        if op["op"] not in allowed:
            invalid.append({"op": op, "reason": "调课模式不支持该操作"})
            continue
        if op["op"] == "cancel_lesson":
            try:
                wd = date.fromisoformat(op["date"]).weekday()
            except ValueError:
                reasons.append("日期格式不合法")
                wd = None
            real = {}
            if wd is not None:
                real = {l["section"]: f"{l['class_name']}{l['subject']}"
                        for l in _lessons_of(wd, date.fromisoformat(op["date"]))}
                if op["section"] not in real:
                    reasons.append(f"该日课表没有「{op['section']}」"
                                   f"（该日有：{'、'.join(real) or '无'}）")
            key = (op["date"], op["section"])
            if key in seen_cancels:
                reasons.append("重复取消")
            seen_cancels.add(key)
            if not reasons:
                op["_cls"] = real[op["section"]]
        elif op["op"] == "create_event":
            t = op.get("title", "")
            m = re.search(r"(\d{1,2})班", t)
            if m and op.get("date"):
                known = {x[0] for x in db.conn().execute(
                    "SELECT DISTINCT class_name FROM lessons WHERE effective_from<=?",
                    (op["date"],))}
                known |= {f"{int(n)}班" for n in
                          json.loads(db.get_config("my_classes", "[]"))}
                if f"{m.group(1)}班" not in known:
                    reasons.append(f"{m.group(1)}班不在你导入的课表里")
            if op.get("start_time") and op.get("end_time") and op["end_time"] <= op["start_time"]:
                reasons.append("结束时间不晚于开始时间")
            if common.duplicate_event_exists(t, op.get("date", ""),
                                             op.get("start_time", ""), op.get("end_time", "")):
                reasons.append("与已有日程完全相同")
            if not _valid_remind(op.get("remind_min")):
                reasons.append("remind_min 应为非负整数分钟")
        elif op["op"] == "add_todo":
            t_title = str(op.get("title", "")).strip()
            if not t_title:
                reasons.append("待办标题为空")
            if op.get("due"):
                try:
                    from datetime import datetime as _dt
                    _dt.strptime(op["due"][:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    reasons.append("due 格式应为 YYYY-MM-DD HH:MM")
            if op.get("priority") is not None and op["priority"] not in (0, 1, 2):
                reasons.append("priority 应为 0/1/2")
            if not _valid_remind(op.get("remind_min")):
                reasons.append("remind_min 应为非负整数分钟")
        elif op["op"] == "update_todo":
            row = None
            if op.get("todo_id"):
                row = db.conn().execute("SELECT * FROM todos WHERE id=?",
                                        (op["todo_id"],)).fetchone()
                if not row:
                    reasons.append(f"待办 #{op['todo_id']} 不存在")
            elif str(op.get("title_match") or "").strip():
                cands = db.conn().execute(
                    "SELECT * FROM todos WHERE done=0 AND title LIKE ? ORDER BY id",
                    (f"%{op['title_match'].strip()}%",)).fetchall()
                if not cands:
                    reasons.append(f"没有标题含「{op['title_match']}」的未完成待办")
                elif len(cands) > 1:
                    reasons.append(f"「{op['title_match']}」匹配到 {len(cands)} 条待办，"
                                   "先用 list_todos 确认后用 todo_id")
                else:
                    row = cands[0]
            else:
                reasons.append("需要 todo_id 或 title_match")
            if row is not None:
                op["_todo_id"] = row["id"]
                op["_prev_todo"] = {"title": row["title"], "due": row["due"],
                                    "priority": row["priority"], "done": row["done"]}
            if op.get("due"):
                try:
                    from datetime import datetime as _dt
                    _dt.strptime(op["due"][:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    reasons.append("due 格式应为 YYYY-MM-DD HH:MM")
            if op.get("priority") is not None and op["priority"] not in (0, 1, 2):
                reasons.append("priority 应为 0/1/2")
            if op.get("done") is not None and op["done"] not in (0, 1, True, False):
                reasons.append("done 应为 true/false")
            if not any(k in op for k in ("due", "title", "priority", "done")):
                reasons.append("没有指定任何修改内容")
        elif op["op"] == "change_event":
            ev = db.conn().execute("SELECT * FROM events WHERE id=?",
                                   (op.get("event_id"),)).fetchone()
            if not ev:
                reasons.append(f"日程 #{op.get('event_id')} 不存在")
            elif ev["source"] != "user":
                reasons.append("校历事件不可修改")
            else:
                op["_prev_event"] = {"date": ev["date"], "start_time": ev["start_time"],
                                     "end_time": ev["end_time"], "title": ev["title"]}
            base_s = op.get("start_time") or (ev["start_time"] if ev else "")
            base_e = op.get("end_time") or (ev["end_time"] if ev else "")
            if base_s and base_e and base_e <= base_s:
                reasons.append("结束时间不晚于开始时间")
            if not any(op.get(k) for k in ("date", "start_time", "end_time", "title")):
                reasons.append("没有指定任何修改内容")
        elif op["op"] == "cancel_event":
            ev = db.conn().execute("SELECT * FROM events WHERE id=?",
                                   (op.get("event_id"),)).fetchone()
            if not ev:
                reasons.append(f"日程 #{op.get('event_id')} 不存在")
            elif ev["source"] != "user":
                reasons.append("校历事件不可删除")
            else:
                op["_prev_event"] = {"id": ev["id"], "date": ev["date"],
                                     "start_time": ev["start_time"],
                                     "end_time": ev["end_time"], "title": ev["title"],
                                     "location": ev["location"]}
        else:
            reasons.append("未知操作类型")
        if reasons:
            invalid.append({"op": op, "reason": "；".join(reasons)})
        else:
            valid.append(op)
    return valid, invalid


# ---------------- 原子落库 + 批次登记 ----------------

def apply(ops, instruction: str, source: str) -> int:
    """校验通过后一次性落库，登记批次。返回 batch_id。"""
    with db.conn() as c:
        cur = c.execute("INSERT INTO agent_batches(instruction, source, status, created_at) "
                        "VALUES(?,?,'applied',?)", (instruction, source, common.now_str()))
        batch_id = cur.lastrowid
        for op in ops:
            if op["op"] == "cancel_lesson":
                wd = date.fromisoformat(op["date"]).weekday()
                cur = c.execute("INSERT INTO lesson_cancels(date, weekday, section, note, created_at) "
                                "VALUES(?,?,?,?,?)",
                                (op["date"], wd, op["section"], op.get("note", ""), common.now_str()))
                target = cur.lastrowid
            elif op["op"] == "add_todo":
                cur = c.execute("INSERT INTO todos(title, due, priority, note, done, created_at, remind_min) "
                                "VALUES(?,?,?,?,0,?,?)",
                                (op["title"], op.get("due", ""),
                                 op.get("priority", 1) or 1, op.get("note", ""),
                                 common.now_str(), op.get("remind_min")))
                target = cur.lastrowid
            elif op["op"] == "update_todo":
                sets, vals = [], []
                for k in ("title", "due", "priority", "remind_min"):
                    if op.get(k) is not None:
                        sets.append(f"{k}=?")
                        vals.append(op[k])
                if op.get("done") is not None:
                    sets.append("done=?")
                    vals.append(1 if op["done"] else 0)
                vals.append(op["_todo_id"])
                c.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id=?", vals)
                target = op["_todo_id"]
            elif op["op"] == "change_event":
                sets, vals = [], []
                for k in ("date", "start_time", "end_time", "title"):
                    if op.get(k):
                        sets.append(f"{k}=?")
                        vals.append(op[k])
                vals.append(op["event_id"])
                c.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", vals)
                target = op["event_id"]
            elif op["op"] == "cancel_event":
                c.execute("DELETE FROM events WHERE id=? AND source='user'",
                          (op["event_id"],))
                target = op["event_id"]
            else:  # create_event
                cur = c.execute("INSERT INTO events(date,start_time,end_time,title,location,note,source,kind,remind_min) "
                                "VALUES(?,?,?,?,?,'','user','event',?)",
                                (op["date"], op.get("start_time", ""), op.get("end_time", ""),
                                 op.get("title", ""), "", op.get("remind_min")))
                target = cur.lastrowid
            c.execute("INSERT INTO agent_batch_ops(batch_id, op_type, payload, target_id, created_at) "
                      "VALUES(?,?,?,?,?)",
                      (batch_id, op["op"], json.dumps(op, ensure_ascii=False), target, common.now_str()))
    return int(batch_id or 0)


def undo_last() -> str:
    """撤销最近一个 applied 批次：逆序回放操作。返回给用户的回执文本。"""
    with db.conn() as c:
        batch = c.execute("SELECT * FROM agent_batches WHERE status='applied' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
        if not batch:
            return "没有可撤销的 agent 操作记录。"
        ops = c.execute("SELECT * FROM agent_batch_ops WHERE batch_id=? ORDER BY id DESC",
                        (batch["id"],)).fetchall()
        undone = 0
        for op in ops:
            if op["op_type"] == "create_event":
                undone += c.execute("DELETE FROM events WHERE id=?", (op["target_id"],)).rowcount
            elif op["op_type"] == "cancel_lesson":
                undone += c.execute("DELETE FROM lesson_cancels WHERE id=?", (op["target_id"],)).rowcount
            elif op["op_type"] == "add_todo":
                undone += c.execute("DELETE FROM todos WHERE id=?", (op["target_id"],)).rowcount
            elif op["op_type"] == "update_todo":
                p = json.loads(op["payload"])["_prev_todo"]
                undone += c.execute("UPDATE todos SET title=?, due=?, priority=?, done=? "
                                    "WHERE id=?",
                                    (p["title"], p["due"], p["priority"], p["done"],
                                     op["target_id"])).rowcount
            elif op["op_type"] == "change_event":
                p = json.loads(op["payload"])["_prev_event"]
                undone += c.execute("UPDATE events SET date=?, start_time=?, end_time=?, "
                                    "title=? WHERE id=?",
                                    (p["date"], p["start_time"], p["end_time"],
                                     p["title"], op["target_id"])).rowcount
            elif op["op_type"] == "cancel_event":
                p = json.loads(op["payload"])["_prev_event"]
                undone += c.execute(
                    "INSERT INTO events(id,date,start_time,end_time,title,location,note,source,kind) "
                    "VALUES(?,?,?,?,?,?,'','user','event')",
                    (p["id"], p["date"], p["start_time"], p["end_time"], p["title"],
                     p["location"])).rowcount
        c.execute("UPDATE agent_batches SET status='undone' WHERE id=?", (batch["id"],))
    log.info("Agent 批次 #%s 已撤销（%d 个操作）：%s", batch["id"], undone, batch["instruction"][:40])
    return (f"↩️ 已撤销「{batch['instruction'][:40]}」的 {undone} 个操作，"
            f"课表/日程已恢复。")


# ---------------- 对外入口 ----------------

def enabled() -> bool:
    return db.get_config("agent_enabled", "1") != "0"


def handle(text: str, today: date | None = None, source: str = "wechat",
           mode: str = "schedule") -> str:
    """处理一条复杂指令：计划 → 校验 → 全部通过才原子落库（任一无效整批拒绝）。

    mode='schedule'：调课/建日程模式（模式隔离：只允许课表类写入）。
    """
    today = today or date.today()
    ops, summary, err = plan_ops(text, today, mode=mode)
    if not ops:
        if not err and summary.strip():
            # 零操作却自称"失败/接口报错"= 模型没产出操作就找借口（幻觉）。
            # 绝不能把借口当答案回给用户：抛回 chatcmd 走规则兜底真实执行。
            add_intent = re.search(
                r"(记一下|记个|记条|加个?|添加|新建|建个?|要设置|提醒我|待办|日程)", text or "")
            if (re.search(r"未成功|失败|无法|报错|不可用|尝试不了|执行不了", summary)
                    or (add_intent and len(summary) < 200)):
                # 写入意图却零操作：无论总结如何措辞，都没完成用户要的事 → 兜底真做
                log.warning("模型零操作未产出写入，转规则兜底：%s | %s", text[:40], summary[:60])
                raise RuntimeError(f"agent 零操作未执行写入：{summary[:60]}")
            # 纯查询/无写入指令：模型经查询工具得出的答案就是回复
            return _strip_markup(summary)
        if "截断" in (err or ""):
            return ("这条指令涉及的改动太多，模型响应被截断了。"
                    "请拆成 2–3 条分别发送（如先发停课、再发补课）。")
        return (f"没能理解这条指令（{err or '没有产出任何操作'}），"
                f"请换个说法，或到网页版手动操作。")
    valid, invalid = validate(ops, today, mode=mode)
    if invalid:
        # 全部操作都是“与已有日程重复” → 不是失败，是确认无需重加
        if all("完全相同" in x["reason"] for x in invalid):
            dates = "、".join(sorted({x["op"].get("date", "") for x in invalid}))
            log.info("Agent 计划全部重复，确认即可：%s", text[:40])
            return f"✅ 这些日程已经在课表里了（{dates}），无需重复添加。"
        # 一条指令是一个完整意图：任一操作不合法就整批拒绝，保证记录完整可溯
        log.warning("Agent 计划校验未过，整批拒绝：%s | %s", text[:40],
                    [x["reason"] for x in invalid])
        reply = ("这条指令我拆解出的操作有问题，为安全起见整批未执行：\n"
                 + "\n".join(f"· {x['reason']}" for x in invalid)
                 + "\n请修正说法后重发。")
        if "截断" in (err or ""):
            reply += "\n（另外：这条指令太长，模型响应被截断了，建议拆成几条发送。）"
        return reply
    batch_id = apply(valid, text, source)
    log.info("Agent 批次 #%d 落库：%d 个操作（%s）", batch_id, len(valid), text[:40])
    head = (summary or "已完成。").strip()
    reply = f"✅ 已执行 {len(valid)} 项操作（回复「撤销」可整体撤销）：\n{head}"
    return _strip_markup(reply)


def _strip_markup(text: str) -> str:
    """纯文本渠道兜底：剥掉模型可能漏出的 markdown 符号（排版规则已禁，双保险）。"""
    text = re.sub(r"\*{1,3}", "", text)                # 加粗/斜体星号
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)  # 标题井号
    return text.replace("`", "")


def classify_mode(text: str) -> str | None:
    """识别指令模式：'schedule'=调课/建日程（模式隔离），None=不进 agent。"""
    t = text.strip()
    if re.search(r"(挪到|移到|改期|调到|换到|补周二|补周三|补周四|补周五|作息|通知|时间调整|"
                 r"改成待办|转成待办|改为待办|改成日程|转成日程|改为日程)", t):
        return "schedule"
    if len(re.findall(r"\d{1,2}[:：点]", t)) >= 3:
        return "schedule"
    return None


def _should_use_agent(text: str) -> bool:
    """兼容旧调用。"""
    return classify_mode(text) is not None
