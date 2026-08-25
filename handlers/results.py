"""
结果列表命令：results列表（AstrBot 适配版）
"""

from __future__ import annotations

from astrbot.api import logger

from ..data_manager import data_manager
from ..data_source import hltv_data
from ..image_utils import image_segment
from ..permissions import is_group_enabled
from ..render import render_results

# 命令正则（供 main.py 的 filter.regex 注册使用）
RESULTS_LIST_RE = r"^(?:results列表|结果列表|results)\s*$"


async def handle_results_list(plugin, event):
    """results列表：聚合查看已订阅赛事的最近结果"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    subscriptions = data_manager.get_subscribed_events(group_id)
    if not subscriptions:
        yield event.plain_result("请先订阅赛事\n使用 event列表 查看可订阅的赛事")
        event.stop_event()
        return

    yield event.plain_result("正在获取比赛结果，请稍候...")

    try:
        results_by_event = {}

        for sub in sorted(subscriptions, key=lambda x: int(x.event_id) if x.event_id.isdigit() else x.event_id):
            results = await hltv_data.get_event_results(sub.event_id)
            if results:
                event_key = f"#{sub.event_id} {sub.event_title}"
                results_by_event[event_key] = results

        if not results_by_event:
            yield event.plain_result("暂无比赛结果")
            event.stop_event()
            return

        img = await render_results(results_by_event)
        yield event.image_result(image_segment(img))
        event.stop_event()
    except Exception as e:
        logger.error(f"获取比赛结果失败: {e}")
        yield event.plain_result("获取比赛结果失败，HLTV 可能暂时无法访问")
        event.stop_event()
