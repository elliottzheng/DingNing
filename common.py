# -*- coding: utf-8 -*-
"""跨路由共享的小工具与鉴权辅助（不含 FastAPI app 对象，避免循环导入）。"""
import hashlib
import hmac
import secrets
from datetime import date, datetime, timedelta

import db
import logic

AUTH_COOKIE = "wb_session"
# 登录/注册页与健康检查不需会话；"/" 首页放开（页面内 API 401 时前端跳登录页）
AUTH_OPEN_PATHS = {"/", "/login", "/register", "/favicon.ico",
                   "/api/health", "/api/login", "/api/register"}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def todo_dict(r) -> dict:
    return {"id": r["id"], "title": r["title"], "due": r["due"],
            "priority": r["priority"], "done": bool(r["done"]),
            "note": r["note"], "created_at": r["created_at"]}


def date_param(d: str | None) -> date:
    from fastapi import HTTPException
    try:
        return date.fromisoformat(d) if d else date.today()
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")


# ---------------- 登录鉴权（PBKDF2 密码哈希 + 可吊销会话 + 登录限流） ----------------

def hash_password(pw: str, iterations: int = 200_000) -> str:
    """PBKDF2-SHA256 哈希，格式 pbkdf2_sha256$iterations$salt$hash。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# 密码哈希/校验沿用下方 hash_password / verify_password；
# 账号、系统级会话（创建/校验/吊销）在 accounts.py（系统库 system.db）。


# ---- 登录限流（内存计数，防暴力破解） ----

_LOGIN_FAILS = {}          # ip -> {"fails": int, "locked_until": datetime}
MAX_FAILS = 5
LOCK_MINUTES = 15


def login_blocked(ip: str) -> bool:
    rec = _LOGIN_FAILS.get(ip)
    return bool(rec and rec["locked_until"] and rec["locked_until"] > datetime.now())


def login_failed(ip: str):
    rec = _LOGIN_FAILS.setdefault(ip, {"fails": 0, "locked_until": None})
    rec["fails"] += 1
    if rec["fails"] >= MAX_FAILS:
        rec["locked_until"] = datetime.now() + timedelta(minutes=LOCK_MINUTES)
        rec["fails"] = 0


def login_succeeded(ip: str):
    _LOGIN_FAILS.pop(ip, None)


def duplicate_event_exists(title: str, date_str: str, start: str, end: str) -> bool:
    """同标题+同日期+同起止的日程已存在（防止重复提交产生一样的日程）。"""
    with db.conn() as c:
        row = c.execute("SELECT 1 FROM events WHERE title=? AND date=? AND start_time=? AND end_time=?",
                        (title, date_str, start or "", end or "")).fetchone()
    return row is not None


# ---------------- 单日调课 ----------------

def eff_weekday(d: date) -> int:
    ov = logic.schedule_overrides().get(d.isoformat())
    return int(ov) if ov is not None else d.weekday()


def lesson_at(d: date, section: str):
    """找到某日期某节次的周期课（按 d 当日生效的课表版本）。"""
    with db.conn() as c:
        ver = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                        (d.isoformat(),)).fetchone()[0]
        if not ver:
            return None
        r = c.execute("SELECT * FROM lessons WHERE weekday=? AND section=? AND effective_from=?",
                      (eff_weekday(d), section, ver)).fetchone()
    return dict(r) if r else None


def lesson_covering(d: date, hhmm: str):
    """返回 d 当天覆盖时刻 hhmm(HH:MM) 的课；停课日/该节已调走返回 None。

    时间兼容 H:MM 与 HH:MM 两种存法（课表导入未统一补零），按分钟数比较。
    """
    t = _hhmm_to_min(hhmm)

    def cov(row):
        s, e = _hhmm_to_min(row["start_time"]), _hhmm_to_min(row["end_time"])
        return s <= t < e

    with db.conn() as c:
        if c.execute("SELECT 1 FROM events WHERE date=? AND suspends=1",
                     (d.isoformat(),)).fetchone():
            return None                                  # 校历停课日：全天无课
        ver = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                        (d.isoformat(),)).fetchone()[0]
        if not ver:
            return None
        rows = c.execute(
            "SELECT * FROM lessons WHERE effective_from=? "
            "AND ((weekday=? AND date IS NULL) OR date=?)",
            (ver, eff_weekday(d), d.isoformat())).fetchall()
    rows = [r for r in rows if r["start_time"] and cov(r)]
    if not rows:
        return None
    rows.sort(key=lambda r: _hhmm_to_min(r["start_time"]), reverse=True)
    r = dict(rows[0])
    with db.conn() as c:
        if c.execute("SELECT 1 FROM lesson_cancels WHERE date=? AND section=?",
                     (d.isoformat(), r["section"])).fetchone():
            return None                                  # 该节已调走：不算上课
    return r


def _hhmm_to_min(s: str) -> int:
    p = s.strip().split(":")
    return int(p[0]) * 60 + int(p[1])


def ring_time_adjust(d: date, hhmm: str) -> str:
    """响铃时刻撞上上课时段 → 挪到该节课开始前 5 分钟（迭代直到不撞课）。

    用于日程/待办提醒：老师上课期间手机不能响。挪出的时刻若仍撞课
    （紧凑课表连堂），继续向前挪；最多回退 6 跳防死循环。
    """
    for _ in range(6):
        les = lesson_covering(d, hhmm)
        if les is None:
            return hhmm
        t = _hhmm_to_min(les["start_time"]) - 5
        if t < 0:
            return hhmm                                  # 无更早空隙：保持原样
        hhmm = f"{t // 60:02d}:{t % 60:02d}"
    return hhmm


def class_sections_on(class_id: int, d: date) -> list[str]:
    """该班在 d 当天的节次列表（按调休有效星期；剔除已单日停课的节次）。

    课表取当日生效的最新版本（MAX(effective_from)<=d）——否则旧版本
    的同名班会串日（如 22班 旧版周五/新版周三）。
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM events WHERE date=? AND suspends=1",
                     (d.isoformat(),)).fetchone():
            return []                                    # 校历停课日（校运会等）：全天无课
        ver = c.execute("SELECT MAX(effective_from) FROM lessons WHERE effective_from<=?",
                        (d.isoformat(),)).fetchone()[0]
        if not ver:
            return []
        secs = [r["section"] for r in c.execute(
            "SELECT section FROM lessons WHERE ((weekday=? AND date IS NULL) OR date=?) "
            "AND class_name=? AND effective_from=?",
            (eff_weekday(d), d.isoformat(), f"{class_id}班", ver)).fetchall()]
        cancelled = {r["section"] for r in c.execute(
            "SELECT section FROM lesson_cancels WHERE date=?", (d.isoformat(),))}
    return [s for s in secs if s not in cancelled]


def _holiday_dates(from_d: date, to_d: date) -> set[str]:
    with db.conn() as c:
        return {r["date"] for r in c.execute(
            "SELECT date FROM events WHERE kind='holiday' AND date BETWEEN ? AND ?",
            (from_d.isoformat(), to_d.isoformat()))}


def next_meeting_date(class_id: int, after: date, horizon: int = 21):
    """该班 after 之后的下一个上课日：跳过校历假期（补班 override 例外）、
    按调休有效星期与单日停课判断；找不到返回 None。"""
    overrides = logic.schedule_overrides()
    holidays = _holiday_dates(after + timedelta(days=1),
                              after + timedelta(days=horizon))
    for i in range(1, horizon + 1):
        d = after + timedelta(days=i)
        if d.isoformat() in holidays and d.isoformat() not in overrides:
            continue                                    # 校历假期（补班日不跳过）
        if class_sections_on(class_id, d):
            return d
    return None
