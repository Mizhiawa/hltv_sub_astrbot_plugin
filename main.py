"""AstrBot HLTV CS 赛事订阅与查询插件主入口（适配 AstrBot 4.29.4 + SnowLuma 2.4.2）。"""

from __future__ import annotations

import asyncio
import base64
import re
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import pytz

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .client import HLTVClient, HLTVFetchError
from .config import PluginConfig
from .data_manager import DataManager, EventSubscription, GroupData
from .handler import HLTVHandler
from .render import HLTVRenderer
from .scheduler import HLTVScheduler

PLUGIN_ID = "hltv_sub"
NO_ACTIVE_HINT = "当前没有进行中的订阅赛事\n使用 我的订阅 查看已订阅赛事的日期"
LIST_MAX_AGE_SECONDS = 600

# 免唤醒前缀直发指令正则（不匹配以 / 开头的消息，避免与 @filter.command 重复触发）
PREFIXLESS_CMD_RE = (
    r"^(?!/)(?:"
    r"hltv开启|hltv启用|hltvon|"
    r"hltv关闭|hltv禁用|hltvoff|"
    r"hltv重置冷却|hltv解封|hltvreset|"
    r"hltv帮助|hltv菜单|hltvhelp|"
    r"event列表|赛事列表|events|"
    r"我的订阅|订阅列表|mysub|"
    r"matches列表|比赛列表|matches|"
    r"results列表|结果列表|results|"
    r"(?:event订阅|订阅赛事|subscribe)(?:\s+\S+)?|"
    r"(?:event取消订阅|取消订阅赛事|unsubscribe)(?:\s+\S+)?|"
    r"(?:stats|比赛数据|数据)(?:\s+\S+)?"
    r")\s*$"
)
_PREFIXLESS_PARSE_RE = re.compile(
    r"^(?P<cmd>hltv开启|hltv启用|hltvon|hltv关闭|hltv禁用|hltvoff|"
    r"hltv重置冷却|hltv解封|hltvreset|"
    r"hltv帮助|hltv菜单|hltvhelp|event列表|赛事列表|events|"
    r"我的订阅|订阅列表|mysub|matches列表|比赛列表|matches|"
    r"results列表|结果列表|results|event订阅|订阅赛事|subscribe|"
    r"event取消订阅|取消订阅赛事|unsubscribe|stats|比赛数据|数据)"
    r"(?:\s+(?P<arg>\S+))?\s*$"
)


def _resolve_data_dir() -> Path:
    try:
        return Path(StarTools.get_data_dir(PLUGIN_ID))
    except TypeError:
        return Path(StarTools.get_data_dir())


def _clean_id_arg(raw: Any) -> str:
    return str(raw or "").strip().lstrip("#").strip()


def _short_date(d: str) -> str:
    return d[5:] if len(d) >= 10 else d


def _by_event_id(subscriptions: list[EventSubscription]) -> list[EventSubscription]:
    return sorted(
        subscriptions,
        key=lambda x: (0, int(x.event_id)) if x.event_id.isdigit() else (1, x.event_id),
    )


def _disable_t2i(result: Any) -> Any:
    if hasattr(result, "use_t2i"):
        with suppress(Exception):
            result.use_t2i(False)
    return result


async def _call_onebot_action(client_or_bot: Any, action: str, **params) -> Any:
    api = getattr(client_or_bot, "api", None)
    if api is not None and hasattr(api, "call_action"):
        return await api.call_action(action, **params)
    if hasattr(client_or_bot, "call_action"):
        return await client_or_bot.call_action(action, **params)
    raise AttributeError("OneBot 客户端不支持 call_action")


class HLTVSubPlugin(Star):
    """HLTV 大型赛事订阅、实时战报推送与数据查询插件。"""

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None
    ) -> None:
        super().__init__(context)
        self.raw_config = config
        self.plugin_config = PluginConfig.from_mapping(config)
        self._tz = pytz.timezone(self.plugin_config.timezone)
        self.data_dir = _resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"[HLTV] 插件数据目录: {self.data_dir}")

        self.data_manager = DataManager(
            self.data_dir,
            default_enabled_groups=self.plugin_config.default_enabled_groups,
        )
        self.client = HLTVClient(self.plugin_config, self.data_dir)
        self.renderer = HLTVRenderer(
            self.plugin_config,
            self.data_dir,
            t2i_fallback=self._astrbot_t2i_fallback,
        )
        self.handler = HLTVHandler(
            config=self.plugin_config,
            data_manager=self.data_manager,
            client=self.client,
            renderer=self.renderer,
            send_to_group=self._send_to_group,
            has_active_platform=self._has_active_platform,
        )
        self.scheduler = HLTVScheduler(
            config=self.plugin_config,
            data_manager=self.data_manager,
            handler=self.handler,
        )
        self._init_task: asyncio.Task[None] | None = None
        self._terminated = False

    async def initialize(self) -> None:
        """AstrBot 加载插件后的异步生命周期钩子。"""
        if self._init_task is None:
            self._init_task = asyncio.create_task(
                self._delayed_init(), name="hltv_delayed_init"
            )

    async def terminate(self) -> None:
        """AstrBot 停用、重载或卸载插件时的资源回收钩子。"""
        self._terminated = True
        if self._init_task is not None:
            self._init_task.cancel()
            await asyncio.gather(self._init_task, return_exceptions=True)
            self._init_task = None
        await self.scheduler.stop()
        await self.client.close()
        await self.renderer.close()
        logger.info("[HLTV] 插件后台调度、HTTP 会话与浏览器池已安全关闭")

    async def _delayed_init(self) -> None:
        """延迟启动：等待 SnowLuma / OneBot 反向 WS 适配器完成握手后再执行首轮检查。"""
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(10)
            if self._terminated:
                return
            try:
                if self.data_manager.has_enabled_groups():
                    try:
                        await self.client.start()
                    except HLTVFetchError as e:
                        logger.info(f"[HLTV] 会话准备暂未完成，后续检查自动恢复：{e}")
                    else:
                        await self.scheduler.auto_subscribe_big_events()
                await self.scheduler.daily_maintenance()
            except Exception as e:
                logger.exception(f"[HLTV] 启动初始化未完成，后续周期检查继续处理: {e}")
            if not self._terminated:
                self.scheduler.start()

    # =========================================================================
    # 平台适配（AstrBot 4.29.4 + SnowLuma 2.4.2 / OneBot v11）
    # =========================================================================

    def _get_platforms(self) -> list[Any]:
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return []
        if hasattr(pm, "get_insts"):
            return list(pm.get_insts() or [])
        return list(getattr(pm, "platform_insts", []) or [])

    def _has_active_platform(self) -> bool:
        return bool(self._get_platforms())

    async def _astrbot_t2i_fallback(self, html: str, width: int) -> bytes:
        """当 Docker 容器未安装本地 Chromium 时，通过 AstrBot 内置 html_render 渲染并返回字节。"""
        width_css = (
            f"<style>html,body{{width:{width}px!important;max-width:{width}px!important;"
            "margin:0!important;}</style>"
        )
        injected = (
            html.replace("</head>", f"{width_css}</head>", 1)
            if "</head>" in html
            else width_css + html
        )
        raw_tmpl = f"{{% raw %}}{injected}{{% endraw %}}"
        options = {"full_page": True, "type": "png", "omit_background": False}
        try:
            rendered = await self.html_render(
                raw_tmpl, {}, return_url=False, options=options
            )
        except TypeError:
            rendered = await self.html_render(raw_tmpl, {}, options=options)

        if isinstance(rendered, bytes):
            return rendered
        result_str = str(rendered).strip()
        if result_str.startswith("base64://"):
            return base64.b64decode(result_str[len("base64://") :])
        if result_str.startswith("data:") and ";base64," in result_str:
            return base64.b64decode(result_str.split(";base64,", 1)[1])
        parsed = urlparse(result_str)
        if parsed.scheme in {"http", "https"}:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as hc:
                resp = await hc.get(result_str)
                resp.raise_for_status()
                return resp.content
        if parsed.scheme == "file":
            return Path(unquote(parsed.path)).read_bytes()
        return Path(result_str).read_bytes()

    async def _send_to_group(
        self, group: GroupData, text: str | None, image_bytes: bytes
    ) -> None:
        """双通道群推送：优先走 AstrBot UMO，失败或缺失时通过 SnowLuma (aiocqhttp) OneBot v11 投递。"""
        components: list[Any] = []
        if text:
            components.append(Comp.Plain(text))
        components.append(Comp.Image.fromBytes(image_bytes))

        mc = MessageChain()
        mc.chain.extend(components)
        _disable_t2i(mc)

        last_err: Exception | None = None

        if group.umo:
            try:
                await self.context.send_message(group.umo, mc)
                return
            except Exception as exc:
                last_err = exc

        for platform in self._get_platforms():
            meta = platform.meta() if hasattr(platform, "meta") else None
            meta_name = getattr(meta, "name", "") if meta else ""
            meta_id = getattr(meta, "id", "") if meta else ""
            is_onebot = (
                meta_name == "aiocqhttp"
                or "cqhttp" in type(platform).__name__.lower()
                or hasattr(platform, "get_client")
            )
            if not is_onebot:
                continue

            if meta_id:
                candidate_umo = f"{meta_id}:GroupMessage:{group.group_id}"
                if candidate_umo != group.umo:
                    try:
                        await self.context.send_message(candidate_umo, mc)
                        self.data_manager.record_group_umo(
                            group.group_id, candidate_umo
                        )
                        return
                    except Exception as exc:
                        last_err = exc

            if group.group_id.isdigit() and hasattr(platform, "get_client"):
                client = platform.get_client()
                if client is not None:
                    b64 = base64.b64encode(image_bytes).decode("ascii")
                    segments: list[dict[str, Any]] = []
                    if text:
                        segments.append({"type": "text", "data": {"text": text}})
                    segments.append(
                        {"type": "image", "data": {"file": f"base64://{b64}"}}
                    )
                    try:
                        await _call_onebot_action(
                            client,
                            "send_group_msg",
                            group_id=int(group.group_id),
                            message=segments,
                        )
                        return
                    except Exception as exc:
                        last_err = exc

        if last_err is not None:
            raise last_err
        raise RuntimeError(f"未找到可向群 {group.group_id} 投递消息的 SnowLuma/OneBot 平台实例")

    async def _has_manage_permission(self, event: AstrMessageEvent) -> bool:
        """校验是否为 AstrBot 全局管理员，或（配置允许时）SnowLuma OneBot 群主/管理员。"""
        if event.is_admin():
            return True
        if not self.plugin_config.allow_group_admin:
            return False

        msg_obj = getattr(event, "message_obj", None)
        role = ""
        if msg_obj is not None:
            sender = getattr(msg_obj, "sender", None)
            if isinstance(sender, dict):
                role = str(sender.get("role") or "")
            elif sender is not None:
                role = str(getattr(sender, "role", "") or "")

            if not role:
                raw_msg = getattr(msg_obj, "raw_message", None)
                if isinstance(raw_msg, dict):
                    raw_sender = raw_msg.get("sender")
                    if isinstance(raw_sender, dict):
                        role = str(raw_sender.get("role") or "")

        if not role:
            gid = str(event.get_group_id() or "").strip()
            uid = str(event.get_sender_id() or "").strip()
            bot = getattr(event, "bot", None)
            if gid.isdigit() and uid.isdigit() and bot is not None:
                with suppress(Exception):
                    info = await _call_onebot_action(
                        bot,
                        "get_group_member_info",
                        group_id=int(gid),
                        user_id=int(uid),
                        no_cache=False,
                    )
                    if isinstance(info, dict):
                        role = str(info.get("role") or "")

        return role.lower() in {"owner", "admin"}

    def _ensure_group_enabled(self, event: AstrMessageEvent) -> bool:
        """检查当前群是否启用 HLTV，并顺带同步最新 UMO。"""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id or not self.data_manager.is_enabled(group_id):
            return False
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo:
            with suppress(OSError):
                self.data_manager.record_group_umo(group_id, umo)
        return True

    def _may_have_matches(self, sub: EventSubscription) -> bool:
        """赛事结束后订阅还会保留几天等赛果推送，欧洲决赛常打到北京时间次日凌晨，留 12 小时宽限。"""
        now = datetime.now(self._tz)
        try:
            end_d = date.fromisoformat(sub.end_date)
        except ValueError:
            return True
        return end_d >= (now - timedelta(hours=12)).date()

    @staticmethod
    def _stop(event: AstrMessageEvent) -> None:
        if hasattr(event, "stop_event"):
            with suppress(Exception):
                event.stop_event()

    # =========================================================================
    # 群命令定义（支持 /前缀 唤醒词触发 + 免前缀直发触发）
    # =========================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("hltv开启", alias={"hltv启用", "hltvon"})
    async def cmd_hltv_enable(self, event: AstrMessageEvent):
        """开启本群 HLTV 赛事订阅与查询功能。"""
        if not await self._has_manage_permission(event):
            yield _disable_t2i(
                event.plain_result("❌ 权限不足：仅群主、群管理员或 Bot 管理员可执行此操作")
            )
            self._stop(event)
            return
        group_id = str(event.get_group_id() or "").strip()
        umo = getattr(event, "unified_msg_origin", "") or ""
        try:
            self.data_manager.set_enabled(group_id, True, umo=umo)
        except OSError:
            logger.exception("[HLTV] 群开关保存失败")
            yield _disable_t2i(event.plain_result("修改未完成：保存数据失败"))
            self._stop(event)
            return
        self.scheduler.sync_events(wake=True)
        yield _disable_t2i(event.plain_result("✅ HLTV 订阅功能已开启"))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("hltv关闭", alias={"hltv禁用", "hltvoff"})
    async def cmd_hltv_disable(self, event: AstrMessageEvent):
        """关闭本群 HLTV 赛事订阅与查询功能。"""
        if not await self._has_manage_permission(event):
            yield _disable_t2i(
                event.plain_result("❌ 权限不足：仅群主、群管理员或 Bot 管理员可执行此操作")
            )
            self._stop(event)
            return
        group_id = str(event.get_group_id() or "").strip()
        umo = getattr(event, "unified_msg_origin", "") or ""
        try:
            self.data_manager.set_enabled(group_id, False, umo=umo)
        except OSError:
            logger.exception("[HLTV] 群开关保存失败")
            yield _disable_t2i(event.plain_result("修改未完成：保存数据失败"))
            self._stop(event)
            return
        self.scheduler.sync_events(wake=False)
        yield _disable_t2i(event.plain_result("❌ HLTV 订阅功能已关闭"))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("hltv重置冷却", alias={"hltv解封", "hltvreset"})
    async def cmd_hltv_reset_cooldown(self, event: AstrMessageEvent):
        """手动清空 HLTV 访问冷却状态。"""
        if not await self._has_manage_permission(event):
            yield _disable_t2i(
                event.plain_result("❌ 权限不足：仅群主、群管理员或 Bot 管理员可执行此操作")
            )
            self._stop(event)
            return
        self.client.reset_cooldown()
        self.scheduler.sync_events(wake=True)
        yield _disable_t2i(event.plain_result("✅ 已清空 HLTV 访问冷却状态，可立即重试查询"))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("hltv帮助", alias={"hltv菜单", "hltvhelp"})
    async def cmd_hltv_help(self, event: AstrMessageEvent):
        """查看 HLTV 插件指令说明与本群状态。"""
        group_id = str(event.get_group_id() or "").strip()
        enabled = self.data_manager.is_enabled(group_id)
        sub_count = len(self.data_manager.get_subscribed_events())
        status_str = "✅ 已开启" if enabled else "❌ 未开启（管理员发送 hltv开启 启用）"
        map_push_str = (
            "✅ 已开启（BO3/BO5 每图结束播报）"
            if self.plugin_config.enable_map_result_push
            else "❌ 已关闭（仅播报开赛提醒与整场结果）"
        )
        proxy_str = (
            f"✅ 已启用 ({self.plugin_config.proxy_url})"
            if self.plugin_config.proxy_url
            else "未配置（直连）"
        )
        help_text = (
            f"🎯 HLTV CS 赛事订阅插件\n"
            f"当前本群状态：{status_str}\n"
            f"非 BO1 逐图播报：{map_push_str}\n"
            f"代理加速状态：{proxy_str}\n"
            f"全局已订阅赛事：{sub_count} 个\n\n"
            f"📌 管理指令（群主/群管/Bot管理员）：\n"
            f"• hltv开启 / hltv关闭 — 开关本群推送与查询\n"
            f"• hltv重置冷却 — 手动清空访问冷却锁（别名：hltv解封 / hltvreset）\n"
            f"• event订阅 <赛事ID> — 订阅指定赛事（别名：订阅赛事 / subscribe）\n"
            f"• event取消订阅 <赛事ID> — 取消订阅（别名：取消订阅赛事 / unsubscribe）\n\n"
            f"📊 查询指令（本群开启后可用）：\n"
            f"• event列表 — 查看近期大型赛事（别名：赛事列表 / events）\n"
            f"• 我的订阅 — 查看已订阅赛事列表（别名：订阅列表 / mysub）\n"
            f"• matches列表 — 查看订阅赛事近期赛程（别名：比赛列表 / matches）\n"
            f"• results列表 — 查看订阅赛事近期赛果（别名：结果列表 / results）\n"
            f"• stats [比赛ID] — 查看单场或最新比赛详细数据（别名：比赛数据 / 数据）"
        )
        yield _disable_t2i(event.plain_result(help_text))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("event列表", alias={"赛事列表", "events"})
    async def cmd_event_list(self, event: AstrMessageEvent):
        """获取 HLTV 正在进行与即将举行的 Big Events 列表。"""
        if not self._ensure_group_enabled(event):
            return
        yield _disable_t2i(event.plain_result("正在获取赛事列表，请稍候..."))
        try:
            events = await self.client.get_big_events()
        except HLTVFetchError as e:
            logger.warning(f"[HLTV] 获取赛事列表失败: {e}")
            yield _disable_t2i(event.plain_result(str(e)))
            self._stop(event)
            return
        except Exception as e:
            logger.exception(f"[HLTV] 获取赛事列表失败: {e}")
            yield _disable_t2i(
                event.plain_result("获取赛事列表失败，HLTV 可能暂时无法访问")
            )
            self._stop(event)
            return

        ongoing = [e for e in events if e.is_ongoing]
        upcoming = [e for e in events if not e.is_ongoing]
        try:
            img = await self.renderer.render_events(ongoing, upcoming)
        except Exception as e:
            logger.exception(f"[HLTV] 渲染赛事列表失败: {e}")
            yield _disable_t2i(event.plain_result("赛事列表图片渲染失败，请检查日志"))
            self._stop(event)
            return
        yield _disable_t2i(event.chain_result([Comp.Image.fromBytes(img)]))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("event订阅", alias={"订阅赛事", "subscribe"})
    async def cmd_event_subscribe(self, event: AstrMessageEvent, event_id: str = ""):
        """订阅指定 HLTV 赛事 ID。"""
        if not self._ensure_group_enabled(event):
            return
        if not await self._has_manage_permission(event):
            yield _disable_t2i(
                event.plain_result("❌ 权限不足：仅群主、群管理员或 Bot 管理员可订阅赛事")
            )
            self._stop(event)
            return

        eid = _clean_id_arg(event_id)
        if not eid:
            yield _disable_t2i(
                event.plain_result("请提供赛事ID，例如：event订阅 7148")
            )
            self._stop(event)
            return
        if not eid.isdigit():
            yield _disable_t2i(
                event.plain_result("赛事ID应为纯数字，例如：event订阅 7148")
            )
            self._stop(event)
            return
        if self.data_manager.is_subscribed(eid):
            yield _disable_t2i(event.plain_result(f"已经订阅了赛事 #{eid}"))
            self._stop(event)
            return

        yield _disable_t2i(event.plain_result("正在获取赛事信息..."))
        try:
            events = await self.client.get_big_events()
            event_info = next((e for e in events if e.id == eid), None)
            if event_info is None:
                event_info = await self.client.get_event_info(eid)
            if event_info.is_ongoing:
                await self.handler.initialize_event_results_as_notified(eid)
        except HLTVFetchError as e:
            logger.warning(f"[HLTV] 订阅赛事 {eid} 失败: {e}")
            yield _disable_t2i(event.plain_result(f"订阅未完成：{e}"))
            self._stop(event)
            return
        except ValueError as e:
            logger.warning(f"[HLTV] 赛事 {eid} 信息解析失败: {e}")
            yield _disable_t2i(
                event.plain_result(f"无法获取赛事 #{eid} 的信息，未创建订阅")
            )
            self._stop(event)
            return
        except Exception as e:
            logger.exception(f"[HLTV] 订阅赛事失败: {e}")
            yield _disable_t2i(event.plain_result("订阅失败，HLTV 可能暂时无法访问"))
            self._stop(event)
            return

        try:
            created = self.data_manager.subscribe_event(
                EventSubscription(
                    event_id=eid,
                    event_title=event_info.title,
                    start_date=event_info.start_date,
                    end_date=event_info.end_date,
                )
            )
        except OSError:
            logger.exception("[HLTV] 订阅时保存数据失败")
            yield _disable_t2i(event.plain_result("订阅未完成：保存数据失败，请查看日志"))
            self._stop(event)
            return
        if not created:
            yield _disable_t2i(event.plain_result(f"已经订阅了赛事 #{eid}"))
            self._stop(event)
            return

        self.scheduler.sync_events(wake=True)
        yield _disable_t2i(event.plain_result(f"✅ 成功订阅赛事：{event_info.title}"))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("event取消订阅", alias={"取消订阅赛事", "unsubscribe"})
    async def cmd_event_unsubscribe(self, event: AstrMessageEvent, event_id: str = ""):
        """取消订阅指定 HLTV 赛事 ID。"""
        if not self._ensure_group_enabled(event):
            return
        if not await self._has_manage_permission(event):
            yield _disable_t2i(
                event.plain_result("❌ 权限不足：仅群主、群管理员或 Bot 管理员可取消订阅")
            )
            self._stop(event)
            return

        eid = _clean_id_arg(event_id)
        if not eid:
            yield _disable_t2i(
                event.plain_result("请提供赛事ID，例如：event取消订阅 7148")
            )
            self._stop(event)
            return

        try:
            removed = self.data_manager.unsubscribe_event(eid)
        except OSError:
            logger.exception("[HLTV] 取消订阅时保存数据失败")
            yield _disable_t2i(event.plain_result("取消订阅未完成：保存数据失败"))
            self._stop(event)
            return
        if not removed:
            yield _disable_t2i(event.plain_result(f"未订阅赛事 #{eid}"))
            self._stop(event)
            return
        self.scheduler.sync_events()
        yield _disable_t2i(event.plain_result(f"✅ 已取消订阅赛事 #{eid}"))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("我的订阅", alias={"订阅列表", "mysub"})
    async def cmd_my_subscriptions(self, event: AstrMessageEvent):
        """查看当前全局已订阅的赛事列表。"""
        if not self._ensure_group_enabled(event):
            return
        subscriptions = self.data_manager.get_subscribed_events()
        if not subscriptions:
            yield _disable_t2i(
                event.plain_result("当前没有订阅任何赛事\n使用 event列表 查看可订阅的赛事")
            )
            self._stop(event)
            return

        lines = ["📋 已订阅的赛事："]
        for sub in _by_event_id(subscriptions):
            lines.append(f"• #{sub.event_id} {sub.event_title}")
            if sub.start_date and sub.end_date:
                lines.append(
                    f"  📅 {_short_date(sub.start_date)} ~ {_short_date(sub.end_date)}"
                )
        yield _disable_t2i(event.plain_result("\n".join(lines)))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("matches列表", alias={"比赛列表", "matches"})
    async def cmd_matches_list(self, event: AstrMessageEvent):
        """查看进行中或即将开赛的已订阅赛事赛程列表。"""
        if not self._ensure_group_enabled(event):
            return
        subscriptions = [
            sub
            for sub in self.handler.active_subscriptions()
            if self._may_have_matches(sub)
        ]
        if not subscriptions:
            yield _disable_t2i(event.plain_result(NO_ACTIVE_HINT))
            self._stop(event)
            return

        yield _disable_t2i(event.plain_result("正在获取比赛列表，请稍候..."))
        matches_by_event: dict[str, list[Any]] = {}
        try:
            for sub in _by_event_id(subscriptions):
                page = await self.client.get_event_matches(
                    sub.event_id,
                    include_partial_tbd=True,
                    max_age=LIST_MAX_AGE_SECONDS,
                )
                if page.matches:
                    matches_by_event[f"#{sub.event_id} {sub.event_title}"] = page.matches
        except HLTVFetchError as e:
            logger.warning(f"[HLTV] 获取比赛列表失败: {e}")
            yield _disable_t2i(event.plain_result(str(e)))
            self._stop(event)
            return
        except Exception as e:
            logger.exception(f"[HLTV] 获取比赛列表失败: {e}")
            yield _disable_t2i(
                event.plain_result("获取比赛列表失败，HLTV 可能暂时无法访问")
            )
            self._stop(event)
            return

        if not matches_by_event:
            yield _disable_t2i(event.plain_result("暂无比赛"))
            self._stop(event)
            return

        all_matches = [m for matches in matches_by_event.values() for m in matches]
        live_count = sum(1 for m in all_matches if m.is_live)
        try:
            img = await self.renderer.render_matches(
                matches_by_event, live_count, len(all_matches) - live_count
            )
        except Exception as e:
            logger.exception(f"[HLTV] 渲染比赛列表失败: {e}")
            yield _disable_t2i(event.plain_result("比赛列表图片渲染失败，请检查日志"))
            self._stop(event)
            return
        yield _disable_t2i(event.chain_result([Comp.Image.fromBytes(img)]))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("results列表", alias={"结果列表", "results"})
    async def cmd_results_list(self, event: AstrMessageEvent):
        """查看已订阅赛事的近期比赛结果列表。"""
        if not self._ensure_group_enabled(event):
            return
        subscriptions = self.handler.active_subscriptions()
        if not subscriptions:
            yield _disable_t2i(event.plain_result(NO_ACTIVE_HINT))
            self._stop(event)
            return

        yield _disable_t2i(event.plain_result("正在获取比赛结果，请稍候..."))
        results_by_event: dict[str, list[Any]] = {}
        try:
            for sub in _by_event_id(subscriptions):
                results = await self.client.get_event_results(
                    sub.event_id,
                    max_age=LIST_MAX_AGE_SECONDS,
                )
                if results:
                    results_by_event[f"#{sub.event_id} {sub.event_title}"] = results
        except HLTVFetchError as e:
            logger.warning(f"[HLTV] 获取比赛结果失败: {e}")
            yield _disable_t2i(event.plain_result(str(e)))
            self._stop(event)
            return
        except Exception as e:
            logger.exception(f"[HLTV] 获取比赛结果失败: {e}")
            yield _disable_t2i(
                event.plain_result("获取比赛结果失败，HLTV 可能暂时无法访问")
            )
            self._stop(event)
            return

        if not results_by_event:
            yield _disable_t2i(event.plain_result("暂无比赛结果"))
            self._stop(event)
            return

        try:
            img = await self.renderer.render_results(results_by_event)
        except Exception as e:
            logger.exception(f"[HLTV] 渲染比赛结果失败: {e}")
            yield _disable_t2i(event.plain_result("比赛结果图片渲染失败，请检查日志"))
            self._stop(event)
            return
        yield _disable_t2i(event.chain_result([Comp.Image.fromBytes(img)]))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("stats", alias={"比赛数据", "数据"})
    async def cmd_stats(self, event: AstrMessageEvent, match_id: str = ""):
        """查看指定比赛 ID 或当前订阅赛事最新完赛的详细统计卡片。"""
        if not self._ensure_group_enabled(event):
            return

        mid = _clean_id_arg(match_id)
        try:
            if mid:
                if not mid.isdigit():
                    yield _disable_t2i(
                        event.plain_result("比赛ID应为纯数字，例如：stats 2370931")
                    )
                    self._stop(event)
                    return
                yield _disable_t2i(event.plain_result(f"正在获取比赛 #{mid} 的数据..."))
                stats = await self.client.get_match_stats(mid)
            else:
                subscriptions = self.handler.active_subscriptions()
                if not subscriptions:
                    yield _disable_t2i(
                        event.plain_result(
                            f"{NO_ACTIVE_HINT}\n或提供比赛ID，例如：stats 2370931"
                        )
                    )
                    self._stop(event)
                    return
                yield _disable_t2i(event.plain_result("正在获取最新比赛数据..."))
                stats = None
                for sub in subscriptions:
                    stats = await self.client.get_latest_result_with_stats(
                        sub.event_id, sub.event_title
                    )
                    if stats is not None:
                        break
                if stats is None:
                    yield _disable_t2i(event.plain_result("暂无比赛数据"))
                    self._stop(event)
                    return
        except HLTVFetchError as e:
            logger.warning(f"[HLTV] 获取比赛数据失败: {e}")
            yield _disable_t2i(event.plain_result(str(e)))
            self._stop(event)
            return
        except ValueError as e:
            logger.warning(f"[HLTV] 解析比赛数据失败: {e}")
            yield _disable_t2i(event.plain_result("无法解析该场比赛数据，请检查比赛ID是否正确"))
            self._stop(event)
            return
        except Exception as e:
            logger.exception(f"[HLTV] 获取比赛数据失败: {e}")
            yield _disable_t2i(
                event.plain_result("获取比赛数据失败，HLTV 可能暂时无法访问")
            )
            self._stop(event)
            return

        try:
            img = await self.renderer.render_stats(stats)
        except Exception as e:
            logger.exception(f"[HLTV] 渲染比赛数据失败: {e}")
            yield _disable_t2i(event.plain_result("比赛数据图片渲染失败，请检查日志"))
            self._stop(event)
            return
        yield _disable_t2i(event.chain_result([Comp.Image.fromBytes(img)]))
        self._stop(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.regex(PREFIXLESS_CMD_RE)
    async def on_prefixless_command(self, event: AstrMessageEvent):
        """当 allow_no_prefix 开启时，支持群内免唤醒词（无 / 前缀）直接触发指令。"""
        if not self.plugin_config.allow_no_prefix:
            return
        raw_text = str(getattr(event, "message_str", "") or "").strip()
        if not raw_text and hasattr(event, "get_message_str"):
            raw_text = str(event.get_message_str() or "").strip()
        matched = _PREFIXLESS_PARSE_RE.match(raw_text)
        if not matched:
            return
        cmd = matched.group("cmd")
        arg = matched.group("arg") or ""

        if cmd in {"hltv开启", "hltv启用", "hltvon"}:
            gen = self.cmd_hltv_enable(event)
        elif cmd in {"hltv关闭", "hltv禁用", "hltvoff"}:
            gen = self.cmd_hltv_disable(event)
        elif cmd in {"hltv重置冷却", "hltv解封", "hltvreset"}:
            gen = self.cmd_hltv_reset_cooldown(event)
        elif cmd in {"hltv帮助", "hltv菜单", "hltvhelp"}:
            gen = self.cmd_hltv_help(event)
        elif cmd in {"event列表", "赛事列表", "events"}:
            gen = self.cmd_event_list(event)
        elif cmd in {"event订阅", "订阅赛事", "subscribe"}:
            gen = self.cmd_event_subscribe(event, arg)
        elif cmd in {"event取消订阅", "取消订阅赛事", "unsubscribe"}:
            gen = self.cmd_event_unsubscribe(event, arg)
        elif cmd in {"我的订阅", "订阅列表", "mysub"}:
            gen = self.cmd_my_subscriptions(event)
        elif cmd in {"matches列表", "比赛列表", "matches"}:
            gen = self.cmd_matches_list(event)
        elif cmd in {"results列表", "结果列表", "results"}:
            gen = self.cmd_results_list(event)
        elif cmd in {"stats", "比赛数据", "数据"}:
            gen = self.cmd_stats(event, arg)
        else:
            return

        async for item in gen:
            yield item
