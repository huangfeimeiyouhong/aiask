#!/usr/bin/env python3
"""脱离当前会话（setsid）后启动 supervisor，使其不受本 agent 后台任务生命周期影响。"""
import os
import sys

PY = "/Users/phil/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
SUP = "/Users/phil/WorkBuddy/2026-07-16-11-31-47/ai_qa_system/supervisor.py"

os.setsid()  # 新建会话，脱离控制终端与本工具进程组
os.execv(PY, [PY, "-u", SUP])
