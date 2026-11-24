"""HLTV 订阅插件配置解析与校验（对接 AstrBot WebUI _conf_schema.json 并兼容旧版配置键）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pytz

from .log_compat import logger


def _pick(data: Mapping[str, Any], primary: str, legacy: str, default: Any) -> Any:
    if primary in data and data[primary] is not None:
        return data[primary]
    if legacy in data and data[legacy] is not None:
        return data[legacy]
    return default


@dataclass(frozen=True)
class PluginConfig:
    """HLTV 订阅插件运行配置。"""

    flaresolverr_url: str = "http://flaresolverr:8191/v1"
    flaresolverr_timeout_seconds: int = 60
    enable_map_result_push: bool = True
    request_interval_seconds: float = 0.0
    session_max_requests: int = 30
    block_cooldown_seconds: int = 600
    block_cooldown_max_seconds: int = 7200
    timezone: str = "Asia/Shanghai"
    auto_unsub_delay_days: int = 2
    auto_sub_interval_days: int = 7
    notified_ttl_days: int = 30
    scheduler_jitter_seconds: int = 8
    render_backend: str = "auto"
    allow_no_prefix: bool = True
    allow_group_admin: bool = True
    default_enabled_groups: tuple[str, ...] = field(default_factory=tuple)
    watermark_text: str = "Designed by Hakuchumu\nModified by Mizhiawa"
    custom_font_dir: str = ""
    playwright_browsers_path: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> PluginConfig:
        data = dict(raw or {})

        raw_url = str(
            _pick(data, "flaresolverr_url", "hltv_flaresolverr_url", "")
        ).strip()
        url = raw_url or cls.flaresolverr_url
        if url and not url.endswith("/v1") and url.rstrip("/").endswith(":8191"):
            url = url.rstrip("/") + "/v1"

        tz_name = (
            str(_pick(data, "timezone", "hltv_timezone", cls.timezone)).strip()
            or "Asia/Shanghai"
        )
        try:
            pytz.timezone(tz_name)
        except Exception:
            try:
                ZoneInfo(tz_name)
            except Exception:
                logger.warning(f"[HLTV] 无效时区配置 {tz_name!r}，已回退为 Asia/Shanghai")
                tz_name = "Asia/Shanghai"

        backend = str(data.get("render_backend") or "auto").strip().lower()
        if backend not in {"auto", "playwright", "astrbot_t2i"}:
            backend = "auto"

        raw_groups = data.get("default_enabled_groups") or []
        groups: list[str] = []
        if isinstance(raw_groups, (list, tuple, set)):
            for item in raw_groups:
                gid = str(item).strip()
                if gid and gid not in groups:
                    groups.append(gid)

        watermark = str(
            _pick(data, "watermark_text", "hltv_watermark_text", cls.watermark_text)
        ).replace("\\n", "\n")

        enable_map_push = bool(
            _pick(
                data,
                "enable_map_result_push",
                "hltv_enable_map_result_push",
                True,
            )
        )

        return cls(
            flaresolverr_url=url,
            flaresolverr_timeout_seconds=max(
                10,
                int(
                    _pick(
                        data,
                        "flaresolverr_timeout_seconds",
                        "hltv_flaresolverr_timeout_seconds",
                        60,
                    )
                ),
            ),
            enable_map_result_push=enable_map_push,
            request_interval_seconds=max(
                0.0, float(data.get("request_interval_seconds", 0.0))
            ),
            session_max_requests=max(
                1, int(data.get("session_max_requests", 30))
            ),
            block_cooldown_seconds=max(
                30,
                int(
                    _pick(
                        data,
                        "block_cooldown_seconds",
                        "hltv_block_cooldown_seconds",
                        600,
                    )
                ),
            ),
            block_cooldown_max_seconds=max(
                60,
                int(
                    _pick(
                        data,
                        "block_cooldown_max_seconds",
                        "hltv_block_cooldown_max_seconds",
                        7200,
                    )
                ),
            ),
            timezone=tz_name,
            auto_unsub_delay_days=max(
                0,
                int(
                    _pick(
                        data,
                        "auto_unsub_delay_days",
                        "hltv_auto_unsub_delay_days",
                        2,
                    )
                ),
            ),
            auto_sub_interval_days=max(
                1, int(data.get("auto_sub_interval_days", 7))
            ),
            notified_ttl_days=max(
                1,
                int(_pick(data, "notified_ttl_days", "hltv_notified_ttl_days", 30)),
            ),
            scheduler_jitter_seconds=max(
                0,
                int(
                    _pick(
                        data,
                        "scheduler_jitter_seconds",
                        "hltv_scheduler_jitter_seconds",
                        8,
                    )
                ),
            ),
            render_backend=backend,
            allow_no_prefix=bool(data.get("allow_no_prefix", True)),
            allow_group_admin=bool(data.get("allow_group_admin", True)),
            default_enabled_groups=tuple(groups),
            watermark_text=watermark,
            custom_font_dir=str(data.get("custom_font_dir") or "").strip(),
            playwright_browsers_path=str(
                data.get("playwright_browsers_path") or ""
            ).strip(),
        )
