"""CSV 时刻表解析与逐行校验。

列（表头支持中文，见 HEADER_ALIASES）：
  班次号、类型(到站/发车 或 arrival/departure)、分钟数(从运营日零点起，可跨午夜)、
  常规开始日、常规结束日、星期(如 1,2,3 或 一,三,五)、
  临时加开日期(分号分隔，可空)、停运日期(分号分隔，可空)
日期格式 YYYY-MM-DD。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from .calendar import MINUTES_PER_DAY, ServiceSchedule

HEADER_ALIASES = {
    "班次号": "trip_id",
    "trip_id": "trip_id",
    "id": "trip_id",
    "类型": "kind",
    "type": "kind",
    "kind": "kind",
    "分钟数": "minute",
    "分钟": "minute",
    "minute": "minute",
    "minutes": "minute",
    "time": "minute",
    "时刻": "minute",
    "常规开始日": "start",
    "开始日": "start",
    "start": "start",
    "常规结束日": "end",
    "结束日": "end",
    "end": "end",
    "星期": "weekdays",
    "weekday": "weekdays",
    "weekdays": "weekdays",
    "临时加开日期": "added",
    "加开": "added",
    "added": "added",
    "停运日期": "suspended",
    "停运": "suspended",
    "suspended": "suspended",
}

REQUIRED_FIELDS = ("trip_id", "kind", "minute", "start", "end", "weekdays")

KIND_ALIASES = {
    "到站": "arrival",
    "到": "arrival",
    "arrival": "arrival",
    "arr": "arrival",
    "发车": "departure",
    "发": "departure",
    "departure": "departure",
    "dep": "departure",
}

WEEKDAY_CN = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}

# 分钟数允许跨午夜，但不能超过七天，防止明显脏数据
MAX_MINUTE = 7 * MINUTES_PER_DAY - 1


@dataclass
class RowError:
    line_number: int  # CSV 物理行号（含表头），便于排班员定位
    trip_id: str
    message: str


@dataclass
class ParseResult:
    version: object | None  # TimetableVersion，出错时为 None
    errors: list[RowError] = field(default_factory=list)
    header_error: str | None = None


def _parse_date(token: str) -> date:
    token = token.strip()
    parts = token.split("-")
    if len(parts) != 3 or any(not p.isdigit() for p in parts):
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{token!r}")
    y, m, d = (int(p) for p in parts)
    try:
        return date(y, m, d)
    except ValueError:
        raise ValueError(f"日期不存在或越界：{token!r}") from None


def _parse_date_list(token: str, line_no: int, label: str) -> tuple[set[date], str | None]:
    values: set[date] = set()
    if not token.strip():
        return values, None
    for chunk in token.replace(",", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            day = _parse_date(chunk)
        except ValueError as exc:
            return set(), f"{label}中的{exc}"
        if day in values:
            return set(), f"{label}存在重复日期 {chunk}"
        values.add(day)
    return values, None


def _parse_weekdays(token: str) -> frozenset[int]:
    token = token.strip()
    if not token:
        raise ValueError("星期不能为空")
    days: set[int] = set()
    # 中文星期可连写，如"一三五"，也可用分隔符
    expanded = token
    for sep in (",", "，", ";", "；", "/", "、", " "):
        expanded = expanded.replace(sep, ",")
    for chunk in expanded.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.isdigit():
            value = int(chunk)
            if not 1 <= value <= 7:
                raise ValueError(f"星期取值应为 1-7：{chunk}")
            days.add(value)
            continue
        if "-" in chunk and all(part.isdigit() for part in chunk.split("-")):
            lo, hi = (int(part) for part in chunk.split("-"))
            if not (1 <= lo <= 7 and 1 <= hi <= 7 and lo <= hi):
                raise ValueError(f"星期区间应为 1-7 内的合法区间：{chunk}")
            days.update(range(lo, hi + 1))
            continue
        for char in chunk:
            if char not in WEEKDAY_CN:
                raise ValueError(f"无法识别的星期：{chunk}")
            days.add(WEEKDAY_CN[char])
    if not days:
        raise ValueError("星期不能为空")
    return frozenset(days)


def parse_version(label: str, raw_bytes: bytes) -> ParseResult:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParseResult(None, header_error="文件不是 UTF-8 编码，请另存为 UTF-8 后再上传")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return ParseResult(None, header_error="文件为空")

    columns: dict[str, int] = {}
    unknown: list[str] = []
    for index, name in enumerate(header):
        key = HEADER_ALIASES.get(name.strip().lower())
        if key is None:
            unknown.append(name.strip())
        else:
            columns[key] = index
    missing = [f for f in REQUIRED_FIELDS if f not in columns]
    if missing or unknown:
        problems = []
        if missing:
            problems.append("缺少必需列：" + "、".join(missing))
        if unknown:
            problems.append("无法识别的列：" + "、".join(unknown))
        return ParseResult(None, header_error="；".join(problems))

    # 延迟导入，避免模块加载期的循环依赖
    from .analyzer import TimetableVersion, Trip

    errors: list[RowError] = []
    trips: list[Trip] = []
    seen_ids: set[str] = set()

    for row_index, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue  # 空行跳过
        trip_id = row[columns["trip_id"]].strip() if len(row) > columns["trip_id"] else ""
        row_errors: list[str] = []

        def fail(message: str) -> None:
            row_errors.append(message)

        if len(row) < len(header):
            fail(f"列数不足：表头 {len(header)} 列，本行 {len(row)} 列")
        if not trip_id:
            fail("班次号为空")
        elif trip_id in seen_ids:
            fail(f"班次号 {trip_id} 在本文件中重复")
        else:
            seen_ids.add(trip_id)

        # 类型
        kind = None
        kind_raw = row[columns["kind"]].strip().lower() if len(row) > columns["kind"] else ""
        kind = KIND_ALIASES.get(kind_raw)
        if kind is None:
            fail(f"类型应为 到站/发车（或 arrival/departure），实际为 {row[columns['kind']].strip() if len(row) > columns['kind'] else ''!r}")

        # 分钟数：出错时用占位值，继续校验后续字段
        minute = 0
        minute_raw = row[columns["minute"]].strip() if len(row) > columns["minute"] else ""
        try:
            minute = int(minute_raw)
            if minute < 0:
                fail(f"分钟数不能为负：{minute}")
                minute = 0
            elif minute > MAX_MINUTE:
                fail(f"分钟数超出允许范围（0-{MAX_MINUTE}，即 7 天内）：{minute}")
        except ValueError:
            fail(f"分钟数应为非负整数，实际为 {minute_raw!r}")

        # 常规起止日
        start = end = None
        if len(row) > max(columns["start"], columns["end"]):
            try:
                start = _parse_date(row[columns["start"]])
            except ValueError as exc:
                fail(str(exc))
            try:
                end = _parse_date(row[columns["end"]])
            except ValueError as exc:
                fail(str(exc))
            if start is not None and end is not None and start > end:
                fail(f"常规开始日 {start} 晚于结束日 {end}")

        # 星期
        weekdays: frozenset[int] = frozenset()
        if len(row) > columns["weekdays"]:
            try:
                weekdays = _parse_weekdays(row[columns["weekdays"]])
            except ValueError as exc:
                fail(str(exc))

        # 临时加开 / 停运日期
        added: set[date] = set()
        suspended: set[date] = set()
        if "added" in columns and len(row) > columns["added"]:
            added, err = _parse_date_list(
                row[columns["added"]], row_index, "临时加开日期"
            )
            if err:
                fail(err)
                added = set()
        if "suspended" in columns and len(row) > columns["suspended"]:
            suspended, err = _parse_date_list(
                row[columns["suspended"]], row_index, "停运日期"
            )
            if err:
                fail(err)
                suspended = set()

        overlap = added & suspended
        if overlap:
            fail(
                "同一日期既加开又停运，无法确定是否开行："
                + "、".join(sorted(d.isoformat() for d in overlap))
            )

        if row_errors:
            for message in row_errors:
                errors.append(RowError(row_index, trip_id or "（空）", message))
            continue

        schedule = ServiceSchedule(
            start=start,
            end=end,
            weekdays=weekdays,
            added=frozenset(added),
            suspended=frozenset(suspended),
        )
        trips.append(Trip(trip_id=trip_id, kind=kind, minute=minute, schedule=schedule))

    if errors:
        return ParseResult(None, errors=errors)
    return ParseResult(TimetableVersion(label=label, trips=tuple(trips)))
