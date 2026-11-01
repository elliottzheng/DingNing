# -*- coding: utf-8 -*-
"""systemone 测试：硅基流动 SystemOne 快速决策模型（未配置 key 时静默降级）。"""
import systemone


def _clear_key():
    import db
    db.set_config("siliconflow_key", "")


def test_disabled_without_key():
    _clear_key()
    assert systemone.enabled() is False
    assert systemone.noul("测试?", "state") is None
    assert systemone.choice("选一个", {"a": "甲"}, "state") is None
    assert systemone.score("打分", ["低", "高"], "state") is None
    assert systemone.classify_intent("9.28课表") == (None, None)
    assert systemone.test_call() is False


def test_model_alias_normalized():
    import db
    _clear_key()
    db.set_config("siliconflow_model", "Kev-4B")
    assert systemone._model() == "kev-4b"
    db.set_config("siliconflow_model", "djev")
    assert systemone._model() == "diffusiongemma"
    db.set_config("siliconflow_model", "SemIf")
    assert systemone._model() == "semif"
    db.set_config("siliconflow_model", "")
    assert systemone._model() == systemone.DEFAULT_MODEL
    db.set_config("siliconflow_model", "")


def test_noul_threshold_boundary_returns_none(monkeypatch):
    """概率正好卡在阈值 → 视为没主见，返回 None 让上层兜底。"""
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    monkeypatch.setattr(systemone, "_decide",
                        lambda state, q, timeout=20: {"q": {"type": "noul", "noul": 0.5}})
    assert systemone.noul("紧急吗?", "x", min_prob=0.5) is None


def test_noul_above_threshold(monkeypatch):
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    monkeypatch.setattr(systemone, "_decide",
                        lambda state, q, timeout=20: {"q": {"noul": 0.92}})
    assert systemone.noul("紧急吗?", "x") is True


def test_choice_unknown_key_rejected(monkeypatch):
    """模型返回不在选项里的 key → None（防止脏值流入业务）。"""
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    monkeypatch.setattr(systemone, "_decide",
                        lambda state, q, timeout=20: {"q": {"choice": "delete"}})
    assert systemone.choice("意图?", {"query": "查", "add": "增"}, "x") is None


def test_choice_valid(monkeypatch):
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    monkeypatch.setattr(systemone, "_decide",
                        lambda state, q, timeout=20: {"q": {"choice": "add"}})
    assert systemone.choice("意图?", {"query": "查", "add": "增"}, "x") == "add"


def test_score_index_and_label(monkeypatch):
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    monkeypatch.setattr(systemone, "_decide",
                        lambda state, q, timeout=20: {"q": {"score": 1}})
    assert systemone.score("多紧急?", ["低", "中", "高"], "x") == 1
    monkeypatch.setattr(systemone, "_decide",
                        lambda state, q, timeout=20: {"q": {"score": "高"}})
    assert systemone.score("多紧急?", ["低", "中", "高"], "x") == 2


def test_classify_intent_query_asks_kind(monkeypatch):
    """判为 query 时继续问一类：查待办还是查课表。"""
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    calls = []

    def fake_choice(question, options, state, **kw):
        calls.append(question)
        if "哪一类" in question:                 # 第二次问：子类
            return "todo"
        return "query"                           # 第一次问：意图

    monkeypatch.setattr(systemone, "choice", fake_choice)
    intent, kind = systemone.classify_intent("我的待办有哪些")
    assert intent == "query" and kind == "todo"
    assert len(calls) == 2                                   # 两次 choice：意图 + 子类


def test_classify_intent_add_skips_kind(monkeypatch):
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")
    calls = []
    monkeypatch.setattr(systemone, "choice",
                        lambda q, o, s, **kw: calls.append(q) or "add")
    intent, kind = systemone.classify_intent("买纸")
    assert intent == "add" and kind is None
    assert len(calls) == 1                                   # add 不再追问子类


def test_decide_network_error_returns_none(monkeypatch):
    """网络异常 → None，不抛（上层继续兜底）。"""
    _clear_key()
    import db
    db.set_config("siliconflow_key", "fake-key")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(systemone, "choice", boom)
    assert systemone.classify_intent("随便") == (None, None)


def test_chatcmd_no_longer_uses_systemone(client, monkeypatch):
    """SystemOne 已移除：意图分类不再调用它（直接 DeepSeek → 规则）。"""
    called = []
    monkeypatch.setattr("systemone.classify_intent",
                        lambda text: called.append("systemone") or ("query", "schedule"))
    monkeypatch.setattr("ai_parse.ai_chat",
                        lambda *a, **k: called.append("deepseek") or '{"intent":"add"}')
    intent, kind, src = __import__("chatcmd").classify("9.28课表")
    assert called == ["deepseek"]                            # 只问 DeepSeek
    assert intent == "add" and src == "deepseek"


def test_calendar_prompt_puts_补班_in_event_criteria(monkeypatch):
    """提示词必须把'补班/调休上班'写进 event 的正面描述。

    历史教训：早先排除条件写在 holiday 的括号里（"不含补班"），模型忽略它，
    把"国庆补班"判成 holiday → 课表错误显示停课。改用正面描述后不靠后处理兜底。
    """
    import db
    db.set_config("siliconflow_key", "fake-key")
    captured = {}

    def fake_decide(state, questions, timeout=20):
        captured.update(state=state, questions=questions)
        return {"q": {"choice": "event", "confidence": 0.9}}

    monkeypatch.setattr(systemone, "_decide", fake_decide)
    import excel_import
    out = excel_import._classify_ai(["国庆补班"])
    assert out == {"国庆补班": "event"}
    crit = captured["questions"]["q"]["criteria"]
    assert "补班" in crit["event"] and "调休上班" in crit["event"]
    assert "不用到校" in crit["holiday"]                  # holiday 只讲放假本身


def test_calendar_classify_rejects_low_confidence(monkeypatch):
    """模型自报把握低 → 该标题不进结果（调用方继续用规则的 event）。"""
    import db
    db.set_config("siliconflow_key", "fake-key")
    monkeypatch.setattr(systemone, "_decide",
                        lambda s, q, timeout=20: {"q": {"choice": "holiday", "confidence": 0.1}})
    import excel_import
    assert excel_import._classify_ai(["某个说不清的事项"]) == {}


def test_calendar_classify_ai_only_for_rule_unknown(client, monkeypatch):
    """规则已认出的（假期/考试）不问 AI——避免无谓计费。"""
    import db
    db.set_config("siliconflow_key", "")
    import excel_import
    calls = []
    monkeypatch.setattr(excel_import, "_classify_ai",
                        lambda titles: calls.append(titles) or {})
    out = excel_import.classify_titles(["国庆假", "期中考试", "社团活动"])
    assert out == {"国庆假": "holiday", "期中考试": "exam", "社团活动": "event"}
    assert calls == [["社团活动"]]                     # 只问了规则没认出的那条


def test_default_model_is_semif():
    """默认模型必须是 semif。

    实测三模型差异（2026-09-25，23 条意图 + 8 条校历）：
      semif          意图 96% / 校历 100% / 0.37s / 会自报低把握
      kev-4b         意图 83% / 校历  75% / 0.54s
      diffusiongemma 意图 78% / 校历  75% / 0.76s / 从不降级（错了也不知道）
    diffusiongemma "永远自信"在降级链路里比低准确率更危险，故锁死默认值。
    """
    assert systemone.DEFAULT_MODEL == "semif"


def test_intent_prompt_lists_examples_not_abstract_criteria():
    """意图提示词必须列举典型输入。

    教训：改写成"query=不含写入动作"这类抽象判据后，短句/无动词句
    （"明天""下周二的会几点"）被系统性判成 add，准确率 96%→78%。
    """
    q = systemone._INTENT_OPTIONS["query"]
    a = systemone._INTENT_OPTIONS["add"]
    assert "课表" in q and "明天" in q                 # query 侧有短句例子
    assert "周五前交成绩册" in a                        # add 侧有典型待办例子
