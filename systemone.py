# -*- coding: utf-8 -*-
"""System One 快速决策模型（硅基流动 Jev 平替：Kev-4b / SemIf / DiffusionGemma）。

专做"一个明确判断"，不生成文字：比通用大模型更快更省，适合意图分类这种
只需要结论的场景。官方接口：

  POST https://api.siliconflow.cn/v1/systemone
  {"model": "semif", "state": <任意 JSON>, "questions": {<key>: {<问题定义>}}}
  → {"answers": {<key>: {...}}, "usage": {...}}

三类问题：
  noul    是/否判断，返回概率（0~1）
  choice  单选，返回选中的 key
  score   等级评分，返回等级索引

凭据：config `siliconflow_key`（未配置 → 全部函数静默降级返回 None）。
免费期：2026-10-08 前免费（官方公告），到期后按量计费——所以必须有多级兜底。
"""
import json
import logging
import urllib.request

import db

log = logging.getLogger("workbench")

API_URL = "https://api.siliconflow.cn/v1/systemone"
DEFAULT_MODEL = "semif"          # 官方 JevBench 实测：准确率最接近 Jev 官方（79.22%），延迟低约 67%

# 模型名规范化：配置里写 "Kev-4b" / "kev-4b" 都能用
_MODEL_ALIAS = {
    "kev": "kev-4b", "kev-4b": "kev-4b", "kev4b": "kev-4b",
    "semif": "semif",
    "diffusiongemma": "diffusiongemma", "djev": "diffusiongemma",
}


def _model() -> str:
    raw = (db.get_config("siliconflow_model") or DEFAULT_MODEL).strip().lower()
    return _MODEL_ALIAS.get(raw, raw or DEFAULT_MODEL)


def enabled() -> bool:
    return bool(db.get_config("siliconflow_key"))


def _decide(state, questions: dict, timeout: int = 20):
    """调 SystemOne。返回 answers dict，失败/未配置返回 None。"""
    key = db.get_config("siliconflow_key")
    if not key:
        return None
    body = json.dumps({"model": _model(), "state": state, "questions": questions},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.warning("SystemOne 调用失败：%r", repr(e)[:120])
        return None
    answers = resp.get("answers")
    if not isinstance(answers, dict):
        log.warning("SystemOne 返回异常：%s", str(resp)[:200])
        return None
    return answers


def noul(question: str, state, min_prob: float = 0.5) -> bool | None:
    """是/否判断。返回 True/False；概率低于 min_prob 或调用失败返回 None（交由上层兜底）。"""
    ans = _decide(state, {"q": {"type": "noul", "instructions": question}})
    if not ans:
        return None
    item = ans.get("q") or {}
    p = item.get("noul")
    if not isinstance(p, (int, float)):
        return None
    if abs(p - min_prob) < 1e-9:        # 正好卡在阈值上 → 没主见，交给下一级
        return None
    return p >= min_prob


def choice(question: str, options: dict, state, min_confidence: float = 0.15) -> str | None:
    """单选。options={key: 描述}。返回选中的 key；失败返回 None。

    min_confidence：模型自报把握低于该值 → 返回 None（没主见，交上层兜底）。
    实测单字"待办"判意图时 confidence 仅 0.185 且判错，加门槛可挡掉这类低把握判断。
    """
    if not options:
        return None
    ans = _decide(state, {"q": {"type": "choice", "instructions": question,
                                "criteria": options}})
    if not ans:
        return None
    item = ans.get("q") or {}
    picked = item.get("choice")
    if picked not in options:
        return None
    conf = item.get("confidence")
    if isinstance(conf, (int, float)) and conf < min_confidence:
        return None
    return picked


def score(question: str, levels: list, state) -> int | None:
    """等级评分。levels=[最低..最高]。返回下标；失败返回 None。"""
    if not levels:
        return None
    ans = _decide(state, {"q": {"type": "score", "instructions": question,
                                "criteria": levels}})
    if not ans:
        return None
    s = (ans.get("q") or {}).get("score")
    if isinstance(s, int) and 0 <= s < len(levels):
        return s
    if isinstance(s, str):
        try:
            i = levels.index(s)
            return i
        except ValueError:
            return None
    return None


# ---------------- 业务：微信消息意图分类 ----------------

_INTENT_QUESTION = "用户发给叮咛的这条微信，是想查询已有信息，还是想新增一条待办/日程？"
# 用例子而非抽象判据。试过改写成"query=不含写入动作，add=想把一件事记进工作台"的
# 判据式描述，结果短句/无动词句（"明天""下周二的会几点"）被系统性判成 add，
# 准确率从 96% 掉到 78%——模型对"陈述一件事"的泛化过头。列举典型输入更稳。
_INTENT_OPTIONS = {
    "query": "查询：查看课表、日程、待办清单、要帮助。如'课表''明天''我的待办有哪些'"
             "'看看下周三的课表'。用户没有要新增任何东西。",
    "add": "新增：记录一条待办或日程（含提醒、截止、会议、活动等要记住或要发生的事）。"
           "如'周五前交成绩册''周三下午3点教研会''明天带U盘'。",
}
# query 内部再分：查待办还是查课表
_QUERY_KIND_QUESTION = "用户想查询的是哪一类信息？"
_QUERY_KIND_OPTIONS = {
    "todo": "待办清单：未完成的任务、截止事项。",
    "schedule": "某一天的课表与日程安排。",
}


def classify_intent(text: str) -> tuple:
    """SystemOne 判意图。返回 (intent, query_kind)。

    intent ∈ {'query','add',None}；None=模型不可用/没主见 → 上层走下一级兜底。
    query_kind 仅 intent=='query' 时可能非空（'todo'/'schedule'/None）。
    任何异常都吞掉返回 (None, None)——本模块是可选加速层，绝不因它中断业务。
    """
    try:
        if not enabled():
            return None, None
        # 意图是二选一，模型自报把握低于 0.25 视为没主见（实测"待办"单字
        # confidence 仅 0.185 且判错；概率 0.41/0.59 接近时不值得信）
        intent = choice(_INTENT_QUESTION, _INTENT_OPTIONS, text, min_confidence=0.25)
        if intent is None:
            return None, None
        if intent == "query":
            return intent, choice(_QUERY_KIND_QUESTION, _QUERY_KIND_OPTIONS, text)
        return intent, None
    except Exception as e:
        log.warning("SystemOne 意图分类异常：%r", repr(e)[:120])
        return None, None


def test_call() -> bool:
    """设置页『测试连通』：用一个固定判断验证 key 与模型可用。"""
    return noul("这是一条测试消息吗？", "叮咛连通性测试") is not None
