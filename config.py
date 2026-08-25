"""HLTV 订阅插件配置（AstrBot 适配版）

说明：
- 配置项与 NoneBot 版完全一致，默认值相同。
- AstrBot 在插件实例化时将 _conf_schema.json 解析出的配置注入到
  Star 构造函数的 config 参数。本模块维护一个全局 Config 单例，
  在插件 __init__/initialize 时调用 configure() 注入，供模块级代码读取。
"""

from __future__ import annotations

from typing import Any

# 默认值（与 _conf_schema.json 保持一致）
_DEFAULTS: dict[str, Any] = {
    "hltv_min_delay": 5.0,  # 最小延迟（秒）- 用于重试时的延迟基数
    "hltv_timeout": 15,  # 超时时间（秒）
    "hltv_timezone": "Asia/Shanghai",  # 时区
    "hltv_auto_unsub_delay_days": 2,  # 赛事结束后延迟多少天自动取消订阅
    "hltv_notified_ttl_days": 30,  # 推送去重状态保留天数
    "hltv_scheduler_max_parallel": 3,  # 多赛事轮询并发上限（防止请求风暴）
    "hltv_scheduler_jitter_seconds": 8,  # 每赛事 job 触发抖动，避免同刻并发
    "hltv_watermark_text": "Designed by Hakuchumu\nModified by M1z",  # 自定义水印文本
    "hltv_proxy_list": [],  # 代理列表
    "hltv_impersonate": "chrome124",  # 浏览器指纹模拟（curl_cffi impersonate）
    "hltv_flaresolverr_url": "",  # FlareSolverr 地址（可选）
    "hltv_superusers": [],  # 插件级超级用户 ID 列表
    "hltv_enable_map_result_push": True,  # 是否逐图播报（非 BO1 每张地图打完播报一次）
}


class Config:
    """HLTV 订阅插件配置（属性访问，缺失时回退默认值）"""

    def __init__(self, data: dict | None = None) -> None:
        self._data: dict = dict(_DEFAULTS)
        if data:
            # 仅覆盖已知配置项，忽略多余键
            for k in _DEFAULTS:
                if k in data and data[k] is not None:
                    self._data[k] = data[k]

    def __getattr__(self, name: str) -> Any:
        # __getattr__ 只在常规查找失败时调用；显式避免访问 _data 自身递归
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def as_dict(self) -> dict:
        return dict(self._data)


# 全局配置单例（插件未注入配置前使用默认值）
_plugin_config = Config()


def configure(data: dict | None) -> Config:
    """由插件入口调用，注入 AstrBot 运行时配置

    注意：原地更新全局单例的内部数据（而非替换对象），保证已通过
    ``from .config import plugin_config`` 绑定到同一对象的模块也能读到新配置。
    """
    global _plugin_config
    new_config = Config(data)
    _plugin_config._data = new_config._data
    return _plugin_config


def get_config() -> Config:
    """获取当前插件配置"""
    return _plugin_config


# 兼容旧代码的模块级配置对象
plugin_config = _plugin_config
