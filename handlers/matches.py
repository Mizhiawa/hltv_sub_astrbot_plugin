"""
比赛列表命令：matches列表（AstrBot 适配版）
"""

from __future__ import annotations

from astrbot.api import logger

from ..data_manager import data_manager
from ..data_source import hltv_data, paused_message
from ..image_utils import image_segment
from ..permissions import is_group_enabled
from ..render import render_matches

# 命令正则（供 main.py 的 filter.regex 注册使用）
MATCHES_LIST_RE = r"^(?:matches列表|比赛列表|matches)\s*$"


async def handle_matches_list(plugin, event):
    """matches列表：聚合查看已订阅赛事的对阵信息"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    subscriptions = data_manager.get_subscribed_events(group_id)
    if not subscriptions:
        yield event.plain_result("请先订阅赛事\n使用 event列表 查看可订阅的赛事")
        event.stop_event()
        return

    yield event.plain_result("正在获取比赛列表，请稍候...")

    try:
        matches_by_event = {}
        live_count = 0
        upcoming_count = 0

        for sub in sorted(subscriptions, key=lambda x: int(x.event_id) if x.event_id.isdigit() else x.event_id):
            matches = await hltv_data.get_event_matches_for_display(sub.event_id)

            if matches:
                event_key = f"#{sub.event_id} {sub.event_title}"
                matches_by_event[event_key] = matches
                for m in matches:
                    if m.is_live:
                        live_count += 1
                    else:
                        upcoming_count += 1

        if not matches_by_event:
            yield event.plain_result(paused_message() or "暂无比赛")
            event.stop_event()
            return

        img = await render_matches(matches_by_event, live_count, upcoming_count)
        yield event.image_result(image_segment(img))
        event.stop_event()
    except Exception as e:
        logger.error(f"获取比赛列表失败: {e}")
        yield event.plain_result("获取比赛列表失败，HLTV 可能暂时无法访问")
        event.stop_event()
