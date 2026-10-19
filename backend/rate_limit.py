# -*- coding: utf-8 -*-
"""[patcher:group-cooldown] meme_manager —— 群级自动配图冷却

设计口径（2026-10-20 用户裁定）
------------------------------
- 只维护**一张内存表**：会话 umo → 最近一次成功发图时刻（``time.monotonic``）；
- 私聊直接放行，只有群聊参与限额（Q5=a）；
- ``interval <= 0`` 视为不限；全局默认 + 每群覆盖（Q3=b，单位秒）；
- 表容量上限 1024，超出按最旧时刻淘汰，避免长期运行无限增长；
- **不落盘**：重启视为配额重置（Q6=a），不引入并发写保护的持久文件。

读写分离（评审要求）
--------------------
- :meth:`GroupImageCooldown.allows` 是**只读**判断，供注入闸 / 附加闸 / 响应闸调用；
  判定阶段绝不写时间戳，否则模型回了纯文字也会白白吃掉配额。
- :meth:`GroupImageCooldown.mark` 只在**图片真正发出后**调用一次。
"""

from __future__ import annotations

import json
import time
from typing import Any

# 内存表单次最多跟踪的会话数；超出后淘汰最旧记录。
MAX_TRACKED_SESSIONS = 1024

# 冷却上限一天，防止误填超大值把群永久锁死。
MAX_INTERVAL_SECONDS = 86400


def parse_interval(value: Any, default: int = 0) -> int:
    """把配置值归一成 0..86400 的整数秒。

    Args:
        value: 配置里的原始值（可能是 int / float / 数字字符串）。
        default: 无法解析时返回的兜底值。

    Returns:
        夹在 ``0..MAX_INTERVAL_SECONDS`` 之间的整数秒，0 表示不限制。
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        try:
            return max(0, min(MAX_INTERVAL_SECONDS, int(default)))
        except (TypeError, ValueError):
            return 0
    return max(0, min(MAX_INTERVAL_SECONDS, seconds))


def normalize_group_map(value: Any) -> dict[str, int]:
    """把 ``per_group`` 配置归一成 ``{群号或 umo: 秒}``。

    面板把 ``dict`` 字段渲染成单行/多行文本，保存回来可能是 JSON 字符串；
    手改配置文件时则是真正的 dict。两种都接受，非法内容一律忽略。

    Args:
        value: dict、JSON 文本或 None。

    Returns:
        归一化后的映射；值已经是夹好的整数秒。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict):
        return {}

    mapping: dict[str, int] = {}
    for raw_key, raw_seconds in value.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        mapping[key] = parse_interval(raw_seconds, 0)
    return mapping


def session_key(event: Any) -> str:
    """返回会话标识（``unified_msg_origin``），用作内存表的键。"""
    return str(getattr(event, "unified_msg_origin", "") or "")


def group_id(event: Any) -> str:
    """返回群号；私聊返回空字符串（核心对私聊的 ``group_id`` 就是空串）。"""
    getter = getattr(event, "get_group_id", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip()
    except Exception:
        return ""


class GroupImageCooldown:
    """群聊自动配图的单向时间闸。

    实例挂在插件对象上（``self._image_cooldown``），插件热重载即重建，
    与"纯内存、重启重置"的口径一致。
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def clear(self) -> None:
        """清空全部记录（仅调试/测试用）。"""
        self._last.clear()

    def tracked(self) -> int:
        """当前跟踪的会话数量（仅调试/测试用）。"""
        return len(self._last)

    def interval_for(
        self,
        event: Any,
        enabled: Any,
        default_interval: Any,
        per_group: Any,
    ) -> int:
        """返回该会话适用的冷却秒数；0 表示不受限。

        优先级：每群覆盖（先群号、后完整 umo）→ 全局默认。
        """
        if not enabled or event is None:
            return 0
        group = group_id(event)
        if not group:
            # 私聊不参与限额。
            return 0
        mapping = normalize_group_map(per_group)
        for key in (group, session_key(event)):
            if key and key in mapping:
                return mapping[key]
        return parse_interval(default_interval, 0)

    def allows(
        self,
        event: Any,
        enabled: Any,
        default_interval: Any,
        per_group: Any,
        *,
        now: float | None = None,
    ) -> bool:
        """只读判断当前会话此刻能否自动发图（不写状态）。"""
        interval = self.interval_for(event, enabled, default_interval, per_group)
        if interval <= 0:
            return True
        last = self._last.get(session_key(event))
        if last is None:
            return True
        current = time.monotonic() if now is None else now
        return (current - last) >= interval

    def remaining(
        self,
        event: Any,
        enabled: Any,
        default_interval: Any,
        per_group: Any,
        *,
        now: float | None = None,
    ) -> float:
        """返回剩余冷却秒数，0 表示此刻可以发（仅用于日志）。"""
        interval = self.interval_for(event, enabled, default_interval, per_group)
        if interval <= 0:
            return 0.0
        last = self._last.get(session_key(event))
        if last is None:
            return 0.0
        current = time.monotonic() if now is None else now
        return max(0.0, interval - (current - last))

    def mark(
        self,
        event: Any,
        enabled: Any,
        default_interval: Any,
        per_group: Any,
        *,
        now: float | None = None,
    ) -> bool:
        """在图片成功发出后记账；不受限的会话不写状态。

        Returns:
            真正写入记录时返回 True。
        """
        interval = self.interval_for(event, enabled, default_interval, per_group)
        if interval <= 0:
            return False
        key = session_key(event)
        if not key:
            return False
        self._last[key] = time.monotonic() if now is None else now
        self._prune()
        return True

    def _prune(self) -> None:
        """表满时淘汰最旧记录，避免内存无界增长。"""
        overflow = len(self._last) - MAX_TRACKED_SESSIONS
        if overflow <= 0:
            return
        for key, _ in sorted(self._last.items(), key=lambda item: item[1])[:overflow]:
            self._last.pop(key, None)
