# -*- coding: utf-8 -*-
"""微信/邮件文字指令 → 工作台动作 → 纯文本回复。

**意图分类交给 AI（DeepSeek）判断**：`classify_intent()` 把用户一句话分成
query（查课表/查待办/帮助）或 add（记待办/日程）。规则只做两件事：
  1) AI 不可用时的兜底（无 ai_api_key / 超时 / 返回不合法）——宁可漏判也不误写；
  2) 已确认是 add 时，`api_quick_add` 内部同样先 AI 后规则地解析字段。

设计原则：**查询类漏判只是少回一条，写入类误判会污染数据**，
所以兜底规则对歧义句一律按 query 处理（"9.28课表""看看周三"这类）。
"""
import json
import logging
import re
from datetime import date, timedelta

import db
import logic
import nparser

log = logging.getLogger("workbench")

HELP = """🤖 叮咛微信指令
· 帮助 —— 显示本清单
· 课表 [日期] —— 当天课表+日程（日期可省略=今天）
· 下周课表 / 本周课程安排 —— 整周概览
· 今天 / 明天 / 周三 / 10月1日 —— 快捷查课表
· 待办 —— 未完成待办列表
· 撤销 —— 撤销最近一次 agent 批量操作
· 其他任意一句话 —— 直接记待办/日程
  例：周五前交成绩册／周三下午3点机房教研会议
· 复杂指令（多时间点/调课/改期/通知）—— 自动进入多步执行模式
消息由叮咛自动处理并原路回复。"""

# 整周概览：提到“本周/这周/下周”且不带具体周几（下周三=单日，不触发）
_WEEK_OVERVIEW = re.compile(r"(本周|这周|下周)(?![一二三四五六日天])")
_UNDO_WORDS = re.compile(r"^(?:撤销|撤回|撤销操作|撤销刚才|撤销上一?条?|undo)$", re.I)

# ---- 查询类关键词（AI 兜底用）：出现即倾向查询 ----
_QUERY_HINT = re.compile(r"(课表|日程|安排|有什么课|查看|查一下?|查询|看看|看一下?|帮我查|显示|列出|都有啥|有啥)")
# ---- 明确的查询对象词 ----
_QUERY_OBJ = re.compile(r"^(?:课表|日程|安排)\s*(.*)$")
# 整句只是一个日期词 → 查课表（"周三下午3点开会"不是纯日期词，不会误判）
_BARE_DAY = re.compile(r"^(今天|今日|今晚|明天|明日|后天|大后天|周[一二三四五六日天]|星期[一二三四五六日天]"
                       r"|这?周[一二三四五六日天末]|下周[一二三四五六日天]?|\d{1,2}月\d{1,2}[日号]?"
                       r"|\d{4}-\d{1,2}-\d{1,2})$")
# 日期词+课程后缀 → 查课表（"今日课程""明天的课""周三日程"）
_DAY_TAIL = re.compile(
    r"^(今天|今日|今晚|明天|明日|后天|大后天|这?周[一二三四五六日天末]|下周[一二三四五六日天]?"
    r"|星期[一二三四五六日天]|[一二三四五六七八九十]{1,3}月[一二三四五六七八九十]{1,3}[日号]?"
    r"|\d{1,2}月\d{1,2}[日号]?|\d{4}-\d{1,2}-\d{1,2})(?:的)?(?:课程|课表|日程|安排|课)$")
_ISO_DAY = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_TODO_WORDS = re.compile(r"^(?:待办|todo|任务)$", re.I)
# query 内部再分：查待办 vs 查课表（"我的待办有哪些""有什么任务"归前者）
_TODO_QUERY = re.compile(r"(待办|todo|任务)")
_HELP_WORDS = {"帮助", "help", "?", "？", "指令", "菜单"}

# AI 意图分类的系统提示
_INTENT_SYSTEM = """你是教师工作台的微信消息意图分类器。把用户发来的一句话分成两类：

- query = 查看/查询已有信息（查课表、查日程、查待办、要帮助）。用户没有要新增任何东西。
- add   = 要新增一条待办或日程（记录一件事、提醒一件事）。

判断要点：
1. 出现"课表/日程/安排"等词且**不含新增动作** → query。例如"9.28课表""看看周三的课表""查一下明天日程"都是 query。
2. 出现"记/加/添加/新增/安排(作动词)/提醒我/创建/写下/别忘了"等新增动作 → add。
3. 只说一件事要发生或要完成，没有任何新增动词 → add。例如"周五前交成绩册""周三下午3点教研会""明天带U盘"。
4. 纯日期词（"明天""周三""9.28"）→ query（用户想看那天的安排）。
5. "待办"单独一个词 → query；"加个待办：买纸" → add。
6. 帮助类（"帮助""help""？""怎么用"）→ query。

只输出 JSON，不要其他文字：
{"intent":"query或add","confidence":0到1,"reason":"不超过20字的判断理由"}
拿不准时 intent 填 query。"""


def _classify_via_ai(text: str) -> str | None:
    """DeepSeek 判意图（第二优先）。返回 'query'/'add'，不可用或失败返回 None。"""
    try:
        import ai_parse
        out = ai_parse.ai_chat(_INTENT_SYSTEM, f"输入：{text}",
                               max_tokens=200, temperature=0)
    except Exception:
        return None
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        data = json.loads(m.group(0) if m else out)
    except Exception:
        return None
    intent = (data.get("intent") or "").strip().lower()
    return intent if intent in ("query", "add") else None


def classify(text: str) -> tuple:
    """意图分类，两级兜底：DeepSeek → 规则。（SystemOne 已移除：主链路统一进智能体后，
    它只剩兜底路径的意图分类职责，网络调用收益为负；语义搜索仍用 SiliconFlow key。）"""
    intent = _classify_via_ai(text)
    if intent in ("query", "add"):
        return intent, None, "deepseek"
    return _classify_fallback(text), None, "rule"


def _classify_fallback(text: str) -> str:
    """AI 不可用时的规则兜底。歧义句一律判 query——查询漏判无害，写入误判污染数据。"""
    t = text.strip()
    if t.lower() in _HELP_WORDS:
        return "query"
    if _TODO_WORDS.match(t):
        return "query"
    if _BARE_DAY.match(t):
        return "query"
    if _QUERY_OBJ.match(t):
        return "query"
    if _QUERY_HINT.search(t):
        return "query"
    return "add"


def _resolve_day(arg: str, today: date):
    """把'明天/周三/10月1日/2026-10-01'解析成日期；认不出返回 None。"""
    arg = (arg or "").strip()
    if not arg:
        return today
    m = _ISO_DAY.match(arg)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 复用自然语言解析器的日期识别（private，但同仓复用优于重复实现）
    d, _frags = nparser._parse_date(arg, today)
    return d


def _fmt_day(d: date) -> str:
    info = logic.day_items(d)
    t, wk = logic.week_number(d)
    head = f"📅 {d.month}/{d.day} {logic.WD[d.weekday()]}"
    if t:
        head += f" · {t['name']}第{wk}周"
    if info["override"] is not None:
        head += f" · 调休按{logic.WD[info['override']]}课表"
    lines = [head]
    if info["holiday"]:
        lines.append("🎉 " + "、".join(info["holiday"]))
    for b in info["banners"]:
        lines.append(f"【{b['label']}】{b['title']}")
    items = info["items"]
    if items:
        lines.append("— 日程 —")
        for it in items:
            tm = it["start"] or "全天"
            if it["end"]:
                tm += f"–{it['end']}"
            loc = f" @{it['location']}" if it["location"] else ""
            mark = "（已停）" if it.get("cancelled") else ""
            lines.append(f"{tm} {it['title']}{loc}{mark}")
    else:
        lines.append("无课程与日程安排")
    todos = [x for x in logic.todos_snapshot() if not x["done"]]
    due_today = [x for x in todos if (x["due"] or "").startswith(d.isoformat())]
    overdue = [x for x in todos if x["due"] and x["due"][:10] < d.isoformat()]
    if due_today:
        lines.append(f"📋 待办（{d.month}/{d.day} 截止 {len(due_today)} 项）")
        for x in due_today[:4]:
            hhmm = (x["due"] or "")[11:16]
            lines.append(f"· {x['title']}" + (f"（{hhmm} 前）" if hhmm else ""))
        if len(due_today) > 4:
            lines.append(f"· ……还有 {len(due_today) - 4} 项")
    elif overdue:
        lines.append(f"📋 待办 {len(todos)} 项未完成（含过期 {len(overdue)} 项）")
    else:
        lines.append(f"📋 待办 {len(todos)} 项未完成，{d.month}/{d.day} 无截止")
    return "\n".join(lines)


def _fmt_week(base: date, label: str) -> str:
    """整周概览：周一~周日逐日一行（含事件与课），空日跳过。"""
    monday = base - timedelta(days=base.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    lines = [f"📅 {label}（{monday.month}/{monday.day}–{days[6].month}/{days[6].day}）"]
    for d in days:
        info = logic.day_items(d)
        wd = "周" + "一二三四五六日"[d.weekday()]
        head = f"{wd} {d.month}/{d.day}"
        if info["holiday"]:
            lines.append(f"{head}：🎉 {'、'.join(info['holiday'])}（停课）")
            continue
        if not info["items"]:
            lines.append(f"{head}：无安排")
            continue
        parts = []
        for it in info["items"]:
            tm = it["start"] or ""
            parts.append(f"{it['title']}({tm})" if tm else it["title"])
        lines.append(f"{head}：" + "；".join(parts))
    todos = [x for x in logic.todos_snapshot() if not x["done"]]
    week_due = [x for x in todos if x["due"] and monday.isoformat() <= x["due"][:10] <= days[6].isoformat()]
    if week_due:
        names = "、".join(x["title"] for x in week_due[:4])
        more = f" 等 {len(week_due)} 项" if len(week_due) > 4 else ""
        lines.append(f"📋 本周截止待办：{names}{more}")
    return "\n".join(lines)


_SEARCH_FILLER = re.compile(
    r"(啥时候|什么时候|是什么时候|是时候|是哪几天|哪几天|哪一天|哪天|哪一日|是几号|几号|的课|课程|课表|日程|安排|举行|举办|"
    r"工作台|叮咛)|[吗呢了呀的吧啊是开到]+$")
_TRAIL_CHARS = "的了呀呢吗吧啊开到"


def _search_events(text: str, today: date) -> str | None:
    """按名称搜日程/事件：剥掉疑问词，剩余作为关键词 LIKE 匹配。找不到返回 None。"""
    kw = _SEARCH_FILLER.sub(" ", text)
    kw = re.sub(r"\s+", " ", kw).strip()
    kw = kw.strip(_TRAIL_CHARS).strip()
    if len(kw) < 2:
        return None
    ps = [kw] + [kw[i] + kw[j] for i in range(len(kw)) for j in range(i + 1, len(kw))]
    ps = list(dict.fromkeys(ps))
    conds = " OR ".join("title LIKE ?" for _ in ps)
    gargs = [f"%{p}%" for p in ps]
    import eventsearch
    r = eventsearch.find(kw, today, 3)
    rows = [{**x, "past": False} for x in r["results"]] + \
           [{**x, "past": True} for x in r["past"]]
    if not rows:
        return None
    lines = [f"🔍 找到 {len(rows)} 条“{kw}”相关："]
    for x in rows:
        d = date.fromisoformat(x["date"])
        wd = "周" + "一二三四五六日"[d.weekday()]
        lines.append(f"· {d.month}/{d.day}（{wd}）{x['title']}"
                     + ("（已过期）" if x["past"] else ""))
    return "\n".join(lines)


_TODO_DONE_HEAD = re.compile(r"^(?:完成了?|做好了?|搞定了?)[：:，,]?\s*(.+)$")
_TODO_DONE_TAIL = re.compile(r"^(.{2,}?)(?:完成了|做好了|搞定了)[。.!！]?$")


def _todo_done_reply(kw: str) -> str:
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, title FROM todos WHERE done=0 AND title LIKE ? "
            "ORDER BY due='' , due LIMIT 5", (f"%{kw}%",))]
        if len(rows) == 1:
            c.execute("UPDATE todos SET done=1 WHERE id=?", (rows[0]["id"],))
            return f"已完成：{rows[0]['title']}"
    if not rows:
        return f"没找到未完成的「{kw}」。"
    return "匹配到多条未完成：\n" + "\n".join(f"· {r['title']}" for r in rows) + "\n请说得更具体些。"


def _legacy_classify(text: str) -> tuple:
    """agent 关闭时的规则兜底：歧义句一律判 query（漏查无害，误加污染）。"""
    tt = text.strip()
    if _TODO_WORDS.match(tt):
        return "query", "todo"
    if _BARE_DAY.match(tt) or _QUERY_OBJ.match(tt) or _WEEK_OVERVIEW.search(tt):
        return "query", "schedule"
    if re.search(r"(帮我?加|加一?个?|添加|新建|建一?个?|记一下|记个|设个?)", tt):
        return "add", None                  # 写入动词优先于查询词（「帮我加个日程」是加不是查）
    if _QUERY_HINT.search(tt):
        return "query", None
    return "add", None


def _fmt_todos() -> str:
    from datetime import datetime
    todos = [t for t in logic.todos_snapshot() if not t["done"]]
    if not todos:
        return "📋 没有未完成的待办 "
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"📋 待办 {len(todos)} 项未完成"]
    for t in todos[:30]:
        due = t["due"]
        if not due:
            tail = "无截止"
        elif due < now:
            tail = f"已逾期 {due[5:16]}"
        else:
            tail = f"截止 {due[5:16]}"
        mark = "❗" if (t["priority"] or 0) >= 2 else ""
        lines.append(f"{mark}· {t['title']}（{tail}）")
    if len(todos) > 30:
        lines.append(f"…另有 {len(todos) - 30} 项")
    return "\n".join(lines)


def _add(text: str) -> str:
    """走 /api/quick_add 同一条链路记待办/日程，返回给用户的确认语。"""
    from fastapi import HTTPException
    from models import QuickAdd
    from routes.todos import api_quick_add

    try:
        r = api_quick_add(QuickAdd(text=text, use_ai=True))
    except HTTPException as e:
        if e.status_code == 409:
            return "已有一条完全相同的日程，未重复添加。"
        return f"添加失败：{e.detail}"
    except Exception as e:
        return f"添加失败：{repr(e)[:80]}"
    item = r["item"]
    if r["type"] == "todo":
        return f"已记待办：{item['title']}\n截止 {item['due'] or '无'}"
    tm = item["start_time"] or "全天"
    if item["end_time"]:
        tm += f"–{item['end_time']}"
    loc = f" @{item['location']}" if item["location"] else ""
    return f"已记日程：{item['title']}\n{item['date']} {tm}{loc}"


def handle(text: str, today: date = None, source: str = "wechat") -> str:
    try:
        reply = _handle(text, today, source)
    except Exception as e:
        log.warning("指令处理异常：%r → %s", (text or "")[:40], repr(e)[:120])
        reply = f"处理出错了：{repr(e)[:60]}\n请稍后再试，或发「帮助」。"
    log.info("指令[%s] %r → %s", source, (text or "")[:50], reply[:60].replace("\n", " / "))
    return reply


def _handle(text: str, today: date = None, source: str = "wechat") -> str:
    """把一条用户消息处理成回复文本（不抛异常）。source 用于批次溯源。"""
    today = today or date.today()
    t = (text or "").strip()
    if not t:
        return "空消息，发「帮助」查看可用指令。"
    if t.lower() in _HELP_WORDS:
        return HELP
    if _UNDO_WORDS.match(t):
        import agent
        return agent.undo_last()
    if _TODO_WORDS.match(t):
        return _fmt_todos()

    # 0) 精确能力指令（无歧义模式，且智能体没有对应工具）+ 元指令，零 LLM
    m_done_h, m_done_t = _TODO_DONE_HEAD.match(t), _TODO_DONE_TAIL.match(t)
    if m_done_h:
        return _todo_done_reply(m_done_h.group(1))
    if m_done_t:
        return _todo_done_reply(m_done_t.group(1))

    # 1) 其余一切（所有查询 + 所有写入）统一进智能体，不做意图猜测。
    #    智能体不可用/异常时才降级到规则路径（保可用性，降级记日志）。
    import agent
    if agent.enabled():
        try:
            return agent.handle(t, today, source=source,
                                mode=agent.classify_mode(t) or "general")
        except Exception as e:  # 网络/凭据/模型侧故障
            log.warning("智能体异常，降级规则路径：%r → %s", t[:50], e)

    # 2) 降级/关闭兜底：旧确定性链路（规则查询 + quick_add）
    return _rule_fallback(t, today, source)


def _rule_fallback(t: str, today, source: str) -> str:
    """智能体不可用时的确定性兜底。顺序：无歧义形态 → 意图分类 → quick_add。"""
    mt = _DAY_TAIL.match(t)                       # "明日课程"类，无歧义
    if mt:
        d = _resolve_day(mt.group(1), today)
        if d is not None:
            return _fmt_day(d)
    if _WEEK_OVERVIEW.search(t):
        base = (today + timedelta(days=7) if "下周" in t else today)
        return _fmt_week(base, "下周" if "下周" in t else "本周")
    if re.search(r"(啥时候|什么时候|哪一天|哪天|哪几|几号)", t):   # 明确时间疑问 → 搜索
        hit = _search_events(t, today)
        if hit:
            return hit
        return f"没找到“{t}”相关的日程。"
    li, lk = _legacy_classify(t)
    if li != "query":
        return _add(t)                            # 添加 → quick_add（含日期词也不劫持）
    if lk == "todo":
        return _fmt_todos()
    m = _QUERY_OBJ.match(t)
    arg = m.group(1) if m else t
    d = _resolve_day(arg, today)
    if d is not None:
        return _fmt_day(d)
    hit = _search_events(arg or t, today)
    if hit:
        return hit
    return f"没找到“{arg or t}”相关的日程。"