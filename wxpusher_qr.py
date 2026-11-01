"""WxPusher 订阅二维码与订阅者查询（扫码自动绑定 UID）。

流程：管理员填好部署级 appToken 后，老师点「扫码订阅」（前端记录时间戳）→
后端 create/qrcode 生成订阅码 → 老师微信扫码关注并订阅 →
前端每 3 秒轮询 uid-poll，后端按 createTime 找「点击之后的新订阅者」→ 自动回填 UID。
（WxPusher 订阅者列表不返回 extra 标记，故按时间窗匹配；多人同时扫码需错开。）
"""
import json
import urllib.request

import db

BASE = "https://wxpusher.zjiecode.com"


def _token() -> str:
    return (db.get_system_config("wxpusher_app_token")
            or db.get_config("wxpusher_app_token") or "").strip()


def create_qrcode(extra: str, valid_time: int = 1800):
    """生成订阅二维码。返回 {code_url} 或 (False, 原因)。"""
    token = _token()
    if not token:
        return False, "管理员尚未配置 WxPusher appToken"
    body = json.dumps({"appToken": token, "extra": extra,
                       "validTime": valid_time}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/fun/create/qrcode", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return False, repr(e)[:120]
    if resp.get("code") != 1000:
        return False, f"服务端返回 {resp}"
    return {"code_url": (resp.get("data") or {}).get("url", ""), "qrcode_id": resp["data"].get("code", "")}


def find_new_uid_since(since_ms: int, pages: int = 3):
    """找 since_ms（毫秒时间戳）之后新出现的订阅者 UID。

    原理：订阅者记录带 createTime（毫秒）。扫码订阅是"点击之后的新订阅"，
    按时间窗匹配即可拿到 UID（无需公网回调；WxPusher 列表不返回 extra，
    按用户名匹配的旧方案不可行）。多个订阅者时取最新的一条。
    """
    token = _token()
    if not token:
        return None
    newest = None, 0
    for page in range(1, pages + 1):
        url = f"{BASE}/api/fun/wxuser?appToken={token}&page={page}&pageSize=50"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None
        data = resp.get("data") or {}
        records = data.get("records") if isinstance(data, dict) else (data or [])
        for u in records or []:
            ct = u.get("createTime") or 0
            if ct >= since_ms and ct > newest[1]:
                newest = (u.get("uid"), ct)
    return newest[0] or None
