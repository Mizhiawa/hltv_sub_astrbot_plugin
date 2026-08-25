"""
apscheduler 封装 - 替代 nonebot_plugin_apscheduler 的全局 scheduler 对象

AstrBot 环境下没有 nonebot 的 scheduler 单例，这里直接用 apscheduler 的
AsyncIOScheduler 提供相同的最小接口（get_job/add_job/pause_job/resume_job/
remove_job/reschedule_job/get_jobs），并管理启动/停止生命周期。

注意：
- AsyncIOScheduler.start() 必须在使用中的事件循环内调用（插件
  initialize() 中），否则任务会挂在孤立的 loop 上而不会执行。
- AsyncIOScheduler 实例被 shutdown 后不可复用，因此 stop 后置空，
  下次通过 get_scheduler() 惰性重建（支持插件禁用->启用的循环）。
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import get_config

_scheduler: AsyncIOScheduler | None = None
_started = False


def get_scheduler() -> AsyncIOScheduler:
    """获取当前调度器实例（惰性创建；stop 后重建）"""
    global _scheduler
    if _scheduler is None:
        tz = get_config().hltv_timezone or "Asia/Shanghai"
        _scheduler = AsyncIOScheduler(timezone=tz)
    return _scheduler


def start_scheduler() -> None:
    """启动调度器（幂等，需在事件循环中调用）"""
    global _started
    if not _started:
        get_scheduler().start()
        _started = True


def stop_scheduler() -> None:
    """停止调度器（幂等）；stop 后下次 get_scheduler() 会重建实例"""
    global _scheduler, _started
    if _started:
        get_scheduler().shutdown(wait=False)
        _started = False
    _scheduler = None
