"""
管理命令：hltv开启 / hltv关闭 / hltv启用 / hltv禁用（AstrBot 适配版）
"""

from __future__ import annotations

import re

from ..data_manager import data_manager
from ..permissions import check_permission
from ..scheduler import hltv_scheduler

# 命令正则（供 main.py 的 filter.regex 注册使用）
HLTV_TOGGLE_RE = r"^hltv(开启|关闭|启用|禁用)\s*$"

_TOGGLE_PATTERN = re.compile(HLTV_TOGGLE_RE)


async def handle_hltv_toggle(plugin, event):
    """hltv开启/关闭：本群功能开关（需要管理员权限）"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0
    platform_id = event.get_platform_id()

    if not await check_permission(plugin, event):
        yield event.plain_result("❌ 需要管理员权限")
        event.stop_event()
        return

    match = _TOGGLE_PATTERN.match(event.get_message_str().strip())
    action = match.group(1) if match else "开启"

    if "开启" in action or "启用" in action:
        data_manager.set_enabled(group_id, True, platform_id=platform_id)
        hltv_scheduler.ensure_job_state()
        hltv_scheduler.refresh_wakeup_jobs()
        yield event.plain_result("✅ HLTV 订阅功能已开启")
        event.stop_event()
    else:
        data_manager.set_enabled(group_id, False, platform_id=platform_id)
        hltv_scheduler.ensure_job_state()
        hltv_scheduler.refresh_wakeup_jobs()
        yield event.plain_result("❌ HLTV 订阅功能已关闭")
        event.stop_event()
