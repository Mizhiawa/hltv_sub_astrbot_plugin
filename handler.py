"""HLTV 比赛检查、订阅维护与推送执行逻辑。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Literal

import pytz

from .client import HLTVClient, HLTVFetchError
from .config import PluginConfig
from .data_manager import DataManager, EventSubscription, GroupData
from .log_compat import get_exc_desc, logger
from .models import (
    EventCheckResult,
    EventMatchesMeta,
    EventMatchesPage,
    EventMvp,
    MapStats,
    MatchInfo,
    MatchStats,
    ResultInfo,
)
from .parsers.common import localize_dt
from .render import HLTVRenderer

UPCOMING_WINDOW_HOURS = 72
OVERDUE_THRESHOLD_MINUTES = 30
MVP_WATCH_HOURS = 3
EventState = Literal["ONGOING", "UPCOMING", "NOT_ONGOING", "ENDED", "UNKNOWN"]

GroupSender = Callable[[GroupData, str | None, bytes], Awaitable[None]]


@dataclass
class CompletedMapResult:
    """BO3/BO5 中已结束、尚未决出整场胜负的单图推送。"""

    match: MatchInfo
    map_index: int
    map_name: str
    notification_id: str
    score1_after_map: int
    score2_after_map: int
    single_map_stats: MatchStats


def get_event_state(
    sub: EventSubscription | None, now: datetime, end_grace_days: int
) -> EventState:
    if sub is None:
        return "UNKNOWN"
    try:
        start = date.fromisoformat(sub.start_date)
        end = date.fromisoformat(sub.end_date)
    except ValueError:
        return "UNKNOWN"
    if end < start:
        return "UNKNOWN"
    tz = now.tzinfo
    start_at = localize_dt(start, time.min, tz)
    expires_at = localize_dt(
        end + timedelta(days=end_grace_days + 1), time.min, tz
    )
    if now >= expires_at:
        return "ENDED"
    if now >= start_at:
        return "ONGOING"
    if now >= start_at - timedelta(hours=UPCOMING_WINDOW_HOURS):
        return "UPCOMING"
    return "NOT_ONGOING"


def _is_final_round_score(score1: int, score2: int) -> bool:
    """CS2 MR12 打到 13，加时每段 MR3（16、19…）且领先至少 2 分。"""
    winner = max(score1, score2)
    loser = min(score1, score2)
    if winner == 13:
        return loser <= 11
    if winner >= 16 and (winner - 16) % 3 == 0:
        return winner - loser >= 2
    return False


def _score_pair(score1: str, score2: str) -> tuple[int, int] | None:
    if not (score1.isdigit() and score2.isdigit()):
        return None
    return int(score1), int(score2)


def _is_map_finished(map_info: MapStats) -> bool:
    pair = _score_pair(map_info.score_team1, map_info.score_team2)
    return pair is not None and _is_final_round_score(*pair)


def has_complete_map_details(stats: MatchStats, map_info: MapStats) -> bool:
    players = stats.map_stats_details.get(map_info.map_name, [])
    return {"team1", "team2"} <= {player.team for player in players}


def get_result_stats_push_block_reason(
    result: ResultInfo, stats: MatchStats
) -> str | None:
    """结果页先出比分，match 页的单图比分和选手数据会晚几分钟；没同步完就等下一轮。"""
    score = (int(result.score1), int(result.score2))
    if max(score) > 5:
        if not _is_final_round_score(*score):
            return f"BO1 回合比分未达到终局: score={result.score1}-{result.score2}"
        expected_maps = 1
    else:
        expected_maps = sum(score)
    if expected_maps == 0:
        return None

    if _score_pair(stats.score1, stats.score2) != score:
        return (
            "stats 总比分未同步到结果页: "
            f"result={result.score1}-{result.score2}, stats={stats.score1}-{stats.score2}"
        )
    played = [m for m in stats.maps if m.score_team1 != "-" and m.score_team2 != "-"]
    if len(played) != expected_maps:
        return (
            f"stats 未更新完整: expected_maps={expected_maps}, played_maps={len(played)}"
        )
    unfinished = [
        f"{m.map_name}({m.score_team1}-{m.score_team2})"
        for m in played
        if not _is_map_finished(m)
    ]
    if unfinished:
        return f"单图比分未达到终局: unfinished_maps={unfinished}"
    only = played[0]
    if expected_maps == 1 and _score_pair(only.score_team1, only.score_team2) != score:
        return (
            "BO1 单图比分未同步到结果页: "
            f"result={result.score1}-{result.score2}, "
            f"map={only.score_team1}-{only.score_team2}"
        )
    incomplete = [m.map_name for m in played if not has_complete_map_details(stats, m)]
    if incomplete:
        return f"单图数据未更新完整: incomplete_map_details={incomplete}"
    return None


def build_completed_map_results(
    match: MatchInfo, stats: MatchStats
) -> list[CompletedMapResult]:
    if not match.maps.isdigit():
        return []
    maps_needed = int(match.maps) // 2 + 1
    score1_after_map = 0
    score2_after_map = 0
    candidates: list[CompletedMapResult] = []

    for map_index, map_info in enumerate(stats.maps, start=1):
        if not _is_map_finished(map_info):
            continue
        if int(map_info.score_team1) > int(map_info.score_team2):
            score1_after_map += 1
        else:
            score2_after_map += 1
        if (
            not has_complete_map_details(stats, map_info)
            or max(score1_after_map, score2_after_map) >= maps_needed
        ):
            continue

        stable_map_id = map_info.stats_id or f"{map_index}:{map_info.map_name}"
        candidates.append(
            CompletedMapResult(
                match=match,
                map_index=map_index,
                map_name=map_info.map_name,
                notification_id=f"{match.id}:{stable_map_id}",
                score1_after_map=score1_after_map,
                score2_after_map=score2_after_map,
                single_map_stats=replace(
                    stats,
                    score1=str(score1_after_map),
                    score2=str(score2_after_map),
                    maps=[map_info],
                    players=[],
                    map_stats_details={
                        map_info.map_name: stats.map_stats_details[map_info.map_name]
                    },
                    team1_vrs=None,
                    team2_vrs=None,
                ),
            )
        )

    return candidates


class HLTVHandler:
    def __init__(
        self,
        *,
        config: PluginConfig,
        data_manager: DataManager,
        client: HLTVClient,
        renderer: HLTVRenderer,
        send_to_group: GroupSender,
        has_active_platform: Callable[[], bool],
    ) -> None:
        self._config = config
        self._tz = pytz.timezone(config.timezone)
        self._end_grace_days = config.auto_unsub_delay_days
        self._data = data_manager
        self._client = client
        self._renderer = renderer
        self._send_to_group = send_to_group
        self._has_active_platform = has_active_platform
        self._initialized_events: set[str] = set()
        self._unavailable_matches: set[str] = set()

    def active_subscriptions(self) -> list[EventSubscription]:
        """只抓进行中或 UPCOMING_WINDOW_HOURS 内开赛的赛事。"""
        now = datetime.now(self._tz)
        return [
            sub
            for sub in self._data.get_subscribed_events()
            if get_event_state(sub, now, self._end_grace_days)
            in ("ONGOING", "UPCOMING")
        ]

    def cleanup_unsubscribed(self) -> None:
        subscribed = self._data.get_all_subscribed_event_ids()
        self._initialized_events.intersection_update(subscribed)
        self._unavailable_matches.intersection_update(subscribed)

    async def run_check_for_event(self, sub: EventSubscription) -> EventCheckResult:
        """执行单个赛事的一轮检查。"""
        result = EventCheckResult()
        event_id = sub.event_id
        if not self._has_active_platform():
            logger.debug(f"[HLTV] 尚未检测到已就绪的消息平台实例，跳过本轮推送 (event={event_id})")
            result.has_error = True
            return result

        try:
            if sub.mvp_status == "watching":
                await self.check_event_mvp(sub)
                return result

            if event_id not in self._initialized_events:
                await self.initialize_event_results_as_notified(event_id)
            snapshot = await self._client.get_event_matches(event_id)
            self._note_matches_availability(event_id, snapshot.meta)
            for match in self._due_reminders(snapshot, result):
                await self.send_match_reminder(sub, match)
            if self._config.enable_map_result_push:
                for completed_map in await self._completed_maps(sub, snapshot):
                    await self.send_completed_map_result(event_id, completed_map)
            results = await self._client.get_event_results(event_id, max_results=5)
            for r in results:
                if not self._data.is_notified("results", r.id):
                    await self.send_match_result(sub, r, check=result)
        except HLTVFetchError as e:
            result.retry_at = e.retry_at
            result.has_error = True
            logger.debug(f"[HLTV] 本轮停止(event={event_id}): {e}")
        except Exception as e:
            result.has_error = True
            logger.exception(f"[HLTV] 检查失败(event={event_id}): {e}")

        return result

    def _note_matches_availability(self, event_id: str, meta: EventMatchesMeta) -> None:
        if meta.is_unavailable and event_id not in self._unavailable_matches:
            self._unavailable_matches.add(event_id)
            logger.warning(
                f"[HLTV] 赛事 {event_id} matches 页面不可用: "
                f"reason={meta.unavailable_reason}, final_url={meta.final_url}"
            )
        elif not meta.is_unavailable and event_id in self._unavailable_matches:
            self._unavailable_matches.discard(event_id)
            logger.info(f"[HLTV] 赛事 {event_id} matches 页面恢复")

    def _due_reminders(
        self, snapshot: EventMatchesPage, check: EventCheckResult
    ) -> list[MatchInfo]:
        now = datetime.now(self._tz)
        if any(h.is_live for h in snapshot.hints):
            check.live_seen_at = now

        waits: list[int] = []
        for h in snapshot.hints:
            if h.is_live:
                continue
            if h.start_time is None:
                if not h.is_tbd:
                    waits.append(0)
                continue
            seconds_until = (h.start_time - now).total_seconds()
            if seconds_until > 0:
                waits.append(int(seconds_until // 60))
            elif -seconds_until <= OVERDUE_THRESHOLD_MINUTES * 60:
                waits.append(0)
        check.next_minutes_hint = min(waits, default=None)

        due = [
            match
            for match in snapshot.matches
            if not self._data.is_notified("starts", match.id)
            and (
                match.is_live
                or match.start_time is None
                or 0
                < (now - match.start_time).total_seconds()
                <= OVERDUE_THRESHOLD_MINUTES * 60
            )
        ]
        for match in due:
            logger.debug(
                f"[HLTV] 开赛提醒触发: match_id={match.id}, live={match.is_live}"
            )
        return due

    async def _completed_maps(
        self, sub: EventSubscription, snapshot: EventMatchesPage
    ) -> list[CompletedMapResult]:
        completed: list[CompletedMapResult] = []
        for match in snapshot.matches:
            if not match.is_live or match.maps not in {"3", "5"}:
                continue
            stats = await self._client.get_match_stats(
                match.id,
                team1=match.team1,
                team2=match.team2,
                event_title=sub.event_title,
            )
            completed.extend(
                candidate
                for candidate in build_completed_map_results(match, stats)
                if not self._data.is_notified(
                    "map_results", candidate.notification_id
                )
            )
        return completed

    async def initialize_event_results_as_notified(self, event_id: str) -> None:
        """首次检查和订阅进行中赛事时调用：把当前已有结果先标记为已推送，避免推历史结果。"""
        results = await self._client.get_event_results(event_id, max_results=10)
        self._mark_existing_results(event_id, results)

    def _mark_existing_results(self, event_id: str, results: list[ResultInfo]) -> None:
        count = 0
        for r in results:
            if not self._data.is_notified("results", r.id):
                self._data.mark_notified("results", r.id)
                count += 1
        self._initialized_events.add(event_id)
        logger.debug(
            f"[HLTV] 订阅初始化：已标记 {count} 条现有结果为已推送 (event {event_id})"
        )

    async def _try_refresh_subscription_meta(self, event_id: str) -> bool:
        try:
            info = await self._client.get_event_info(event_id)
        except Exception as e:
            logger.warning(f"[HLTV] 补全赛事元信息失败(event={event_id}): {e}")
            return False
        self._data.update_subscription_meta(
            event_id,
            event_title=info.title,
            start_date=info.start_date,
            end_date=info.end_date,
        )
        return True

    async def daily_maintenance(self) -> None:
        """清过期去重；已结束的赛事补发漏掉的赛果，全部发完后自动退订。"""
        cleaned = self._data.cleanup_notified_state(self._config.notified_ttl_days)
        if cleaned:
            logger.info(f"[HLTV] 已清理 {cleaned} 条过期去重记录")
        if not self._data.has_enabled_groups():
            return

        for event_id in sorted(self._data.get_all_subscribed_event_ids()):
            try:
                sub = self._data.get_subscription(event_id)
                state = get_event_state(
                    sub, datetime.now(self._tz), self._end_grace_days
                )
                if state == "UNKNOWN":
                    if not await self._try_refresh_subscription_meta(event_id):
                        continue
                    sub = self._data.get_subscription(event_id)
                    state = get_event_state(
                        sub, datetime.now(self._tz), self._end_grace_days
                    )
                if state != "ENDED":
                    if state == "UNKNOWN":
                        logger.warning(f"[HLTV] 赛事 {event_id} 日期仍无效，保留订阅")
                    continue

                if not self._has_active_platform():
                    continue
                results = await self._client.get_event_results(
                    event_id,
                    max_results=10,
                    force_refresh=True,
                )
                if not results:
                    logger.info(f"[HLTV] 延后退订 {event_id}：没有可确认的赛果")
                    continue
                if event_id not in self._initialized_events:
                    self._mark_existing_results(event_id, results)
                assert sub is not None
                for result in results:
                    if not self._data.is_notified("results", result.id):
                        await self.send_match_result(sub, result)
                if all(self._data.is_notified("results", r.id) for r in results):
                    if self._data.unsubscribe_event(event_id):
                        logger.info(f"[HLTV] 已自动退订 {event_id} {sub.event_title}")
            except HLTVFetchError as e:
                logger.info(f"[HLTV] 延后维护赛事 {event_id}：{e}")
                break
            except Exception as e:
                logger.exception(f"[HLTV] 维护赛事 {event_id} 失败，保留订阅：{e}")

    async def auto_subscribe_big_events(self) -> list[str]:
        """抓取 /events，订阅尚未订阅的 big events。"""
        if not self._data.has_enabled_groups():
            return []
        try:
            events = await self._client.get_big_events()
        except HLTVFetchError as e:
            logger.info(f"[HLTV] 自动订阅跳过: {e}")
            return []
        except Exception as e:
            logger.exception(f"[HLTV] 自动订阅失败: {e}")
            return []

        added = [
            f"#{event.id} {event.title}"
            for event in events
            if self._data.subscribe_event(
                EventSubscription(
                    event_id=event.id,
                    event_title=event.title,
                    start_date=event.start_date,
                    end_date=event.end_date,
                )
            )
        ]
        logger.info(
            "[HLTV] 自动订阅完成: " + (", ".join(added) if added else "无新赛事")
        )
        return added

    async def _broadcast(
        self,
        event_id: str,
        *,
        text: str | None,
        image_bytes: bytes,
        desc: str,
    ) -> bool:
        """发到所有开启的群；有一个群成功就算推送过，全失败留给下轮重试。"""
        groups = self._data.get_enabled_groups_by_event(event_id)
        if not groups:
            return False
        sent = False
        for group in groups:
            try:
                await self._send_to_group(group, text, image_bytes)
                sent = True
                logger.info(f"[HLTV] 已发送{desc}到群 {group.group_id}")
            except Exception as e:
                logger.warning(
                    f"[HLTV] 发送{desc}到群 {group.group_id} 失败: {get_exc_desc(e)}"
                )
        if not sent:
            logger.info(f"[HLTV] {desc}没有发送成功，不标记为已推送，下轮将重试")
        return sent

    async def send_match_reminder(
        self, sub: EventSubscription, match: MatchInfo
    ) -> None:
        stats = await self._client.get_match_stats(
            match.id,
            team1=match.team1,
            team2=match.team2,
            event_title=sub.event_title,
        )
        try:
            img = await self._renderer.render_reminder(match, stats, sub.event_title)
        except Exception as e:
            logger.exception(f"[HLTV] 渲染比赛提醒 {match.id} 失败，下轮将重试: {e}")
            return
        if await self._broadcast(
            sub.event_id,
            text=None,
            image_bytes=img,
            desc=f"比赛提醒 {match.team1} vs {match.team2}",
        ):
            self._data.mark_notified("starts", match.id)

    async def send_match_result(
        self,
        sub: EventSubscription,
        result: ResultInfo,
        *,
        check: EventCheckResult | None = None,
    ) -> None:
        try:
            stats = await self._client.get_match_stats(
                result.id,
                team1=result.team1,
                team2=result.team2,
                event_title=sub.event_title,
            )
            if block_reason := get_result_stats_push_block_reason(result, stats):
                logger.info(
                    f"[HLTV] match {result.id} stats 未准备好：{block_reason}，跳过本次推送等待下次轮询"
                )
                return
            score_line = f"{result.team1} {result.score1}:{result.score2} {result.team2}"
            img = await self._renderer.render_stats(stats)
            text = f"🏁 比赛已结束\n{score_line}\n\n"
        except HLTVFetchError:
            raise
        except Exception as e:
            if check is not None:
                check.has_error = True
            logger.exception(f"[HLTV] 处理比赛结果 {result.id} 失败: {e}")
            return

        desc = f"比赛结果 {result.team1} vs {result.team2}"
        if not await self._broadcast(
            sub.event_id, text=text, image_bytes=img, desc=desc
        ):
            return
        self._data.mark_notified("results", result.id)
        current = self._data.get_subscription(sub.event_id)
        if stats.is_grand_final and current is not None and not current.mvp_status:
            self._data.set_mvp_status(sub.event_id, "watching")
            logger.info(f"[HLTV] 决赛已推送，开始等待 MVP (event={sub.event_id})")

    async def check_event_mvp(self, sub: EventSubscription) -> None:
        try:
            since = datetime.fromisoformat(sub.mvp_watch_since).replace(tzinfo=None)
        except ValueError:
            since = datetime.now()
        watched = datetime.now() - since
        if watched > timedelta(hours=MVP_WATCH_HOURS):
            self._data.set_mvp_status(sub.event_id, "expired")
            logger.info(
                f"[HLTV] 决赛后 {MVP_WATCH_HOURS} 小时未见 MVP，停止等待 (event={sub.event_id})"
            )
            return
        info = await self._client.get_event_info(
            sub.event_id, sub.event_title, max_age=120
        )
        if info.mvp is not None:
            await self.send_event_mvp(sub, info.mvp)

    async def send_event_mvp(
        self, sub: EventSubscription, mvp: EventMvp
    ) -> None:
        try:
            img = await self._renderer.render_mvp(sub.event_title, mvp)
        except Exception as e:
            logger.exception(
                f"[HLTV] 渲染赛事 MVP {sub.event_id} 失败，下轮将重试: {e}"
            )
            return
        text = f"🏆 {sub.event_title} MVP：{mvp.nickname}\n\n"
        desc = f"赛事 MVP {sub.event_title} {mvp.nickname}"
        if await self._broadcast(sub.event_id, text=text, image_bytes=img, desc=desc):
            self._data.set_mvp_status(sub.event_id, "pushed")

    async def send_completed_map_result(
        self, event_id: str, completed: CompletedMapResult
    ) -> None:
        if not self._config.enable_map_result_push:
            return
        match = completed.match
        score_line = (
            f"{match.team1} {completed.score1_after_map}:"
            f"{completed.score2_after_map} {match.team2}"
        )
        try:
            img = await self._renderer.render_stats(completed.single_map_stats)
        except Exception as e:
            logger.exception(
                f"[HLTV] 渲染单图结果 {completed.notification_id} 失败: {e}"
            )
            return
        text = f"🗺️ 地图已结束 · BO{match.maps} 图{completed.map_index}\n{score_line}\n\n"
        desc = f"单图结果 {match.team1} vs {match.team2} {completed.map_name}"
        if await self._broadcast(event_id, text=text, image_bytes=img, desc=desc):
            self._data.mark_notified("map_results", completed.notification_id)
