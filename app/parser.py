"""CSV 时刻表解析与逐行校验。

列（表头支持中文，见 HEADER_ALIASES）：
  班次号、类型(到站/发车 或 arrival/departure)、分钟数(从运营日零点起，可跨午夜)、
  常规开始日、常规结束日、星期(如 1,2,3 或 一,三,五)、
  临时加开日期(分号分隔，可空)、停运日期(分号分隔，可空)
日期格式 YYYY-MM-DD。

文件名/表头层面的问题（编码、表头缺列/多列/重复列、CSV 引号不闭合等）
属于拒收级错误；具体数据行的字段问题属于问题行，调用方可继续合并其余合格行。
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
    version: object | None  # TimetableVersion；仅当完全没有问题行时生成
    errors: list[RowError] = field(default_factory=list)
    header_error: str | None = None
    csv_error: str | None = None
    # 发布合并用：逐行合格的班次（文件顺序），同号重复在 duplicate_policy="all"
    # 下已剔除
    valid_trips: tuple = ()
    # 非空数据行中出现过的班次号（按首次出现顺序，含问题行）
    ordered_ids: tuple[str, ...] = ()
    # 非空但班次号为空的行号；只要存在，发布时就必须保留新表漏列的旧班次
    missing_id_rows: tuple[int, ...] = ()
    row_count: int = 0  # 非空数据行数

    @property
    def rejected(self) -> bool:
        """文件级错误：应整份拒收，不能进入合并。"""
        return bool(self.header_error or self.csv_error)


@dataclass
class _ParsedRow:
    line_number: int
    trip_id: str
    trip: object | None
    messages: list[str]


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


def _parse_date_list(token: str, label: str) -> tuple[set[date], str | None]:
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


def _parse_row(row: list[str], header_len: int, columns: dict[str, int]) -> _ParsedRow:
    """解析一条非空数据行；收集该行全部字段问题后返回。"""
    trip_id = row[columns["trip_id"]].strip() if len(row) > columns["trip_id"] else ""
    messages: list[str] = []

    if len(row) < header_len:
        messages.append(f"列数不足：表头 {header_len} 列，本行 {len(row)} 列")
    elif len(row) > header_len:
        messages.append(f"列数过多：表头 {header_len} 列，本行 {len(row)} 列")

    if not trip_id:
        messages.append("班次号为空")

    # 类型
    kind = None
    kind_raw = row[columns["kind"]].strip().lower() if len(row) > columns["kind"] else ""
    kind = KIND_ALIASES.get(kind_raw)
    if kind is None:
        raw = row[columns["kind"]].strip() if len(row) > columns["kind"] else ""
        messages.append(f"类型应为 到站/发车（或 arrival/departure），实际为 {raw!r}")

    # 分钟数：出错时用占位值，继续校验后续字段
    minute = 0
    minute_raw = row[columns["minute"]].strip() if len(row) > columns["minute"] else ""
    try:
        minute = int(minute_raw)
        if minute < 0:
            messages.append(f"分钟数不能为负：{minute}")
            minute = 0
        elif minute > MAX_MINUTE:
            messages.append(
                f"分钟数超出允许范围（0-{MAX_MINUTE}，即 7 天内）：{minute}"
            )
    except ValueError:
        messages.append(f"分钟数应为非负整数，实际为 {minute_raw!r}")

    # 常规起止日
    start = end = None
    if len(row) > max(columns["start"], columns["end"]):
        try:
            start = _parse_date(row[columns["start"]])
        except ValueError as exc:
            messages.append(str(exc))
        try:
            end = _parse_date(row[columns["end"]])
        except ValueError as exc:
            messages.append(str(exc))
        if start is not None and end is not None and start > end:
            messages.append(f"常规开始日 {start} 晚于结束日 {end}")

    # 星期
    weekdays: frozenset[int] = frozenset()
    if len(row) > columns["weekdays"]:
        try:
            weekdays = _parse_weekdays(row[columns["weekdays"]])
        except ValueError as exc:
            messages.append(str(exc))

    # 临时加开 / 停运日期
    added: set[date] = set()
    suspended: set[date] = set()
    if "added" in columns and len(row) > columns["added"]:
        added, err = _parse_date_list(row[columns["added"]], "临时加开日期")
        if err:
            messages.append(err)
            added = set()
    if "suspended" in columns and len(row) > columns["suspended"]:
        suspended, err = _parse_date_list(row[columns["suspended"]], "停运日期")
        if err:
            messages.append(err)
            suspended = set()

    overlap = added & suspended
    if overlap:
        messages.append(
            "同一日期既加开又停运，无法确定是否开行："
            + "、".join(sorted(d.isoformat() for d in overlap))
        )

    trip = None
    if not messages:
        from .analyzer import Trip

        schedule = ServiceSchedule(
            start=start,
            end=end,
            weekdays=weekdays,
            added=frozenset(added),
            suspended=frozenset(suspended),
        )
        trip = Trip(trip_id=trip_id, kind=kind, minute=minute, schedule=schedule)
    return _ParsedRow(line_number=0, trip_id=trip_id, trip=trip, messages=messages)


def parse_version(
    label: str, raw_bytes: bytes, *, duplicate_policy: str = "first"
) -> ParseResult:
    """解析并校验一份 CSV。

    duplicate_policy:
      - "first"（默认，供接驳核查页使用）：同号重复只把后出现行标为问题，
        首行仍算合格；
      - "all"（发布用）：同一班次号出现多行时，所有相关行都记为问题行，
        该班次不采用本次来稿的值。
    """
    if duplicate_policy not in ("first", "all"):
        raise ValueError(f"未知的 duplicate_policy：{duplicate_policy!r}")

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParseResult(
            None, header_error="文件不是 UTF-8 编码，请另存为 UTF-8 后再上传"
        )

    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        return ParseResult(None, header_error="文件为空")
    except csv.Error as exc:
        return ParseResult(
            None,
            csv_error=f"CSV 无法完整解析（第 {reader.line_num} 行附近）：{exc}",
        )

    columns: dict[str, int] = {}
    unknown: list[str] = []
    duplicate_columns: list[str] = []
    for index, name in enumerate(header):
        key = HEADER_ALIASES.get(name.strip().lower())
        if key is None:
            unknown.append(name.strip())
        elif key in columns:
            duplicate_columns.append(name.strip())
        else:
            columns[key] = index
    missing = [f for f in REQUIRED_FIELDS if f not in columns]
    if missing or unknown or duplicate_columns:
        problems = []
        if missing:
            problems.append("缺少必需列：" + "、".join(missing))
        if unknown:
            problems.append("无法识别的列：" + "、".join(unknown))
        if duplicate_columns:
            problems.append("表头列重复：" + "、".join(duplicate_columns))
        return ParseResult(None, header_error="；".join(problems))

    parsed_rows: list[_ParsedRow] = []
    id_counts: dict[str, int] = {}
    ordered_ids: list[str] = []
    ordered_seen: set[str] = set()
    missing_id_rows: list[int] = []
    seen_ids: set[str] = set()

    previous_line = reader.line_num
    try:
        for row in reader:
            line_number = previous_line + 1
            previous_line = reader.line_num
            if not any(cell.strip() for cell in row):
                continue  # 空行跳过
            parsed = _parse_row(row, len(header), columns)
            parsed.line_number = line_number

            if parsed.trip_id:
                if parsed.trip_id not in ordered_seen:
                    ordered_seen.add(parsed.trip_id)
                    ordered_ids.append(parsed.trip_id)
                id_counts[parsed.trip_id] = id_counts.get(parsed.trip_id, 0) + 1
                if duplicate_policy == "first":
                    if parsed.trip_id in seen_ids:
                        parsed.messages.append(
                            f"班次号 {parsed.trip_id} 在本文件中重复"
                        )
                        parsed.trip = None
                    else:
                        seen_ids.add(parsed.trip_id)
            else:
                missing_id_rows.append(line_number)

            parsed_rows.append(parsed)
    except csv.Error as exc:
        return ParseResult(
            None,
            csv_error=f"CSV 无法完整解析（第 {reader.line_num} 行附近）：{exc}",
        )

    if duplicate_policy == "all":
        duplicated = {tid for tid, count in id_counts.items() if count > 1}
        if duplicated:
            line_map: dict[str, list[int]] = {tid: [] for tid in duplicated}
            for parsed in parsed_rows:
                if parsed.trip_id in duplicated:
                    line_map[parsed.trip_id].append(parsed.line_number)
            for parsed in parsed_rows:
                if parsed.trip_id in duplicated:
                    lines = "、".join(str(n) for n in line_map[parsed.trip_id])
                    parsed.messages.append(
                        f"班次号 {parsed.trip_id} 在本文件中重复"
                        f"（共 {id_counts[parsed.trip_id]} 行：第 {lines} 行）"
                    )
                    parsed.trip = None

    errors: list[RowError] = []
    valid = []
    for parsed in parsed_rows:
        if parsed.trip is not None:
            valid.append(parsed.trip)
        for message in parsed.messages:
            errors.append(RowError(parsed.line_number, parsed.trip_id or "（空）", message))

    valid_trips = tuple(valid)
    if errors:
        version = None
    else:
        from .analyzer import TimetableVersion

        version = TimetableVersion(label=label, trips=valid_trips)

    return ParseResult(
        version,
        errors=errors,
        valid_trips=valid_trips,
        ordered_ids=tuple(ordered_ids),
        missing_id_rows=tuple(missing_id_rows),
        row_count=len(parsed_rows),
    )
