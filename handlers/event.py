"""
赛事相关命令：event列表 / event订阅 / event取消订阅 / 我的订阅（AstrBot 适配版）

处理函数为 async generator：每次 yield 一条回复（等价于原 NoneBot 版的
send/finish），末尾调用 event.stop_event() 阻断后续 LLM 处理。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..data_manager import EventSubscription, data_manager
from ..data_source import hltv_data, paused_message
from ..image_utils import image_segment
from ..permissions import check_permission, is_group_enabled
from ..render import render_events

# 命令正则（供 main.py 的 filter.regex 注册使用）
EVENT_LIST_RE = r"^(?:event列表|赛事列表|events)\s*$"
EVENT_SUBSCRIBE_RE = r"^(?:event订阅|订阅赛事|subscribe)(?:\s+(.*))?\s*$"
EVENT_UNSUBSCRIBE_RE = r"^(?:event取消订阅|取消订阅赛事|unsubscribe)(?:\s+(.*))?\s*$"
MY_SUBSCRIPTIONS_RE = r"^(?:我的订阅|订阅列表|mysub)\s*$"

_SUBSCRIBE_PATTERN = re.compile(EVENT_SUBSCRIBE_RE)
_UNSUBSCRIBE_PATTERN = re.compile(EVENT_UNSUBSCRIBE_RE)


async def handle_event_list(plugin, event):
    """event列表：查看近期大型赛事"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    yield event.plain_result("正在获取赛事列表，请稍候...")

    try:
        events = await hltv_data.get_big_events()

        if not events:
            yield event.plain_result(paused_message() or "暂无赛事数据")
            event.stop_event()
            return

        ongoing = [e for e in events if e.is_ongoing]
        upcoming = [e for e in events if not e.is_ongoing]

        subscribed_ids = data_manager.get_subscribed_event_ids(group_id)

        img = await render_events(ongoing, upcoming, subscribed_ids)
        yield event.image_result(image_segment(img))
        event.stop_event()
    except Exception as e:
        logger.error(f"获取赛事列表失败: {e}")
        yield event.plain_result("获取赛事列表失败，HLTV 可能暂时无法访问")
        event.stop_event()


async def handle_event_subscribe(plugin, event):
    """event订阅 [ID]：订阅赛事（需要管理员权限）"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    if not await check_permission(plugin, event):
        yield event.plain_result("❌ 只有群主或管理员可以订阅赛事")
        event.stop_event()
        return

    match = _SUBSCRIBE_PATTERN.match(event.get_message_str().strip())
    event_id = (match.group(1) if match and match.group(1) else "").strip()
    if not event_id:
        yield event.plain_result("请提供赛事ID，例如：event订阅 7148")
        event.stop_event()
        return

    # 全局同步多订阅：若该赛事已在全局订阅中，直接提示
    if data_manager.is_subscribed(group_id, event_id):
        yield event.plain_result(f"已经订阅了赛事 #{event_id}")
        event.stop_event()
        return

    yield event.plain_result("正在获取赛事信息...")

    try:
        events = await hltv_data.get_big_events()
        event_info = None
        for e in events:
            if e.id == event_id:
                event_info = e
                break

        if not event_info:
            event_info = await hltv_data.get_event_info(event_id)

        if event_info:
            created = data_manager.subscribe_event(
                group_id=group_id,
                subscription=EventSubscription(
                    event_id=event_id,
                    event_title=event_info.title,
                    start_date=event_info.start_date,
                    end_date=event_info.end_date,
                ),
            )
            if not created:
                yield event.plain_result(f"已经订阅了赛事 #{event_id}")
                event.stop_event()
                return

            from ..scheduler import hltv_scheduler

            # 进行中赛事先标记已有结果，避免订阅后立刻推历史结果
            if event_info.is_ongoing:
                await hltv_scheduler.initialize_event_results_as_notified(event_id)

            hltv_scheduler.ensure_event_job_state(event_id)
            hltv_scheduler.refresh_wakeup_jobs()

            yield event.plain_result(f"✅ 成功订阅赛事：{event_info.title}")
            event.stop_event()
        else:
            # 未获取到详细信息：仍允许订阅，元信息后续由每日维护自动补全
            created = data_manager.subscribe_event(
                group_id=group_id,
                subscription=EventSubscription(
                    event_id=event_id,
                    event_title=f"Event #{event_id}",
                    start_date="",
                    end_date="",
                ),
            )
            if not created:
                yield event.plain_result(f"已经订阅了赛事 #{event_id}")
                event.stop_event()
                return

            from ..scheduler import hltv_scheduler

            hltv_scheduler.ensure_event_job_state(event_id)
            hltv_scheduler.refresh_wakeup_jobs()

            yield event.plain_result(f"✅ 成功订阅赛事 #{event_id}")
            event.stop_event()
    except Exception as e:
        logger.error(f"订阅赛事失败: {e}")
        yield event.plain_result("订阅失败，HLTV 可能暂时无法访问")
        event.stop_event()


async def handle_event_unsubscribe(plugin, event):
    """event取消订阅 [ID]：取消订阅（需要管理员权限）"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    if not await check_permission(plugin, event):
        yield event.plain_result("❌ 只有群主或管理员可以取消订阅")
        event.stop_event()
        return

    match = _UNSUBSCRIBE_PATTERN.match(event.get_message_str().strip())
    event_id = (match.group(1) if match and match.group(1) else "").strip()
    if not event_id:
        yield event.plain_result("请提供赛事ID，例如：event取消订阅 7148")
        event.stop_event()
        return

    if data_manager.unsubscribe_event_global(event_id):
        from ..scheduler import hltv_scheduler

        hltv_scheduler.ensure_job_state()
        hltv_scheduler.refresh_wakeup_jobs()
        yield event.plain_result(f"✅ 已取消订阅赛事 #{event_id}")
        event.stop_event()
    else:
        yield event.plain_result(f"未订阅赛事 #{event_id}")
        event.stop_event()


async def handle_my_subscriptions(plugin, event):
    """我的订阅：查看已订阅赛事"""
    group_id = int(event.get_group_id()) if event.get_group_id() else 0

    if not is_group_enabled(group_id):
        event.stop_event()
        return

    subscriptions = data_manager.get_subscribed_events(group_id)
    if not subscriptions:
        yield event.plain_result("当前没有订阅任何赛事\n使用 event列表 查看可订阅的赛事")
        event.stop_event()
        return

    msg = "📋 已订阅的赛事：\n"
    for sub in subscriptions:
        msg += f"• #{sub.event_id} {sub.event_title}\n"
        if sub.start_date and sub.end_date:
            msg += f"  📅 {sub.start_date} ~ {sub.end_date}\n"

    yield event.plain_result(msg.strip())
    event.stop_event()
