"""可注入时钟：领域与应用层只依赖该抽象，过期判定可在测试中精确控制。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试用固定时钟，可手工推进。"""

    def __init__(self, start: datetime) -> None:
        self._at = _aware(start)

    def now(self) -> datetime:
        return self._at

    def advance(self, **kwargs) -> datetime:
        self._at = self._at + timedelta(**kwargs)
        return self._at

    def set(self, value: datetime) -> None:
        self._at = _aware(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
