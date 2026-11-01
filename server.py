# -*- coding: utf-8 -*-
"""教师工作台主服务：FastAPI + SQLite，可独立常驻运行（pythonw）。

路由按域拆分在 routes/ 包（schedule / todos / events / chat / admin），
本文件只负责：日志、应用组装、登录中间件、启动任务、静态页与 pythonw 引导。
"""
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db
import mailer
import routes
from common import AUTH_COOKIE, AUTH_OPEN_PATHS

BASE_DIR = db.BASE_DIR
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_handlers = [RotatingFileHandler(os.path.join(LOG_DIR, "server.log"),
                                 maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")]
if sys.stderr is not None:          # pythonw 下无控制台，不挂终端输出
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=_handlers)
log = logging.getLogger("workbench")
# 显式挂 handler 并切断 root 传播，避免 uvicorn dictConfig 重置 root 后丢日志
log.setLevel(logging.INFO)
for h in _handlers:
    log.addHandler(h)
log.propagate = False

app = FastAPI(title="叮咛", docs_url=None, redoc_url=None)
app.include_router(routes.api_router)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",          # 防类型嗅探
    "X-Frame-Options": "DENY",                    # 防点击劫持
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: https://wxpusher.zjiecode.com https://mp.weixin.qq.com; "
                                "connect-src 'self'; frame-ancestors 'none'"),
}


@app.middleware("http")
async def auth_middleware(request, call_next):
    """会话解析 + 登录验证 + 安全响应头 + 静态资源缓存策略。

    会话有效 → ContextVar 绑定该用户的独立业务库；开放路径不设（业务表查询不可用）。
    """
    import accounts
    path = request.url.path
    username = accounts.check_session(request.cookies.get(AUTH_COOKIE, ""))
    if path not in AUTH_OPEN_PATHS and not path.startswith("/static/") and not username:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录或密码已变更，请重新登录"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    if username:
        db.set_current_user(username)
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    # 前端脚本/样式 no-store：改版后任何刷新必定拿到最新（曾被浏览器缓存坑过）
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    else:
        resp.headers["Cache-Control"] = "no-cache"
    # 前端脚本/样式用 no-store：改版后任何刷新必定拿到最新（模块脚本曾被内存缓存坑过）
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    else:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def startup():
    db.init_db()
    mailer.setup_scheduler()


# ---------------------------------------------------------------- 静态页面

START_TS = str(int(time.time()))          # 每次重启变化，作为静态资源版本号


@app.get("/")
def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        html = f.read()
    # 版本号注入：重启后浏览器必然拉取最新 JS/CSS，不依赖缓存策略
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={START_TS}"')
    html = html.replace('src="/static/js/main.js"', f'src="/static/js/main.js?v={START_TS}"')
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={START_TS}"')
    return Response(content=html, media_type="text/html")


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/favicon.ico")
def favicon_ico():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(BASE_DIR, "static", "favicon.svg"),
                        media_type="image/svg+xml")


def _boot_fail(msg: str):
    with open(os.path.join(LOG_DIR, "boot_error.txt"), "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now()}] {msg}\n")


def _already_running(port: int) -> bool:
    """探测本配置端口是否已有实例（开机竞态防重复启动）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
            return True
    except Exception:
        return False


if __name__ == "__main__":
    import traceback
    import urllib.request
    import uvicorn
    if sys.stdout is None:      # pythonw 无控制台：uvicorn 的 isatty 检测会崩
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    attempts = 0
    port = int(os.environ.get("PORT", "8321"))
    while True:
        attempts += 1
        if _already_running(port):      # 已有实例在跑（开机竞态）→ 不重复启动
            break
        try:
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
            break
        except SystemExit:              # 端口被占等场景 uvicorn 以 SystemExit 退出
            if _already_running(port):
                break
            if attempts >= 4:
                _boot_fail(f"端口 {port} 迟迟不可用，重试 {attempts} 次后放弃")
                break
            time.sleep(5)               # 开机时等其他启动项释放端口
        except BaseException:
            with open(os.path.join(LOG_DIR, "boot_error.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now()}] pythonw 启动失败:\n")
                traceback.print_exc(file=f)
            break
