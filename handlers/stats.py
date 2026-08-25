"""
比赛数据命令：stats / stats <match_id>（AstrBot 适配版）
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..data_manager import data_manager
from ..data_source import hltv_data
from ..image_utils import image_segment
from ..permissions import is_group_enabled
from ..render import render_stats

# 命令正则（供 main.py 的 filter.regex 注册使用）
STATS_RE = r"^(?:stats|比赛数据|数据)(?:\s+(.*))?\s*$"

_STATS_PATTERN = re.compile(STATS_RE)


async def handle_stats(plugin, event):
    """stats [match_id]：查看比赛详细数据（图片）"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    match = _STATS_PATTERN.match(event.get_message_str().strip())
    match_id = (match.group(1) if match and match.group(1) else "").strip()
    subscriptions = data_manager.get_subscribed_events(group_id)

    if not match_id:
        # 获取最新比赛数据
        if not subscriptions:
            yield event.plain_result("请先订阅赛事，或提供比赛ID\n例如：stats 2370931")
            event.stop_event()
            return

        yield event.plain_result("正在获取最新比赛数据...")

        try:
            for sub in subscriptions:
                stats = await hltv_data.get_latest_result_with_stats(sub.event_id, sub.event_title)
                if stats:
                    img = await render_stats(stats)
                    yield event.image_result(image_segment(img))
                    event.stop_event()
                    return

            yield event.plain_result("暂无比赛数据")
            event.stop_event()
        except Exception as e:
            logger.error(f"获取比赛数据失败: {e}")
            yield event.plain_result("获取比赛数据失败，HLTV 可能暂时无法访问")
            event.stop_event()

    else:
        # 获取指定比赛数据
        yield event.plain_result(f"正在获取比赛 #{match_id} 的数据...")

        try:
            # 优先直接按 match_id 拉取，避免先遍历所有订阅赛事 results 带来的额外请求开销
            stats = await hltv_data.get_match_stats(match_id=match_id)

            # 直连失败时，再尝试通过订阅赛事补全 slug 信息后重试（兼容少量边缘路由）
            if not stats and subscriptions:
                team1 = ""
                team2 = ""
                event_title = ""

                for sub in subscriptions:
                    results = await hltv_data.get_event_results(sub.event_id, max_results=10)
                    for r in results:
                        if r.id == match_id:
                            team1 = r.team1
                            team2 = r.team2
                            event_title = sub.event_title
                            break
                    if team1:
                        break

                stats = await hltv_data.get_match_stats(
                    match_id=match_id,
                    team1=team1,
                    team2=team2,
                    event_title=event_title,
                )

            if stats:
                img = await render_stats(stats)
                yield event.image_result(image_segment(img))
                event.stop_event()
            else:
                yield event.plain_result(f"无法获取比赛 #{match_id} 的数据")
                event.stop_event()
        except Exception as e:
            logger.error(f"获取比赛数据失败: {e}")
            yield event.plain_result("获取比赛数据失败，HLTV 可能暂时无法访问")
            event.stop_event()
