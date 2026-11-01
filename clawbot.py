# -*- coding: utf-8 -*-
"""微信 ClawBot 双向通道：轮询你发到 ClawBot 的文字，交给 chatcmd 处理，结果原路回微信。

官方限制（pushplus 文档 /doc/channel/clawbot.html）：
  · 每下发 10 条消息后需用户主动发起一次对话；每 24h 也需一次主动对话。
    只收不发无此问题；本模块回复也算下发，因此**回复有间隔保护**。
  · 内容模板必须 txt，否则富文本只显示前 50 字。
开放接口需 accessKey（用户 token + secretKey 换取，2 小时过期，模块内缓存刷新）。
凭据缺任何一个 → 全部接口静默降级，不影响现有推送。
"""
import json
import logging
import re
import time
import urllib.request

import db

log = logging.getLogger("workbench")

import chatcmd  # noqa: E402  （chatcmd 不依赖本模块，无循环导入）

SEND_URL = "http://www.pushplus.plus/send"
ACCESSKEY_URL = "https://www.pushplus.plus/api/common/openApi/getAccessKey"
GETMSG_URL = "https://www.pushplus.plus/api/open/clawBot/getMsg"
BOTINFO_URL = "https://www.pushplus.plus/api/open/clawBot/botInfo"

# accessKey 缓存：{"key": str, "expires": float(epoch)}，进程内有效
_ak_cache = {"key": "", "expires": 0.0, "cool_until": 0.0}

# 微信 ClawBot 每下发 10 条要求用户主动发起一次对话 → 回复间隔与上限保护，
# 避免用户被动收到过多消息后被微信限制下发。
MIN_REPLY_GAP = 60.0        # 两次回复最小间隔（秒）
MAX_REPLIES_PER_DAY = 20    # 单日自动回复上限（查询类指令不占额度）
_reply_state = {"last": 0.0, "day": "", "count": 0}


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _credentials() -> tuple:
    return db.get_config("clawbot_token"), db.get_config("clawbot_secret")


def enabled() -> bool:
    tok, sec = _credentials()
    return bool(tok and sec)


def _access_key(force: bool = False) -> str:
    """换取并缓存 accessKey（2h 有效，提前 5 分钟刷新）。

    换取失败（限流/未授权）后冷却 5 分钟再试，避免每 10 秒的轮询硬撞接口限流。
    """
    now = time.time()
    if not force and _ak_cache.get("cool_until", 0) > now:
        return ""
    if not force and _ak_cache["key"] and _ak_cache["expires"] > now + 300:
        return _ak_cache["key"]
    tok, sec = _credentials()
    if not (tok and sec):
        return ""
    try:
        resp = _post_json(ACCESSKEY_URL, {"token": tok, "secretKey": sec})
    except Exception as e:
        log.warning("ClawBot accessKey 获取失败：%r", e)
        _ak_cache.update(key="", expires=0, cool_until=now + 300)
        return ""
    if resp.get("code") != 200:
        log.warning("ClawBot accessKey 接口返回：%s", resp)
        _ak_cache.update(key="", expires=0, cool_until=now + 300)
        return ""
    data = resp.get("data") or {}
    key = data.get("accessKey") or ""
    _ak_cache.update(key=key, expires=now + int(data.get("expiresIn") or 7200),
                     cool_until=0)
    return key


def bot_info() -> dict:
    """ClawBot 绑定详情（供设置页显示是否已绑定）。未配置/未绑定返回 {}。"""
    if not enabled():
        return {}
    ak = _access_key()
    if not ak:
        return {}
    try:
        resp = _get_json(BOTINFO_URL, headers={"access-key": ak})
    except Exception as e:
        log.warning("ClawBot botInfo 查询失败：%r", e)
        return {}
    return resp.get("data") if resp.get("code") == 200 else {}


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _can_reply(needs_quota: bool) -> bool:
    """查询类不占下发额度；写入类受间隔与单日上限保护。"""
    if _reply_state["day"] != _today():
        _reply_state.update(day=_today(), count=0, last=0.0)
    if not needs_quota:
        return True
    if time.time() - _reply_state["last"] < MIN_REPLY_GAP:
        return False
    return _reply_state["count"] < MAX_REPLIES_PER_DAY


def _mark_reply():
    _reply_state.update(last=time.time())
    if _reply_state["day"] != _today():
        _reply_state.update(day=_today(), count=0)
    _reply_state["count"] += 1


def _wechat_breaks(text: str) -> str:
    """微信把单个换行折叠掉：孤立 \n → 双换行（连续换行保持）。"""
    return re.sub(r"(?<!\n)\n(?!\n)", "\n\n", text)


def _send_txt(text: str, needs_quota: bool = True) -> bool:
    """经 ClawBot 渠道下发纯文本（模板必须 txt）。"""
    token, _sec = _credentials()
    if not token:
        return False
    if not _can_reply(needs_quota):
        return False
    text = _wechat_breaks(text)
    try:
        resp = _post_json(SEND_URL, {"token": token, "title": "叮咛",
                                     "content": text, "channel": "clawbot", "template": "txt"})
    except Exception as e:
        log.warning("ClawBot 发送失败：%r", e)
        return False
    if resp.get("code") != 200:
        log.warning("ClawBot 发送返回：%s", resp)
        return False
    _mark_reply()
    return True


def _fetch_messages() -> list:
    """拉取用户主动发给 ClawBot 的消息。返回 [{type, text}]。

    accessKey 被平台单点失效时 getMsg 返回 302「未登录」——立即强制换新 key
    重试一次，不等 2 小时缓存过期（否则期间消息一直拉不到）。
    """
    for force in (False, True):
        ak = _access_key(force=force)
        if not ak:
            return []
        try:
            resp = _get_json(GETMSG_URL, headers={"access-key": ak})
        except Exception as e:
            log.warning("ClawBot getMsg 失败：%r", e)
            return []
        if resp.get("code") == 302 and not force:
            log.warning("ClawBot accessKey 已失效，强制刷新重试")
            continue
        if resp.get("code") != 200:
            log.warning("ClawBot getMsg 返回：%s", resp)
            return []
        return resp.get("data") or []
    return []


def _is_query_only(text: str) -> bool:
    """查询类消息不占用下发额度（微信每下发约 10 条需主动对话一次）。"""
    t = text.strip()
    if t.lower() in chatcmd._HELP_WORDS:
        return True
    if chatcmd._TODO_WORDS.match(t):
        return True
    intent, _kind, _src = chatcmd.classify(t)
    return intent == "query"


def poll_once() -> int:
    """拉一次消息并逐条处理回执。返回处理条数（未配置时 0）。"""
    if not enabled():
        return 0
    msgs = _fetch_messages()
    n = 0
    for m in msgs:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        try:
            reply = chatcmd.handle(text)
        except Exception as e:
            log.warning("ClawBot 指令处理异常：%r", e)
            reply = f"处理出错了：{repr(e)[:60]}\n发「帮助」查看可用指令。"
        if _send_txt(reply, needs_quota=not _is_query_only(text)):
            n += 1
    return n


def test_send() -> bool:
    """设置页『发送测试消息』：直接下发一条，验证 token 与渠道可用。"""
    return _send_txt("叮咛微信通道已连通。发「帮助」查看可用指令。", needs_quota=False)
