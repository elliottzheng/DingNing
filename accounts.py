# -*- coding: utf-8 -*-
"""账号与系统库操作：用户注册/校验、系统级会话（90 天）、邀请码管理。

系统库（system.db）独立于个人业务库；本模块直接连系统库，不依赖 ContextVar。
密码哈希沿用 common.hash_password / verify_password。
"""
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

import common
import db

SESSION_DAYS = 90

USERNAME_MIN, USERNAME_MAX = 2, 32


def valid_username(username: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]{%d,%d}"
                             % (USERNAME_MIN, USERNAME_MAX), username or ""))


def _sys() -> sqlite3.Connection:
    c = sqlite3.connect(db.SYSTEM_DB)
    c.row_factory = sqlite3.Row
    return c


# ---------------- 用户 ----------------

def count_users() -> int:
    with _sys() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(username: str):
    with _sys() as c:
        return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def create_user(username: str, password: str, is_admin: bool = False):
    """创建账号并初始化该用户的独立业务库（建表 + 默认配置种子）。"""
    db.init_db_file(db.user_db_path(username))
    with _sys() as c:
        c.execute("INSERT INTO users(username, pw_hash, is_admin, created_at) VALUES(?,?,?,?)",
                  (username, common.hash_password(password),
                   1 if is_admin else 0, common.now_str()))


def delete_user(username: str):
    """删除账号 + 该用户全部会话 + 独立业务库文件。"""
    with _sys() as c:
        c.execute("DELETE FROM sessions WHERE username=?", (username,))
        c.execute("DELETE FROM users WHERE username=?", (username,))
    path = db.user_db_path(username)
    if os.path.exists(path):
        import gc
        import time
        for i in range(6):                      # Windows：等短连接对象被回收后才能删文件
            try:
                os.remove(path)
                break
            except PermissionError:
                gc.collect()
                time.sleep(0.05 * (i + 1))
        else:
            os.remove(path)


def list_users() -> list:
    with _sys() as c:
        return [dict(r) for r in
                c.execute("SELECT username, is_admin, created_at FROM users ORDER BY id")]


def verify_login(username: str, password: str) -> bool:
    u = get_user(username)
    return bool(u and common.verify_password(password, u["pw_hash"]))


def change_password(username: str, new_password: str):
    """改密码并吊销该用户全部会话（其他设备全部下线）。"""
    with _sys() as c:
        c.execute("UPDATE users SET pw_hash=? WHERE username=?",
                  (common.hash_password(new_password), username))
        c.execute("DELETE FROM sessions WHERE username=?", (username,))


# ---------------- 系统级会话 ----------------

def create_session(username: str) -> str:
    token = secrets.token_hex(32)
    now = datetime.now()
    with _sys() as c:
        c.execute("INSERT INTO sessions(token, username, created_at, expires_at) VALUES(?,?,?,?)",
                  (token, username, now.strftime("%Y-%m-%d %H:%M:%S"),
                   (now + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")))
    return token


def check_session(token: str) -> str | None:
    """校验会话，有效返回用户名，无效返回 None（过期会话顺手清除）。"""
    if not token:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _sys() as c:
        row = c.execute("SELECT username, expires_at FROM sessions WHERE token=?",
                        (token,)).fetchone()
        if row is None:
            return None
        if row["expires_at"] < now:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
    return row["username"]


def destroy_session(token: str):
    with _sys() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# ---------------- 邀请码 ----------------

def get_invite_code() -> str:
    with _sys() as c:
        row = c.execute("SELECT value FROM config WHERE key='invite_code'").fetchone()
        if row and row["value"]:
            return row["value"]
        code = secrets.token_hex(4).upper()
        c.execute("INSERT OR REPLACE INTO config(key, value) VALUES('invite_code', ?)", (code,))
        return code


def reset_invite_code() -> str:
    code = secrets.token_hex(4).upper()
    with _sys() as c:
        c.execute("INSERT INTO config(key, value) VALUES('invite_code', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (code,))
    return code
