"""图片工具 - 替代原 NoneBot 项目中的 utils.image_utils.image_segment

AstrBot 通过本地图片文件路径发送图片（Image(file=...)），因此这里把图片
字节写入插件数据目录下的 images/ 临时目录，返回文件路径。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

_images_dir: str | None = None
"""图片临时目录（由插件入口初始化）"""


def init_images_dir(path: str) -> None:
    """初始化图片临时目录（应指向插件数据目录下的 images/）"""
    global _images_dir
    _images_dir = path
    Path(path).mkdir(parents=True, exist_ok=True)


def _ensure_dir() -> str:
    global _images_dir
    if _images_dir is None:
        _images_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "hltv_sub_images")
        Path(_images_dir).mkdir(parents=True, exist_ok=True)
    return _images_dir


def image_segment(img_bytes: bytes) -> str:
    """将图片字节保存为本地 PNG 文件，返回文件路径（供 astrbot 图片消息段使用）"""
    path = os.path.join(_ensure_dir(), f"{uuid.uuid4().hex}.png")
    with open(path, "wb") as f:
        f.write(img_bytes)
    return path


def cleanup_images(ttl_seconds: int = 24 * 3600) -> int:
    """清理超过 TTL 的临时图片文件，返回清理数量（启动时调用）"""
    if _images_dir is None or not os.path.isdir(_images_dir):
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for name in os.listdir(_images_dir):
        if not name.endswith(".png"):
            continue
        full = os.path.join(_images_dir, name)
        try:
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
                removed += 1
        except OSError:
            pass
    return removed
