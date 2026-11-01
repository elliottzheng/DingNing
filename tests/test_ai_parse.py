# -*- coding: utf-8 -*-
"""ai_parse 测试：mock 网络层，验证请求体与响应解析。"""
import json

import ai_parse
import db


class FakeResp:
    """模拟 urlopen 返回的上下文管理器。"""
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _with_fake_key(monkeypatch):
    """进度：测试自带假 key，不依赖部署环境配置。"""
    orig = db.get_config
    monkeypatch.setattr(db, "get_config",
                        lambda k, d="": "test-key" if k == "ai_api_key" else orig(k, d))


def test_ai_parse_success(monkeypatch):
    _with_fake_key(monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp({"choices": [{"message": {"content": json.dumps({
            "type": "event", "title": "机房教研会议", "date": "2026-09-16",
            "start_time": "15:00", "end_time": "16:00", "location": "机房",
            "note": "", "priority": 1, "due": None}, ensure_ascii=False)}}]})

    monkeypatch.setattr(ai_parse.urllib.request, "urlopen", fake_urlopen)
    p = ai_parse.ai_parse("周三下午3点机房教研会议", __import__("datetime").date(2026, 9, 14))
    assert p is not None
    assert p["type"] == "event" and p["start_time"] == "15:00" and p["location"] == "机房"
    # 请求体约束：关闭思考（v4-flash 默认思考会耗尽 token）、JSON 输出
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["auth"] == f"Bearer {db.get_config('ai_api_key')}"


def test_ai_parse_empty_content_returns_none(monkeypatch):
    monkeypatch.setattr(ai_parse.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp({"choices": [{"message": {}}]}))
    assert ai_parse.ai_parse("任意输入", __import__("datetime").date(2026, 9, 14)) is None


def test_ai_chat_returns_text(monkeypatch):
    _with_fake_key(monkeypatch)
    monkeypatch.setattr(ai_parse.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(
                            {"choices": [{"message": {"content": "整理好的文本"}}]}))
    assert ai_parse.ai_chat("系统提示", "用户内容") == "整理好的文本"


def test_quick_add_falls_back_to_rules_when_ai_fails(client, monkeypatch):
    """AI 挂掉时 use_ai=True 自动回退规则解析，不报错。"""
    monkeypatch.setattr(ai_parse, "ai_parse", lambda *a, **k: None)
    r = client.post("/api/quick_add", json={"text": "周五前交成绩册", "use_ai": True})
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "todo" and data["source"] == "rule"
