"""基于原生 asyncio 的自适应轮询、每日维护与 Big Events 自动订阅调度器。"""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytz

from .config import PluginConfig
from .data_manager import DataManager
from .handler import UPCOMING_WINDOW_HOURS, HLTVHandler, get_event_state
from .log_compat import logger
from .parsers.common import localize_dt

DEFAULT_INTERVAL_MINUTES = 3
ADAPTIVE_INTERVAL_TABLE = [(60, 3), (6 * 60, 30), (24 * 60, 180)]
POST_LIVE_GRACE_MINUTES = 30


@dataclass
class _Schedule:
    next_check_at: datetime
    last_live_seen_at: datetime | None = None


class HLTVScheduler:
    def __init__(
        self,
        *,
        config: PluginConfig,
        data_manager: DataManager,
        handler: HLTVHandler,
    ) -> None:
        self._config = config
        self._tz = pytz.timezone(config.timezone)
        self._end_grace_days = max(0, config.auto_unsub_delay_days)
        self._data = data_manager
        self._handler = handler
        self._events: dict[str, _Schedule] = {}
        self._had_receivers = False
        self._round_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = False

    def start(self) -> None:
        if self._tasks:
            return
        self._stopped = False
        self.sync_events()
        self._had_receivers = self._data.has_enabled_groups()
        self._tasks = [
            asyncio.create_task(self._due_loop(), name="hltv_check_due"),
            asyncio.create_task(
                self._daily_maintenance_loop(), name="hltv_daily_maintenance"
            ),
            asyncio.create_task(
                self._auto_subscribe_loop(), name="hltv_auto_subscribe"
            ),
        ]
        logger.info("[HLTV] 周期检查、每日维护和自动订阅任务已启动")

    async def stop(self) -> None:
        self._stopped = True
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _due_loop(self) -> None:
        with suppress(asyncio.CancelledError):
            while not self._stopped:
                try:
                    await self.check_due_events()
                except Exception as exc:
                    logger.exception(f"[HLTV] 周期扫描任务异常: {exc}")
                await asyncio.sleep(60)

    async def _daily_maintenance_loop(self) -> None:
        """每天按配置时区 04:30 执行一次过期去重清理与完赛退订维护。"""
        with suppress(asyncio.CancelledError):
            while not self._stopped:
                now = datetime.now(self._tz)
                target = localize_dt(now.date(), time(4, 30), self._tz)
                if target <= now:
                    target = localize_dt(
                        now.date() + timedelta(days=1), time(4, 30), self._tz
                    )
                wait_seconds = max(1.0, (target - now).total_seconds())
                await asyncio.sleep(wait_seconds)
                if self._stopped:
                    break
                try:
                    await self.daily_maintenance()
                except Exception as exc:
                    logger.exception(f"[HLTV] 每日维护任务异常: {exc}")

    async def _auto_subscribe_loop(self) -> None:
        interval_seconds = max(1, self._config.auto_sub_interval_days) * 86400
        with suppress(asyncio.CancelledError):
            while not self._stopped:
                await asyncio.sleep(interval_seconds)
                if self._stopped:
                    break
                try:
                    await self.auto_subscribe_big_events()
                except Exception as exc:
                    logger.exception(f"[HLTV] 自动订阅周期任务异常: {exc}")

    def sync_events(self, *, wake: bool = False) -> None:
        subscribed = self._data.get_all_subscribed_event_ids()
        now = datetime.now(self._tz)
        for event_id in list(self._events.keys() - subscribed):
            self._events.pop(event_id, None)
        for event_id in subscribed:
            if event_id not in self._events:
                self._events[event_id] = _Schedule(now)
            elif wake:
                self._events[event_id].next_check_at = now
        self._handler.cleanup_unsubscribed()

    async def check_due_events(self) -> None:
        if self._round_lock.locked():
            logger.debug("[HLTV] 上一轮检查还没结束，跳过本拍")
            return
        async with self._round_lock:
            await self._check_due_events()

    async def _check_due_events(self) -> None:
        if not self._data.has_enabled_groups():
            self._had_receivers = False
            return
        if not self._had_receivers:
            self._had_receivers = True
            await self.auto_subscribe_big_events()
        self.sync_events()
        for event_id in sorted(self._events):
            schedule = self._events.get(event_id)
            if schedule is not None and schedule.next_check_at <= datetime.now(
                self._tz
            ):
                await self._check_event(event_id)

    async def _check_event(self, event_id: str) -> None:
        sub = self._data.get_subscription(event_id)
        if sub is None:
            return
        now = datetime.now(self._tz)
        schedule = self._events.setdefault(event_id, _Schedule(now))
        state = get_event_state(sub, now, self._end_grace_days)
        if state == "NOT_ONGOING":
            start_at = localize_dt(
                date.fromisoformat(sub.start_date), time.min, self._tz
            )
            schedule.next_check_at = start_at - timedelta(hours=UPCOMING_WINDOW_HOURS)
            return
        if state not in ("ONGOING", "UPCOMING"):
            schedule.next_check_at = now + timedelta(days=1)
            if state == "UNKNOWN":
                logger.warning(f"[HLTV] 赛事 {event_id} 日期无效，等待维护补全")
            return

        result = await self._handler.run_check_for_event(sub)
        sub = self._data.get_subscription(event_id)
        if sub is None:
            self.sync_events()
            return

        now = datetime.now(self._tz)
        if result.live_seen_at is not None:
            schedule.last_live_seen_at = result.live_seen_at
        in_grace = (
            schedule.last_live_seen_at is not None
            and now - schedule.last_live_seen_at
            <= timedelta(minutes=POST_LIVE_GRACE_MINUTES)
        )
        watching_mvp = sub.mvp_status == "watching"
        if result.has_error or in_grace or watching_mvp:
            minutes = DEFAULT_INTERVAL_MINUTES
        elif result.next_minutes_hint is None:
            minutes = 360
        else:
            minutes = next(
                (
                    interval
                    for upper, interval in ADAPTIVE_INTERVAL_TABLE
                    if result.next_minutes_hint <= upper
                ),
                360,
            )
        jitter = random.randint(0, max(0, self._config.scheduler_jitter_seconds))
        schedule.next_check_at = now + timedelta(minutes=minutes, seconds=jitter)
        if result.retry_at:
            schedule.next_check_at = max(
                schedule.next_check_at,
                datetime.fromtimestamp(result.retry_at, self._tz),
            )
        logger.debug(
            f"[HLTV] 赛事 {event_id} 下次检查：{schedule.next_check_at.isoformat()}"
        )

    async def daily_maintenance(self) -> None:
        async with self._round_lock:
            try:
                await self._handler.daily_maintenance()
            finally:
                self.sync_events(wake=True)

    async def auto_subscribe_big_events(self) -> list[str]:
        try:
            return await self._handler.auto_subscribe_big_events()
        finally:
            self.sync_events()
