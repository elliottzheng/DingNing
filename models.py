# -*- coding: utf-8 -*-
"""API 请求模型（Pydantic）。"""
from pydantic import BaseModel


class TodoIn(BaseModel):
    title: str
    due: str = ""
    priority: int = 1
    note: str = ""
    done: bool = False
    remind_min: int | None = None      # 距截止提前提醒分钟；None=不提醒


class TodoPatch(BaseModel):
    title: str | None = None
    due: str | None = None
    priority: int | None = None
    note: str | None = None
    done: bool | None = None
    remind_min: int | None = None      # -1 = 清除提醒


class EventIn(BaseModel):
    title: str
    date: str
    start_time: str = ""
    end_time: str = ""
    location: str = ""
    note: str = ""
    remind_min: int | None = None      # 开始前提前提醒分钟；None=不提醒


class EventPatch(BaseModel):
    title: str | None = None
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None
    note: str | None = None
    remind_min: int | None = None      # -1 = 清除提醒


class QuickAdd(BaseModel):
    text: str
    use_ai: bool = True


class LessonCancelIn(BaseModel):
    date: str
    weekday: int
    section: str
    note: str = ""


class LessonMoveIn(BaseModel):
    date: str            # 原课日期
    weekday: int         # 原课星期（含调休换算）
    section: str         # 原课节次
    new_date: str        # 换到哪一天
    new_start: str
    new_end: str
    note: str = ""




class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


class RegisterIn(BaseModel):
    username: str
    password: str
    invite_code: str = ""
