"""HLTV 全局订阅、群开关（兼容 AstrBot UMO 与 HakuBot 旧数据）及推送去重持久化。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from .json_io import atomic_write_json, load_json
from .log_compat import logger


@dataclass
class EventSubscription:
    event_id: str
    event_title: str
    start_date: str
    end_date: str
    # 决赛结果推送后置为 watching 开始扫 MVP，推送后为 pushed，超时为 expired。
    mvp_status: str = ""
    mvp_watch_since: str = ""


@dataclass
class GroupData:
    group_id: str
    enabled: bool = False
    # AstrBot 会话统一来源（如 aiocqhttp:GroupMessage:123456789）；空串时由 SnowLuma/OneBot 适配器兜底发送
    umo: str = ""


NotifiedKind = Literal["starts", "results", "map_results"]
NOTIFIED_KINDS: tuple[NotifiedKind, ...] = ("starts", "results", "map_results")


def _norm_group_id(group_id: str | int) -> str:
    return str(group_id).strip()


class DataManager:
    def __init__(
        self,
        data_dir: Path,
        default_enabled_groups: Sequence[str] = (),
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data_file = self._data_dir / "subscriptions.json"
        state = load_json(
            self._data_file, missing_ok=True, default={}, backup_on_error=True
        )
        if not state.success:
            logger.error(
                f"[HLTV] 订阅状态文件损坏，已备份为 {state.backup_path} 并使用空状态初始化: {state.error}"
            )
            raw = {}
        else:
            raw = state.data

        self._groups: dict[str, GroupData] = {}
        for item in raw.get("groups", []):
            gid = _norm_group_id(item.get("group_id", ""))
            if not gid:
                continue
            umo = str(item.get("umo", "") or "")
            platform_id = str(item.get("platform_id", "") or "").strip()
            if not umo and platform_id:
                umo = f"{platform_id}:GroupMessage:{gid}"
            self._groups[gid] = GroupData(
                group_id=gid,
                enabled=bool(item.get("enabled", False)),
                umo=umo,
            )

        raw_subs = raw.get("global_subscriptions")
        if raw_subs is None:
            merged_subs: dict[str, dict] = {}
            for item in raw.get("groups", []):
                if item.get("enabled", False):
                    for s in item.get("subscribed_events", []):
                        if "event_id" in s and "event_title" in s:
                            merged_subs[str(s["event_id"])] = s
            raw_subs = list(merged_subs.values())

        self._global_subscriptions: list[EventSubscription] = [
            EventSubscription(
                event_id=str(item["event_id"]),
                event_title=str(item["event_title"]),
                start_date=str(item.get("start_date", "")),
                end_date=str(item.get("end_date", "")),
                mvp_status=str(item.get("mvp_status", "")),
                mvp_watch_since=str(item.get("mvp_watch_since", "")),
            )
            for item in raw_subs
            if "event_id" in item and "event_title" in item
        ]

        scheduler_state = raw.get("scheduler_state", {})
        now_iso = datetime.now().isoformat()
        self._notified: dict[NotifiedKind, dict[str, str]] = {}
        for kind in NOTIFIED_KINDS:
            bucket = scheduler_state.get(f"notified_{kind}", {})
            if isinstance(bucket, dict):
                self._notified[kind] = {str(k): str(v) for k, v in bucket.items()}
            elif isinstance(bucket, list):
                self._notified[kind] = {str(k): now_iso for k in bucket}
            else:
                self._notified[kind] = {}

        # 将 WebUI 配置中新增的默认群号合并进来（不覆盖群内手动关闭过的记录）
        added_default = False
        for raw_gid in default_enabled_groups:
            gid = _norm_group_id(raw_gid)
            if gid and gid not in self._groups:
                self._groups[gid] = GroupData(group_id=gid, enabled=True)
                added_default = True
        if added_default:
            self._save()

    def _save(self, *, groups=None, subscriptions=None, notified=None) -> None:
        groups = self._groups if groups is None else groups
        subscriptions = (
            self._global_subscriptions if subscriptions is None else subscriptions
        )
        notified = self._notified if notified is None else notified
        atomic_write_json(
            self._data_file,
            {
                "groups": [asdict(g) for g in groups.values()],
                "global_subscriptions": [asdict(s) for s in subscriptions],
                "scheduler_state": {f"notified_{k}": v for k, v in notified.items()},
            },
        )
        self._groups = groups
        self._global_subscriptions = subscriptions
        self._notified = notified

    def is_enabled(self, group_id: str | int) -> bool:
        group = self._groups.get(_norm_group_id(group_id))
        return group is not None and group.enabled

    def has_enabled_groups(self) -> bool:
        return any(group.enabled for group in self._groups.values())

    def set_enabled(
        self, group_id: str | int, enabled: bool, *, umo: str = ""
    ) -> None:
        gid = _norm_group_id(group_id)
        existing = self._groups.get(gid)
        resolved_umo = umo or (existing.umo if existing else "")
        self._save(
            groups={
                **self._groups,
                gid: GroupData(group_id=gid, enabled=enabled, umo=resolved_umo),
            }
        )

    def record_group_umo(self, group_id: str | int, umo: str) -> None:
        """在已记录的群会话中自动同步最新 unified_msg_origin。"""
        gid = _norm_group_id(group_id)
        if not gid or not umo:
            return
        existing = self._groups.get(gid)
        if existing is None or existing.umo == umo:
            return
        self._save(
            groups={
                **self._groups,
                gid: replace(existing, umo=umo),
            }
        )

    def get_subscribed_events(self) -> list[EventSubscription]:
        return list(self._global_subscriptions)

    def subscribe_event(self, subscription: EventSubscription) -> bool:
        if self.is_subscribed(subscription.event_id):
            return False
        self._save(subscriptions=[*self._global_subscriptions, subscription])
        return True

    def unsubscribe_event(self, event_id: str) -> bool:
        remaining = [s for s in self._global_subscriptions if s.event_id != event_id]
        if len(remaining) == len(self._global_subscriptions):
            return False
        self._save(subscriptions=remaining)
        return True

    def is_subscribed(self, event_id: str) -> bool:
        return any(s.event_id == event_id for s in self._global_subscriptions)

    def get_all_subscribed_event_ids(self) -> set[str]:
        return {s.event_id for s in self._global_subscriptions}

    def get_enabled_groups_by_event(self, event_id: str) -> list[GroupData]:
        if not self.is_subscribed(event_id):
            return []
        return [g for g in self._groups.values() if g.enabled]

    def get_subscription(self, event_id: str) -> EventSubscription | None:
        return next(
            (s for s in self._global_subscriptions if s.event_id == event_id), None
        )

    def update_subscription_meta(
        self,
        event_id: str,
        *,
        event_title: str,
        start_date: str,
        end_date: str,
    ) -> None:
        current = self.get_subscription(event_id)
        if current is not None:
            self._replace_subscription(
                replace(
                    current,
                    event_title=event_title,
                    start_date=start_date,
                    end_date=end_date,
                )
            )

    def set_mvp_status(self, event_id: str, status: str) -> None:
        current = self.get_subscription(event_id)
        if current is None:
            return
        since = (
            datetime.now().isoformat()
            if status == "watching"
            else current.mvp_watch_since
        )
        self._replace_subscription(
            replace(current, mvp_status=status, mvp_watch_since=since)
        )

    def _replace_subscription(self, updated: EventSubscription) -> None:
        if updated == self.get_subscription(updated.event_id):
            return
        self._save(
            subscriptions=[
                updated if s.event_id == updated.event_id else s
                for s in self._global_subscriptions
            ]
        )

    def is_notified(self, kind: NotifiedKind, item_id: str) -> bool:
        return item_id in self._notified[kind]

    def mark_notified(self, kind: NotifiedKind, item_id: str) -> None:
        self._save(
            notified={
                **self._notified,
                kind: {**self._notified[kind], item_id: datetime.now().isoformat()},
            }
        )

    def cleanup_notified_state(self, ttl_days: int) -> int:
        cutoff = datetime.now() - timedelta(days=max(1, ttl_days))
        kept: dict[NotifiedKind, dict[str, str]] = {}
        for kind, records in self._notified.items():
            kept_records: dict[str, str] = {}
            for key, ts in records.items():
                try:
                    parsed = datetime.fromisoformat(ts).replace(tzinfo=None)
                except ValueError:
                    continue
                if parsed >= cutoff:
                    kept_records[key] = ts
            kept[kind] = kept_records
        removed = sum(map(len, self._notified.values())) - sum(map(len, kept.values()))
        if removed:
            self._save(notified=kept)
        return removed
