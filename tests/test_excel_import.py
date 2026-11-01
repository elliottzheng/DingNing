# -*- coding: utf-8 -*-
"""excel_import 测试：课表版本化 / 幂等 / 校历解析。"""
from datetime import date

import db
import excel_import
import openpyxl


def _make_schedule_xlsx(path):
    """构造 2 节课的最小课表：行=节次、列=星期、时间列（同真实格式）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "节次"
    ws["B1"] = "时间"
    ws["C1"] = "星期一"
    ws["D1"] = "星期二"
    ws["A2"] = "上午1"
    ws["B2"] = "08:00-08:45"
    ws["C2"] = "91班"
    ws["A3"] = "上午2"
    ws["B3"] = "09:00-09:45"
    ws["D3"] = "92班"
    wb.save(path)





def test_schedule_versioning_and_idempotency(tmp_path):
    """同日重导 = 覆盖同一版本；不同日重导 = 新版本，历史版本保留。"""
    xlsx = tmp_path / "课表.xlsx"
    _make_schedule_xlsx(str(xlsx))

    v1 = excel_import.import_schedule(str(xlsx), effective_from="2031-03-01")
    assert v1["effective_from"] == "2031-03-01"

    v2 = excel_import.import_schedule(str(xlsx), effective_from="2031-03-02")
    assert v2["effective_from"] == "2031-03-02"
    v3 = excel_import.import_schedule(str(xlsx), effective_from="2031-03-02")   # 同日重导

    with db.conn() as c:
        versions = [r[0] for r in c.execute(
            "SELECT DISTINCT effective_from FROM lessons WHERE effective_from LIKE '2031%'")]
    assert sorted(versions) == ["2031-03-01", "2031-03-02"]     # 两个版本都在

    # 查询 3/2 当天应取 3/2 版本；重导 3/2 不产生重复
    excel_import.import_schedule(str(xlsx), effective_from="2031-03-02")
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM lessons WHERE effective_from='2031-03-02'").fetchone()[0]
    assert n == 2

    # 清理测试版本
    with db.conn() as c:
        c.execute("DELETE FROM lessons WHERE effective_from LIKE '2031%'")


def test_schedule_parses_cells(client, tmp_path):
    """单元格解析：行=节次、列=星期 → weekday/class_name。"""
    xlsx = str(tmp_path / "s.xlsx")
    _make_schedule_xlsx(xlsx)
    excel_import.import_schedule(xlsx, effective_from="2031-04-01")
    with db.conn() as c:
        rows = {(r["weekday"], r["section"]): r["class_name"] for r in c.execute(
            "SELECT weekday, section, class_name FROM lessons WHERE effective_from='2031-04-01'")}
    assert rows[(0, "上午1")] == "91班"        # 周一
    assert rows[(1, "上午2")] == "92班"        # 周二
    with db.conn() as c:
        c.execute("DELETE FROM lessons WHERE effective_from='2031-04-01'")


def test_arrangement_loaded_at_startup(client):
    """conftest 已直接导入示例《教学安排》校历：两学期锚点 + 足量日历事件。"""
    terms = db.get_terms()
    assert [t["name"] for t in terms] == ["第一学期", "第二学期"]
    assert terms[0]["start"] == "2026-08-31"
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE source='calendar'").fetchone()[0]
    assert n >= 20

def test_grade_format_import(client, monkeypatch):
    """年级大课表（行=班级、列=星期×节次、格=科目）按「我的科目/我的班级」过滤导入。"""
    import db as dbmod
    dbmod.set_config("my_subjects", '["脑"]')
    dbmod.set_config("my_classes", '[22, 23, 25]')
    dbmod.set_config("lesson_subject", '信息')   # 显式映射（脑→信息）
    W = 30
    grid = [[""] * W for _ in range(9)]
    grid[2][2], grid[2][10], grid[2][18] = "星期一", "星期二", "星期三"
    for base in (2, 10, 18):
        grid[3][base] = "上午"
        grid[3][base + 4] = "下午"
        for k in range(4):
            grid[4][base + k] = str(k + 1)
            grid[4][base + 4 + k] = str(k + 1)

    def row_fill(pairs):
        row = [""] * W
        for (day, half, period), subj in pairs.items():
            row[{0: 2, 1: 10, 2: 18}[day] + (0 if half == "上午" else 4) + (period - 1)] = subj
        return row

    grid[5] = row_fill({(2, "上午", 3): "脑"}); grid[5][1] = "17"   # 17班（过滤）
    grid[6] = row_fill({(2, "下午", 2): "脑"}); grid[6][1] = "22"   # 22班 → 导入
    grid[7] = row_fill({(1, "上午", 2): "脑"}); grid[7][1] = "23"   # 23班 → 导入
    grid[8] = row_fill({(2, "上午", 3): "脑"}); grid[8][1] = "24"   # 24班（过滤）

    eff = "2031-06-01"
    try:
        r = excel_import._import_grade(grid, 2, "", eff)
        assert sorted(r["classes"]) == [22, 23]
        assert r["skipped_classes"] == [17, 24]
        with dbmod.conn() as c:
            rows = {(x["weekday"], x["section"]): x["class_name"] for x in c.execute(
                "SELECT weekday, section, class_name FROM lessons WHERE effective_from=?", (eff,))}
        assert rows == {(2, "下午2"): "22班", (1, "上午2"): "23班"}
        with dbmod.conn() as c:
            subs = {x["subject"] for x in c.execute(
                "SELECT subject FROM lessons WHERE effective_from=?", (eff,))}
        assert subs == {"信息"}                     # 科目统一映射为 lesson_subject
    finally:
        with dbmod.conn() as c:
            c.execute("DELETE FROM lessons WHERE effective_from=?", (eff,))
        dbmod.set_config("my_subjects", "")
        dbmod.set_config("my_classes", "")
        dbmod.set_config("lesson_subject", "")


# ---------------- 明文课表 ----------------

def test_parse_text_basic_variants():
    rows, warns, errs = excel_import.parse_schedule_text(
        "周一 上午1 17班\n星期二 下午2 21班 语文课\n周三 上午1 23班 24班 @机房\n"
        "礼拜四 上午二 25班 # 注释\n周五 下午一 26班")
    assert errs == []
    assert rows == [
        (0, "上午1", "", "", "17班", "", ""),
        (1, "下午2", "", "", "21班", "语文", ""),
        (2, "上午1", "", "", "23班", "", "机房"),
        (2, "上午1", "", "", "24班", "", "机房"),
        (3, "上午2", "", "", "25班", "", ""),
        (4, "下午1", "", "", "26班", "", ""),
    ]


def test_parse_text_field_order_free():
    rows, _, errs = excel_import.parse_schedule_text("22班 周一 上午2", ["上午1", "上午2", "上午3", "上午4", "下午1", "下午2", "下午3"])
    assert errs == [] and rows == [(0, "上午2", "", "", "22班", "", "")]


def test_parse_text_errors_have_lineno():
    text = ("周一 上午1 17班\n"
            "周二 上午9 18班\n"          # 节次不存在于标准集
            "周三 19班\n"                # 缺节次
            "周四 下午2\n"               # 缺班级
            "周五 下午1 26班 未知词")     # 无法识别
    rows, warns, errs = excel_import.parse_schedule_text(
        text, ["上午1", "上午2", "上午3", "上午4", "下午1", "下午2", "下午3"])
    assert len(errs) == 3 and all(e.startswith("第") for e in errs)   # 上午9 现为警告
    assert len(rows) == 2                 # 合法行仍被解析；全不导由导入层保证


def test_parse_text_dedupe_warns():
    rows, warns, errs = excel_import.parse_schedule_text(
        "周一 上午1 17班\n周一 上午1 17班", ["上午1", "上午2", "上午3", "上午4", "下午1", "下午2", "下午3"])
    assert errs == [] and len(rows) == 1 and warns and "重复" in warns[0]


def test_import_text_roundtrip(client):
    db.set_config("lesson_times", db.get_config("lesson_times") or
                  __import__("json").dumps({"上午1": ["08:00", "08:45"],
                                            "下午1": ["14:15", "15:00"]}, ensure_ascii=False))
    r = client.post("/api/import/text", json={
        "text": "周一 上午1 17班\n周二 下午1 21班 语文课",
        "effective_from": "2031-07-01"})
    assert r.status_code == 200, r.text
    assert r.json()["lessons"] == 2
    with db.conn() as c:
        rows = {(x["weekday"], x["section"]): (x["class_name"], x["subject"])
                for x in c.execute(
                    "SELECT weekday, section, class_name, subject FROM lessons "
                    "WHERE effective_from='2031-07-01'")}
    assert rows[(0, "上午1")] == ("17班", "")
    assert rows[(1, "下午1")] == ("21班", "语文")


def test_import_text_all_or_nothing(client):
    r = client.post("/api/import/text", json={
        "text": "周一 上午1 17班\n坏行不解析", "effective_from": "2031-07-02"})
    assert r.status_code == 400
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM lessons WHERE effective_from='2031-07-02'"
                         ).fetchone()[0] == 0


# ---------------- 明文校历 ----------------

def test_parse_calendar_basic(client):
    text = ("9/28-9/30 月考\n"
            "10/1-10/7 国庆假\n"
            "11/12 家长会 18:30-20:00\n"
            "1/15 期末统考\n"
            "11/5-11/6 校运会\n"
            "10/22 模拟考试\n"
            "1/9 初三体育期末考\n"
            "6/24 下午初一道史期末考\n"
            "11/18 期中考试\n"
            "11/20 交流 [停课]")
    events, warns, errs = excel_import.parse_calendar_text(text, this_year=2026)
    assert errs == []
    by = {}
    for d, t0, t1, title, kind, susp in events:
        by.setdefault(title, []).append((d, t0, t1, kind, susp))
    assert len(by["月考"]) == 3                  # 区间按天展开
    assert by["国庆假"][0][0] == "2026-10-01"
    assert by["国庆假"][-1][0] == "2026-10-07"
    assert by["国庆假"][0][3] == "holiday"       # “假”自动判假期
    assert by["家长会"][0][1] == "18:30" and by["家长会"][0][2] == "20:00"
    assert by["家长会"][0][3] == "event"
    assert by["期末统考"][0][0] == "2027-01-15"  # 1月归入学年次年
    assert by["期末统考"][0][3] == "exam"
    assert by["期末统考"][0][4] == 1                        # 期末默认停课
    assert by["月考"][0][4] == 1                            # 月考默认停课
    assert all(x[4] == 1 for x in by["校运会"]) and len(by["校运会"]) == 2  # 运动会自动停课
    assert by["模拟考试"][0][4] == 0                        # 模拟考不停课
    assert by["初三体育期末考"][0][4] == 0                  # 体育考试不停课
    assert by["下午初一道史期末考"][0][4] == 0              # 半日考试不整日停课
    assert by["期中考试"][0][4] == 1                        # 期中默认停课
    assert by["交流"][0][4] == 1                            # [停课] 显式标注


def test_parse_calendar_kind_override_and_errors(client):
    events, warns, errs = excel_import.parse_calendar_text(
        "10/20 秋游 [活动]\n10/25 模拟考试\n12/1 视力检查 [考试]\nxx月xx日 乱写")
    assert any(title == "秋游" and kind == "event" and susp == 0
               for _, _, _, title, kind, susp in events)
    assert any(title == "视力检查" and kind == "exam"
               for _, _, _, title, kind, _s in events)
    assert len(errs) == 1 and "无法识别日期" in errs[0]


def test_import_calendar_text_replaces(client):
    """整份替换语义：重导后旧校历事件不残留。"""
    r = client.post("/api/import/calendar-text", json={
        "text": "9/28-9/30 月考\n11/12 家长会", "year": 2026})
    assert r.status_code == 200 and r.json()["events"] == 4
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE source='calendar' "
                         "AND date IN ('2026-09-28','2026-09-29','2026-09-30')").fetchone()[0] == 3
    r = client.post("/api/import/calendar-text", json={"text": "12/1 视力筛查", "year": 2026})
    assert r.status_code == 200
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE source='calendar' "
                         "AND date LIKE '2026-09-2%'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM events WHERE title='视力筛查'").fetchone()[0] == 1
    with db.conn() as c:
        c.execute("DELETE FROM events WHERE title='视力筛查'")

def test_text_import_append_mode(client):
    """追加模式：保留已有课，新增新课，同班同节次局部更新。"""
    import excel_import
    from datetime import date
    eff = "2031-09-01"
    excel_import.import_schedule_text("周一 上午1 17班 语文课\n周一 下午1 17班 数学课",
                                      effective_from=eff, replace=True)
    excel_import.import_schedule_text("周二 上午1 18班 信息课", effective_from=eff, replace=False)
    with __import__("db").conn() as c:
        rows = {(r["weekday"], r["section"], r["class_name"]): r["subject"]
                for r in c.execute("SELECT * FROM lessons WHERE effective_from=?", (eff,))}
    assert rows.get((0, "上午1", "17班")) == "语文"       # 原有课保留
    assert rows.get((0, "下午1", "17班")) == "数学"       # 原有课保留
    assert rows.get((1, "上午1", "18班")) == "信息"       # 追加的新课
    # 同班同节次追加 → 更新不重复
    excel_import.import_schedule_text("周一 上午1 17班 语文课", effective_from=eff, replace=False)
    with __import__("db").conn() as c:
        n = c.execute("SELECT COUNT(*) FROM lessons WHERE effective_from=? AND weekday=0 "
                      "AND section='上午1' AND class_name='17班'", (eff,)).fetchone()[0]
        subj = c.execute("SELECT subject FROM lessons WHERE effective_from=? AND weekday=0 "
                         "AND section='上午1' AND class_name='17班'", (eff,)).fetchone()[0]
    assert n == 1 and subj == "语文"
