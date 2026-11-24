"""HLTV 页面数据获取、FlareSolverr 会话管理、缓存与按作用域隔离的访问冷却。"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytz

from .config import PluginConfig
from .json_io import atomic_write_json, load_json
from .log_compat import logger
from .models import EventInfo, EventMatchesPage, MatchStats, ResultInfo
from .parsers.common import make_soup
from .parsers.events import parse_big_events, parse_event_info
from .parsers.matches import parse_event_matches_page
from .parsers.results import parse_event_results
from .parsers.stats import parse_match_stats

GLOBAL_SCOPE = "*"


def _scope_of(cache_key: str) -> str:
    prefix = cache_key.split(":", 1)[0]
    if prefix in {"events", "event", "matches", "results", "stats"}:
        return "events" if prefix == "event" else prefix
    return GLOBAL_SCOPE


class HLTVFetchError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        retry_at: float = 0,
        recoverable: bool = False,
        should_cooldown: bool = True,
        global_ban: bool = False,
    ) -> None:
        self.reason = reason
        self.retry_at = retry_at
        self.recoverable = recoverable
        self.should_cooldown = should_cooldown
        self.global_ban = global_ban
        message = f"HLTV 暂时无法访问（{reason}）"
        if retry_at:
            when = datetime.fromtimestamp(retry_at, timezone.utc).astimezone(
                timezone(timedelta(hours=8))
            )
            message = f"HLTV 已暂停访问（{reason}），下次允许尝试：{when:%m-%d %H:%M:%S}（北京时间）"
        super().__init__(message)


@dataclass
class FetchResult:
    text: str
    final_url: str = ""


class HLTVClient:
    BASE_URL = "https://www.hltv.org"

    def __init__(self, config: PluginConfig, data_dir: Path) -> None:
        self._tz = pytz.timezone(config.timezone)
        self._timeout = config.flaresolverr_timeout_seconds
        self._interval = config.request_interval_seconds
        self._session_max_requests = config.session_max_requests
        self._endpoint = config.flaresolverr_url
        self._cooldown = config.block_cooldown_seconds
        self._max_cooldown = config.block_cooldown_max_seconds

        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "http_state.json"
        self._session_state_path = data_dir / "session_state.json"

        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._request_count = 0
        self._session_requests = 0
        self._cache: OrderedDict[str, tuple[float, FetchResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[FetchResult]] = {}
        self._closed = False

        # 按作用域记录冷却状态：{scope: {"blocked_until": float, "blocks": int, "reason": str}}
        self._cooldown_scopes: dict[str, dict[str, float | int | str]] = {}
        state = load_json(
            self._state_path, missing_ok=True, default={}, backup_on_error=True
        )
        if state.success and isinstance(state.data, dict):
            # 若修改过 flaresolverr_url，自动丢弃旧地址留下的冷却状态
            saved_endpoint = state.data.get("endpoint", "")
            raw_scopes = state.data.get("scopes")
            if saved_endpoint == self._endpoint and isinstance(raw_scopes, dict):
                now_ts = time.time()
                for sc, info in raw_scopes.items():
                    if isinstance(info, dict) and float(info.get("blocked_until", 0.0)) > now_ts:
                        self._cooldown_scopes[sc] = {
                            "blocked_until": float(info.get("blocked_until", 0.0)),
                            "blocks": int(info.get("blocks", 0)),
                            "reason": str(info.get("reason", "访问冷却中")),
                        }

        session_state = load_json(
            self._session_state_path, missing_ok=True, default={}, backup_on_error=True
        )
        raw_session = session_state.data if session_state.success else {}
        self._session_id = str(raw_session.get("selected_session_id", ""))
        self._pending_cleanup_id = str(
            raw_session.get("pending_cleanup_session_id", "")
        )

    async def get_big_events(self) -> list[EventInfo]:
        """获取 Big Events（正在进行 + 即将举行的赛事）。"""
        page = await self._fetch(
            f"{self.BASE_URL}/events", cache_key="events", ttl=3600
        )
        return parse_big_events(page.text, self._tz)

    async def get_event_info(
        self,
        event_id: str,
        event_title: str = "",
        *,
        max_age: int = 3600,
    ) -> EventInfo:
        """获取赛事详细信息。"""
        title_slug = event_title.lower().replace(" ", "-") if event_title else "event"
        url = f"{self.BASE_URL}/events/{event_id}/{title_slug}"
        page = await self._fetch(url, cache_key=f"event:{event_id}", ttl=max_age)
        return parse_event_info(page.text, event_id=event_id, tz=self._tz)

    async def get_event_results(
        self,
        event_id: str,
        max_results: int = 20,
        *,
        force_refresh: bool = False,
        max_age: int = 120,
    ) -> list[ResultInfo]:
        """获取赛事的已结束比赛结果。"""
        url = f"{self.BASE_URL}/results?event={event_id}"
        page = await self._fetch(
            url,
            cache_key=f"results:{event_id}",
            ttl=max_age,
            force_refresh=force_refresh,
        )
        results = parse_event_results(page.text, self._tz, max_results=max_results)
        logger.debug(
            f"[HLTV][RESULTS] parsed event={event_id} count={len(results)} max_results={max_results}"
        )
        return results

    async def get_match_stats(
        self, match_id: str, team1: str = "", team2: str = "", event_title: str = ""
    ) -> MatchStats:
        """获取比赛详细数据。"""
        t1_slug = team1.lower().replace(" ", "-") if team1 else "team1"
        t2_slug = team2.lower().replace(" ", "-") if team2 else "team2"
        event_slug = event_title.lower().replace(" ", "-") if event_title else "event"
        url = f"{self.BASE_URL}/matches/{match_id}/{t1_slug}-vs-{t2_slug}-{event_slug}"
        logger.debug(f"[HLTV][STATS] fetch_start match_id={match_id} url={url}")

        page = await self._fetch(url, cache_key=f"stats:{match_id}", ttl=60)
        parsed = parse_match_stats(
            page.text,
            match_id=match_id,
            tz=self._tz,
            team1=team1,
            team2=team2,
            event_title=event_title,
        )
        logger.debug(
            f"[HLTV][STATS] parse_ok match_id={match_id} maps={len(parsed.maps)} players={len(parsed.players)}"
        )
        return parsed

    async def get_latest_result_with_stats(
        self, event_id: str, event_title: str = ""
    ) -> MatchStats | None:
        """获取最近一场比赛的详细数据。"""
        results = await self.get_event_results(event_id, max_results=1)
        if not results:
            logger.warning(f"[HLTV][LATEST_STATS] no_results event={event_id}")
            return None

        result = results[0]
        logger.debug(
            f"[HLTV][LATEST_STATS] picked_latest event={event_id} match_id={result.id} "
            f"teams={result.team1} vs {result.team2}"
        )
        return await self.get_match_stats(
            result.id,
            team1=result.team1,
            team2=result.team2,
            event_title=event_title,
        )

    async def get_event_matches(
        self,
        event_id: str,
        *,
        include_partial_tbd: bool = False,
        max_age: int = 120,
    ) -> EventMatchesPage:
        page = await self._fetch(
            f"{self.BASE_URL}/events/{event_id}/matches",
            cache_key=f"matches:{event_id}",
            ttl=max_age,
        )
        return parse_event_matches_page(
            page.text,
            page.final_url,
            event_id,
            self._tz,
            include_partial_tbd=include_partial_tbd,
        )

    def _save_state(self) -> None:
        atomic_write_json(
            self._state_path,
            {"endpoint": self._endpoint, "scopes": self._cooldown_scopes},
        )

    def reset_cooldown(self) -> None:
        """手动清空所有作用域的访问冷却状态并落盘。"""
        self._cooldown_scopes.clear()
        self._save_state()
        logger.info("[HLTV] 已重置全部访问冷却状态")

    def _check_pause(self, scope: str = GLOBAL_SCOPE) -> None:
        now_ts = time.time()
        for check_sc in (GLOBAL_SCOPE, scope):
            entry = self._cooldown_scopes.get(check_sc)
            if entry is None:
                continue
            until = float(entry.get("blocked_until", 0.0))
            if now_ts < until:
                reason = str(entry.get("reason") or "访问冷却中")
                raise HLTVFetchError(f"冷却中：{reason}", retry_at=until)

    def _pause(
        self, scope: str, reason: str, *, global_ban: bool = False
    ) -> HLTVFetchError:
        target_scope = GLOBAL_SCOPE if global_ban else scope
        prev = self._cooldown_scopes.get(target_scope, {})
        blocks = int(prev.get("blocks", 0)) + 1
        delay = min(self._cooldown * 2 ** min(blocks - 1, 20), self._max_cooldown)
        until = time.time() + delay
        self._cooldown_scopes[target_scope] = {
            "blocked_until": until,
            "blocks": blocks,
            "reason": reason,
        }
        self._save_state()
        logger.warning(
            f"[HLTV] 暂停访问 scope={target_scope} reason={reason} seconds={delay}"
        )
        return HLTVFetchError(reason, retry_at=until, global_ban=global_ban)

    def _clear_scope(self, scope: str) -> None:
        changed = False
        for sc in (GLOBAL_SCOPE, scope):
            if sc in self._cooldown_scopes:
                self._cooldown_scopes.pop(sc, None)
                changed = True
        if changed:
            self._save_state()
            logger.info(f"[HLTV] 作用域 {scope} 已恢复正常访问")

    def _save_session_state(self) -> None:
        atomic_write_json(
            self._session_state_path,
            {
                "selected_session_id": self._session_id,
                "pending_cleanup_session_id": self._pending_cleanup_id,
            },
        )

    async def start(self) -> None:
        async with self._lock:
            self._check_pause(GLOBAL_SCOPE)
            await self._prepare_session()

    async def _prepare_session(self) -> None:
        data = await self._call("sessions.list")
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or any(
            not isinstance(session, str) or not session for session in sessions
        ):
            raise HLTVFetchError("FlareSolverr 会话列表无效", should_cooldown=False)

        if not self._session_id:
            self._session_id = sessions[0] if sessions else str(uuid4())
            self._save_session_state()
            logger.debug(f"[HLTV] 选择浏览器会话 session={self._session_id}")

        if self._session_id not in sessions:
            created = await self._call("sessions.create", session=self._session_id)
            if created.get("session") != self._session_id:
                raise HLTVFetchError(
                    "FlareSolverr 返回了不匹配的会话 ID", should_cooldown=False
                )
            self._session_requests = 0
            logger.debug(f"[HLTV] 新会话创建成功 session={self._session_id}")

        if self._pending_cleanup_id:
            if self._pending_cleanup_id in sessions:
                await self._call("sessions.destroy", session=self._pending_cleanup_id)
            logger.debug(f"[HLTV] 旧会话清理完成 session={self._pending_cleanup_id}")
            self._pending_cleanup_id = ""
            self._save_session_state()

    async def _replace_session(self) -> None:
        self._pending_cleanup_id = self._session_id
        self._session_id = str(uuid4())
        self._save_session_state()
        logger.debug("[HLTV] 开始更换浏览器会话")
        await self._prepare_session()

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._cache.clear()

    async def _fetch(
        self,
        url: str,
        *,
        cache_key: str,
        ttl: int,
        force_refresh: bool = False,
    ) -> FetchResult:
        if self._closed:
            raise HLTVFetchError("客户端已关闭", should_cooldown=False)
        cached = self._cache.get(cache_key)
        if not force_refresh and cached and time.monotonic() - cached[0] < ttl:
            self._cache.move_to_end(cache_key)
            logger.debug(f"[HLTV] cache_hit page={cache_key}")
            return cached[1]
        task = self._inflight.get(cache_key)
        if task is not None:
            return await asyncio.shield(task)
        scope = _scope_of(cache_key)
        self._check_pause(scope)
        task = asyncio.create_task(self._fetch_and_cache(url, cache_key, scope))
        self._inflight[cache_key] = task
        task.add_done_callback(lambda done: self._finish(cache_key, done))
        return await asyncio.shield(task)

    def _finish(self, key: str, task: asyncio.Task[FetchResult]) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _fetch_and_cache(self, url: str, key: str, scope: str) -> FetchResult:
        async with self._lock:
            self._check_pause(scope)
            try:
                await self._prepare_session()
                if self._session_requests >= self._session_max_requests:
                    await self._replace_session()
                try:
                    result = await self._request(url, key)
                except HLTVFetchError as exc:
                    logger.warning(f"[HLTV] 首次抓取失败 page={key} reason={exc.reason}")
                    if not exc.recoverable:
                        raise
                    await self._replace_session()
                    result = await self._request(url, key)
                    logger.info(
                        f"[HLTV] 换会话重试成功 page={key} first_reason={exc.reason}"
                    )
            except HLTVFetchError as exc:
                if exc.should_cooldown:
                    raise self._pause(
                        scope, exc.reason, global_ban=exc.global_ban
                    ) from None
                logger.warning(f"[HLTV] 请求异常（未计入冷却锁）: {exc.reason}")
                raise
            self._clear_scope(scope)
            self._cache[key] = (time.monotonic(), result)
            self._cache.move_to_end(key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
            return result

    async def _call(self, command: str, **params) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout + 30, trust_env=False
            )
        try:
            response = await self._client.post(
                self._endpoint,
                json={"cmd": command, **params},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise HLTVFetchError(
                "FlareSolverr 连接失败或响应无效", should_cooldown=False
            ) from None

        if not isinstance(data, dict):
            raise HLTVFetchError("FlareSolverr 响应无效", should_cooldown=False)
        if data.get("status") != "ok":
            message = str(data.get("message", "")).lower()
            if command == "sessions.destroy" and "session doesn't exist" in message:
                return data
            if command != "request.get":
                raise HLTVFetchError(
                    f"FlareSolverr 会话管理失败（{command}）", should_cooldown=False
                )
            if any(
                marker in message
                for marker in (
                    "tab crashed",
                    "invalid session id",
                    "disconnected",
                    "no such window",
                    "chrome not reachable",
                    "not connected to devtools",
                )
            ):
                raise HLTVFetchError(
                    "FlareSolverr 浏览器崩溃或断连",
                    recoverable=True,
                    should_cooldown=False,
                )
            if "timeout after" in message:
                raise HLTVFetchError("页面处理超时", recoverable=True)
            if any(
                marker in message
                for marker in (
                    "cloudflare has blocked",
                    "access denied",
                )
            ):
                raise HLTVFetchError(
                    "IP 被 Cloudflare 封禁或拒绝访问",
                    recoverable=True,
                    global_ban=True,
                )
            if any(
                marker in message
                for marker in (
                    "captcha detected",
                    "challenge not solved",
                )
            ):
                raise HLTVFetchError("浏览器挑战未通过", recoverable=True)
            raise HLTVFetchError("FlareSolverr 页面处理异常", recoverable=True)
        if not response.is_success:
            raise HLTVFetchError("FlareSolverr API 请求失败", should_cooldown=False)
        return data

    async def _request(self, url: str, key: str) -> FetchResult:
        delay = self._last_request + self._interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_request = time.monotonic()
        self._request_count += 1
        self._session_requests += 1
        logger.debug(
            f"[HLTV] request page={key} count={self._request_count} "
            f"session={self._session_id}"
        )
        data = await self._call(
            "request.get",
            session=self._session_id,
            url=url,
            maxTimeout=self._timeout * 1000,
        )
        solution = data.get("solution")
        if not isinstance(solution, dict):
            raise HLTVFetchError("FlareSolverr 页面响应无效", should_cooldown=False)
        html = solution.get("response")
        final_url = solution.get("url")
        if not isinstance(html, str) or not html.strip():
            raise HLTVFetchError("页面响应为空", recoverable=True)
        if not isinstance(final_url, str):
            raise HLTVFetchError("页面地址缺失", recoverable=True)
        try:
            parts = urlsplit(final_url)
        except ValueError:
            raise HLTVFetchError("页面地址无效", recoverable=True) from None
        if parts.scheme != "https" or parts.hostname != "www.hltv.org":
            raise HLTVFetchError("页面跳转到了非 HLTV 地址", recoverable=True)

        soup = make_soup(html)
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        heading = soup.h1.get_text(" ", strip=True).lower() if soup.h1 else ""
        hard_blocked = any(
            marker in title or marker in heading
            for marker in ("access denied", "sorry, you have been blocked")
        )
        if hard_blocked:
            raise HLTVFetchError(
                "页面提示拒绝访问或 IP 被封禁", recoverable=True, global_ban=True
            )
        challenged = (
            "just a moment" in title
            or "just a moment" in heading
            or soup.select_one(
                "#challenge-form, #cf-challenge-running, #cf-please-wait, "
                "#challenge-spinner, #turnstile-wrapper, .cf-error-details"
            )
            is not None
        )
        if challenged:
            raise HLTVFetchError("页面仍为 Cloudflare 挑战页", recoverable=True)
        if "hltv.org" not in title or any(
            marker in title for marker in ("not found", "server error", "unavailable")
        ):
            raise HLTVFetchError("无法识别 HLTV 页面", recoverable=True)
        body = soup.body
        if body is not None:
            for element in body.select(
                "script, style, template, noscript, #onetrust-consent-sdk"
            ):
                element.decompose()
        if body is None or not body.get_text(" ", strip=True):
            raise HLTVFetchError("页面正文为空", recoverable=True)
        logger.debug(
            f"[HLTV] response page={key} "
            f"elapsed={time.monotonic() - self._last_request:.2f}s"
        )
        return FetchResult(text=html, final_url=final_url)
