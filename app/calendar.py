"""运营日历：按"常规区间+星期"与临时加开/停运日期描述班次的运营日。

临时调整按班次自己的运营日覆盖周历：
- 停运日期即使命中常规星期也不开行；
- 加开日期即使不在常规区间内或不命中星期也开行。

所有查询都在"实际运营日"之间跳跃，不逐日展开日历。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MINUTES_PER_DAY = 1440


@dataclass(frozen=True)
class ServiceSchedule:
    start: date
    end: date
    weekdays: frozenset[int]  # ISO 星期：1=周一 ... 7=周日
    added: frozenset[date] = frozenset()
    suspended: frozenset[date] = frozenset()

    def operates_on(self, day: date) -> bool:
        """该班次在给定运营日是否开行。"""
        if day in self.suspended:
            return False
        if day in self.added:
            return True
        if day < self.start or day > self.end:
            return False
        return day.isoweekday() in self.weekdays

    def _first_regular_on_or_after(self, day: date) -> date | None:
        if day < self.start:
            day = self.start
        if day > self.end:
            return None
        weekday = day.isoweekday()
        delta = min((w - weekday) % 7 for w in self.weekdays)
        candidate = day + timedelta(days=delta)
        return candidate if candidate <= self.end else None

    @staticmethod
    def _next_regular_after(day: date, end: date, weekdays: frozenset[int]) -> date | None:
        weekday = day.isoweekday()
        # 严格大于 0 的最小位移
        delta = min((w - weekday - 1) % 7 + 1 for w in weekdays)
        candidate = day + timedelta(days=delta)
        return candidate if candidate <= end else None

    def first_on_or_after(self, day: date) -> date | None:
        """返回 >= day 的首个开行运营日，不存在则返回 None。

        常规流按周跳跃，加开流按有序日期跳跃，二路归并并跳过停运日，
        中间不枚举非运营日。
        """
        extra = tuple(
            sorted(d for d in self.added if d >= day and d not in self.suspended)
        )
        extra_index = 0
        regular = self._first_regular_on_or_after(day)
        while True:
            candidates = []
            if regular is not None:
                candidates.append(regular)
            if extra_index < len(extra):
                candidates.append(extra[extra_index])
            if not candidates:
                return None
            best = min(candidates)
            if best in self.suspended:
                if regular == best:
                    regular = self._next_regular_after(
                        regular, self.end, self.weekdays
                    )
                if (
                    extra_index < len(extra)
                    and extra[extra_index] == best
                ):
                    extra_index += 1
                continue
            return best


def common_first(
    schedule_a: ServiceSchedule, schedule_b: ServiceSchedule, day: date
) -> date | None:
    """两个日历都开行的、>= day 的首个共同运营日。

    各自跳到自身下一运营日后对齐，任何一方耗尽即停止，不逐日试算。
    """
    while True:
        next_a = schedule_a.first_on_or_after(day)
        next_b = schedule_b.first_on_or_after(day)
        if next_a is None or next_b is None:
            return None
        if next_a == next_b:
            return next_a
        day = next_a if next_a > next_b else next_b
