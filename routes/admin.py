# -*- coding: utf-8 -*-
"""配置 / 推送 / 登录路由。"""
import json
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import accounts
import clawbot
import db
import mailer
import systemone
from common import (AUTH_COOKIE, date_param, login_blocked, login_failed,
                    login_succeeded, verify_password)
from models import LoginIn, RegisterIn

router = APIRouter()

CONFIG_KEYS = {"push_time", "lesson_subject", "lesson_location",
               "my_subjects", "my_classes", "lesson_times", "schedule_overrides",
               "subject_groups", "wxpusher_uid", "lesson_remind_min"}


def _mask_token(t: str) -> str:
    return f"****{t[-4:]}" if t and len(t) >= 4 else ("" if not t else "****")


def _require_admin():
    u = accounts.get_user(db.current_username())
    if not u or not u["is_admin"]:
        raise HTTPException(403, "需要管理员权限")


@router.get("/api/health")
def api_health():
    from datetime import datetime
    return {"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@router.get("/api/me")
def api_me():
    u = accounts.get_user(db.current_username())
    return {"username": db.current_username(), "is_admin": bool(u and u["is_admin"])}


@router.get("/api/config")
def api_get_config():
    return {k: db.get_config(k) for k in CONFIG_KEYS} | {
        "terms": db.get_terms(),
        "lesson_times": db.get_config("lesson_times"),
        "pushplus_token_masked": _mask_token(db.get_config("pushplus_token")),
        "wxpusher_token_masked": _mask_token(db.get_config("wxpusher_app_token")),
        "clawbot_enabled": clawbot.enabled(),
        "clawbot_bound": bool(clawbot.bot_info()),
        "systemone_enabled": systemone.enabled(),
        "systemone_model": db.get_config("siliconflow_model") or systemone.DEFAULT_MODEL,
    }


@router.put("/api/config")
def api_put_config(body: dict, request = None):
    from fastapi import Request
    for k, v in body.items():
        if k in CONFIG_KEYS:
            db.set_config(k, str(v))
    if "pushplus_token" in body and str(body["pushplus_token"]).strip():
        db.set_config("pushplus_token", str(body["pushplus_token"]).strip())   # 空值=不修改
    if "wxpusher_app_token" in body and str(body["wxpusher_app_token"]).strip():
        db.set_config("wxpusher_app_token", str(body["wxpusher_app_token"]).strip())  # 空值=不修改
    if "wxpusher_uid" in body:
        db.set_config("wxpusher_uid", str(body["wxpusher_uid"]).strip())       # 可清空=停用 APP 推送
    for k in ("clawbot_token", "clawbot_secret"):
        if k in body and str(body[k]).strip():
            db.set_config(k, str(body[k]).strip())                             # 空值=不修改
    if "siliconflow_key" in body and str(body["siliconflow_key"]).strip():
        db.set_config("siliconflow_key", str(body["siliconflow_key"]).strip())  # 空值=不修改
    if "siliconflow_model" in body and str(body["siliconflow_model"]).strip():
        db.set_config("siliconflow_model", str(body["siliconflow_model"]).strip())
    new_session = False
    if "login_password" in body and str(body["login_password"]).strip():
        # 修改自己的登录密码（系统库校验当前密码），成功后吊销该用户全部会话
        me = db.current_username()
        cur = str(body.get("current_password", ""))
        u = accounts.get_user(me)
        if not u or not verify_password(cur, u["pw_hash"]):
            raise HTTPException(403, "当前密码不正确")
        accounts.change_password(me, str(body["login_password"]).strip())
        new_session = True                       # 当前设备自动续登，免得改完密码自己被踢
    resp = JSONResponse({"ok": True})
    if new_session:                                    # 当前设备自动续登，免得改完密码自己被踢
        resp.set_cookie(AUTH_COOKIE, accounts.create_session(db.current_username()),
                        max_age=90 * 86400, httponly=True, samesite="lax", path="/")
    return resp


@router.get("/api/digest/preview")
def api_digest_preview(d: str | None = None):
    target = date_param(d) if d else date.today() + timedelta(days=1)
    subject, html = mailer.build_digest(target)
    return JSONResponse({"subject": subject, "html": html})


@router.post("/api/digest/send")
def api_digest_send(d: str | None = None):
    target = date_param(d) if d else date.today() + timedelta(days=1)
    return {"ok": mailer.digest_job(target), "target": target.isoformat()}


@router.post("/api/clawbot/test")
def api_clawbot_test():
    if not clawbot.enabled():
        raise HTTPException(400, "未配置 ClawBot token/secretKey")
    return {"ok": clawbot.test_send()}


@router.post("/api/clawbot/poll")
def api_clawbot_poll():
    """手动触发一次收消息（正常由后台定时任务轮询）。"""
    if not clawbot.enabled():
        raise HTTPException(400, "未配置 ClawBot token/secretKey")
    return {"handled": clawbot.poll_once()}


@router.post("/api/systemone/test")
def api_systemone_test():
    if not systemone.enabled():
        raise HTTPException(400, "未配置硅基流动 Key")
    return {"ok": systemone.test_call()}


# ---------------- 登录 ----------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · 叮咛</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; min-height: 100vh;
    display: grid; place-items: center; color: #1c2333;
    background: radial-gradient(1100px 500px at 85% -8%, #e3ebff 0%, transparent 58%),
                radial-gradient(900px 460px at -8% 108%, #fdeef6 0%, transparent 52%), #f3f5fa; }
  .card { width: min(360px, 88vw); background: #fff; border-radius: 18px; padding: 34px 30px 28px;
    box-shadow: 0 2px 8px rgba(23,35,61,.06), 0 16px 40px -16px rgba(23,35,61,.18); }
  .brand { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 800; margin-bottom: 24px; }
  .brand-logo { width: 38px; height: 38px; border-radius: 11px; flex: none;
    box-shadow: 0 4px 10px -2px rgba(59,91,253,.5); }
  input { width: 100%; border: 1px solid #e6e9f2; border-radius: 10px; padding: 12px 14px;
    font: inherit; background: #fbfcff; }
  input:focus { outline: none; border-color: #3b6ef5; box-shadow: 0 0 0 3px #dfe8ff; background: #fff; }
  button { width: 100%; margin-top: 14px; border: none; border-radius: 10px; padding: 12px 0;
    font: inherit; font-weight: 700; color: #fff; cursor: pointer;
    background: linear-gradient(135deg, #5b86f7, #3b5bfd); box-shadow: 0 4px 14px -3px rgba(59,91,253,.5); }
  button:active { transform: translateY(1px); }
  .msg { min-height: 20px; margin-top: 12px; font-size: 13.5px; color: #e5484d; text-align: center; }
  .hint { margin-top: 14px; font-size: 13px; text-align: center; }
  .hint a { color: #3b6ef5; }
</style>
</head>
<body>
  <div class="card">
    <div class="brand"><img class="brand-logo" src="/static/favicon.svg" alt="叮咛">叮咛</div>
    <input type="text" id="un" placeholder="用户名" autocomplete="username" autofocus>
    <input type="password" id="pw" placeholder="密码" autocomplete="current-password" style="margin-top:10px">
    <button onclick="doLogin()">登 录</button>
    <div class="msg" id="msg"></div>
    <p class="hint">没有账号？<a href="/register">注册</a></p>
  </div>
<script>
  const un = document.getElementById("un"), inp = document.getElementById("pw");
  for (const el of [un, inp]) el.addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
  async function doLogin() {
    const res = await fetch("/api/login", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: un.value.trim(), password: inp.value }) });
    if (res.ok) { location.href = "/"; return; }
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (e) {}
    document.getElementById("msg").textContent =
      detail || (res.status === 401 ? "用户名或密码不正确" : "登录失败，请重试");
    inp.select();
  }
</script>
</body>
</html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>注册 · 叮咛</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; min-height: 100vh;
    display: grid; place-items: center; color: #1c2333;
    background: radial-gradient(1100px 500px at 85% -8%, #e3ebff 0%, transparent 58%),
                radial-gradient(900px 460px at -8% 108%, #fdeef6 0%, transparent 52%), #f3f5fa; }
  .card { width: min(360px, 88vw); background: #fff; border-radius: 18px; padding: 34px 30px 28px;
    box-shadow: 0 2px 8px rgba(23,35,61,.06), 0 16px 40px -16px rgba(23,35,61,.18); }
  .brand { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 800; margin-bottom: 24px; }
  .brand-logo { width: 38px; height: 38px; border-radius: 11px; flex: none;
    box-shadow: 0 4px 10px -2px rgba(59,91,253,.5); }
  input { width: 100%; border: 1px solid #e6e9f2; border-radius: 10px; padding: 12px 14px;
    font: inherit; background: #fbfcff; }
  input:focus { outline: none; border-color: #3b6ef5; box-shadow: 0 0 0 3px #dfe8ff; background: #fff; }
  button { width: 100%; margin-top: 14px; border: none; border-radius: 10px; padding: 12px 0;
    font: inherit; font-weight: 700; color: #fff; cursor: pointer;
    background: linear-gradient(135deg, #5b86f7, #3b5bfd); box-shadow: 0 4px 14px -3px rgba(59,91,253,.5); }
  button:active { transform: translateY(1px); }
  .msg { min-height: 20px; margin-top: 12px; font-size: 13.5px; color: #e5484d; text-align: center; }
  .hint { margin-top: 14px; font-size: 13px; text-align: center; }
  .hint a { color: #3b6ef5; }
</style>
</head>
<body>
  <div class="card">
    <div class="brand"><img class="brand-logo" src="/static/favicon.svg" alt="注册账号">注册账号</div>
    <input type="text" id="un" placeholder="用户名（2-32 位，中英文/数字/下划线）" autocomplete="username" autofocus>
    <input type="password" id="pw" placeholder="密码（至少 6 位）" autocomplete="new-password" style="margin-top:10px">
    <input type="password" id="pw2" placeholder="确认密码" autocomplete="new-password" style="margin-top:10px">
    <input type="text" id="ic" placeholder="邀请码（首个注册的用户无需邀请码）" style="margin-top:10px">
    <button onclick="doRegister()">注 册</button>
    <div class="msg" id="msg"></div>
    <p class="hint">已有账号？<a href="/login">去登录</a></p>
  </div>
<script>
  const un = document.getElementById("un"), pw = document.getElementById("pw"),
        pw2 = document.getElementById("pw2"), ic = document.getElementById("ic");
  for (const el of [un, pw, pw2, ic]) el.addEventListener("keydown", e => { if (e.key === "Enter") doRegister(); });
  async function doRegister() {
    if (pw.value !== pw2.value) { document.getElementById("msg").textContent = "两次输入的密码不一致"; return; }
    const res = await fetch("/api/register", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: un.value.trim(), password: pw.value,
                             invite_code: ic.value.trim() }) });
    if (res.ok) { location.href = "/"; return; }
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (e) {}
    document.getElementById("msg").textContent = detail || "注册失败，请重试";
  }
</script>
</body>
</html>"""


@router.get("/login")
def login_page():
    return Response(content=LOGIN_HTML, media_type="text/html")


@router.get("/register")
def register_page():
    return Response(content=REGISTER_HTML, media_type="text/html")


@router.post("/api/login")
def api_login(b: LoginIn, request = None):
    from fastapi import Request
    ip = request.client.host if request and request.client else "unknown"
    if login_blocked(ip):
        raise HTTPException(429, "失败次数过多，请 15 分钟后再试")
    if not b.username.strip() or not accounts.verify_login(b.username.strip(), b.password):
        login_failed(ip)
        raise HTTPException(401, "用户名或密码不正确")
    login_succeeded(ip)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(AUTH_COOKIE, accounts.create_session(b.username.strip()),
                    max_age=90 * 86400, httponly=True, samesite="lax", path="/")
    return resp


@router.post("/api/register")
def api_register(b: RegisterIn, request = None):
    from fastapi import Request
    ip = request.client.host if request and request.client else "unknown"
    if login_blocked(ip):
        raise HTTPException(429, "失败次数过多，请 15 分钟后再试")
    username = b.username.strip()
    if not accounts.valid_username(username):
        raise HTTPException(400, "用户名需为 2-32 位中英文、数字或下划线")
    if len(b.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    first = accounts.count_users() == 0
    if not first:                                   # 非首个用户必须持有效邀请码
        if b.invite_code.strip().upper() != accounts.get_invite_code().upper():
            login_failed(ip)
            raise HTTPException(403, "邀请码不正确，请向管理员索取")
    if accounts.get_user(username):
        raise HTTPException(400, "用户名已存在")
    accounts.create_user(username, b.password, is_admin=first)
    login_succeeded(ip)
    resp = JSONResponse({"ok": True, "is_admin": first})
    resp.set_cookie(AUTH_COOKIE, accounts.create_session(username),
                    max_age=90 * 86400, httponly=True, samesite="lax", path="/")
    return resp


@router.post("/api/logout")
def api_logout(request = None):
    from fastapi import Request
    if request is not None:
        accounts.destroy_session(request.cookies.get(AUTH_COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


# ---------------- 管理员：用户管理 ----------------

@router.get("/api/admin/users")
def api_admin_users():
    _require_admin()
    return {"users": accounts.list_users(), "invite_code": accounts.get_invite_code()}


@router.post("/api/admin/invite/reset")
def api_admin_invite_reset():
    _require_admin()
    return {"ok": True, "invite_code": accounts.reset_invite_code()}


@router.delete("/api/admin/users/{username}")
def api_admin_delete_user(username: str):
    _require_admin()
    if username == db.current_username():
        raise HTTPException(400, "不能删除当前登录的账号")
    if not accounts.get_user(username):
        raise HTTPException(404, "用户不存在")
    accounts.delete_user(username)
    return {"ok": True}

@router.post("/api/wxpusher/test")
def api_wxpusher_test():
    """用当前用户的 UID 走部署级 appToken 发一条测试消息。"""
    uid = (db.get_config("wxpusher_uid") or "").strip()
    if not uid:
        raise HTTPException(400, "请先填写 WxPusher UID")
    import wxpusher
    ok, detail = wxpusher.send(uid, "叮咛 · WxPusher 测试消息",
                               "<p>收到这条说明 APP 推送通道正常。</p>")
    return {"ok": ok, "detail": detail}


@router.post("/api/pushplus/test")
def api_pushplus_test():
    import mailer
    ok = mailer.send_pushplus("叮咛 · PushPlus 测试消息", "<p>收到这条说明 PushPlus 通道正常。</p>")
    return {"ok": ok}

@router.post("/api/wxpusher/qrcode")
def api_wxpusher_qrcode():
    """生成当前用户的订阅二维码（extra=用户名，扫码后自动绑定）。"""
    import wxpusher_qr
    me = db.current_username()
    r = wxpusher_qr.create_qrcode(extra=me)
    if isinstance(r, tuple):
        raise HTTPException(400, r[1])
    return r


@router.post("/api/wxpusher/uid-poll")
def api_wxpusher_uid_poll(b: dict = None):
    """查「点扫码之后」新出现的订阅者 UID（时间窗匹配）。"""
    import wxpusher_qr
    body = b or {}
    since = int(body.get("since_ms") or 0)
    uid = wxpusher_qr.find_new_uid_since(since)
    return {"uid": uid}
