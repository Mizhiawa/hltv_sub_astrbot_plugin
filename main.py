"""
HLTV 订阅插件（AstrBot 适配版）入口

命令行为与原 NoneBot 版保持一致：
- 命令无前缀、无需唤醒词（通过 filter.regex 注册，直接在群内输入命令即触发）
- 未启用群的群内命令被静默忽略
- 推送/提醒由内部 apscheduler 定时任务驱动，通过 context.send_message 主动发消息
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import Image, MessageType, Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import handlers
from .config import configure
from .data_manager import data_manager
from .data_source import hltv_data
from .image_utils import cleanup_images, init_images_dir
from .scheduler import (
    delayed_init,
    prepare_scheduler,
    start_scheduler_async,
    stop_scheduler_async,
)


class HltvSubPlugin(Star):
    """HLTV CS2 赛事订阅和比赛信息查询插件"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)

        # 注入运行时配置（供模块级代码读取）
        configure(config)

        # 初始化数据目录与图片临时目录
        plugin_name = getattr(self, "name", None) or "hltv_sub"
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        data_manager.set_data_dir(data_dir)

        # 按注入后的配置重建数据源（时区/超时/代理等）
        # 必须在数据目录注入之后：HTTP 客户端的冷却状态与
        # Cloudflare 通行证就存放在该目录下。
        hltv_data.reconfigure()

        self._init_task: Optional[asyncio.Task] = None

        images_dir = data_dir / "images"
        init_images_dir(str(images_dir))
        try:
            cleanup_images()
        except Exception as e:
            logger.debug(f"清理历史临时图片失败: {e}")

        # 注册定时任务（幂等），注入主动推送消息的回调
        prepare_scheduler(self._send_message_to_group)

    async def _send_message_to_group(
        self,
        platform_id: str,
        group_id: int,
        text: str,
        image_path: str,
    ) -> bool:
        """通过 AstrBot 主动向群发送消息（推送回调）"""
        try:
            chain_components = []
            if text:
                chain_components.append(Plain(text))
            if image_path:
                chain_components.append(Image(file=image_path))
            if not chain_components:
                return False

            # 旧数据可能没有记录平台 ID：回退到第一个 aiocqhttp 平台实例
            target_platform_id = platform_id or self._find_aiocqhttp_platform_id()
            if not target_platform_id:
                logger.warning(f"[HLTV Scheduler] 未找到可用平台实例，跳过推送 group={group_id}")
                return False

            session = MessageSession(
                platform_name=target_platform_id,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=str(group_id),
            )
            await self.context.send_message(session, MessageChain(chain_components))
            return True
        except Exception as e:
            logger.error(f"[HLTV Scheduler] 向群 {group_id} 发送消息失败: {e}")
            return False

    def _find_aiocqhttp_platform_id(self) -> str:
        """查找第一个 aiocqhttp 平台实例 ID（旧数据回退用）"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if meta.name == "aiocqhttp":
                    return meta.id
        except Exception:
            pass
        return ""

    # -------------------- 命令处理 --------------------

    @filter.regex(handlers.event.EVENT_LIST_RE)
    async def cmd_event_list(self, event: AstrMessageEvent):
        async for r in handlers.event.handle_event_list(self, event):
            yield r

    @filter.regex(handlers.event.EVENT_SUBSCRIBE_RE)
    async def cmd_event_subscribe(self, event: AstrMessageEvent):
        async for r in handlers.event.handle_event_subscribe(self, event):
            yield r

    @filter.regex(handlers.event.EVENT_UNSUBSCRIBE_RE)
    async def cmd_event_unsubscribe(self, event: AstrMessageEvent):
        async for r in handlers.event.handle_event_unsubscribe(self, event):
            yield r

    @filter.regex(handlers.event.MY_SUBSCRIPTIONS_RE)
    async def cmd_my_subscriptions(self, event: AstrMessageEvent):
        async for r in handlers.event.handle_my_subscriptions(self, event):
            yield r

    @filter.regex(handlers.matches.MATCHES_LIST_RE)
    async def cmd_matches_list(self, event: AstrMessageEvent):
        async for r in handlers.matches.handle_matches_list(self, event):
            yield r

    @filter.regex(handlers.results.RESULTS_LIST_RE)
    async def cmd_results_list(self, event: AstrMessageEvent):
        async for r in handlers.results.handle_results_list(self, event):
            yield r

    @filter.regex(handlers.stats.STATS_RE)
    async def cmd_stats(self, event: AstrMessageEvent):
        async for r in handlers.stats.handle_stats(self, event):
            yield r

    @filter.regex(handlers.admin.HLTV_TOGGLE_RE)
    async def cmd_hltv_toggle(self, event: AstrMessageEvent):
        async for r in handlers.admin.handle_hltv_toggle(self, event):
            yield r

    @filter.regex(handlers.help.HLTV_HELP_RE)
    async def cmd_hltv_help(self, event: AstrMessageEvent):
        async for r in handlers.help.handle_hltv_help(self, event):
            yield r

    @filter.regex(handlers.debug.HLTV_CHECK_RE)
    async def cmd_hltv_check(self, event: AstrMessageEvent):
        async for r in handlers.debug.handle_hltv_check(self, event):
            yield r

    @filter.regex(handlers.debug.HLTV_TRIGGER_RE)
    async def cmd_hltv_trigger(self, event: AstrMessageEvent):
        async for r in handlers.debug.handle_hltv_trigger(self, event):
            yield r

    # -------------------- 生命周期 --------------------

    async def initialize(self) -> None:
        """插件激活时调用：启动定时任务并执行延迟初始化"""
        # 提前准备浏览器会话（仅当配置了 FlareSolverr 时才真正动作），
        # 这样第一次查询就走在「已就绪」的路径上。
        try:
            await hltv_data.start()
        except Exception as e:
            logger.warning(f"HTTP 会话准备未完成，后续轮询会自动恢复: {e}")

        await start_scheduler_async()
        self._init_task = asyncio.create_task(delayed_init())

    async def terminate(self) -> None:
        """插件停用/重载时调用：停止调度器并释放资源"""
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
            self._init_task = None
        await stop_scheduler_async()
        try:
            await hltv_data.close()
        except Exception:
            pass
