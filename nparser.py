# -*- coding: utf-8 -*-
"""中文自然语言 → 待办/日程 的轻量规则解析器（离线，不依赖大模型）。

示例：
  「周三下午3点机房教研会议」   → 日程 日期=周三 15:00-15:45
  「明天下午2点到4点初二监考」  → 日程 14:00-16:00
  「周五前交成绩册」           → 待办 截止=周五 17:00
"""
import re
from datetime import date, datetime, timedelta

WEEK_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
           "日": 6, "天": 6, "末": 5}  # “周末”按周六处理

P_TODAY    = re.compile(r"(?:今天|今日|今晚天|今晚)")
P_TOMORROW = re.compile(r"(?:明天|明日)")
P_DAYAFTER = re.compile(r"(?:大后天|后天)")
P_NEXTWEEK = re.compile(r"(?:下(?:一)?(?:个)?(?:周|星期|礼拜)([一二三四五六日天])?)")
P_WEEKDAY  = re.compile(r"(?:(?:本|这)?(?:周|星期|礼拜)([一二三四五六日天末]))")
P_MD       = re.compile(r"(?:(\d{1,2})\s*[月\.\/\-]\s*(\d{1,2})\s*[日号]?)")

PERIOD = r"(?:凌晨|清晨|早上|早晨|上午|中午|午后|下午|傍晚|晚上|夜里|今晚)"
# 一个时间词：可选时段 + 时:分 / X点(半|一刻|三刻|MM分)?
P_TIME = re.compile(rf"({PERIOD})?(\d{{1,2}})(?:[:：点时](\d{{1,2}})?分?(?:钟)?(半|一刻|三刻)?)")
RANGE_SEP = re.compile(r"\s*(?:到|至|~|—|–|-)\s*")

LEAD_WORDS = re.compile(r"^(?:请|麻烦)?(?:帮我|帮我一下|给我)?"
                        r"(?:添加|新增|新建|记录|记一下|记个|加一下?|安排|创建|提醒我?|设个?|设置)"
                        r"(?:一个|一条|个)?(?:日程|待办|事项|任务|提醒)?(?:[:：,，\s])*")
TRAIL_WORDS = re.compile(r"(?:的提醒|的日程|的待办|的事项)$")
REMIND_MIN = re.compile(r"提前(?:约|大约)?(\d+)分钟(?:的)?提醒|提前(?:一|两|三|半)?天?的?提醒|(?:开始|截止)(?:时|前)提醒")
_CN_MIN = {"一": 1, "两": 2, "三": 3, "五": 5, "十": 10, "半": 0}
FILLER = re.compile(r"(?:^(?:关于|主题[为是]?|内容[为是]?)[:：]?|待办[:：]|日程[:：]|提醒[:：])")

DEADLINE_WORDS = re.compile(
    r"(?:截止|之前|以前|前交|前提交|前上交|前完成|前要|要交|上交|提交|交齐"
    r"|(?:[0-9一二三四五六七八九十]+[天日号]|周[一二三四五六日天末]|星期[一二三四五六日天末]|[0-9]{1,2}月[0-9]{1,2}[日号]?)前)")
# 强活动词：几乎必然是"到点发生的事"，规则可直接判 event
ACTIVITY_STRONG = re.compile(
    r"(?:会议|议会|大会|例会|研讨会|讲座|报告会|典礼|仪式|演练|彩排|运动会|体育节"
    r"|活动|竞赛|比赛|教研|开会|监考|值班|考试|上课|代课|调课|报到|家长会|班会"
    r"|早读|晨会|开学|毕业|休业|军训|实践|参观|访问|交流|座谈|答辩|演出|放映)")
# 弱活动词：既可能是日程也可能是待办（"约谈学生"可记待办，"听课"是日程），交给 AI 判
ACTIVITY_WORDS = re.compile(r"(?:家访|约谈|听课|评课|培训|拍照|排练|分析)")
URGENT_WORDS = re.compile(r"(?:紧急|重要|尽快)")
LOCATION_WORDS = re.compile(r"(?:在|于)([\u4e00-\u9fa5A-Za-z0-9]{2,10}?)(?=\s|$|，|,|。|；|，)")
LOCATION_TAIL = re.compile(r"(?:开会|上课|听课|考试|集合|活动|培训|报到|拍照)$")


def _norm_hour(h: int, period: str) -> int:
    if period in ("下午", "午后", "傍晚", "晚上", "夜里", "今晚"):
        return h + 12 if h < 12 else h
    if period in ("凌晨", "清晨"):
        return h if h < 12 else h - 12
    if period == "中午":
        return h if h >= 11 else h + 12
    if 1 <= h <= 5:          # 无时段标记的「3点」按下午理解
        return h + 12
    return h


def _norm_minute(m, special) -> int:
    if special == "半":
        return 30
    if special == "一刻":
        return 15
    if special == "三刻":
        return 45
    return int(m or 0)


def _hm(total_min: int) -> str:
    total_min %= 24 * 60
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _time_of(match) -> str:
    period, hh, mm, special = match.groups()
    return _hm(_norm_hour(int(hh), period or "") * 60 + _norm_minute(mm, special))


def _parse_date(text: str, today: date):
    """返回 (date|None, 应从标题中剔除的片段列表)。"""
    m = P_NEXTWEEK.search(text)
    if m:
        base = today + timedelta(days=7 - today.weekday())          # 下周一
        return base + timedelta(days=WEEK_CN.get(m.group(1) or "一", 0)), [m.group(0)]
    for pat, offset in ((P_TODAY, 0), (P_TOMORROW, 1), (P_DAYAFTER, None)):
        m = pat.search(text)
        if m:
            if offset is None:
                offset = 3 if "大后天" in m.group(0) else 2
            return today + timedelta(days=offset), [m.group(0)]
    m = P_WEEKDAY.search(text)
    if m:
        d = today + timedelta(days=(WEEK_CN[m.group(1)] - today.weekday()) % 7)
        return d, [m.group(0)]
    m = P_MD.search(text)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= dy <= 31:
            try:
                d = date(today.year, mo, dy)
                if d < today:
                    d = date(today.year + 1, mo, dy)
                return d, [m.group(0)]
            except ValueError:
                pass
    return None, []


def _parse_range(text: str):
    """「2点到4点」「14:00-15:30」「下午2点到4点」→ (start,end,片段) 或 None。"""
    m = RANGE_SEP.search(text)
    while m:
        left_tokens = list(P_TIME.finditer(text[:m.start()]))
        right = P_TIME.search(text[m.end():])
        if left_tokens and right and (m.start() - left_tokens[-1].end() <= 1):
            t1 = left_tokens[-1]
            start, end = _time_of(t1), _time_of(right)
            if end > start:
                # 片段 = 左时间词起点 → 右时间词在原文中的结束位置
                return start, end, text[t1.start(): m.end() + right.end()]
        m = RANGE_SEP.search(text, m.end())
    return None


def _ai_kind(text: str):
    """（已移除 SystemOne）歧义句由调用方的确定性启发式处理，此函数保留占位。"""
    return None


def parse(text: str, today: date = None) -> dict:
    """把一句话解析成结构化条目。"""
    today = today or date.today()
    raw = text.strip().replace("明晚", "明天晚上").replace("今晚", "今天晚上")
    body = LEAD_WORDS.sub("", FILLER.sub("", raw)).strip()
    body = TRAIL_WORDS.sub("", body).strip() or raw

    # 1) 时间段
    start_time = end_time = None
    consumed = []
    rng = _parse_range(body)
    if rng:
        start_time, end_time, frag = rng
        consumed.append(frag)
    else:
        m = P_TIME.search(body)
        if m:
            start_time = _time_of(m)
            consumed.append(m.group(0))
            total = int(start_time[:2]) * 60 + int(start_time[3:]) + 45   # 默认 45 分钟
            end_time = _hm(total)

    # 2) 日期
    d, frags = _parse_date(body, today)
    consumed += frags

    # 3) 地点
    location = ""
    lm = LOCATION_WORDS.search(body)
    if lm:
        location = LOCATION_TAIL.sub("", lm.group(1))

    # 4) 标题 = 原文剔除时间/日期片段与地点短语
    title = body
    for frag in consumed:
        title = title.replace(frag, " ")
    if location:
        title = title.replace(f"在{location}", " ").replace(f"于{location}", " ")
    title = re.sub(r"[：:]", " ", title)
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"^[，,。；;：:\s]+|[，,。；;：:\s]+$", "", title)
    title = re.sub(r"^(?:之前|以前|前)", "", title)          # 「周五前交…」→「交…」
    title = re.sub(r"^(?:本|这)\s*$", "", title).strip()     # 孤立的「本/这」
    title = title or raw

    # 5) 类型判定：有钟点时间必是日程；否则强关键词定夺，弱/无关键词问 SystemOne
    if start_time:
        kind = "event"
    elif DEADLINE_WORDS.search(body):
        kind = "todo"
    elif ACTIVITY_STRONG.search(body):
        kind = "event"
    else:
        # 无关键词或只命中弱活动词（约谈/听课/培训…）→ 口语歧义，问 SystemOne
        kind = "event" if ACTIVITY_WORDS.search(body) else "todo"   # 歧义句：活动词 → 日程，否则待办

    target_date = d or today
    due = ""
    if kind == "todo":
        if d:
            due = f"{d.isoformat()} {start_time or '17:00'}"
        elif start_time:
            due = f"{today.isoformat()} {start_time}"

    rm = None
    mrm = REMIND_MIN.search(body) or REMIND_MIN.search(raw)
    if mrm:
        d1 = mrm.group(1)
        rm = int(d1) if d1 and d1.isdigit() else None
    return {
        "type": kind,
        "title": title,
        "date": target_date.isoformat(),
        "start_time": start_time or "",
        "end_time": end_time or "",
        "location": location,
        "due": due,
        "remind_min": rm,
        "priority": 2 if URGENT_WORDS.search(body) else 1,
    }


if __name__ == "__main__":
    cases = [
        "周三下午3点机房教研会议",
        "明天下午2点到4点初二监考",
        "周五前交成绩册",
        "明天上午10点在高一机房听课",
        "下周一交校本课程教案",
        "10月15日下午4点半家长会",
        "今晚8点改期中试卷",
        "本周五之前提交社团名单",
        "紧急：明天早上9点教务处开会",
        "9月10日 14:00-15:30 初一家长课堂",
    ]
    for t in cases:
        print(t, "=>", parse(t, today=date(2026, 9, 5)))
