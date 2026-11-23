import unittest
from app.parser import parse_version

HEADER = "班次号,类型,分钟数,常规开始日,常规结束日,星期,临时加开日期,停运日期\n"


def parse(text: str):
    return parse_version("测试", text.encode("utf-8"))


class TestParserHappyPath(unittest.TestCase):
    def test_parses_chinese_weekdays_and_semicolon_dates(self):
        text = HEADER + (
            "A1,到站,1380,2026-11-01,2026-12-31,一三五,,\n"
            "D1,发车,1470,2026-11-01,2026-12-31,1;2;3,,2026-11-20\n"
        )
        result = parse(text)
        assert result.errors == []
        assert result.header_error is None
        trips = {t.trip_id: t for t in result.version.trips}
        assert trips["A1"].kind == "arrival"
        assert trips["A1"].minute == 1380
        assert trips["A1"].schedule.weekdays == frozenset({1, 3, 5})
        assert trips["D1"].kind == "departure"
        assert trips["D1"].minute == 1470  # 跨午夜
        assert trips["D1"].schedule.weekdays == frozenset({1, 2, 3})
        from datetime import date
        assert date(2026, 11, 20) in trips["D1"].schedule.suspended

    def test_english_headers_and_kind_accepted(self):
        text = (
            "trip_id,kind,minute,start,end,weekdays,added,suspended\n"
            "A1,arrival,60,2026-11-01,2026-12-31,1-7,,\n"
        )
        result = parse(text)
        assert result.errors == []
        assert result.version.trips[0].kind == "arrival"
        assert result.version.trips[0].schedule.weekdays == frozenset(range(1, 8))

    def test_blank_lines_are_skipped(self):
        text = HEADER + "\nA1,到站,60,2026-11-01,2026-12-31,一,,\n\n"
        result = parse(text)
        assert result.errors == []
        assert len(result.version.trips) == 1


class TestParserErrors(unittest.TestCase):
    def test_missing_header_column_is_header_error(self):
        text = "班次号,类型,分钟数\nA1,到站,60\n"
        result = parse(text)
        assert result.version is None
        assert "缺少必需列" in result.header_error

    def test_unknown_column_reported(self):
        text = "班次号,类型,分钟数,常规开始日,常规结束日,星期,备注\nA1,到站,60,2026-11-01,2026-12-31,一,随便\n"
        result = parse(text)
        assert result.version is None
        assert "无法识别的列" in result.header_error

    def test_row_errors_collect_all_problems(self):
        text = HEADER + (
            "A1,到站,-5,2026-11-01,2026-12-31,一,,\n"
            "A2,停靠,60,2026-11-01,2026-12-31,八,,\n"
            "A3,发车,60,2026-12-31,2026-11-01,一,,\n"
            "A4,发车,60,2026-11-01,2026-12-31,一,2026-11-21,2026-11-21\n"
            "A1,到站,60,2026-11-01,2026-12-31,一,,\n"
        )
        result = parse(text)
        assert result.version is None
        # 每行的全部问题都会被收集：5 个数据行，第 2、3 行各有 2 个问题
        line_numbers = [e.line_number for e in result.errors]
        assert line_numbers == [2, 3, 3, 4, 5, 6]
        messages = " ".join(e.message for e in result.errors)
        assert "不能为负" in messages
        assert "类型应为" in messages
        assert "无法识别的星期" in messages
        assert "晚于结束日" in messages
        assert "既加开又停运" in messages
        assert "重复" in messages

    def test_multiple_errors_in_one_row_all_reported(self):
        # 同一行内分钟数为负、日期倒置、星期非法，应一次全部标出
        text = HEADER + "A1,到站,-1,2026-12-31,2026-11-01,八,,\n"
        result = parse(text)
        assert len(result.errors) == 3
        assert {e.line_number for e in result.errors} == {2}

    def test_bad_date_and_short_row_reported(self):
        text = HEADER + "A1,到站,60,2026/11/01,2026-12-31,一,,\nA2,到站,60\n"
        result = parse(text)
        assert len(result.errors) == 2
        assert "YYYY-MM-DD" in result.errors[0].message
        assert "列数不足" in result.errors[1].message

    def test_duplicate_exception_date_reported(self):
        text = HEADER + "A1,到站,60,2026-11-01,2026-12-31,一,2026-11-21;2026-11-21,\n"
        result = parse(text)
        assert len(result.errors) == 1

    def test_impossible_date_message_is_chinese(self):
        text = HEADER + "A1,到站,60,2026-13-01,2026-12-31,一,,\n"
        result = parse(text)
        assert len(result.errors) == 1
        assert "日期不存在或越界" in result.errors[0].message
