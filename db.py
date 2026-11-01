# -*- coding: utf-8 -*-
"""数据库层：SQLite，短连接，每个操作独立开连接（线程安全）。

多用户版：每位教师一个独立库文件 data/users/<用户名>.db，业务表不含 user_id。
当前用户由登录中间件通过 ContextVar 设置；未设置时回落：
  - 设置了 WORKBENCH_DB 环境变量 → 沿用该文件（测试/脚本兼容）
  - 否则指向系统库 system.db（仅系统表可用，业务表查询不可用）
"""
import contextvars
import json
import os
import secrets
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据根目录：测试用 WORKBENCH_DB 时整体跟随其所在目录，避免污染真实数据
DATA_DIR = (os.path.dirname(os.environ["WORKBENCH_DB"])
            if os.environ.get("WORKBENCH_DB") else os.path.join(BASE_DIR, "data"))
SYSTEM_DB = os.path.join(DATA_DIR, "system.db")        # 用户/会话/邀请码
USERS_DIR = os.path.join(DATA_DIR, "users")            # 每用户业务库
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")         # 用户上传的 Excel
_FALLBACK_DB = os.environ.get("WORKBENCH_DB") or SYSTEM_DB

# 登录中间件按会话解析出的当前用户（请求级）
_current_db: contextvars.ContextVar = contextvars.ContextVar("workbench_current_db", default=None)
_current_user: contextvars.ContextVar = contextvars.ContextVar("workbench_current_user", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lessons (          -- 周期课表（每周循环）
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday    INTEGER NOT NULL,               -- 0=周一 ... 4=周五, 5=周六, 6=周日
    section    TEXT NOT NULL,                  -- 节次，如 上午2
    start_time TEXT NOT NULL,                  -- HH:MM
    end_time   TEXT NOT NULL,
    class_name TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    weeks      TEXT NOT NULL DEFAULT 'all'     -- all | odd | even（单双周），预留
);
CREATE TABLE IF NOT EXISTS events (           -- 一次性日程 + 校历事件
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,                  -- YYYY-MM-DD
    start_time TEXT NOT NULL DEFAULT '',       -- 空 = 全天
    end_time   TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL,
    location   TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'user',   -- user | calendar
    kind       TEXT NOT NULL DEFAULT 'event'   -- event | exam | holiday | festival | week_note
);
CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    due        TEXT NOT NULL DEFAULT '',       -- YYYY-MM-DD HH:MM，空=无截止
    priority   INTEGER NOT NULL DEFAULT 1,     -- 0 低 1 中 2 高
    done       INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lesson_cancels (   -- 单日调课：某日期停掉某节课
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,                 -- 停课日期 YYYY-MM-DD
    weekday     INTEGER NOT NULL,              -- 原课星期（0=周一，含调休换算）
    section     TEXT NOT NULL,                 -- 节次
    note        TEXT NOT NULL DEFAULT '',      -- 如：换至9/11上午1
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (         -- 登录会话（随机令牌，可吊销）
    token      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (          -- 导入记录（幂等去重）
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                  -- schedule | arrangement
    filename   TEXT NOT NULL,
    file_hash  TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_batches (    -- agent 指令批次（一条指令 = 一批操作，可整批撤销）
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction TEXT NOT NULL,                  -- 用户原始指令
    source      TEXT NOT NULL DEFAULT 'wechat', -- wechat | web
    status      TEXT NOT NULL DEFAULT 'applied',-- applied | undone
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_batch_ops (  -- 批次内的单个操作（撤销按逆序回放）
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL,
    op_type    TEXT NOT NULL,                   -- create_event | cancel_lesson
    payload    TEXT NOT NULL,                   -- 操作参数 JSON
    target_id  INTEGER,                         -- 落库后的行 id（events.id / lesson_cancels.id）
    created_at TEXT NOT NULL
);
"""

# 系统库：账号 / 会话 / 邀请码（业务数据一律在个人库）
SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL UNIQUE,
    pw_hash    TEXT NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_CONFIG = {
    "push_time": "21:00",          # 每晚推送时间
    "pushplus_token": "",
    "wxpusher_app_token": "",      # WxPusher 应用 token（APP 推送，优先于 PushPlus）
    "wxpusher_uid": "",            # 用户订阅应用后的 UID          # PushPlus 微信推送 token
    "last_digest_date": "",        # 最近一次成功推送的日期
    "lesson_subject": "",          # 导入后课表里显示的科目名；空 = 沿用课表格子原样
    "my_subjects": "[]",           # 你的课在课表格子里的叫法（JSON 数组），如 ["语文", "语"]
    "my_classes": "[]",            # 只导入这些班（班号数组）；空 = 课表里出现的班全要
    "schedule_overrides": "{}",    # 调休/补班：{日期: 按周几上课(0=周一..6=周日)}
    "lesson_times": json.dumps({   # 作息时间表（可在设置页调整）
        "上午1": ["08:00", "08:45"], "上午2": ["09:00", "09:45"],
        "上午3": ["10:15", "11:00"], "上午4": ["11:15", "12:05"],
        "下午1": ["14:15", "15:00"], "下午2": ["15:15", "16:00"],
        "下午3": ["16:15", "17:00"]}, ensure_ascii=False),
    "lesson_location": "",         # 课程默认地点（可自行配置，如 电脑教室）
    "ai_api_key": "",              # DeepSeek key：部署后在设置页填自己的
    "ai_model": "deepseek-v4-flash",
}

# 学期锚点：第 1 周周一 与 学期最后一天（由校历解析时自动写入）
DEFAULT_TERMS = [
    {"name": "第一学期", "start": "2026-08-31", "end": "2027-01-31"},
    {"name": "第二学期", "start": "2027-02-22", "end": "2027-07-18"},
]


def user_db_path(username: str) -> str:
    return os.path.join(USERS_DIR, f"{username}.db")


def set_current_user(username: str):
    """把当前上下文的业务库切到指定用户（返回 token 供 reset，测试/后台任务用）。"""
    _current_user.set(username)
    return _current_db.set(user_db_path(username))


def current_username() -> str:
    return _current_user.get()


def all_usernames() -> list:
    """系统库里的全部用户名（直接读系统库，不依赖 ContextVar）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(SYSTEM_DB)
    c.row_factory = sqlite3.Row
    try:
        return [r["username"] for r in
                c.execute("SELECT username FROM users ORDER BY id")]
    finally:
        c.close()


def conn() -> sqlite3.Connection:
    path = _current_db.get() or _FALLBACK_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # timeout=10：写锁被后台任务（推送巡检等）短暂持有时等待重试，而不是立即抛 locked
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


def init_db_file(path: str):
    """对任意路径执行建表 + 迁移 + 默认配置种子（注册新用户与初始化共用）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        # 版本化课表：lessons 增加生效日期列（老库兼容迁移）
        # 校历事件停课标记（校运会等：当日自动停课，无需逐节手工登记）
        ecols = [r[1] for r in c.execute("PRAGMA table_info(events)")]
        if "suspends" not in ecols:
            c.execute("ALTER TABLE events ADD COLUMN suspends INTEGER NOT NULL DEFAULT 0")
        if "embed" not in ecols:
            c.execute("ALTER TABLE events ADD COLUMN embed BLOB")   # 语义搜索向量缓存
        if "remind_min" not in ecols:
            c.execute("ALTER TABLE events ADD COLUMN remind_min INTEGER")   # 提前提醒分钟（NULL=不提醒）
        tcols = [r[1] for r in c.execute("PRAGMA table_info(todos)")]
        if "remind_min" not in tcols:
            c.execute("ALTER TABLE todos ADD COLUMN remind_min INTEGER")    # 待办：距截止提前量
        c.execute("""CREATE TABLE IF NOT EXISTS reminders_sent(
            rkey TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL)""")
        c.execute(
            "UPDATE events SET suspends=1 WHERE suspends=0 AND "
            "(title LIKE '%运会%' OR title LIKE '%期中%' OR title LIKE '%期末%' OR title LIKE '%月考%') "
            "AND title NOT LIKE '%体育%' AND title NOT LIKE '%动员%' AND title NOT LIKE '%表彰%' "
            "AND title NOT LIKE '%家长会%' AND title NOT LIKE '%模拟%' AND title NOT LIKE '%练习%' "
            "AND title NOT LIKE '%适应性%' AND title NOT LIKE '%温书%' AND title NOT LIKE '%报名%' "
            "AND title NOT LIKE '%志愿%' AND title NOT LIKE '%公布%' AND title NOT LIKE '%编班%' "
            "AND title NOT LIKE '%大扫除%' AND title NOT LIKE '%培训%' AND title NOT LIKE '%抽查%' "
            "AND title NOT LIKE '%分析会%' "
            "AND substr(title,1,2) NOT IN ('上午','下午')")
        c.execute(
            "UPDATE events SET suspends=0 WHERE suspends=1 AND "
            "(title LIKE '%体育%' OR title LIKE '%动员%' OR title LIKE '%表彰%' OR title LIKE '%家长会%' "
            "OR title LIKE '%模拟%' OR title LIKE '%练习%' OR title LIKE '%适应性%' OR title LIKE '%温书%' "
            "OR title LIKE '%报名%' OR title LIKE '%志愿%' OR title LIKE '%公布%' OR title LIKE '%编班%' "
            "OR title LIKE '%大扫除%' OR title LIKE '%培训%' OR title LIKE '%分析会%' "
            "OR substr(title,1,2) IN ('上午','下午'))")
        lcols = [r[1] for r in c.execute("PRAGMA table_info(lessons)")]
        if "note" not in lcols:
            c.execute("ALTER TABLE lessons ADD COLUMN note TEXT")   # 单次课的调课说明
        if "date" not in lcols:
            # 单次课：NULL=每周循环课；有值=仅该日期上课（调课/补课的正确模型）
            c.execute("ALTER TABLE lessons ADD COLUMN date TEXT")
        if "effective_from" not in lcols:
            c.execute("ALTER TABLE lessons ADD COLUMN effective_from TEXT NOT NULL DEFAULT ''")
        c.execute("UPDATE lessons SET effective_from='2026-09-01' "
                  "WHERE effective_from='' OR effective_from IS NULL")
        for k, v in DEFAULT_CONFIG.items():
            c.execute("INSERT OR IGNORE INTO config(key, value) VALUES(?,?)", (k, v))
        # 旧版本遗留的 SMTP 配置键清理（已改用 PushPlus）
        c.execute("DELETE FROM config WHERE key IN ('smtp_host','smtp_port','smtp_ssl','smtp_user','smtp_pass','mail_to')")
        row = c.execute("SELECT value FROM config WHERE key='terms'").fetchone()
        if row is None:
            import json
            c.execute("INSERT INTO config(key, value) VALUES('terms', ?)",
                      (json.dumps(DEFAULT_TERMS, ensure_ascii=False),))


def get_system_config(key: str, default: str = "") -> str:
    """部署级配置（system.db）：与应用/部署相关的全局值，与用户无关。"""
    with sqlite3.connect(SYSTEM_DB) as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_system_config(key: str, value: str):
    with sqlite3.connect(SYSTEM_DB) as c:
        c.execute("INSERT INTO config(key, value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def init_system_db():
    """初始化系统库（用户/会话/邀请码）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(SYSTEM_DB) as c:
        c.executescript(SYSTEM_SCHEMA)
        c.execute("INSERT OR IGNORE INTO config(key, value) VALUES('invite_code', ?)",
                  (secrets.token_hex(4).upper(),))


def init_db():
    """启动初始化：系统库必建；设置了 WORKBENCH_DB 时同步初始化回落库（测试兼容）；
    并对全部既有用户库补跑迁移（幂等）——结构性变更（新列等）不漏老库。"""
    init_system_db()
    if os.environ.get("WORKBENCH_DB"):
        init_db_file(_FALLBACK_DB)
    for name in all_usernames():
        try:
            init_db_file(user_db_path(name))
        except sqlite3.Error as e:
            # 单个库失败（被锁等）不影响其他库；下一轮启动会补迁移
            print(f"[迁移] 用户库 {name} 失败：{e}")


def get_config(key: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str):
    with conn() as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_terms() -> list:
    import json
    try:
        return json.loads(get_config("terms", json.dumps(DEFAULT_TERMS)))
    except Exception:
        return list(DEFAULT_TERMS)
