"""
scheduler 启动/注册逻辑（AstrBot 适配版）

职责（与原 NoneBot 版一致）：
- 注册 per-event apscheduler interval job（hltv_check_{event_id}）
- 注册 daily maintenance job（自动退订 + 去重状态清理）
- 绑定 core.HLTVScheduler 的 job 控制方法到 apscheduler
- 提供插件启动/停止钩子（prepare_scheduler / start_scheduler / stop_scheduler / delayed_init）

消息推送通过注入的 send_message_to_group 回调完成（由插件入口实现，
内部使用 AstrBot 的 context.send_message 主动发消息）。
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable

from astrbot.api import logger

from ..config import plugin_config
from ..data_manager import data_manager
from .apscheduler_shim import get_scheduler as _get_scheduler
from .constants import DAILY_MAINTENANCE_JOB_ID, DEFAULT_INTERVAL_MINUTES, event_job_id
from .core import HLTVScheduler

# 发送回调类型：async (platform_id, group_id, text, image_path) -> bool
SendCallback = Callable[[str, int, str, str], Awaitable[bool]]

hltv_scheduler = HLTVScheduler()

_SCHEDULER_SETUP_DONE = False


def _ensure_event_job(event_id: str) -> None:
    job_id = event_job_id(event_id)

    try:
        existing = _get_scheduler().get_job(job_id)
        if existing is not None:
            return

        async def _scheduled_check(eid: str):
            jitter = max(0, int(plugin_config.hltv_scheduler_jitter_seconds))
            if jitter > 0:
                await asyncio.sleep(random.randint(0, jitter))
            await hltv_scheduler.run_check_for_event(eid)

        _get_scheduler().add_job(
            _scheduled_check,
            trigger="interval",
            minutes=DEFAULT_INTERVAL_MINUTES,
            args=[event_id],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"[HLTV Scheduler] 已创建赛事定时任务: {job_id}")
    except Exception as e:
        logger.warning(f"[HLTV Scheduler] 创建赛事定时任务失败 ({job_id}): {e}")


def _pause_event_job(event_id: str) -> None:
    job_id = event_job_id(event_id)
    try:
        _get_scheduler().pause_job(job_id)
        logger.info(f"[HLTV Scheduler] 已暂停赛事定时任务: {job_id}")
    except Exception:
        pass


def _resume_event_job(event_id: str) -> None:
    job_id = event_job_id(event_id)
    try:
        _get_scheduler().resume_job(job_id)
        logger.info(f"[HLTV Scheduler] 已恢复赛事定时任务: {job_id}")
    except Exception:
        pass


def _remove_event_job(event_id: str) -> None:
    job_id = event_job_id(event_id)
    try:
        _get_scheduler().remove_job(job_id)
        logger.info(f"[HLTV Scheduler] 已移除赛事定时任务: {job_id}")
    except Exception:
        pass


def _reschedule_event_job_interval(event_id: str, minutes: int) -> None:
    minutes = max(DEFAULT_INTERVAL_MINUTES, int(minutes))
    state = hltv_scheduler._get_event_poll_state(event_id)

    if minutes == state.current_interval_minutes:
        return

    job_id = event_job_id(event_id)
    try:
        _ensure_event_job(event_id)
        _get_scheduler().reschedule_job(job_id, trigger="interval", minutes=minutes)
        logger.info(
            f"[HLTV Scheduler] 自适应轮询(event={event_id})："
            f"{state.current_interval_minutes}min -> {minutes}min"
        )
        state.current_interval_minutes = minutes
    except Exception as e:
        logger.warning(f"[HLTV Scheduler] 调整赛事定时任务间隔失败 ({job_id}): {e}")


# 将 job 控制方法注入 scheduler 实例（避免 core 直接依赖 apscheduler）
hltv_scheduler._ensure_event_job = _ensure_event_job  # type: ignore[assignment]
hltv_scheduler._pause_event_job = _pause_event_job  # type: ignore[assignment]
hltv_scheduler._resume_event_job = _resume_event_job  # type: ignore[assignment]
hltv_scheduler._remove_event_job = _remove_event_job  # type: ignore[assignment]
hltv_scheduler._reschedule_event_job_interval = _reschedule_event_job_interval  # type: ignore[assignment]


async def _daily_maintenance():
    await hltv_scheduler.daily_maintenance()


async def delayed_init():
    """延迟初始化，等待一段时间后再执行（由插件 initialize 触发）"""
    await asyncio.sleep(10)
    try:
        count = await hltv_scheduler.init_existing_results()
        if count > 0:
            logger.info(f"[HLTV Scheduler] 启动初始化完成，标记了 {count} 条历史结果")

        # 按当前订阅状态决定每个 event job 的运行状态
        hltv_scheduler.ensure_job_state()

        # 重建/清理 wakeup job（重启后 apscheduler 内存 job 会丢）
        hltv_scheduler.refresh_wakeup_jobs()

        # 启动时先跑一轮每日维护（自动退订/清理）
        await hltv_scheduler.daily_maintenance()
    except Exception as e:
        logger.error(f"[HLTV Scheduler] 启动初始化失败: {e}")


def _ensure_daily_maintenance_job() -> None:
    try:
        existing = _get_scheduler().get_job(DAILY_MAINTENANCE_JOB_ID)
        if existing is not None:
            return

        _get_scheduler().add_job(
            _daily_maintenance,
            trigger="cron",
            hour=4,
            minute=30,
            id=DAILY_MAINTENANCE_JOB_ID,
            replace_existing=True,
        )
        logger.info("[HLTV Scheduler] 已注册每日维护任务 (04:30)")
    except Exception as e:
        logger.warning(f"[HLTV Scheduler] 注册每日维护任务失败: {e}")


def prepare_scheduler(send_message_to_group: SendCallback) -> None:
    """注册定时任务并注入消息发送回调（幂等，由插件 __init__ 调用）"""
    global _SCHEDULER_SETUP_DONE
    if _SCHEDULER_SETUP_DONE:
        # 重载场景：仅更新发送回调
        hltv_scheduler._send_message_to_group = send_message_to_group
        return

    hltv_scheduler._send_message_to_group = send_message_to_group

    # 1) 为当前已订阅赛事预创建 per-event jobs
    for event_id in data_manager.get_all_subscribed_event_ids():
        _ensure_event_job(event_id)

    # 2) 注册 daily maintenance job
    _ensure_daily_maintenance_job()

    _SCHEDULER_SETUP_DONE = True


async def start_scheduler_async() -> None:
    """启动 apscheduler（需在事件循环中调用，由插件 initialize 触发）"""
    from .apscheduler_shim import start_scheduler

    start_scheduler()
    logger.info("[HLTV Scheduler] 多赛事定时任务已启动")


async def stop_scheduler_async() -> None:
    """停止调度器并重置注册状态（由插件 terminate 触发）"""
    global _SCHEDULER_SETUP_DONE
    from .apscheduler_shim import stop_scheduler

    stop_scheduler()
    _SCHEDULER_SETUP_DONE = False
    logger.info("[HLTV Scheduler] 定时任务已停止")
