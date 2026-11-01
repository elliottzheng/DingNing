# -*- coding: utf-8 -*-
"""Excel 导入：任教课表 + 学年教学安排（校历）→ SQLite。"""
import hashlib
import json
import re
from datetime import date, datetime, timedelta

import openpyxl

import db

CN_MONTH = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
            "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
WEEKDAY_HEADER = {"星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3,
                  "星期五": 4, "星期六": 5, "星期日": 6, "周日": 6}

TIME_SPLIT = re.compile(r"[—–\-~～]")


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def already_imported(kind: str, fh: str) -> bool:
    with db.conn() as c:
        row = c.execute("SELECT 1 FROM imports WHERE kind=? AND file_hash=?", (kind, fh)).fetchone()
        return row is not None


def mark_imported(kind: str, filename: str, fh: str):
    with db.conn() as c:
        c.execute("INSERT INTO imports(kind, filename, file_hash, imported_at) VALUES(?,?,?,?)",
                  (kind, filename, fh, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


# ---------------------------------------------------------------- 课表

def _load_grid(path: str) -> list:
    """把 xlsx/xls 首个 sheet 读成 0 起始的二维字符串网格（已去首尾空白）。"""
    if str(path).lower().endswith(".xls"):
        import xlrd
        ws = xlrd.open_workbook(path).sheet_by_index(0)
        return [[str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
                for r in range(ws.nrows)]
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    return [[str(ws.cell(r, c).value or "").strip() for c in range(1, ws.max_column + 1)]
            for r in range(1, ws.max_row + 1)]


def _cell_int(text: str):
    """从格子文本里提取整数（'17.0'/'17班'/'初一二3班'→数字），无则 None。"""
    t = str(text).strip()
    if not t:
        return None
    try:
        return int(float(t))
    except ValueError:
        m = re.search(r"\d+", t)
        return int(m.group()) if m else None


def _parse_time_range(text: str):
    text = str(text).strip()
    m = TIME_SPLIT.split(text)
    if len(m) != 2:
        return None, None
    def norm(t):
        t = t.strip().replace("：", ":")
        parts = t.split(":")
        if len(parts) == 2:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        return ""
    return norm(m[0]), norm(m[1])


def _detect_format(grid: list) -> str:
    """课表格式识别：'personal'（行=节次、列=星期、格=班级）或 'grade'（行=班级、
    列=星期×节次、格=科目）。grade 的特征：存在一行含 ≥3 个星期表头，但该行
    （及其附近）没有『节次』列。"""
    for r in range(min(len(grid), 15)):
        weekdays = sum(1 for c in grid[r] if c in WEEKDAY_HEADER)
        if weekdays >= 3:
            has_period_col = any("节次" in cell for cell in grid[r])
            if not has_period_col:
                return "grade", r
            return "personal", r
    return "personal", -1


def _import_grade(grid: list, wr: int, default_location: str, effective_from: str) -> dict:
    """年级大课表导入：行=班级，列=星期×节次，格=科目。

    只导入「我的科目」（config my_subjects）命中、且（config my_classes 非空时）
    属于「我教的班」的格子。科目显示名统一为 lesson_subject。
    """
    my_subjects = {x.strip() for x in json.loads(db.get_config("my_subjects", "[]")) if x.strip()}
    my_classes = {int(x) for x in json.loads(db.get_config("my_classes", "[]"))}
    display = db.get_config("lesson_subject", "").strip()
    if not my_subjects:
        raise ValueError("请先在设置里填写『我的科目』——你的课在课表格子里的叫法（如语文老师填 [\"语文\", \"语\"]）")

    days = {}
    for c, cell in enumerate(grid[wr]):
        if cell in WEEKDAY_HEADER:
            days[c] = WEEKDAY_HEADER[cell]
    starts = sorted(days)
    spans = []
    for i, c0 in enumerate(starts):
        c_end = (starts[i + 1] - 1) if i + 1 < len(starts) else len(grid[0]) - 1
        spans.append((days[c0], c0, c_end))

    # 表头下方：半天行（上午/下午）与节次行（数字）
    hr = next((r for r in range(wr + 1, min(wr + 4, len(grid)))
               if any("上午" in grid[r][c] or "下午" in grid[r][c]
                      for _, c0, c1 in spans for c in range(c0, min(c1 + 1, len(grid[r]))))), wr + 1)
    pr = hr + 1

    # 半天/节次在真实课表里是合并单元格：标签只在块首列，必须按天前向填充
    sec_by_col = {}
    for wd, c0, c1 in spans:
        cur_half, cur_n = "", None
        for c in range(c0, min(c1 + 1, len(grid[wr]))):
            if hr < len(grid) and c < len(grid[hr]) and grid[hr][c]:
                h = grid[hr][c]
                cur_half = "上午" if "上午" in h else ("下午" if "下午" in h else cur_half)
            if pr < len(grid) and c < len(grid[pr]) and grid[pr][c]:
                n = _cell_int(grid[pr][c])
                if n is not None:
                    cur_n = n
            if cur_half and cur_n is not None:
                sec_by_col[c] = f"{cur_half}{cur_n}"

    # 班级列：数据区前两列中数字最多的列
    data_rows = list(range(pr + 1, len(grid)))
    cls_col, best = 0, -1
    for cand in (0, 1):
        cnt = sum(1 for r in data_rows if _cell_int(grid[r][cand]) is not None)
        if cnt > best:
            cls_col, best = cand, cnt

    rows, seen_classes = [], set()
    for r in data_rows:
        no = _cell_int(grid[r][cls_col]) if cls_col < len(grid[r]) else None
        if no is None:
            continue
        seen_classes.add(no)
        if my_classes and no not in my_classes:
            continue
        try:
            ltimes = json.loads(db.get_config("lesson_times", "{}") or "{}") or {}
        except Exception:
            ltimes = {}
        for wd, c0, c1 in spans:
            for c in range(c0, min(c1 + 1, len(grid[r]))):
                subj = grid[r][c]
                if subj and subj in my_subjects and c in sec_by_col:
                    sec = sec_by_col[c]
                    t0, t1 = (ltimes.get(sec) or ["", ""])[:2]
                    rows.append((wd, sec, t0, t1, f"{no}班",
                                 display or _canon_subject(subj), default_location))

    eff = effective_from or date.today().isoformat()
    with db.conn() as c:
        c.execute("DELETE FROM lessons WHERE effective_from=?", (eff,))
        c.executemany(
            "INSERT INTO lessons(weekday, section, start_time, end_time, class_name, subject, location, effective_from) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(*r, eff) for r in rows])
    imported = sorted({int(str(r[4]).replace("班", "")) for r in rows})
    skipped = sorted(seen_classes - set(imported))
    return {"lessons": len(rows), "effective_from": eff,
            "classes": imported, "skipped_classes": skipped,
            "format": "grade"}


def _canon_subject(subj: str) -> str:
    """科目别名归一（多科目兼容）。清单顺序即分组：「科目名在前、别名随后」。

    例：清单 [语文, 语, 文, 数学, 数] → 文/语 归 语文，数 归 数学；
    ZYL 清单 [信息, 信息技术, …] → 信息 本身保持不变（首位即主名）。
    无归属的文本保留原文（如「班会」）。
    """
    if not subj:
        return subj
    try:
        groups = json.loads(db.get_config("subject_groups", "[]") or "[]")
    except Exception:
        groups = []
    if groups:                                          # 结构化科目组：主名 + 别名精确/包含归一
        for g in groups:
            name = (g.get("name") or "").strip()
            if not name:
                continue
            if subj == name:
                return name
            if subj in [a.strip() for a in g.get("aliases", []) if a.strip()]:
                return name
        for g in groups:
            name = (g.get("name") or "").strip()
            if name and subj in name:
                return name                              # 子串兜底（文 ⊂ 语文 已被别名覆盖时的补充）
        return subj
    try:
        mine = [x.strip() for x in json.loads(db.get_config("my_subjects", "[]")) if x.strip()]
    except Exception:
        return subj
    if not mine:
        return subj
    try:
        idx = mine.index(subj)
    except ValueError:
        idx = None
    ahead = mine[:idx] if idx is not None else mine       # 不在清单 → 全清单视为候选主名
    for m in reversed(ahead):
        if subj in m and len(m) > len(subj):
            return m                                       # 别名 → 最近的主名
    return subj


def import_schedule(path: str, default_location: str = "", effective_from: str = None) -> dict:
    """解析任教课表并入库。支持两种格式（自动识别）：
    1. personal：个人课表（行=节次、列=星期，单元格=班级名）
    2. grade：年级大课表（行=班级、列=星期×节次，单元格=科目缩写）——
       按 config my_subjects / my_classes 过滤「我的课」。"""
    grid = _load_grid(path)
    fmt, wr = _detect_format(grid)
    if fmt == "grade":
        return _import_grade(grid, wr, default_location, effective_from)

    header_row, col_map, sec_col, time_col = None, {}, None, None
    for r in range(min(len(grid), 15)):
        cm, tc, tm = {}, None, None
        for cidx, v in enumerate(grid[r]):
            if v in WEEKDAY_HEADER:
                cm[WEEKDAY_HEADER[v]] = cidx
            elif v == "节次":
                tc = cidx
            elif v in ("时间", "作息"):
                tm = cidx
        if cm and tc is not None:
            header_row, col_map, sec_col = r, cm, tc
            time_col = tm if tm is not None else tc + 1
            break
    if not col_map or sec_col is None:
        raise ValueError("未找到课表表头（需包含『节次』和『星期一~五』）")

    rows, lesson_times = [], {}
    for r in range(header_row + 1, len(grid)):
        section = grid[r][sec_col] if sec_col < len(grid[r]) else ""
        section = section.strip()
        if not section or section.startswith(("注", "备注", "说明")):
            continue
        t_start, t_end = "", ""
        if time_col and time_col < len(grid[r]):
            t_start, t_end = _parse_time_range(grid[r][time_col])
        for weekday, cidx in sorted(col_map.items()):
            cell = grid[r][cidx] if cidx < len(grid[r]) else ""
            if not cell:
                continue
            name = cell
            if not name or name in {"—", "－", "-", "无", "休息"}:
                continue
            name = re.sub(r"\s+", " ", name)
            subj_disp = db.get_config("lesson_subject", "") or _canon_subject(name)
            rows.append((weekday, section, t_start, t_end, name,
                         subj_disp, default_location))
        if t_start and t_end:
            lesson_times[section] = [t_start, t_end]

    eff = effective_from or date.today().isoformat()
    with db.conn() as c:
        # 版本化课表：同一生效日期重导=幂等覆盖；其他历史版本保留不动
        c.execute("DELETE FROM lessons WHERE effective_from=?", (eff,))
        c.executemany(
            "INSERT INTO lessons(weekday, section, start_time, end_time, class_name, subject, location, effective_from) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [( *r, eff) for r in rows])
    if lesson_times:
        db.set_config("lesson_times", json.dumps(lesson_times, ensure_ascii=False))
    return {"lessons": len(rows), "sections": lesson_times, "effective_from": eff}


# ---------------------------------------------------------------- 明文课表

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}
_CN_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WD_TOKEN = re.compile(r"^(?:周|星期|礼拜)([一二三四五六日天])$")
_SEC_TOKEN = re.compile(r"^(上午|下午|晚上)([1-9一二三四五六七八九])$")


def _norm_section(half: str, n: int) -> str:
    return f"{half}{n}"


def parse_schedule_text(text: str, known_sections: list | None = None):
    """明文课表解析：一行=一节课，字段顺序随意。

    返回 (rows, warnings, errors)。
    rows 元素 = (weekday, section, start, end, class_name, subject, location)。
    errors 元素 = "第N行: 原因"。
    known_sections：作息表里已有的节次；不在其中的节次照样导入，但产生警告
    （该节没有时间显示，可在设置页补作息）。
    """
    std = ({"上午1", "上午2", "上午3", "上午4", "下午1", "下午2", "下午3", "下午4",
            "晚上1", "晚上2", "晚上3"} if not known_sections else {x for x in known_sections if x})
    rows, warnings, errors = [], [], []
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = (line.replace("\u3000", " ").replace("，", " ").replace("、", " ")
                    .replace("；", " ").replace(";", " ").replace("\t", " "))
        wd, sec, subj, loc, classes = None, None, None, None, []
        for tok in re.split(r"[\s]+", line):
            t = tok.strip()
            if not t:
                continue
            if t.startswith("#"):
                break
            m = _WD_TOKEN.match(t)
            if m and wd is None:
                wd = _CN_WD.get(m.group(1))
                continue
            m = _SEC_TOKEN.match(t)
            if m and sec is None:
                half, n = m.group(1), _CN_NUM.get(m.group(2)) or int(m.group(2))
                sec = _norm_section(half, n)
                continue
            if t.startswith("@") and loc is None:
                loc = t[1:].strip()
                continue
            if t.endswith("课") and len(t) > 2 and subj is None:
                subj = t[:-1]
                continue
            if "班" in t:
                classes.append(t)
                continue
            # 裸科目别名（在我的科目里，如 文/语/数）→ 视为科目，不必带「课」
            if subj is None:
                try:
                    mine = [x.strip() for x in json.loads(db.get_config("my_subjects", "[]")) if x.strip()]
                except Exception:
                    mine = []
                if t in mine:
                    subj = _canon_subject(t) or t
                    continue
            errors.append(f"第{lineno}行: 无法识别「{t}」")
        if wd is None and sec is None and not classes:
            continue                                     # 整行都认不出 → 已有错误记录
        if wd is None:
            errors.append(f"第{lineno}行: 缺星期（如 周一/星期三）")
        if sec is None:
            errors.append(f"第{lineno}行: 缺节次（如 上午2/下午1）")
        if not classes:
            errors.append(f"第{lineno}行: 缺班级（如 22班，可写多个）")
        if errors and errors[-1].startswith(f"第{lineno}行"):
            continue
        if sec not in std:
            warnings.append(f"节次「{sec}」不在作息时间表里，该节不显示时间"
                            "（可在设置页「作息时间表」添加）")
        for cls in classes:
            key = (wd, sec, cls)
            if key in seen:
                warnings.append(f"第{lineno}行: {cls} {sec} 重复，已忽略")
                continue
            seen.add(key)
            t0, t1 = "", ""
            rows.append((wd, sec, t0, t1, cls, subj or "", loc or ""))
    return rows, warnings, errors


def import_schedule_text(text: str, default_location: str = "",
                         effective_from: str = None, replace: bool = True) -> dict:
    """明文课表导入：有错全不导。

    replace=True  覆盖 effective_from 版本（整份重导）。
    replace=False 追加：保留该版本已有课，仅新增/更新与本批同班同节的课
                  ——适合补加一节常驻课或第二个年级的课表。"""
    times = {}
    try: times = json.loads(db.get_config("lesson_times", "{}") or "{}") or {}
    except Exception: times = {}
    valid = list(times)
    rows, warnings, errors = parse_schedule_text(text, valid)
    rows = [(wd, sec, *(times.get(sec) or ["", ""]), cls, _canon_subject(subj), loc)
            for (wd, sec, _, _, cls, subj, loc) in rows]
    if errors:
        raise ValueError("\n".join(errors))
    eff = effective_from or date.today().isoformat()
    with db.conn() as c:
        if replace:
            c.execute("DELETE FROM lessons WHERE effective_from=?", (eff,))
        else:
            # 追加：同班同节次的旧课让位给新课（局部更新，不是整份清空）
            keys = [(r[0], r[1], r[4]) for r in rows]
            for wd, sec, cls in keys:
                c.execute("DELETE FROM lessons WHERE effective_from=? AND weekday=? "
                          "AND section=? AND class_name=?", (eff, wd, sec, cls))
        c.executemany(
            "INSERT INTO lessons(weekday, section, start_time, end_time, class_name, subject, location, effective_from) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(*r, eff) for r in rows])
    return {"lessons": len(rows), "effective_from": eff, "warnings": warnings,
            "format": "text"}


# ---------------------------------------------------------------- 校历（教学安排）

DATE_CELL = re.compile(r"^\s*([一二三四五六七八九十]{1,2}月)?\s*(\d{1,2})\s*[日]?\s*(.*)$")
WEEK_CELL = re.compile(r"第\s*(\d+)\s*周")

# events.kind 合法值（SystemOne 返回值必须落在这里，防脏值入库）
_KINDS = ("holiday", "festival", "exam", "event", "week_note")


def _classify(title: str) -> str:
    if "假" in title and "补班" not in title and "放假通知" not in title:
        return "holiday"
    if any(k in title for k in ("考试", "月考", "期中", "期末", "质检", "练习", "模拟", "一模", "二模", "中考", "小中考")):
        return "exam"
    return "event"


# 校历事项分类的提问与选项。要点：把"补班/调休上班"明确写进 event 的正面描述，
# 而不是藏在 holiday 的排除项里——"国庆补班"四个字三个像假期，排除写在括号里
# 模型会忽略（实测选 holiday），写成 event 的典型例子才稳。
_CALENDAR_QUESTION = "这条学校教学安排事项，本质上是放假、考试，还是师生要到校的活动/上班？"
_CALENDAR_OPTIONS = {
    "holiday": "师生不用到校：放假、假期、寒暑假、停课、休息。典型如'国庆假''秋假''停课'。",
    "exam": "组织考试或测评：期中/期末/月考/质检/模拟考/体育中考/实验室操作考试。",
    "event": "师生要到校办事或参加活动：补班、调休上班、开学/毕业典礼、会议、检查、"
             "家长会、军训、研学、比赛、演练。典型如'国庆补班''10月10日补班''初一初三熟悉考场'。",
}


def _classify_ai(titles: list) -> dict:
    """用 SystemOne 逐条判定校历事项类型。返回 {title: kind}，失败/未配置返回 {}。

    只在规则判为 'event'（关键词没认出来）的标题上调用，避免无谓计费；
    调用方对缺键的标题继续用规则结果。
    """
    unknown = [t for t in dict.fromkeys(titles) if t]
    if not unknown:
        return {}
    try:
        import systemone
        if not systemone.enabled():
            return {}
    except Exception:
        return {}
    out = {}
    for t in unknown:
        try:
            k = systemone.choice(_CALENDAR_QUESTION, _CALENDAR_OPTIONS, t,
                                 min_confidence=0.25)
            if k in _KINDS:
                out[t] = k
        except Exception:
            break
    return out


def classify_titles(titles: list) -> dict:
    """规则 + SystemOne 混合分类。返回 {title: kind}（规则优先，AI 只补规则没认出的）。"""
    out = {}
    for t in dict.fromkeys(titles):
        if t:
            out[t] = _classify(t)
    need_ai = [t for t, k in out.items() if k == "event"]
    if need_ai:
        out.update({t: k for t, k in _classify_ai(need_ai).items() if k in _KINDS})
    return out


def parse_arrangement_sheet(ws, sheet_name: str):
    """结构化遍历一个学期 sheet：奇数行=日期行，偶数行=事项行，事项取同列上一日期。

    返回 (events, term_start, term_end)；events 元素为 (date, title, kind)。
    """
    years = [int(y) for y in re.findall(r"(20\d{2})", str(ws["A1"].value or ""))]
    if "第二学期" in sheet_name or "下学期" in sheet_name:
        year = years[-1] if years else 2027   # 2026-2027学年第二学期落在 2027 年
        month = 2
    else:
        year = years[0] if years else 2026
        month = 8                             # 第一学期第 1 周始于 8 月底

    events, notes = [], {}
    pending = []           # (date, title) 待判类型的校历事项
    cur_dates = {}        # 列号 -> 该列当前生效日期
    week_first = {}       # 周次 -> 该周最先出现的日期
    last_day = None
    all_dates = []

    for r in range(1, ws.max_row + 1):
        week_no = None
        a = ws.cell(r, 1).value
        if a:
            m = WEEK_CELL.search(str(a))
            if m:
                week_no = int(m.group(1))
        if week_no is not None and r % 2 == 1:
            iv = ws.cell(r, 9).value
            if iv is not None and str(iv).strip() and not str(iv).strip().isdigit():
                notes[week_no] = str(iv).strip()

        for cidx in range(2, 9):  # B..H = 周一..周日
            v = ws.cell(r, cidx).value
            if v is None or not str(v).strip():
                continue
            v = str(v).strip()

            if week_no is not None:
                m = DATE_CELL.match(v)
                if m:
                    mon_txt, day_txt, suffix = m.group(1), int(m.group(2)), (m.group(3) or "").strip()
                    if mon_txt:
                        new_month = CN_MONTH[mon_txt[:-1]]
                        if last_day is not None and (new_month, day_txt) < (month, last_day):
                            year += 1          # 显式月份回退（十二月→一月）即跨年
                        month = new_month
                    elif last_day is not None and day_txt < last_day:
                        month += 1             # 日期回落即进入下一个月
                        if month > 12:
                            month, year = 1, year + 1
                    last_day = day_txt
                    d = date(year, month, day_txt)
                    cur_dates[cidx] = d
                    all_dates.append(d)
                    week_first.setdefault(week_no, d)
                    if suffix:
                        events.append((d, f"节日·{suffix}", "festival"))
                    continue

            d = cur_dates.get(cidx)
            if d is None:
                continue
            title = v.replace("\n", "；").strip().strip("；;,， ")
            if title:
                pending.append((d, title))

    # 类型判定：规则先判，规则没认出的（判为 event 的）再问 SystemOne
    ai_kinds = classify_titles([t for _d, t in pending])
    for d, title in pending:
        events.append((d, title, ai_kinds.get(title, "event")))

    for week_no, txt in notes.items():
        d = week_first.get(week_no)
        if d:
            events.append((d, f"[本周] {txt}", "week_note"))

    return events, (min(all_dates) if all_dates else None), (max(all_dates) if all_dates else None)


def import_arrangement(path: str) -> dict:
    """解析学年教学安排（校历），生成事件 + 学期锚点。替换 calendar 来源事件。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    all_events, terms = [], []
    for ws in wb.worksheets:
        events, start, end = parse_arrangement_sheet(ws, ws.title)
        if not start:
            continue
        for d, title, kind in events:
            susp = 1 if (_SUSPEND_RE.search(title) and not _SUSPEND_NEG_RE.search(title)
                         and not _SUSPEND_HALF_RE.match(title)) else 0
            all_events.append((d.isoformat(), "", "", title, "", "", "calendar", kind, susp))
        terms.append({"name": ws.title, "start": start.isoformat(), "end": end.isoformat()})

    terms.sort(key=lambda t: t["start"])
    with db.conn() as c:
        c.execute("DELETE FROM events WHERE source='calendar'")
        c.executemany(
            "INSERT INTO events(date, start_time, end_time, title, location, note, source, kind, suspends) "
            "VALUES(?,?,?,?,?,?,?,?,?)", all_events)
    db.set_config("terms", json.dumps(terms, ensure_ascii=False))
    return {"events": len(all_events), "terms": terms}


# ---------------------------------------------------------------- 明文校历

_CAL_RANGE = re.compile(r"^([\d/月日~～\-至]+)$")
_CAL_TIME = re.compile(r"^(\d{1,2}:\d{2})[-–~～](\d{1,2}:\d{2})$")
_CAL_KIND = {"假期": "holiday", "节日": "festival", "考试": "exam",
             "活动": "event", "备注": "week_note"}
_SUSPEND_RE = re.compile("运会|期中|期末|月考")     # 校运会/运动会、大考默认当日停课
_SUSPEND_NEG_RE = re.compile(
    "体育|动员|表彰|家长会|模拟|练习|适应性|温书|报名|志愿|公布|编班|大扫除|培训|熟悉考场|抽查|分析会")
_SUSPEND_HALF_RE = re.compile("^(上午|下午)")       # 标题带上/下午前缀 = 半日，不整日停课


def _cal_date(token: str, this_year: int):
    """'2026-10-01' / '10/1' / '10月1日' / '9月30' → date。无法解析返回 None。"""
    t = token.strip().replace("日", "").replace("号", "")
    m = re.match(r"^(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})$", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", t)
    if m:
        mo, dd = int(m.group(1)), int(m.group(2))
        year = this_year if mo >= 8 else this_year + 1     # 学年跨年：8月起为当年，1~7月为次年
        return date(year, mo, dd)
    m = re.match(r"^([一二三四五六七八九十]{1,2})月(\d{1,2})$", t)
    if m:
        mo = CN_MONTH.get(m.group(1))
        dd = int(m.group(2))
        year = this_year if mo >= 8 else this_year + 1
        return date(year, mo, dd)
    return None


def parse_calendar_text(text: str, this_year: int = None):
    """明文校历解析：一行=一件事。

    格式：日期[~结束日期] [HH:MM-HH:MM] 事项名称
    类型按名称自动判定（含"假"→假期、"考/试"→考试，其余活动），
    也可在行尾显式标注：[假期] [节日] [考试] [活动] [备注]。

    行尾可加 [停课] 标注（如「11/5-11/6 校运会」含"运动会"自动视为停课）。

    返回 (events, warnings, errors)。
    events 元素 = (date_iso, start, end, title, kind, suspends)。
    """
    from datetime import date as _date, timedelta
    this_year = this_year or _date.today().year
    events, warnings, errors = [], [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("\u3000", " ").replace("：", ":")
        toks = [t for t in re.split(r"[\s]+", line) if t]
        if not toks:
            continue
        first = toks[0]
        parts = re.split(r"[~～\-至]", first, maxsplit=1)
        d0 = d1 = None
        if len(parts) == 2 and ":" not in first:
            d0 = _cal_date(parts[0], this_year)             # 区间首（如 9/28-9/30）
            d1 = _cal_date(parts[1], this_year)             # 区间尾
            if d0 is None or d1 is None:
                errors.append(f"第{lineno}行: 无法识别日期区间「{first}」"
                              "（支持 10/1-10/7、10月1日至10月7日）")
                continue
        else:
            d0 = _cal_date(first, this_year)
            if d0 is None:
                errors.append(f"第{lineno}行: 无法识别日期「{first}」"
                              "（支持 10/1、10月1日、2026-10-01、区间 10/1-10/7）")
                continue
        toks = toks[1:]
        t0 = t1 = ""
        time_idx = next((i for i, t in enumerate(toks) if _CAL_TIME.match(t)), None)
        if time_idx is not None:
            m = _CAL_TIME.match(toks[time_idx])
            t0, t1 = m.group(1), m.group(2)
            toks = toks[:time_idx] + toks[time_idx + 1:]
        title = " ".join(toks).strip()
        suspends = 0
        if title.endswith("[停课]"):
            title = title[:-len("停课") - 2].strip()
            suspends = 1
        kind_override = None
        for tag, kind in _CAL_KIND.items():
            if title.endswith(f"[{tag}]"):
                title = title[:-len(tag) - 2].strip()
                kind_override = kind
                break
        if not title:
            errors.append(f"第{lineno}行: 缺事项名称")
            continue
        kind = kind_override or _classify(title)
        if (suspends == 0 and _SUSPEND_RE.search(title)   # 校运会/期中/期末/月考默认停课
                and not _SUSPEND_NEG_RE.search(title)
                and not _SUSPEND_HALF_RE.match(title)):   # 显式 [停课] 不受守卫影响
            suspends = 1
        span = (d1 - d0).days if d1 else 0
        if span < 0 or span > 60:
            errors.append(f"第{lineno}行: 日期区间不合法")
            continue
        for i in range(span + 1):
            dd = d0 + timedelta(days=i)
            events.append((dd.isoformat(), t0, t1, title, kind, suspends))
        if span:
            warnings.append(f"{title}：已按每天一条展开 {span + 1} 天")
    return events, warnings, errors


def import_calendar_text(text: str, this_year: int = None) -> dict:
    """明文校历导入：替换全部日历来源事件（与 Excel 校历导入同语义）。"""
    events, warnings, errors = parse_calendar_text(text, this_year)
    if errors:
        raise ValueError("\n".join(errors))
    with db.conn() as c:
        c.execute("DELETE FROM events WHERE source='calendar'")
        c.executemany(
            "INSERT INTO events(date, start_time, end_time, title, location, note, source, kind, suspends) "
            "VALUES(?,?,?,?,?,?, 'calendar', ?, ?)",
            [(d, t0, t1, title, "", "", kind, susp)
             for (d, t0, t1, title, kind, susp) in events])
    return {"events": len(events), "warnings": warnings, "format": "calendar-text"}


