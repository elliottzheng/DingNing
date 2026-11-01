# -*- coding: utf-8 -*-
"""clawbot 测试：凭据缺失时静默降级；消息处理与回复节流（全部 mock 网络）。"""
import time

import clawbot
import db


def _clear_creds():
    db.set_config("clawbot_token", "")
    db.set_config("clawbot_secret", "")


def test_disabled_without_credentials():
    _clear_creds()
    assert clawbot.enabled() is False
    assert clawbot.poll_once() == 0


def test_access_key_empty_without_credentials():
    _clear_creds()
    clawbot._ak_cache.update(key="", expires=0.0)
    assert clawbot._access_key() == ""


def test_bot_info_empty_when_disabled():
    _clear_creds()
    assert clawbot.bot_info() == {}


def test_test_send_false_without_credentials():
    _clear_creds()
    assert clawbot.test_send() is False


def test_send_throttled_within_gap(monkeypatch):
    """写入类回复受最小间隔保护：刚回一条 → 立刻再回被拒。"""
    _clear_creds()
    db.set_config("clawbot_token", "fake-token")
    db.set_config("clawbot_secret", "fake-secret")
    calls = {"n": 0}

    def fake_post(url, body, headers=None, timeout=25):
        calls["n"] += 1
        return {"code": 200}

    monkeypatch.setattr(clawbot, "_post_json", fake_post)
    clawbot._reply_state.update(last=0.0, day=clawbot._today(), count=0)   # 干净起点
    assert clawbot._send_txt("第一条") is True
    assert calls["n"] == 1
    assert clawbot._send_txt("第二条（应被间隔拦下）") is False
    assert calls["n"] == 1
    # 查询类不占额度，不受间隔限制
    assert clawbot._send_txt("查询回复", needs_quota=False) is True
    assert calls["n"] == 2


def test_daily_reply_cap(monkeypatch):
    """单日写入类回复达到上限后拒绝。"""
    _clear_creds()
    db.set_config("clawbot_token", "fake-token")
    db.set_config("clawbot_secret", "fake-secret")
    monkeypatch.setattr(clawbot, "_post_json", lambda *a, **k: {"code": 200})
    clawbot._reply_state.update(last=0.0, day=clawbot._today(), count=clawbot.MAX_REPLIES_PER_DAY)
    assert clawbot._send_txt("超出上限") is False


def test_poll_processes_messages(monkeypatch):
    """拉到的消息逐条经 chatcmd 处理并回执；计数正确。"""
    _clear_creds()
    db.set_config("clawbot_token", "fake-token")
    db.set_config("clawbot_secret", "fake-secret")
    monkeypatch.setattr(clawbot, "_fetch_messages",
                        lambda: [{"type": 1, "text": "帮助"}, {"type": 1, "text": ""}])
    sent = []
    monkeypatch.setattr(clawbot, "_send_txt", lambda text, needs_quota=True: sent.append(text) or True)
    assert clawbot.poll_once() == 1                       # 空文本跳过
    assert "课表" in sent[0]


def test_poll_handler_exception_gets_error_reply(monkeypatch):
    """指令处理抛异常 → 回错误提示，不让整个轮询崩掉。"""
    _clear_creds()
    db.set_config("clawbot_token", "fake-token")
    db.set_config("clawbot_secret", "fake-secret")
    monkeypatch.setattr(clawbot, "_fetch_messages", lambda: [{"type": 1, "text": "whatever"}])
    sent = []

    def boom(text):
        raise RuntimeError("boom")

    monkeypatch.setattr("chatcmd.handle", boom)
    monkeypatch.setattr(clawbot, "_send_txt", lambda text, needs_quota=True: sent.append(text) or True)
    clawbot.poll_once()
    assert "处理出错" in sent[0]


def test_query_detection():
    assert clawbot._is_query_only("帮助") is True
    assert clawbot._is_query_only("待办") is True
    assert clawbot._is_query_only("课表 明天") is True
    assert clawbot._is_query_only("明天") is True
    assert clawbot._is_query_only("周五前交成绩册") is False
    assert clawbot._is_query_only("周三下午3点开会") is False


def test_scheduler_includes_clawbot_poll(client):
    """后台调度器挂了 clawbot_poll 定时任务（未配置凭据时静默跳过）。"""
    import mailer
    sched = mailer.setup_scheduler()
    try:
        assert sched.get_job("clawbot_poll") is not None
    finally:
        sched.shutdown(wait=False)


def test_poll_job_noop_without_credentials():
    """调度入口在无凭据时不报错。"""
    import mailer
    _clear_creds()
    mailer._clawbot_poll()                              # 不抛异常即通过


def test_send_txt_normalizes_single_newlines(monkeypatch):
    """微信折叠单换行：发送前孤立 \n → \n\n；已有 \n\n 不变。"""
    _clear_creds()
    db.set_config("clawbot_token", "fake-token")
    db.set_config("clawbot_secret", "fake-secret")
    monkeypatch.setattr(clawbot, "_post_json", lambda url, body, headers=None, timeout=25: {"code": 200})
    clawbot._reply_state.update(last=0.0, day=clawbot._today(), count=0)
    assert clawbot._send_txt("第一行\n第二行\n\n第三行") is True
    body = None
    # 取最后一次 _post_json 的 body
    captured = {}
    orig = clawbot._post_json
    def cap(url, b, headers=None, timeout=25):
        captured.update(b)
        return {"code": 200}
    monkeypatch.setattr(clawbot, "_post_json", cap)
    clawbot._reply_state.update(last=0.0, count=0)
    clawbot._send_txt("A\nB\n\nC\nD")
    assert captured["content"] == "A\n\nB\n\nC\n\nD"
