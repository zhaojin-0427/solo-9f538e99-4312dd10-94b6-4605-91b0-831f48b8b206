import unittest
from datetime import date

from app.analyzer import (
    TimetableVersion,
    Trip,
    analyze,
    find_connections,
)
from app.calendar import ServiceSchedule


def schedule(start="2026-01-01", end="2026-12-31", weekdays=(1, 2, 3, 4, 5, 6, 7),
             added=(), suspended=()):
    return ServiceSchedule(
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        weekdays=frozenset(weekdays),
        added=frozenset(date.fromisoformat(d) for d in added),
        suspended=frozenset(date.fromisoformat(d) for d in suspended),
    )


def trip(trip_id, kind, minute, **kwargs):
    return Trip(trip_id=trip_id, kind=kind, minute=minute, schedule=schedule(**kwargs))


def version(label, trips):
    return TimetableVersion(label=label, trips=tuple(trips))


class TestCrossMidnightConnections(unittest.TestCase):
    def test_departure_after_midnight_from_next_operating_day(self):
        # 到站 23:00（1380 分），次日 00:30 发车（30 分，属于下一个运营日）
        v = version("v", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 30),
        ])
        day = date(2026, 11, 19)
        conns = find_connections(v, v.by_id("A1"), day, max_wait=90)
        assert len(conns) == 1
        assert conns[0].departure_trip_id == "D1"
        assert conns[0].departure_day == date(2026, 11, 20)
        assert conns[0].wait_minutes == 90

    def test_arrival_itself_after_midnight_uses_absolute_time(self):
        # 到站时刻本身跨午夜：次日 00:10（1450 分），同日 00:40 发车（1480 分）
        v = version("v", [
            trip("A1", "arrival", 1450),
            trip("D1", "departure", 1480),
            trip("D2", "departure", 0),  # 下一运营日 00:00，仍早于到站，不可接
        ])
        day = date(2026, 11, 19)
        conns = find_connections(v, v.by_id("A1"), day, max_wait=60)
        assert [c.departure_trip_id for c in conns] == ["D1"]
        assert conns[0].wait_minutes == 30

    def test_departure_before_arrival_never_counts(self):
        v = version("v", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1370),  # 早 10 分钟
        ])
        conns = find_connections(v, v.by_id("A1"), date(2026, 11, 19), max_wait=60)
        assert conns == ()

    def test_wait_equal_to_limit_still_connects(self):
        v = version("v", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410),  # 恰好等待 30 分
        ])
        conns = find_connections(v, v.by_id("A1"), date(2026, 11, 19), max_wait=30)
        assert len(conns) == 1
        # 缩到 29 分则不可接驳
        conns2 = find_connections(v, v.by_id("A1"), date(2026, 11, 19), max_wait=29)
        assert conns2 == ()


class TestAnalyze(unittest.TestCase):
    def _old_version(self):
        return version("旧版", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410),   # 等待 30 分
        ])

    def test_first_lost_day_when_departure_suspended(self):
        old = self._old_version()
        new = version("新版", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410, suspended=("2026-11-20",)),
        ])
        result = analyze(old, new, date(2026, 11, 18), date(2026, 11, 25), 30)
        assert result.compared_arrivals == 1
        assert len(result.lost) == 1
        lost = result.lost[0]
        assert lost.arrival_trip_id == "A1"
        assert lost.first_day == date(2026, 11, 20)
        assert len(lost.old_connections) == 1
        assert lost.old_connections[0].departure_trip_id == "D1"
        assert lost.old_connections[0].wait_minutes == 30

    def test_suspension_overrides_weekday_on_first_day(self):
        # D1 常规仅周一开行，2026-11-23 周一停运；加开 2026-11-21 周六
        old = version("旧版", [
            trip("A1", "arrival", 1380, weekdays=(1, 2, 3, 4, 5)),
            trip("D1", "departure", 1410, weekdays=(1,)),
        ])
        new = version("新版", [
            trip("A1", "arrival", 1380, weekdays=(1, 2, 3, 4, 5)),
            trip("D1", "departure", 1410, weekdays=(1,),
                 added=("2026-11-21",), suspended=("2026-11-23",)),
        ])
        result = analyze(old, new, date(2026, 11, 19), date(2026, 11, 27), 30)
        # 旧版里 A1 只在工作日有车可接且 D1 仅周一可接：首个共同可接日为 11-23
        # 新版 11-23 D1 停运 -> 失去接驳；11-21 周六 A1 旧版不开行，不参与
        assert len(result.lost) == 1
        assert result.lost[0].first_day == date(2026, 11, 23)

    def test_no_loss_when_alternative_departure_exists(self):
        old = version("旧版", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410),
        ])
        new = version("新版", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410, suspended=("2026-11-20",)),
            trip("D9", "departure", 1405),  # 新版新增替代车，等待 25 分
        ])
        result = analyze(old, new, date(2026, 11, 18), date(2026, 11, 25), 30)
        assert result.lost == ()

    def test_arrival_operating_days_must_be_common(self):
        # 旧版 A1 全周开行，新版仅工作日开行；周末即使旧版有车也不纳入比较
        old = version("旧版", [
            trip("A1", "arrival", 1380, weekdays=(1, 2, 3, 4, 5, 6, 7)),
            trip("D1", "departure", 1410, weekdays=(1, 2, 3, 4, 5, 6, 7)),
        ])
        new = version("新版", [
            trip("A1", "arrival", 1380, weekdays=(1, 2, 3, 4, 5)),
            trip("D1", "departure", 1410, weekdays=(1, 2, 3, 4, 5)),
        ])
        result = analyze(old, new, date(2026, 11, 19), date(2026, 11, 30), 30)
        assert result.lost == ()
        assert result.compared_arrivals == 1

    def test_unmatched_arrivals_reported_separately(self):
        old = version("旧版", [trip("A1", "arrival", 60), trip("A2", "arrival", 70)])
        new = version("新版", [trip("A1", "arrival", 60), trip("A3", "arrival", 80)])
        result = analyze(old, new, date(2026, 11, 19), date(2026, 11, 30), 30)
        assert result.only_old_arrivals == ("A2",)
        assert result.only_new_arrivals == ("A3",)
        assert result.compared_arrivals == 1

    def test_range_boundaries_are_inclusive(self):
        old = self._old_version()
        new = version("新版", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410, suspended=("2026-11-20",)),
        ])
        # 范围结束日恰好是失去接驳日，应被报告
        result = analyze(old, new, date(2026, 11, 20), date(2026, 11, 20), 30)
        assert len(result.lost) == 1
        # 范围在其之前则无结论
        result_before = analyze(old, new, date(2026, 11, 18), date(2026, 11, 19), 30)
        assert result_before.lost == ()

    def test_multi_year_range_completes(self):
        # 跨三年，仅 1 月 1 日加开，验证按运营日跳跃、不逐日展开
        old = version("旧版", [
            trip("A1", "arrival", 60, start="2026-01-01", end="2028-12-31",
                 weekdays=(1,), added=tuple(f"{y}-01-01" for y in (2026, 2027, 2028))),
            trip("D1", "departure", 75, start="2026-01-01", end="2028-12-31",
                 weekdays=(1,), added=tuple(f"{y}-01-01" for y in (2026, 2027, 2028))),
        ])
        new = version("新版", [
            trip("A1", "arrival", 60, start="2026-01-01", end="2028-12-31",
                 weekdays=(1,), added=tuple(f"{y}-01-01" for y in (2026, 2027, 2028))),
            trip("D1", "departure", 75, start="2026-01-01", end="2028-12-31",
                 weekdays=(1,), added=tuple(f"{y}-01-01" for y in (2026, 2027, 2028)),
                 suspended=("2027-01-01",)),
        ])
        result = analyze(old, new, date(2026, 1, 1), date(2028, 12, 31), 30)
        assert len(result.lost) == 1
        assert result.lost[0].first_day == date(2027, 1, 1)

    def test_each_version_uses_its_own_arrival_minute(self):
        # 新版到站提前 10 分钟（1370），旧版接驳车 1410 对新版等待 40 分，超出 30 分上限
        old = version("旧版", [
            trip("A1", "arrival", 1380),
            trip("D1", "departure", 1410),
        ])
        new = version("新版", [
            trip("A1", "arrival", 1370),
            trip("D1", "departure", 1410),
        ])
        result = analyze(old, new, date(2026, 11, 19), date(2026, 11, 19), 30)
        assert len(result.lost) == 1
        assert result.lost[0].old_arrival_minute == 1380
        assert result.lost[0].new_arrival_minute == 1370
        # 上限放宽到 40 后新版仍可接驳
        result2 = analyze(old, new, date(2026, 11, 19), date(2026, 11, 19), 40)
        assert result2.lost == ()
