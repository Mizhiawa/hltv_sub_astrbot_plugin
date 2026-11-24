"""日志兼容层：在 AstrBot 运行时使用 astrbot.api.logger，独立单测时回退标准 logging。"""

from __future__ import annotations

import logging

try:
    from astrbot.api import logger  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    logger = logging.getLogger("astrbot_plugin_hltv_cs")


def get_exc_desc(exc: Exception) -> str:
    """获取异常的简短描述。"""
    return f"{type(exc).__name__}: {exc}"
