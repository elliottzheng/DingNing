# -*- coding: utf-8 -*-
"""路由聚合：server.py 只需 include 此包。"""
from fastapi import APIRouter

from . import admin, chat, events, schedule, todos

api_router = APIRouter()
api_router.include_router(schedule.router)
api_router.include_router(todos.router)
api_router.include_router(events.router)
api_router.include_router(admin.router)
api_router.include_router(chat.router)
