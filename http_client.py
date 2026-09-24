"""
HLTV HTTP 客户端（请求节流 / 页面缓存 / Cloudflare 挑战处理 / 会话自愈）

背景：HLTV 由 Cloudflare 保护。`/matches` 这类动态页面比 `/events` 这类
可缓存页面更容易触发 CF 的交互式挑战，返回 403 + "Just a moment..." 挑战页。
挑战页靠调整请求头解不掉：它要求浏览器执行 JS 并带回 cf_clearance，必须换到
一个「已经通过挑战的浏览器会话」。

因此这里采用分层策略：

1. 首选 curl_cffi 直连（TLS / HTTP2 指纹伪装），成本最低、延迟最小；
2. 命中挑战页时立刻停止重试 —— 对挑战页连续重试只会抬高 CF 风控评分，
   这正是之前 403 越试越多的原因；改为交给 FlareSolverr 的持久会话求解；
3. 求解成功后把 FlareSolverr 拿到的 cf_clearance 及其配套 User-Agent
   回灌给 curl_cffi 会话，后续请求重新走快速路径（一次浏览器求解，
   覆盖之后的一批请求）；
4. 所有请求串行 + 全局最小间隔，避免同一出口 IP 的请求风暴；
5. 页面带 TTL 缓存，并对同一 URL 合并并发请求（in-flight dedup）；
6. 连续命中挑战时进入冷却（指数退避，可持久化到磁盘），冷却期内直接
   快速失败、不再访问 HLTV；冷却到期后的下一次自然轮询即作为恢复探测。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
from curl_cffi.requests import AsyncSession

from .log_utils import get_logger

logger = get_logger("hltv_sub.http_client")

# 缓存条目上限（按 URL 计），防止长时间运行下内存无限增长
_CACHE_MAX_ENTRIES = 256

# Cloudflare 交互式挑战页的特征（可用 FlareSolverr 求解）
_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "challenge-platform",
    "cf_chl_opt",
    "cf-chl-",
    "verifying you are human",
    "enable javascript and cookies to continue",
)

# Cloudflare 硬封禁页的特征（换会话也无效，只能冷却等待）
_CF_BLOCK_MARKERS = (
    "you have been blocked",
    "attention required",
    "error 1020",
    "access denied",
)

# 挑战页体积很小，只扫描前若干字节即可判定，避免无谓的全量文本处理
_CF_SCAN_LIMIT = 8000

# FlareSolverr 报告的可恢复错误：换一个浏览器会话后重试往往就好了
_FS_RECOVERABLE_MARKERS = (
    "tab crashed",
    "invalid session id",
    "disconnected",
    "no such window",
    "chrome not reachable",
    "not connected to devtools",
    "timeout after",
)

# 瞬时网络错误的重试次数（连接抖动 / 5xx），与挑战处理无关
_TRANSIENT_RETRIES = 2


class HLTVFetchError(Exception):
    """HLTV 暂时不可访问（含冷却信息），供上层决定如何提示用户"""

    def __init__(self, reason: str, *, retry_at: float = 0.0, recoverable: bool = False):
        self.reason = reason
        self.retry_at = retry_at
        self.recoverable = recoverable
        super().__init__(reason)


@dataclass
class FetchResult:
    text: Optional[str]
    status_code: Optional[int] = None
    final_url: str = ""
    error: str = ""
    # 请求因冷却被直接拒绝（未访问 HLTV），调用方据此区分「真没数据」
    paused: bool = False
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text)


def _chrome_hint_version(impersonate: str) -> str:
    if impersonate == "chrome":
        return "142"
    if impersonate.startswith("chrome"):
        suffix = impersonate.removeprefix("chrome")
        version = "".join(ch for ch in suffix if ch.isdigit())
        if version:
            return version
    return "142"


_UA_PLATFORM_HINTS: tuple[tuple[str, str, str], ...] = (
    ("windows", '"Windows"', "?0"),
    ("macintosh", '"macOS"', "?0"),
    ("mac os x", '"macOS"', "?0"),
    ("android", '"Android"', "?1"),
    ("iphone", '"iOS"', "?1"),
    ("ipad", '"iOS"', "?1"),
    ("linux", '"Linux"', "?0"),
)


def _platform_from_ua(user_agent: str) -> tuple[str, str]:
    low = user_agent.lower()
    for token, platform, mobile in _UA_PLATFORM_HINTS:
        if token in low:
            return platform, mobile
    return '"Windows"', "?0"


def _chrome_version_from_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", user_agent)
    return match.group(1) if match else ""


def _build_headers(
    impersonate: str, url: str, user_agent: str = ""
) -> dict[str, str]:
    """构建完整的现代 Chrome 浏览器请求头

    ``user_agent`` 非空时强制使用它 —— cf_clearance 与签发它的 User-Agent
    绑定，复用 FlareSolverr 拿到的通行证时必须一并复用它的 UA。此时
    Sec-Ch-Ua 系列也同步按该 UA 推导，否则「Linux 的 UA + Windows 的
    sec-ch-ua-platform」本身就是 Cloudflare 会抓的不一致。

    导航上下文按目标 URL 推导 —— 站内子页面在真实浏览器里是站内跳转
    （same-origin + Referer），一律声明成地址栏全新导航反而是异常信号。
    """
    chrome_version = _chrome_version_from_ua(user_agent) or _chrome_hint_version(
        impersonate
    )
    if user_agent:
        platform, mobile = _platform_from_ua(user_agent)
    else:
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )
        platform, mobile = '"Windows"', "?0"

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Sec-Ch-Ua": f'"Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}", "Not.A/Brand";v="99"',
        "Sec-Ch-Ua-Mobile": mobile,
        "Sec-Ch-Ua-Platform": platform,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    parts = urlsplit(url)
    if parts.path in ("", "/"):
        headers["Sec-Fetch-Site"] = "none"
    else:
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["Referer"] = f"{parts.scheme}://{parts.netloc}/"

    return headers


def _looks_like_cf_challenge(status_code: Optional[int], body: str) -> bool:
    if status_code not in (403, 429, 503):
        return False
    head = (body or "")[:_CF_SCAN_LIMIT].lower()
    return any(marker in head for marker in _CF_CHALLENGE_MARKERS)


def _looks_like_cf_block(status_code: Optional[int], body: str) -> bool:
    if status_code not in (403, 429, 503):
        return False
    head = (body or "")[:_CF_SCAN_LIMIT].lower()
    return any(marker in head for marker in _CF_BLOCK_MARKERS)


# 冷却作用域：Cloudflare 的挑战规则通常按路径生效 —— 动态页（比赛列表）
# 会被挑战，而列表页（赛事列表）在 CDN 上有缓存、往往照常返回。因此冷却
# 必须按作用域隔离，否则 matches 被拦会连带停掉本来正常的 events/results。
_SCOPE_GLOBAL = "*"


def _scope_for_url(url: str) -> str:
    """按目标 URL 归类请求作用域，用于隔离冷却状态"""
    path = urlsplit(url).path
    if "matches" in path:
        # /events/{id}/matches 与 /matches/{id}/... 都归入 matches
        return "matches"
    if path.startswith("/results"):
        return "results"
    if path.startswith("/events"):
        return "events"
    return "other"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写 JSON：崩溃或断电时不会留下半截文件"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except Exception as e:
        logger.debug(f"[HLTV] 写入状态文件失败: {path} err={e}")


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug(f"[HLTV] 读取状态文件失败: {path} err={e}")
    return {}


class HLTVHttpClient:
    def __init__(
        self,
        *,
        timeout: int,
        request_interval: float = 8.0,
        proxy_list: list[str] | None = None,
        impersonate: str = "chrome136",
        flaresolverr_url: str = "",
        flaresolverr_timeout: int = 60,
        cooldown: int = 600,
        max_cooldown: int = 7200,
        state_dir: str | Path | None = None,
    ) -> None:
        self._timeout = timeout
        self._interval = max(0.0, float(request_interval))
        self._proxy_list = proxy_list or []
        self._impersonate = impersonate
        self._flaresolverr_url = (
            flaresolverr_url.rstrip("/") if flaresolverr_url else ""
        )
        self._flaresolverr_timeout = flaresolverr_timeout
        self._cooldown = cooldown
        self._max_cooldown = max_cooldown
        self._session: Optional[AsyncSession] = None

        # 状态持久化（冷却状态 + FlareSolverr 会话 + cf_clearance）
        self._state_dir = Path(state_dir) if state_dir else None
        self._state_path = self._state_dir / "http_state.json" if self._state_dir else None
        self._session_state_path = (
            self._state_dir / "session_state.json" if self._state_dir else None
        )

        # 请求节流 / 并发控制
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._request_count = 0
        self._cache: OrderedDict[str, tuple[float, FetchResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._closed = False

        # 冷却（熔断）状态：按作用域隔离，见 _scope_for_url
        # scope -> {"until": float, "blocks": int, "reason": str}
        self._cooldowns: dict[str, dict] = {}

        # FlareSolverr 会话状态：会话独立于 Bot 进程，重启后继续复用
        self._fs_client: Optional[httpx.AsyncClient] = None
        self._fs_session_id = ""
        self._fs_pending_cleanup_id = ""

        # 从 FlareSolverr 回灌的 Cloudflare 通行证
        self._clearance_value = ""
        self._clearance_ua = ""
        self._clearance_expires = 0.0

        # 代理轮换状态（失败退避 + 冷却）
        self._proxy_cursor: int = 0
        self._proxy_failures: dict[str, int] = {}
        self._proxy_cooldown_until: dict[str, float] = {}
        self._proxy_backoff_base_seconds: int = 3
        self._proxy_backoff_max_seconds: int = 60

        self._load_state()

    # -------------------- 状态持久化 --------------------

    def _load_state(self) -> None:
        if self._state_path is None:
            return

        state = _read_json(self._state_path)
        self._cooldowns = self._parse_cooldowns(state)
        now = time.time()
        for scope, entry in self._cooldowns.items():
            if entry["until"] > now:
                logger.info(
                    f"[HLTV] 读取到未过期的访问冷却 scope={scope} "
                    f"({entry['reason'] or '未知原因'}, 剩余 {entry['until'] - now:.0f}s)"
                )

        session_state = _read_json(self._session_state_path)
        self._fs_session_id = str(session_state.get("selected_session_id", "") or "")
        self._fs_pending_cleanup_id = str(
            session_state.get("pending_cleanup_session_id", "") or ""
        )

        clearance = session_state.get("clearance") or {}
        if isinstance(clearance, dict):
            expires = float(clearance.get("expires", 0.0) or 0.0)
            if clearance.get("value") and expires > time.time() + 60:
                self._clearance_value = str(clearance["value"])
                self._clearance_ua = str(clearance.get("user_agent", "") or "")
                self._clearance_expires = expires
                logger.info(
                    f"[HLTV] 复用已保存的 Cloudflare 通行证，剩余 {expires - time.time():.0f}s"
                )

    def _parse_cooldowns(self, state: dict) -> dict[str, dict]:
        """读取冷却状态；兼容早期的全局扁平格式（映射为全局作用域）"""
        out: dict[str, dict] = {}

        raw = state.get("cooldowns")
        if isinstance(raw, dict):
            for scope, entry in raw.items():
                if not isinstance(scope, str) or not isinstance(entry, dict):
                    continue
                try:
                    until = float(entry.get("until", 0.0) or 0.0)
                    blocks = int(entry.get("blocks", 0) or 0)
                except (TypeError, ValueError):
                    continue
                out[scope] = {
                    "until": until,
                    "blocks": blocks,
                    "reason": str(entry.get("reason", "") or ""),
                }
            return out

        legacy_until = float(state.get("blocked_until", 0.0) or 0.0)
        if legacy_until > time.time():
            out[_SCOPE_GLOBAL] = {
                "until": legacy_until,
                "blocks": int(state.get("blocks", 0) or 0),
                "reason": str(state.get("pause_reason", "") or ""),
            }
        return out

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        _atomic_write_json(
            self._state_path,
            {"cooldowns": self._cooldowns},
        )

    def _save_session_state(self) -> None:
        if self._session_state_path is None:
            return
        clearance: dict = {}
        if self._clearance_value and self._clearance_expires > 0:
            clearance = {
                "value": self._clearance_value,
                "user_agent": self._clearance_ua,
                "expires": self._clearance_expires,
            }
        _atomic_write_json(
            self._session_state_path,
            {
                "selected_session_id": self._fs_session_id,
                "pending_cleanup_session_id": self._fs_pending_cleanup_id,
                "clearance": clearance,
            },
        )

    # -------------------- 冷却（熔断，按作用域隔离） --------------------

    def _active_cooldown(self, scope: str) -> Optional[dict]:
        """取该作用域当前生效的冷却（全局冷却对所有作用域生效）"""
        now = time.time()
        for key in (scope, _SCOPE_GLOBAL):
            entry = self._cooldowns.get(key)
            if entry and entry["until"] > now:
                return {**entry, "scope": key}
        return None

    def pause_info(self, scope: str = "") -> tuple[str, float]:
        """返回冷却状态 (原因, 可重试时间戳)；未冷却时为 ("", 0.0)

        不传 scope 时：全局冷却优先，否则返回最晚解除的那个（保守估计，
        避免把「马上能恢复」说得比实际乐观）。
        """
        if scope:
            entry = self._active_cooldown(scope)
            return (entry["reason"] or "访问冷却中", entry["until"]) if entry else ("", 0.0)

        now = time.time()
        active = [e for e in self._cooldowns.values() if e["until"] > now]
        if not active:
            return "", 0.0

        global_entry = self._cooldowns.get(_SCOPE_GLOBAL)
        if global_entry and global_entry["until"] > now:
            return global_entry["reason"] or "访问冷却中", global_entry["until"]

        latest = max(active, key=lambda e: e["until"])
        return latest["reason"] or "访问冷却中", latest["until"]

    def _check_pause(self, scope: str) -> Optional[HLTVFetchError]:
        entry = self._active_cooldown(scope)
        if entry:
            return HLTVFetchError(
                entry["reason"] or "访问冷却中", retry_at=entry["until"]
            )
        return None

    def _pause(
        self, reason: str, *, scope: str, blocked: bool = True
    ) -> HLTVFetchError:
        """在指定作用域进入冷却：连续被拦时指数退避，避免持续打同一出口 IP"""
        entry = self._cooldowns.setdefault(
            scope, {"until": 0.0, "blocks": 0, "reason": ""}
        )
        if blocked:
            entry["blocks"] = int(entry.get("blocks", 0)) + 1
            delay = min(
                self._cooldown * 2 ** min(entry["blocks"] - 1, 20), self._max_cooldown
            )
        else:
            # 非拦截类故障（如网络中断）给一个短冷却，防止请求风暴
            delay = min(60, self._cooldown)

        entry["until"] = time.time() + delay
        entry["reason"] = reason
        self._save_state()
        logger.warning(
            f"[HLTV] 进入冷却 scope={scope} reason={reason} "
            f"blocks={entry['blocks']} delay={delay}s"
        )
        return HLTVFetchError(reason, retry_at=entry["until"])

    def _note_success(self, scope: str) -> None:
        """请求成功说明该链路可用：清掉本作用域与全局的冷却计数"""
        cleared = False
        for key in (scope, _SCOPE_GLOBAL):
            entry = self._cooldowns.get(key)
            if entry and (entry["until"] or entry["blocks"]):
                entry["until"] = 0.0
                entry["blocks"] = 0
                entry["reason"] = ""
                cleared = True
        if cleared:
            logger.info(f"[HLTV] scope={scope} 请求恢复成功，重置冷却计数")
            self._save_state()

    # -------------------- 请求节流 --------------------

    async def _wait_turn(self) -> None:
        """串行 + 最小间隔：同一出口 IP 上不允许请求风暴

        调用方需持有 self._lock，因此这里是「排队等自己的时间片」，
        而不是并发抢跑。
        """
        delay = self._last_request + self._interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_request = time.monotonic()
        self._request_count += 1

    # -------------------- 会话管理 --------------------

    def _effective_clearance_ua(self) -> str:
        """仅在通行证仍有效时才沿用它的 UA，避免「过期通行证 + 别人家的 UA」"""
        if self._clearance_value and self._clearance_expires > time.time():
            return self._clearance_ua
        return ""

    def _apply_clearance(self, session: AsyncSession) -> None:
        if not self._clearance_value or self._clearance_expires <= time.time():
            return
        try:
            session.cookies.set(
                "cf_clearance", self._clearance_value, domain=".hltv.org", path="/"
            )
        except Exception as e:
            logger.debug(f"[HLTV] 写入 cf_clearance 失败: {e}")

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate=self._impersonate)
            self._apply_clearance(self._session)
        return self._session

    async def start(self) -> None:
        """准备浏览器会话（若配置了 FlareSolverr），不访问 HLTV 页面

        只和本机 FlareSolverr 通信，不碰 HLTV，所以不受任何作用域冷却影响。
        """
        if not self._flaresolverr_url:
            return
        async with self._lock:
            await self._prepare_fs_session()

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()

        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        if self._fs_client is not None:
            try:
                await self._fs_client.aclose()
            except Exception:
                pass
            self._fs_client = None

    # -------------------- 代理轮换 --------------------

    def _pick_proxy(self) -> Optional[str]:
        """选择当前可用代理（轮换 + 冷却过滤）"""
        if not self._proxy_list:
            return None

        now = time.time()
        candidates = [
            p for p in self._proxy_list if self._proxy_cooldown_until.get(p, 0.0) <= now
        ]
        if not candidates:
            # 全部在冷却中时，允许继续轮换，避免完全阻塞
            candidates = self._proxy_list

        idx = self._proxy_cursor % len(candidates)
        proxy = candidates[idx]
        self._proxy_cursor = (self._proxy_cursor + 1) % max(1, len(candidates))
        return proxy

    def _mark_proxy_failure(self, proxy: Optional[str]) -> None:
        if not proxy:
            return

        failures = self._proxy_failures.get(proxy, 0) + 1
        self._proxy_failures[proxy] = failures

        backoff = min(
            self._proxy_backoff_base_seconds * (2 ** max(0, failures - 1)),
            self._proxy_backoff_max_seconds,
        )
        self._proxy_cooldown_until[proxy] = time.time() + backoff

    def _mark_proxy_success(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        self._proxy_failures[proxy] = 0
        self._proxy_cooldown_until[proxy] = 0.0

    # -------------------- FlareSolverr --------------------

    async def _fs_call(self, command: str, **params) -> dict:
        """调用 FlareSolverr API，失败时抛可恢复/不可恢复的 HLTVFetchError"""
        if self._fs_client is None:
            self._fs_client = httpx.AsyncClient(
                timeout=self._flaresolverr_timeout + 30, trust_env=False
            )

        try:
            response = await self._fs_client.post(
                self._flaresolverr_url, json={"cmd": command, **params}
            )
            data = response.json()
        except Exception as e:
            raise HLTVFetchError(f"FlareSolverr 连接失败: {e!r}") from None

        if not isinstance(data, dict):
            raise HLTVFetchError("FlareSolverr 响应格式无效")

        if data.get("status") != "ok":
            message = str(data.get("message", "")).lower()
            if command == "sessions.destroy" and "session doesn't exist" in message:
                return data
            if command != "request.get":
                raise HLTVFetchError(f"FlareSolverr 会话管理失败（{command}）")
            # 浏览器崩溃 / 页面超时属于「换个会话可能就好」的情况，
            # 由调用方触发 session 自愈后重试。
            recoverable = any(m in message for m in _FS_RECOVERABLE_MARKERS) or any(
                m in message
                for m in ("captcha detected", "challenge not solved", "cloudflare")
            )
            raise HLTVFetchError(
                "浏览器挑战未通过" if recoverable else "FlareSolverr 页面处理异常",
                recoverable=recoverable,
            )

        if not response.is_success:
            raise HLTVFetchError("FlareSolverr API 请求失败")

        return data

    async def _prepare_fs_session(self) -> None:
        """确保 FlareSolverr 上存在可用的持久会话

        会话 ID 持久化到磁盘，Bot 重启后继续复用同一个浏览器会话，
        已取得的 cf_clearance 就不会因为重启而白费。
        """
        data = await self._fs_call("sessions.list")
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            raise HLTVFetchError("FlareSolverr 会话列表无效")
        known = {s for s in sessions if isinstance(s, str) and s}

        if not self._fs_session_id:
            self._fs_session_id = next(iter(known), "") or f"hltv-{os.getpid()}-{int(time.time())}"
            self._save_session_state()
            logger.info(f"[HLTV] 选用浏览器会话 session={self._fs_session_id}")

        if self._fs_session_id not in known:
            created = await self._fs_call("sessions.create", session=self._fs_session_id)
            if created.get("session") != self._fs_session_id:
                raise HLTVFetchError("FlareSolverr 返回了不匹配的会话 ID")
            logger.info(f"[HLTV] 已创建浏览器会话 session={self._fs_session_id}")

        # 上一次换会话被中断时，这里补做清理，避免浏览器实例泄漏
        if self._fs_pending_cleanup_id:
            if self._fs_pending_cleanup_id in known:
                await self._fs_call(
                    "sessions.destroy", session=self._fs_pending_cleanup_id
                )
            logger.info(f"[HLTV] 旧会话清理完成 session={self._fs_pending_cleanup_id}")
            self._fs_pending_cleanup_id = ""
            self._save_session_state()

    async def _replace_fs_session(self) -> None:
        """换一个新会话（session 自愈：挑战未通过 / 浏览器崩溃后重新开局）"""
        self._fs_pending_cleanup_id = self._fs_session_id
        self._fs_session_id = f"hltv-{os.getpid()}-{int(time.time() * 1000)}"
        # 先落盘新旧 ID：创建超时或 Bot 重启后仍能继续同一次更换流程
        self._save_session_state()
        logger.info(
            f"[HLTV] 更换浏览器会话 old={self._fs_pending_cleanup_id} new={self._fs_session_id}"
        )
        await self._prepare_fs_session()

    def _harvest_clearance(self, solution: dict) -> bool:
        """把 FlareSolverr 拿到的通行证回灌给 curl_cffi 会话

        返回本次是否新得到（或刷新了）cf_clearance。
        """
        cookies = solution.get("cookies")
        if not isinstance(cookies, list):
            return False

        value = ""
        expires = 0.0
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            if cookie.get("name") != "cf_clearance":
                continue
            value = str(cookie.get("value") or "")
            try:
                expires = float(cookie.get("expires") or 0.0)
            except (TypeError, ValueError):
                expires = 0.0
            break

        if not value:
            return False

        # 没有过期时间时按 30 分钟保守估算
        if expires <= time.time():
            expires = time.time() + 1800

        ua = str(solution.get("userAgent") or "") or self._clearance_ua
        refreshed = value != self._clearance_value
        self._clearance_value = value
        self._clearance_ua = ua
        self._clearance_expires = expires
        self._save_session_state()

        if self._session is not None:
            try:
                self._session.cookies.set(
                    "cf_clearance", value, domain=".hltv.org", path="/"
                )
            except Exception as e:
                logger.debug(f"[HLTV] 回灌 cf_clearance 失败: {e}")

        if refreshed:
            logger.info(f"[HLTV] 已获取 Cloudflare 通行证，有效期 {expires - time.time():.0f}s")
        return refreshed

    async def _solve_via_flaresolverr(self, url: str) -> FetchResult:
        """用 FlareSolverr 求解挑战并取回页面（含一次会话自愈重试）"""
        if not self._flaresolverr_url:
            return FetchResult(
                text=None, error="flaresolverr_not_configured", paused=False
            )

        for attempt in range(2):
            try:
                await self._prepare_fs_session()
                await self._wait_turn()
                logger.info(f"[HLTV] FlareSolverr 求解: {url}")
                data = await self._fs_call(
                    "request.get",
                    session=self._fs_session_id,
                    url=url,
                    maxTimeout=self._flaresolverr_timeout * 1000,
                )
            except HLTVFetchError as e:
                if e.recoverable and attempt == 0:
                    logger.warning(f"[HLTV] 浏览器会话异常，换会话重试: {e.reason}")
                    await self._replace_fs_session()
                    continue
                raise

            solution = data.get("solution")
            if not isinstance(solution, dict):
                raise HLTVFetchError("FlareSolverr 页面响应无效")

            # 即使页面仍被拦，通行证也可能已经拿到，先回灌再判断
            self._harvest_clearance(solution)

            html = solution.get("response")
            final_url = str(solution.get("url") or url)
            if not isinstance(html, str) or not html.strip():
                if attempt == 0:
                    logger.warning("[HLTV] FlareSolverr 返回空页面，换会话重试")
                    await self._replace_fs_session()
                    continue
                raise HLTVFetchError("FlareSolverr 返回空页面", recoverable=True)

            parts = urlsplit(final_url)
            if parts.hostname != "www.hltv.org":
                raise HLTVFetchError(f"页面跳转到了非 HLTV 地址: {final_url}")

            # FlareSolverr 的 solution.status 恒为 200，必须看页面本身
            if _looks_like_cf_challenge(403, html) or _looks_like_cf_block(403, html):
                if attempt == 0:
                    logger.warning("[HLTV] 页面仍为挑战页，换会话重试")
                    await self._replace_fs_session()
                    continue
                return FetchResult(
                    text=None,
                    status_code=403,
                    final_url=final_url,
                    error="cf_challenge_unsolved",
                )

            logger.info(f"[HLTV] FlareSolverr 求解成功: {url}")
            return FetchResult(text=html, status_code=200, final_url=final_url)

        return FetchResult(text=None, error="flaresolverr_retry_exhausted")

    # -------------------- curl_cffi 快速路径 --------------------

    async def _request_curl(self, url: str) -> FetchResult:
        """一次 curl_cffi 直连请求（不含重试）；挑战页归类为 cf_challenge"""
        session = await self._get_session()
        proxy = self._pick_proxy()
        headers = _build_headers(self._impersonate, url, self._effective_clearance_ua())

        response = await session.get(
            url,
            proxy=proxy,
            timeout=self._timeout,
            headers=headers,
        )

        status = response.status_code
        body = response.text or ""
        final_url = str(response.url)

        if status == 200 and not _looks_like_cf_challenge(status, body):
            self._mark_proxy_success(proxy)
            return FetchResult(text=body, status_code=status, final_url=final_url)

        self._mark_proxy_failure(proxy)

        if _looks_like_cf_block(status, body):
            return FetchResult(
                text=None,
                status_code=status,
                final_url=final_url,
                error="cf_block",
            )

        if _looks_like_cf_challenge(status, body) or status == 403:
            return FetchResult(
                text=None,
                status_code=status,
                final_url=final_url,
                error="cf_challenge",
            )

        return FetchResult(
            text=None, status_code=status, final_url=final_url, error=f"http_{status}"
        )

    async def _fetch_direct(self, url: str, scope: str) -> FetchResult:
        """快速路径 + 瞬时故障重试 + 挑战兜底

        - 挑战页：不重试（重试只会加重风控），交 FlareSolverr；
        - 瞬时错误（超时 / 5xx / 网络抖动）：小退避重试；
        - 挑战求解后若拿到了通行证，再试一次快速路径 ——
          这一步让「一次浏览器求解」换来之后一批请求的低延迟。

        失败时只在 ``scope`` 作用域内冷却，不影响其它路径的正常查询。
        """
        result = FetchResult(text=None, error="not_attempted")

        for attempt in range(_TRANSIENT_RETRIES):
            if attempt:
                await asyncio.sleep(min(2.0 * attempt, 5.0) + random.uniform(0, 1.0))
            try:
                await self._wait_turn()
                result = await self._request_curl(url)
            except Exception as e:
                result = FetchResult(text=None, error=f"exception: {e!r}")

            if result.text:
                return result
            # 挑战/硬封禁不是重试能解决的，直接跳出
            if result.error in ("cf_challenge", "cf_block"):
                break

        if result.error == "cf_block":
            # 硬封禁对整个出口 IP 生效，因此冷却全局作用域
            raise self._pause("出口 IP 被 Cloudflare 封禁", scope=_SCOPE_GLOBAL)

        if result.error != "cf_challenge":
            return result

        # 命中挑战：交给 FlareSolverr 求解
        fs_result = await self._solve_via_flaresolverr(url)
        if fs_result.text:
            if self._clearance_value:
                try:
                    await self._wait_turn()
                    fast = await self._request_curl(url)
                except Exception as e:
                    logger.debug(f"[HLTV] 通行证快速路径重试失败: {e!r}")
                else:
                    if fast.text:
                        logger.info("[HLTV] 通行证生效，已回到 curl_cffi 快速路径")
                        return fast
            return fs_result

        if fs_result.error == "flaresolverr_not_configured":
            raise self._pause(
                "命中 Cloudflare 挑战且未配置 FlareSolverr（建议配置后重试）",
                scope=scope,
            )

        raise self._pause("Cloudflare 挑战未通过", scope=scope)

    # -------------------- 对外接口 --------------------

    async def fetch_with_meta(
        self,
        url: str,
        *,
        cache_key: str = "",
        ttl: float = 0.0,
        force_refresh: bool = False,
    ) -> FetchResult:
        """获取 HTML + 响应元信息

        ``cache_key`` / ``ttl`` 给出时启用页面缓存与并发合并；同一 URL 的
        并发调用只会产生一次真实请求，其余等待同一结果。
        """
        if self._closed:
            return FetchResult(text=None, error="客户端已关闭")

        key = cache_key or url
        now = time.monotonic()

        if cached := self._cache.get(key):
            if not force_refresh and cached[0] > now:
                self._cache.move_to_end(key)
                logger.debug(f"[HLTV] 命中缓存: {key}")
                result = cached[1]
                return FetchResult(
                    text=result.text,
                    status_code=result.status_code,
                    final_url=result.final_url,
                    error=result.error,
                    from_cache=True,
                )
            if cached[0] <= now:
                self._cache.pop(key, None)

        task = self._inflight.get(key)
        if task is not None:
            # 已有一模一样的请求在跑，等它的结果，不重复打 HLTV
            return await asyncio.shield(task)

        scope = _scope_for_url(url)
        exc = self._check_pause(scope)
        if exc:
            logger.info(f"[HLTV] 冷却中跳过请求: {url} (scope={scope}, {exc.reason})")
            return FetchResult(text=None, error=f"paused: {exc.reason}", paused=True)

        task = asyncio.create_task(self._fetch_and_cache(url, key, ttl, scope))
        self._inflight[key] = task
        task.add_done_callback(lambda done: self._finish(key, done))
        return await asyncio.shield(task)

    def _finish(self, key: str, task: asyncio.Task) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            # 取一次异常，避免「Task exception was never retrieved」噪音
            task.exception()

    async def _fetch_and_cache(
        self, url: str, key: str, ttl: float, scope: str
    ) -> FetchResult:
        async with self._lock:
            exc = self._check_pause(scope)
            if exc:
                return FetchResult(text=None, error=f"paused: {exc.reason}", paused=True)

            try:
                result = await self._fetch_direct(url, scope)
            except HLTVFetchError as e:
                # FlareSolverr 侧抛出的错误没有经过 _pause()，这里补上熔断，
                # 否则「挑战解不掉」会变成每次请求都重新打一遍 HLTV。
                # 与 Cloudflare 无关的故障（例如 FlareSolverr 挂了）只给短冷却，
                # 避免把一次服务异常放大成小时级的冷却。
                if not e.retry_at:
                    blocked = any(
                        k in e.reason for k in ("Cloudflare", "挑战", "封禁")
                    )
                    self._pause(e.reason, scope=scope, blocked=blocked)
                return FetchResult(
                    text=None, error=f"paused: {e.reason}", paused=True
                )

            if not result.text:
                return result

            self._note_success(scope)

            if ttl > 0:
                self._cache[key] = (time.monotonic() + ttl, result)
                self._cache.move_to_end(key)
                while len(self._cache) > _CACHE_MAX_ENTRIES:
                    self._cache.popitem(last=False)

            return result

    async def fetch(
        self,
        url: str,
        *,
        cache_key: str = "",
        ttl: float = 0.0,
        force_refresh: bool = False,
    ) -> Optional[str]:
        """发送请求获取 HTML（兼容旧接口，失败返回 None）"""
        result = await self.fetch_with_meta(
            url, cache_key=cache_key, ttl=ttl, force_refresh=force_refresh
        )
        return result.text
