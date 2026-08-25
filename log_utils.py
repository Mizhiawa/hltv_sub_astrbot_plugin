"""日志工具 - 替代原 NoneBot 项目中的 utils.tools.get_logger

AstrBot 的 astrbot.api.logger 会根据调用方模块自动路由到对应插件的 logger，
因此这里直接返回该对象即可。
"""

from __future__ import annotations

from astrbot.api import logger

__all__ = ["get_logger", "logger"]


def get_logger(name: str):
    """获取日志对象（AstrBot 自动按调用位置路由到插件 logger）"""
    return logger
