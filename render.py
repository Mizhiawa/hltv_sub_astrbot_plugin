"""HLTV HTML/图片渲染模块：适配 Ubuntu Docker Playwright 浏览器池与 AstrBot T2I 自动回退。"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2
import pytz

from .config import PluginConfig
from .log_compat import logger
from .models import (
    EventInfo,
    EventMvp,
    MatchInfo,
    MatchStats,
    ResultInfo,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
IMAGE_TIMEOUT_MS = 15_000

VETO_PATTERN = re.compile(r"^\d+\.\s*(.+?)\s+(removed|picked)\s+(.+)$")
LEFTOVER_PATTERN = re.compile(r"^\d+\.\s*(.+?)\s+was left over$")

# Docker 容器必须带 --no-sandbox 与 --disable-dev-shm-usage（避免 64MB /dev/shm 渲染大图崩溃）
DOCKER_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

SYSTEM_CHROMIUM_EXECUTABLES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
)

MISANS_WEIGHTS = {
    "Regular": 400,
    "Medium": 500,
    "Semibold": 600,
    "Bold": 700,
    "Heavy": 900,
}


def _chrome_user_agent(version: str) -> str:
    """去除默认 HeadlessChrome 标识，防止 Cloudflare 拦截 HLTV 静态队标与地图横幅。"""
    major = (version or "131").split(".")[0]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


@lru_cache(maxsize=1)
def _barlow_font_css() -> str:
    """将内置的 Barlow Condensed 窄体数字字体内联为 Data URI，消除跨环境路径与远程 T2I 字体缺失问题。"""
    specs = (
        ("BarlowCondensed-Medium.ttf", 500),
        ("BarlowCondensed-SemiBold.ttf", 600),
        ("BarlowCondensed-Bold.ttf", 700),
    )
    rules: list[str] = []
    font_dir = TEMPLATE_DIR / "fonts"
    for filename, weight in specs:
        fp = font_dir / filename
        if fp.is_file():
            encoded = base64.b64encode(fp.read_bytes()).decode("ascii")
            rules.append(
                f'@font-face{{font-family:"Barlow Condensed";'
                f'src:url("data:font/ttf;base64,{encoded}") format("truetype");'
                f"font-weight:{weight};font-style:normal;}}"
            )
    return "\n".join(rules)


def _safe_is_file(path: Path) -> bool:
    with suppress(OSError):
        return path.is_file()
    return False


def _discover_cjk_font_css(custom_font_dir: str, data_dir: Path) -> str:
    """按优先级搜索 MiSans 或 Ubuntu/Linux/Windows/macOS 系统内置中文字体。"""
    search_dirs: list[Path] = []
    if custom_font_dir:
        search_dirs.append(Path(custom_font_dir))
    search_dirs.append(data_dir / "fonts")
    search_dirs.append(TEMPLATE_DIR / "fonts")

    # 1. 若存在 MiSans 字重文件，完整映射 5 个字重到 Haku 字体族（与 HakuBot 原版完全一致）
    for folder in search_dirs:
        if _safe_is_file(folder / "MiSans-Regular.ttf"):
            rules = []
            for weight_name, weight_num in MISANS_WEIGHTS.items():
                candidate = folder / f"MiSans-{weight_name}.ttf"
                if not _safe_is_file(candidate):
                    candidate = folder / "MiSans-Regular.ttf"
                rules.append(
                    f"@font-face{{font-family:'Haku';src:url('{candidate.resolve().as_uri()}');"
                    f"font-weight:{weight_num};}}"
                )
            return "\n".join(rules)

    # 2. Ubuntu / Debian 容器与常见系统的本地中文字体文件兜底
    regular_candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    bold_candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
    ]
    if os.name == "nt":
        regular_candidates.append(Path("C:/Windows/Fonts/msyh.ttc"))
        bold_candidates.append(Path("C:/Windows/Fonts/msyhbd.ttc"))

    reg_path = next((p for p in regular_candidates if _safe_is_file(p)), None)
    if reg_path is None:
        return ""
    bold_path = next((p for p in bold_candidates if _safe_is_file(p)), reg_path)

    reg_uri = reg_path.resolve().as_uri()
    bold_uri = bold_path.resolve().as_uri()
    return "\n".join(
        [
            f"@font-face{{font-family:'Haku';src:url('{reg_uri}');font-weight:400;}}",
            f"@font-face{{font-family:'Haku';src:url('{reg_uri}');font-weight:500;}}",
            f"@font-face{{font-family:'Haku';src:url('{bold_uri}');font-weight:600;}}",
            f"@font-face{{font-family:'Haku';src:url('{bold_uri}');font-weight:700;}}",
            f"@font-face{{font-family:'Haku';src:url('{bold_uri}');font-weight:900;}}",
        ]
    )


@lru_cache(maxsize=4)
def _jinja_env(template_dir: str) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        enable_async=True,
    )


class BrowserPool:
    """Playwright 异步浏览器连接池：空闲自动回收内存，单页面异常隔离。"""

    def __init__(
        self,
        *,
        browsers_path: str = "",
        data_dir: Path,
        max_contexts: int = 4,
        idle_seconds: int = 300,
    ) -> None:
        self._slots = asyncio.Semaphore(max_contexts)
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._idle: asyncio.Task[None] | None = None
        self._active = 0
        self._idle_seconds = idle_seconds
        self._browsers_path = browsers_path
        self._data_dir = data_dir

    async def _launch_browser(self) -> Any:
        from playwright.async_api import async_playwright

        if self._playwright is None:
            if self._browsers_path:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self._browsers_path
            elif (
                "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
                and (self._data_dir / "playwright").is_dir()
            ):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
                    (self._data_dir / "playwright").resolve()
                )
            self._playwright = await async_playwright().start()

        # 1) 优先启动 Playwright 默认安装的 Chromium
        try:
            return await self._playwright.chromium.launch(
                headless=True, args=DOCKER_CHROMIUM_ARGS
            )
        except Exception as first_err:
            # 2) Ubuntu 容器内若通过 apt 安装了系统 chromium，则尝试可执行文件路径
            for exe in SYSTEM_CHROMIUM_EXECUTABLES:
                if Path(exe).is_file():
                    with suppress(Exception):
                        return await self._playwright.chromium.launch(
                            executable_path=exe,
                            headless=True,
                            args=DOCKER_CHROMIUM_ARGS,
                        )
            # 3) 桌面环境尝试 chrome / msedge 通道
            for channel in ("chrome", "msedge"):
                with suppress(Exception):
                    return await self._playwright.chromium.launch(
                        channel=channel,
                        headless=True,
                        args=DOCKER_CHROMIUM_ARGS,
                    )
            raise first_err

    @asynccontextmanager
    async def page(self, *, device_scale_factor: float = 2, **options) -> AsyncIterator[Any]:
        async with self._slots:
            context = None
            acquired = False
            try:
                async with self._lock:
                    if self._idle is not None:
                        self._idle.cancel()
                        self._idle = None
                    if self._browser is None or not self._browser.is_connected():
                        self._browser = await self._launch_browser()
                    self._active += 1
                    acquired = True
                    browser = self._browser

                context = await browser.new_context(
                    device_scale_factor=device_scale_factor,
                    **{"user_agent": _chrome_user_agent(browser.version), **options},
                )
                yield await context.new_page()
            finally:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    if acquired:
                        async with self._lock:
                            self._active -= 1
                            if self._active == 0:
                                self._idle = asyncio.create_task(self._close_idle())

    async def _close_idle(self) -> None:
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(self._idle_seconds)
            async with self._lock:
                if self._active == 0:
                    await self._close_resources()

    async def _close_resources(self) -> None:
        try:
            if self._browser is not None:
                with suppress(Exception):
                    await self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                with suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    async def close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            await asyncio.gather(self._idle, return_exceptions=True)
            self._idle = None
        async with self._lock:
            await self._close_resources()


def _veto_steps(stats: MatchStats) -> list[dict[str, str]]:
    """把 '3. Spirit picked Dust2' 拆成结构化步骤；认不出的行原样保留。"""
    sides = {stats.team1.lower(): "team1", stats.team2.lower(): "team2"}
    steps: list[dict[str, str]] = []
    for line in stats.vetos:
        if match := VETO_PATTERN.match(line):
            team, action, map_name = match.groups()
            steps.append(
                {
                    "action": "pick" if action == "picked" else "ban",
                    "side": sides.get(team.lower(), ""),
                    "map": map_name,
                }
            )
        elif match := LEFTOVER_PATTERN.match(line):
            steps.append({"action": "left", "side": "", "map": match.group(1)})
        else:
            steps.append({"action": "text", "side": "", "map": line})
    return steps


class HLTVRenderer:
    """HLTV 卡片图片渲染器。"""

    def __init__(
        self,
        config: PluginConfig,
        data_dir: Path,
        t2i_fallback: Callable[[str, int], Awaitable[bytes]] | None = None,
    ) -> None:
        self._config = config
        self._tz = pytz.timezone(config.timezone)
        self._data_dir = Path(data_dir)
        self._t2i_fallback = t2i_fallback
        self._pool = BrowserPool(
            browsers_path=config.playwright_browsers_path,
            data_dir=self._data_dir,
        )
        self._font_css = _discover_cjk_font_css(config.custom_font_dir, self._data_dir)
        self._playwright_unavailable_warned = False

    async def close(self) -> None:
        await self._pool.close()

    async def render_html_string(
        self, template_name: str, context: dict[str, Any], *, show_images: bool = True
    ) -> str:
        env = _jinja_env(str(TEMPLATE_DIR.resolve()))
        template = env.get_template(template_name)
        full_ctx = {
            "font_css": self._font_css,
            "barlow_font_css": _barlow_font_css(),
            "timestamp": datetime.now(self._tz).strftime("%Y-%m-%d %H:%M:%S"),
            "watermark_text": self._config.watermark_text,
            "show_images": show_images,
            **context,
        }
        return await template.render_async(**full_ctx)

    async def _screenshot_playwright(
        self, html: str, *, width: int, timeout_ms: float
    ) -> bytes:
        async with self._pool.page(
            viewport={"width": width, "height": 10}, device_scale_factor=2
        ) as page:
            page.set_default_timeout(timeout_ms)
            await page.set_content(html, wait_until="load")
            await page.evaluate("document.fonts.ready")
            await page.evaluate(
                "Promise.all(Array.from(document.images).map(i => i.decode().catch(() => {})))"
            )
            return await page.screenshot(
                full_page=True, type="png", timeout=timeout_ms
            )

    async def _render_via_playwright(
        self, template_name: str, context: dict[str, Any], width: int
    ) -> bytes:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        html = await self.render_html_string(template_name, context, show_images=True)
        try:
            return await self._screenshot_playwright(
                html, width=width, timeout_ms=IMAGE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            logger.warning(
                f"[HLTV] {template_name} 远程队标/横幅加载超时，自动降级渲染无图卡片"
            )
            fallback_html = await self.render_html_string(
                template_name, context, show_images=False
            )
            return await self._screenshot_playwright(
                fallback_html, width=width, timeout_ms=IMAGE_TIMEOUT_MS
            )

    async def _render(
        self, template_name: str, context: dict[str, Any], width: int
    ) -> bytes:
        backend = self._config.render_backend
        if backend == "astrbot_t2i":
            if self._t2i_fallback is None:
                raise RuntimeError("当前环境未绑定 AstrBot html_render 回退函数")
            html = await self.render_html_string(
                template_name, context, show_images=True
            )
            return await self._t2i_fallback(html, width)

        try:
            return await self._render_via_playwright(template_name, context, width)
        except Exception as pw_err:
            if backend == "auto" and self._t2i_fallback is not None:
                if not self._playwright_unavailable_warned:
                    self._playwright_unavailable_warned = True
                    logger.warning(
                        f"[HLTV] 容器内本地 Playwright 暂不可用（{pw_err}），"
                        "已自动切换至 AstrBot 内置 html_render 服务进行渲染。"
                    )
                html = await self.render_html_string(
                    template_name, context, show_images=True
                )
                return await self._t2i_fallback(html, width)
            raise

    async def render_events(
        self,
        ongoing_events: list[EventInfo],
        upcoming_events: list[EventInfo],
    ) -> bytes:
        ongoing = [
            {**asdict(e), "start_date": e.start_date[5:], "end_date": e.end_date[5:]}
            for e in ongoing_events
        ]
        upcoming = [
            {**asdict(e), "start_date": e.start_date[5:], "end_date": e.end_date[5:]}
            for e in upcoming_events
        ]
        return await self._render(
            "events.html",
            {"ongoing_events": ongoing, "upcoming_events": upcoming},
            width=650,
        )

    async def render_matches(
        self,
        matches_by_event: dict[str, list[MatchInfo]],
        live_count: int,
        upcoming_count: int,
    ) -> bytes:
        matches_dict = {
            event_name: [
                {
                    **asdict(m),
                    "date": m.start_time.strftime("%m-%d") if m.start_time else "",
                    "time": m.start_time.strftime("%H:%M") if m.start_time else "",
                }
                for m in matches
            ]
            for event_name, matches in matches_by_event.items()
        }
        return await self._render(
            "matches.html",
            {
                "matches_by_event": matches_dict,
                "live_count": live_count,
                "upcoming_count": upcoming_count,
            },
            width=700,
        )

    async def render_results(
        self, results_by_event: dict[str, list[ResultInfo]]
    ) -> bytes:
        results_dict = {
            event_name: [{**asdict(r), "date": r.date[5:]} for r in results]
            for event_name, results in results_by_event.items()
        }
        return await self._render(
            "results.html",
            {"results_by_event": results_dict},
            width=700,
        )

    async def render_stats(self, stats: MatchStats) -> bytes:
        played_maps = [
            m for m in stats.maps if m.score_team1 != "-" and m.score_team2 != "-"
        ]
        has_single_map_details = (
            len(played_maps) == 1 and played_maps[0].map_name in stats.map_stats_details
        )
        stats_dict = {
            **asdict(stats),
            "show_total_overview": not has_single_map_details,
            "veto_steps": _veto_steps(stats),
        }
        return await self._render("stats.html", {"stats": stats_dict}, width=1040)

    async def render_reminder(
        self, match: MatchInfo, stats: MatchStats, event_title: str
    ) -> bytes:
        return await self._render(
            "reminder.html",
            {
                "match": asdict(match),
                "stats": asdict(stats),
                "event_title": event_title,
            },
            width=560,
        )

    async def render_mvp(self, event_title: str, mvp: EventMvp) -> bytes:
        return await self._render(
            "mvp.html",
            {"mvp": asdict(mvp), "event_title": event_title},
            width=640,
        )
