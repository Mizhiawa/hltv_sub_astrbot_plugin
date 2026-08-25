"""
HLTV 权限与开关辅助函数（AstrBot 适配版）

与原 NoneBot 版语义一致：
- check_permission：超级用户（AstrBot admins_id + 插件 hltv_superusers）或群主/管理员
- is_group_enabled：群组是否启用插件
"""

from __future__ import annotations

from .config import get_config
from .data_manager import data_manager


def is_group_enabled(group_id: int) -> bool:
    """检查群组是否启用插件"""
    return data_manager.is_enabled(group_id)


def get_superusers(plugin) -> set[str]:
    """获取超级用户 ID 集合：插件配置 hltv_superusers + AstrBot 全局 admins_id"""
    result: set[str] = set()
    try:
        for uid in get_config().get("hltv_superusers", []) or []:
            result.add(str(uid))
    except Exception:
        pass

    try:
        admins = plugin.context.get_config().get("admins_id", []) or []
        for uid in admins:
            result.add(str(uid))
    except Exception:
        pass

    return result


def check_superuser(plugin, event) -> bool:
    """检查消息发送者是否为超级用户"""
    return event.get_sender_id() in get_superusers(plugin)


async def check_permission(plugin, event) -> bool:
    """检查权限：超级用户、群主或管理员（与原 NoneBot 版行为一致）"""
    if check_superuser(plugin, event):
        return True

    group_id = event.get_group_id()
    user_id = event.get_sender_id()
    if not group_id or not user_id:
        return False

    # aiocqhttp(OneBot v11)：查询群成员角色，与原实现完全一致
    bot = getattr(event, "bot", None)
    if bot is not None and hasattr(bot, "call_action"):
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
            )
            return info.get("role") in ("owner", "admin")
        except Exception:
            return False

    # 其他平台：回退到事件自带的管理员标记
    return event.is_admin()
