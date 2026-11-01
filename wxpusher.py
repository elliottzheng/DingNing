"""WxPusher APP 推送通道（https://wxpusher.zjiecode.com）。

用户安装 WxPusher APP 并订阅应用后，在 APP「我的」里复制 UID 填入设置。
发送：POST /api/send/message，成功码 1000。失败返回 (False, 原因)，
由调用方决定兜底（当前：回退 PushPlus）。
"""
import json
import urllib.request

import db

API = "https://wxpusher.zjiecode.com/api/send/message"


def configured() -> bool:
    return bool((db.get_config("wxpusher_uid") or "").strip()
                and (db.get_system_config("wxpusher_app_token")
                     or db.get_config("wxpusher_app_token") or "").strip())


def send(uid: str, title: str, html: str, app_token: str | None = None):
    """发送 HTML 消息到指定 UID。返回 (ok, detail)。

    appToken 为部署级配置：系统库优先，兼容旧版存在用户库的写法。"""
    token = (app_token or db.get_system_config("wxpusher_app_token")
             or db.get_config("wxpusher_app_token") or "").strip()
    uid = (uid or "").strip()
    if not token or not uid:
        return False, "appToken 或 UID 未配置"
    body = json.dumps({"appToken": token, "uids": [uid], "title": title,
                       "content": html, "contentType": 2,
                       "summary": title[:98]}).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return False, repr(e)[:120]
    if resp.get("code") == 1000:
        return True, ""
    return False, f"服务端返回 {resp}"
