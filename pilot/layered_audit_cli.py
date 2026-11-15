"""控制台入口：复刻各引擎脚本自身的 sys.path 设置，不改动任何引擎。

引擎之间用扁平兄弟导入（import oracle_bridge），直接跑脚本时由脚本自己把
engine 目录插进 sys.path。作为安装包调用时需要同样的设置，故有此薄壳。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from audit_entry import main  # noqa: E402

__all__ = ["main"]
