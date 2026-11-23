"""接驳核查核心算法。

规则：
- 按班次号匹配两版在同一运营日都开行的到站班次；
- 等待时间从各版自己的到站时刻（可跨午夜）起算，达到上限仍可接驳；
- 接驳车可以属于另一个运营日（到站后午夜前后的发车都要考虑）；
- 临时加开/停运按班次自己的运营日覆盖周历；
- 找出旧版有车可接、新版无车可接的首个运营日。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .calendar import MINUTES_PER_DAY, ServiceSchedule, common_first


@dataclass(frozen=True)
class Trip:
    trip_id: str
    kind: str  # "arrival" | "departure"
    minute: int  # 从本运营日零点起算的分钟数，可 >= 1440
    schedule: ServiceSchedule


@dataclass(frozen=True)
class TimetableVersion:
    label: str
    trips: tuple[Trip, ...]

    def by_id(self, trip_id: str) -> Trip | None:
        for trip in self.trips:
            if trip.trip_id == trip_id:
                return trip
        return None

    @property
    def arrivals(self) -> tuple[Trip, ...]:
        return tuple(t for t in self.trips if t.kind == "arrival")

    @property
    def departures(self) -> tuple[Trip, ...]:
        return tuple(t for t in self.trips if t.kind == "departure")


@dataclass(frozen=True)
class Connection:
    departure_trip_id: str
    departure_day: date  # 发车班次自己的运营日
    wait_minutes: int  # 从到站时刻到发车绝对时刻的等待分钟数


@dataclass(frozen=True)
class LostConnection:
    arrival_trip_id: str
    first_day: date
    old_arrival_minute: int
    new_arrival_minute: int
    old_connections: tuple[Connection, ...]  # 当日旧版可接的全部发车班次


@dataclass(frozen=True)
class AnalysisResult:
    lost: tuple[LostConnection, ...]
    only_old_arrivals: tuple[str, ...]  # 仅旧版存在的到站班次
    only_new_arrivals: tuple[str, ...]  # 仅新版存在的到站班次
    compared_arrivals: int


def _ceil_div(numerator: int, denominator: int) -> int:
    """整数上取整除法，支持负数分子。"""
    assert denominator > 0
    return -((-numerator) // denominator)


def find_connections(
    version: TimetableVersion, arrival: Trip, day: date, max_wait: int
) -> tuple[Connection, ...]:
    """某到站班次在 day 运营日到站时，该版本内可接的全部发车班次。

    到站与发车的分钟数都可能跨午夜（>=1440），因此运营日偏移 k 的
    枚举窗口按数据中实际的最大发车分钟数推导，而不是假设分钟数 < 1440。
    """
    departures = version.departures
    if not departures:
        return ()

    arrival_absolute = arrival.minute  # 以 day 零点为基准
    earliest_absolute = arrival_absolute
    latest_absolute = arrival_absolute + max_wait
    max_departure_minute = max(d.minute for d in departures)
    # 对某运营日偏移 k，发车绝对时刻落在 [k*1440, k*1440+max_departure_minute]，
    # 该区间与等待窗口有交集才需要枚举。
    k_min = _ceil_div(
        earliest_absolute - max_departure_minute, MINUTES_PER_DAY
    )
    k_max = latest_absolute // MINUTES_PER_DAY

    found: list[Connection] = []
    for departure in departures:
        for k in range(k_min, k_max + 1):
            departure_day = date.fromordinal(day.toordinal() + k)
            if not departure.schedule.operates_on(departure_day):
                continue
            departure_absolute = k * MINUTES_PER_DAY + departure.minute
            wait = departure_absolute - arrival_absolute
            if 0 <= wait <= max_wait:
                found.append(
                    Connection(
                        departure_trip_id=departure.trip_id,
                        departure_day=departure_day,
                        wait_minutes=wait,
                    )
                )
    # 按发车绝对时刻（等待时长）、班次号排序，输出稳定
    found.sort(key=lambda c: (c.wait_minutes, c.departure_trip_id))
    return tuple(found)


def analyze(
    old: TimetableVersion,
    new: TimetableVersion,
    range_start: date,
    range_end: date,
    max_wait: int,
) -> AnalysisResult:
    old_arrivals = {t.trip_id: t for t in old.arrivals}
    new_arrivals = {t.trip_id: t for t in new.arrivals}

    only_old = tuple(sorted(set(old_arrivals) - set(new_arrivals)))
    only_new = tuple(sorted(set(new_arrivals) - set(old_arrivals)))

    lost: list[LostConnection] = []
    compared = 0

    for trip_id in sorted(set(old_arrivals) & set(new_arrivals)):
        old_arrival = old_arrivals[trip_id]
        new_arrival = new_arrivals[trip_id]

        # 两版在同一运营日都开行才纳入比较；共同运营日之间跳跃，不逐日试算
        common_start = common_first(
            old_arrival.schedule, new_arrival.schedule, range_start
        )
        if common_start is None or common_start > range_end:
            continue
        compared += 1

        cursor = common_start
        while cursor <= range_end:
            old_connections = find_connections(old, old_arrival, cursor, max_wait)
            if old_connections:
                new_connections = find_connections(new, new_arrival, cursor, max_wait)
                if not new_connections:
                    lost.append(
                        LostConnection(
                            arrival_trip_id=trip_id,
                            first_day=cursor,
                            old_arrival_minute=old_arrival.minute,
                            new_arrival_minute=new_arrival.minute,
                            old_connections=old_connections,
                        )
                    )
                    break  # 仅报告首个运营日
            # 跳到下一个两版共同开行日
            next_day = common_first(
                old_arrival.schedule,
                new_arrival.schedule,
                date.fromordinal(cursor.toordinal() + 1),
            )
            if next_day is None or next_day > range_end:
                break
            cursor = next_day

    return AnalysisResult(
        lost=tuple(lost),
        only_old_arrivals=only_old,
        only_new_arrivals=only_new,
        compared_arrivals=compared,
    )
