import unittest
from datetime import date, timedelta

from app.calendar import ServiceSchedule, common_first


def make_schedule(start="2026-01-01", end="2026-12-31", weekdays=(1, 2, 3, 4, 5),
                  added=(), suspended=()):
    def to_day(value):
        return value if isinstance(value, date) else date.fromisoformat(value)

    return ServiceSchedule(
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        weekdays=frozenset(weekdays),
        added=frozenset(to_day(d) for d in added),
        suspended=frozenset(to_day(d) for d in suspended),
    )


class TestServiceSchedule(unittest.TestCase):
    def test_weekday_calendar_skips_weekend(self):
        schedule = make_schedule(start="2026-11-19", end="2026-11-30")
        # 2026-11-19 是周四
        first = schedule.first_on_or_after(date(2026, 11, 19))
        assert first == date(2026, 11, 19)
        # 2026-11-20 周五之后是周末，下一个运营日直接跳到周一 11-23
        nxt = schedule.first_on_or_after(date(2026, 11, 21))
        assert nxt == date(2026, 11, 23)

    def test_suspension_overrides_matching_weekday(self):
        # 2026-11-20 周五，被停运
        schedule = make_schedule(suspended=("2026-11-20",))
        assert schedule.operates_on(date(2026, 11, 20)) is False
        nxt = schedule.first_on_or_after(date(2026, 11, 20))
        assert nxt == date(2026, 11, 23)

    def test_added_date_overrides_out_of_range_and_weekend(self):
        # 2026-11-21 周六，不在工作日历内，加开后应开行
        schedule = make_schedule(added=("2026-11-21",))
        assert schedule.operates_on(date(2026, 11, 21)) is True
        first = schedule.first_on_or_after(date(2026, 11, 21))
        assert first == date(2026, 11, 21)

    def test_added_date_before_range_is_serviced(self):
        schedule = make_schedule(start="2026-12-01", added=("2026-11-15",))
        assert schedule.operates_on(date(2026, 11, 15)) is True
        first = schedule.first_on_or_after(date(2026, 11, 1))
        assert first == date(2026, 11, 15)

    def test_first_on_or_after_past_end_returns_none(self):
        schedule = make_schedule(start="2026-01-01", end="2026-01-02")
        assert schedule.first_on_or_after(date(2026, 2, 1)) is None

    def test_suspended_added_conflict_is_serviced_neither(self):
        # 解析层会拦截同加同停；这里直接构造时停运优先
        schedule = make_schedule(added=("2026-11-21",), suspended=("2026-11-21",))
        assert schedule.operates_on(date(2026, 11, 21)) is False
        assert schedule.first_on_or_after(date(2026, 11, 21)) == date(2026, 11, 23)


class TestCommonFirst(unittest.TestCase):
    def test_aligns_two_calendars(self):
        weekday_only = make_schedule(weekdays=(1,))  # 仅周一
        daily = make_schedule(weekdays=(1, 2, 3, 4, 5, 6, 7))
        day = common_first(weekday_only, daily, date(2026, 11, 19))  # 周四
        assert day == date(2026, 11, 23)  # 下一个周一

    def test_suspension_in_one_calendar_moves_common_day(self):
        plain = make_schedule()
        # 周四 11-19、周五 11-20 均停运，首个共同运营日为周一 11-23
        with_hold = make_schedule(suspended=("2026-11-19", "2026-11-20"))
        day = common_first(plain, with_hold, date(2026, 11, 19))
        assert day == date(2026, 11, 23)

    def test_no_common_day_in_range(self):
        mondays = make_schedule(start="2026-11-01", end="2026-11-30", weekdays=(1,))
        tuesdays = make_schedule(start="2026-11-01", end="2026-11-30", weekdays=(2,))
        assert common_first(mondays, tuesdays, date(2026, 11, 1)) is None

    def test_multi_year_jump_does_not_iterate_days(self):
        # 跨三年的稀疏日历：常规区间不重叠、加开日交错 -> 无任何共同运营日，
        # 且查询应按运营日跳跃完成而不是逐日展开
        added_a = {
            date(2026, 1, 1) + timedelta(days=31 * i)
            for i in range(36)
        }
        added_b = {
            date(2026, 1, 1) + timedelta(days=31 * i + 1)
            for i in range(36)
        }
        sparse_a = make_schedule(start="2025-01-01", end="2025-12-31",
                                 weekdays=(1,), added=added_a)
        sparse_b = make_schedule(start="2029-01-01", end="2029-12-31",
                                 weekdays=(2,), added=added_b)
        assert common_first(sparse_a, sparse_b, date(2026, 1, 1)) is None
        # 同型日历应直接跳到首个共同加开日（2026-01-01 之后的 31 天）
        first = common_first(sparse_a, sparse_a, date(2026, 1, 2))
        assert first == (date(2026, 1, 1) + timedelta(days=31))
