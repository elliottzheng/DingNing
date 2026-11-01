# -*- coding: utf-8 -*-
"""pytest 全局夹具：把数据库指到临时目录，注册测试用户并绑定 ContextVar，隔离真实数据。"""
import os
import sys
import tempfile

import pytest

# 必须在导入 server/db 之前设置
_TEST_DIR = tempfile.mkdtemp(prefix="workbench_test_")
_TEST_DB = os.path.join(_TEST_DIR, "workbench_test.db")
os.environ["WORKBENCH_DB"] = _TEST_DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
import accounts  # noqa: E402
import db  # noqa: E402

TEST_USER = "tester"
TEST_PASSWORD = "test-pw-123"


def _generate_sample_files():
    """生成脱敏示例课表/教学安排（结构同真实格式，内容为通用示例）。

    任何克隆/分发的目录首次跑测试时自给自足，不携带任何真实校历或个人课表。
    """
    import openpyxl

    # ---- 示例课表（个人格式：行=节次、列=星期、格=班级名）----
    p1 = os.path.join(db.BASE_DIR, "示例课表.xlsx")
    if not os.path.exists(p1):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "示例"
        ws["A1"] = "节次"
        ws["B1"] = "时间"
        for i, wd in enumerate(["星期一", "星期二", "星期三", "星期四", "星期五"]):
            ws.cell(1, 3 + i).value = wd
        sections = [("上午1", "8:00-8:45"), ("上午2", "9:00-9:45"), ("上午3", "10:15-11:00"),
                    ("上午4", "11:15-12:05"), ("下午1", "14:15-15:00"),
                    ("下午2", "15:15-16:00"), ("下午3", "16:15-17:00")]
        lessons = {  # 示例排布（与任何真实学校无关）
            "上午1": ["17班", "20班", "23班", "", "26班"],
            "上午2": ["", "21班", "24班", "", ""],
            "下午1": ["18班", "22班", "25班", "27班", ""],
            "下午2": ["", "", "26班", "28班", ""],
            "下午3": ["19班", "", "", "", ""],
        }
        for r, (sec, tm) in enumerate(sections, start=2):
            ws.cell(r, 1).value = sec
            ws.cell(r, 2).value = tm
            for i in range(5):
                v = lessons.get(sec, [""] * 5)[i]
                if v:
                    ws.cell(r, 3 + i).value = v
        wb.save(p1)

    # ---- 示例教学安排（结构同真实格式：奇数行日期、偶数行事项）----
    p2 = os.path.join(db.BASE_DIR, "示例教学安排.xlsx")
    if not os.path.exists(p2):
        from datetime import date, timedelta
        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "第一学期"
        ws1["A1"] = "2026-2027学年第一学期教学安排示例"
        CN = {1: "一月", 2: "二月", 3: "三月", 4: "四月", 5: "五月", 6: "六月",
              7: "七月", 8: "八月", 9: "九月", 10: "十月", 11: "十一月", 12: "十二月"}
        d = date(2026, 8, 31)
        week = 1
        r = 3                                   # 日期行必须落奇数行（解析器语义）
        while d <= date(2026, 12, 31):
            ws1.cell(r, 1).value = f"第{week}周"
            for i in range(7):
                dd = d + timedelta(days=i)
                label = f"{CN[dd.month]} {dd.day}" if (dd.day <= 3 or i == 0) else str(dd.day)
                ws1.cell(r, 2 + i).value = label
            r += 1                              # 偶数行 = 事项行
            if week % 2:
                ws1.cell(r, 3).value = "教研活动"
            if week in (5, 11, 17):
                ws1.cell(r, 5).value = "阶段练习"
            if week % 3 == 0:
                ws1.cell(r, 7).value = "社团活动"
            if week == 10:
                ws1.cell(r, 5).value = "校运会"    # 11/5 周四（课表查询用例的固定锚点）
            ws1.cell(r, 9).value = "教学例会" if week % 2 else ""
            d += timedelta(days=7)
            week += 1
            r += 1
        ws2 = wb.create_sheet("第二学期")
        ws2["A1"] = "2026-2027学年第二学期教学安排示例"
        CN = {2: "二月", 3: "三月", 4: "四月", 5: "五月", 6: "六月", 7: "七月"}
        d = date(2027, 2, 22)
        week = 1
        r = 3
        while d <= date(2027, 7, 2):
            ws2.cell(r, 1).value = f"第{week}周"
            for i in range(7):
                dd = d + timedelta(days=i)
                ws2.cell(r, 2 + i).value = (f"{CN[dd.month]} {dd.day}" if dd.day <= 3 or i == 0
                                            else str(dd.day))
            r += 1
            if week % 2:
                ws2.cell(r, 4).value = "教研活动"
            if week in (4, 10, 16):
                ws2.cell(r, 6).value = "阶段练习"
            d += timedelta(days=7)
            week += 1
            r += 1
        wb.save(p2)


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    """所有测试（含不依赖 client 的纯单元测试）都基于已初始化、已导入示例课表的测试用户库。

    先初始化系统库并注册用户 tester（首个用户 = 管理员），再把 ContextVar 绑到
    tester 的个人库——单元测试直接调 db 的用例与走 HTTP 的用例共享同一份数据。
    课表额外按固定生效日 2026-09-01 导入一份，使写死日期的用例不受运行日期影响。
    """
    import glob
    import os
    _generate_sample_files()
    db.init_db()
    if not accounts.get_user(TEST_USER):
        accounts.create_user(TEST_USER, TEST_PASSWORD, is_admin=True)
    db.set_current_user(TEST_USER)
    import excel_import
    for p in glob.glob(os.path.join(db.BASE_DIR, "*教学安排*.xlsx")):
        excel_import.import_arrangement(p)   # 校历：直接导入（不走目录扫描）
    for p in glob.glob(os.path.join(db.BASE_DIR, "*课表*.xlsx")):
        excel_import.import_schedule(p, effective_from="2026-09-01")
    yield


@pytest.fixture(scope="session")
def client():
    from server import app
    with TestClient(app) as c:
        _login(c)
        yield c


def _login(c):
    """给测试客户端种上 tester 的会话 Cookie（多用户中间件需要）。"""
    c.cookies.set("wb_session", accounts.create_session(TEST_USER))

