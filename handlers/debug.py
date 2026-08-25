"""
超级用户调试命令：hltv_check / hltv_trigger（AstrBot 适配版）
"""

from __future__ import annotations

from astrbot.api import logger

from ..data_manager import data_manager
from ..permissions import check_superuser
from ..scheduler import hltv_scheduler

# 命令正则（供 main.py 的 filter.regex 注册使用）
HLTV_CHECK_RE = r"^hltv_check\s*$"
HLTV_TRIGGER_RE = r"^hltv_trigger\s*$"


async def handle_hltv_check(plugin, event):
    """hltv_check：查看即将开始的比赛列表与提醒去重状态（仅超级用户）"""
    if not check_superuser(plugin, event):
        event.stop_event()
        return

    yield event.plain_result("正在检查即将开始的比赛...")

    try:
        upcoming = await hltv_scheduler.get_upcoming_info()

        if not upcoming:
            yield event.plain_result("暂无即将开始的比赛")
            event.stop_event()
            return

        msg = "📋 即将开始的比赛：\n\n"

        for match in upcoming[:10]:
            if match.minutes_until >= 60:
                hours = match.minutes_until // 60
                mins = match.minutes_until % 60
                time_str = f"{hours}小时{mins}分钟" if mins > 0 else f"{hours}小时"
            else:
                time_str = f"{match.minutes_until}分钟"

            bo_text = f"BO{match.maps}" if match.maps else ""
            notified = "✓" if data_manager.is_start_notified(match.match_id) else ""

            msg += f"⏰ {time_str}后 {notified}\n"
            msg += f"🎮 {match.team1} vs {match.team2}\n"
            msg += f"🏆 {match.event_title}"
            if bo_text:
                msg += f" | {bo_text}"
            msg += "\n\n"

        if len(upcoming) > 10:
            msg += f"... 还有 {len(upcoming) - 10} 场比赛"

        yield event.plain_result(msg.strip())
        event.stop_event()
    except Exception as e:
        logger.error(f"检查比赛失败: {e}")
        yield event.plain_result(f"检查失败: {e}")
        event.stop_event()


async def handle_hltv_trigger(plugin, event):
    """hltv_trigger：手动执行一次全量检查（仅超级用户）"""
    if not check_superuser(plugin, event):
        event.stop_event()
        return

    yield event.plain_result("正在手动执行定时任务检查...")

    try:
        result = await hltv_scheduler.run_check()

        msg = "📊 检查结果：\n\n"
        msg += f"即将开始：{len(result['upcoming_matches'])} 场\n"
        msg += f"新结果：{len(result['new_results'])} 场\n"

        if result["errors"]:
            msg += f"错误：{len(result['errors'])} 个\n"
            for err in result["errors"][:3]:
                msg += f"  - {err}\n"

        yield event.plain_result(msg.strip())
        event.stop_event()
    except Exception as e:
        logger.error(f"手动触发检查失败: {e}")
        yield event.plain_result(f"执行失败: {e}")
        event.stop_event()
