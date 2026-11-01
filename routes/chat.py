# -*- coding: utf-8 -*-
"""网页快速输入框统一入口：与微信 ClawBot 同一条 chatcmd 链路。

一句话 = 记待办/日程、查课表/整周、搜事件、撤销——
行为与微信端完全一致（同一 handle、同一校验、同一整批拒绝语义）。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import chatcmd

router = APIRouter()


class ChatIn(BaseModel):
    text: str


@router.post("/api/chat")
def api_chat(b: ChatIn):
    text = b.text.strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    return {"reply": chatcmd.handle(text, source="web")}
