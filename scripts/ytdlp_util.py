#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt-dlp 调用的统一解析（本机 yt-dlp 装在该 venv 里但不在 PATH 上）。

优先级：
  1) 显式传入的路径
  2) PATH 上的 yt-dlp
  3) 当前解释器同目录（Scripts/）下的 yt-dlp.exe
  4) `python -m yt_dlp`（模块调用，最通用）
返回形如 ["<exe>", ...] 或 ["<python>", "-m", "yt_dlp", ...] 的**前缀列表**。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def ytdlp_cmd(explicit: str = "") -> list[str] | None:
    if explicit:
        return [explicit]
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    # 与当前解释器同级的 Scripts 目录
    exe_dir = Path(sys.executable).parent
    for name in ("yt-dlp.exe", "yt-dlp"):
        cand = exe_dir / name
        if cand.exists():
            return [str(cand)]
    # 退路：模块调用（需已 pip install yt-dlp）
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None
