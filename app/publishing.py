"""班表发布：修订号合并、问题行清单与原子持久化。

合并规则（以"发布前已生效版本"为基准）：
- 表头不合规、CSV 结构损坏（引号不闭合等）或非 UTF-8：整份来稿拒收；
- 班次号唯一且字段全部合格的行：新增或替换；
- 同号重复的行、字段有错的行：只记录问题行，不采用该行的新值；
  若该班次号在已生效版本中有记录，则原值沿用；
- 只要有一个非空数据行缺班次号，就不删除新表漏列的任何旧班次；
  否则，旧班次中未在来稿出现的班次被移除；
- 合并始终基于提交那一刻的最新已生效版本（"留用取发布前最新值"）；
- 修订号必须高于当前已生效修订号，否则拒收；
- 新班表与本次问题清单一同写入同一个状态文件，写入失败则完全保留上次结果。

解析/校验发生在提交锁之外，因此慢来稿不会阻塞其他来稿发布；只有
"读取最新状态 -> 合并 -> 原子写入"这段提交过程需要串行。
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .analyzer import Trip
from .calendar import ServiceSchedule
from .parser import ParseResult, parse_version

STATE_FORMAT = 1
CSV_HEADER = [
    "班次号",
    "类型",
    "分钟数",
    "常规开始日",
    "常规结束日",
    "星期",
    "临时加开日期",
    "停运日期",
]
KIND_TO_CSV = {"arrival": "到站", "departure": "发车"}
WEEKDAY_TO_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}


class PublishError(Exception):
    """发布失败；message 可直接展示给排班员。"""

    def __init__(self, message: str, *, current_revision: int | None = None):
        super().__init__(message)
        self.message = message
        self.current_revision = current_revision


class RevisionConflict(PublishError):
    """修订号不高于当前已生效版本。"""


class SubmissionRejected(PublishError):
    """表头/CSV 结构层面不合规，整份来稿拒收。"""


class PublishFailed(PublishError):
    """落盘失败，已保留上一次结果。"""


@dataclass(frozen=True)
class PublishOutcome:
    revision: int
    issues: tuple[dict, ...]
    applied: tuple[dict, ...]  # {"trip_id", "action": "added"|"replaced"}
    kept: tuple[str, ...]
    removed: tuple[str, ...]
    missing_id_rows: tuple[int, ...]
    submitted_rows: int
    filename: str


def _trip_to_dict(trip: Trip) -> dict:
    return {
        "trip_id": trip.trip_id,
        "kind": trip.kind,
        "minute": trip.minute,
        "start": trip.schedule.start.isoformat(),
        "end": trip.schedule.end.isoformat(),
        "weekdays": sorted(trip.schedule.weekdays),
        "added": sorted(d.isoformat() for d in trip.schedule.added),
        "suspended": sorted(d.isoformat() for d in trip.schedule.suspended),
    }


def _trip_from_dict(data: dict) -> Trip:
    return Trip(
        trip_id=data["trip_id"],
        kind=data["kind"],
        minute=int(data["minute"]),
        schedule=ServiceSchedule(
            start=date.fromisoformat(data["start"]),
            end=date.fromisoformat(data["end"]),
            weekdays=frozenset(int(w) for w in data["weekdays"]),
            added=frozenset(date.fromisoformat(d) for d in data.get("added", [])),
            suspended=frozenset(
                date.fromisoformat(d) for d in data.get("suspended", [])
            ),
        ),
    )


def render_timetable_csv(trips: Iterable[Trip]) -> str:
    """按现有对外格式输出当前班表。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for trip in sorted(trips, key=lambda t: (t.trip_id, t.kind, t.minute)):
        writer.writerow(
            [
                trip.trip_id,
                KIND_TO_CSV[trip.kind],
                trip.minute,
                trip.schedule.start.isoformat(),
                trip.schedule.end.isoformat(),
                "".join(WEEKDAY_TO_CN[w] for w in sorted(trip.schedule.weekdays)),
                ";".join(sorted(d.isoformat() for d in trip.schedule.added)),
                ";".join(sorted(d.isoformat() for d in trip.schedule.suspended)),
            ]
        )
    return buffer.getvalue()


def _empty_state() -> dict:
    return {"format": STATE_FORMAT, "revision": 0, "trips": [], "revisions": []}


def load_state(path: Path) -> dict:
    """读取状态文件；不存在时返回空状态（首次部署）。"""
    if not path.exists():
        return _empty_state()
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("format") != STATE_FORMAT:
        raise RuntimeError(f"无法识别的状态文件格式：{path}")
    state.setdefault("revisions", [])
    state.setdefault("trips", [])
    state.setdefault("revision", 0)
    return state


def write_state_atomic(path: Path, state: dict) -> None:
    """把完整状态写入临时文件并 fsync，再用 os.replace 原子换入。

    任一步失败都不会破坏旧文件；调用方只在成功后更新内存状态。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=".timetable-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        # 目录项 fsync 尽力而为：它发生在换入成功之后，失败也不能再报告
        # "保存失败"——否则磁盘已是新状态而内存还停在旧版本。
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class PublishStore:
    """线程安全的发布状态仓库（单进程内串行提交）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state = load_state(self.path)

    @property
    def revision(self) -> int:
        # self._state 只由本实例在提交后更新；若其他进程已发布，先对齐磁盘。
        with self._lock:
            self._refresh()
            return int(self._state["revision"])

    def current_trips(self) -> tuple[Trip, ...]:
        with self._lock:
            self._refresh()
            return tuple(_trip_from_dict(t) for t in self._state["trips"])

    def current_csv(self) -> str:
        return render_timetable_csv(self.current_trips())

    def revision_records(self) -> tuple[dict, ...]:
        with self._lock:
            self._refresh()
            return tuple(dict(record) for record in self._state["revisions"])

    def revision_record(self, revision: int) -> dict | None:
        with self._lock:
            self._refresh()
            for record in self._state["revisions"]:
                if record.get("revision") == revision:
                    return dict(record)
            return None

    def _refresh(self) -> None:
        try:
            self._state = load_state(self.path)
        except (OSError, ValueError, RuntimeError):
            pass  # 读取失败时退回内存中的上一次成功状态

    def publish(
        self, revision: int, filename: str, raw_bytes: bytes
    ) -> PublishOutcome:
        # 解析与逐行校验在锁外进行：慢来稿不会挡住其他来稿的提交。
        parsed = parse_version(
            f"修订 {revision}", raw_bytes, duplicate_policy="all"
        )
        return self.publish_parsed(revision, filename, parsed)

    def publish_parsed(
        self, revision: int, filename: str, parsed: ParseResult
    ) -> PublishOutcome:
        if parsed.header_error:
            raise SubmissionRejected(f"来稿文件不合规，整份拒收：{parsed.header_error}")
        if parsed.csv_error:
            raise SubmissionRejected(f"来稿文件不合规，整份拒收：{parsed.csv_error}")

        with self._lock:
            return self._commit_locked(revision, filename, parsed)

    def _commit_locked(
        self, revision: int, filename: str, parsed: ParseResult
    ) -> PublishOutcome:
        """在持有发布锁时，把已解析来稿合并进*当前最新*已生效状态并落盘。

        先重新读取状态文件：同一进程多线程、或容器外其他进程刚写入时，
        都能以磁盘上的最新已生效版本为基准，避免丢失更新。
        """
        current = load_state(self.path)
        current_revision = int(current["revision"])
        if revision <= current_revision:
            raise RevisionConflict(
                f"修订号 {revision} 不高于已生效版本 {current_revision}，拒收",
                current_revision=current_revision,
            )

        old_trips = {t["trip_id"]: _trip_from_dict(t) for t in current["trips"]}
        submitted_ids = set(parsed.ordered_ids)
        missing_id_rows = tuple(parsed.missing_id_rows)
        keep_omitted = bool(missing_id_rows)

        merged = dict(old_trips)
        removed: list[str] = []
        if not keep_omitted:
            for trip_id in old_trips:
                if trip_id not in submitted_ids:
                    removed.append(trip_id)
                    del merged[trip_id]

        applied: list[dict] = []
        applied_ids: set[str] = set()
        for trip in parsed.valid_trips:
            action = "replaced" if trip.trip_id in old_trips else "added"
            merged[trip.trip_id] = trip
            applied_ids.add(trip.trip_id)
            applied.append({"trip_id": trip.trip_id, "action": action})

        # 有旧记录但本次未采用新值（问题行/同号重复/因缺号而保留）的班次沿用旧值
        kept = sorted(
            trip_id
            for trip_id in old_trips
            if trip_id in merged and trip_id not in applied_ids
        )

        issues = tuple(
            {
                "line_number": err.line_number,
                "trip_id": err.trip_id,
                "message": err.message,
                # 该班次号是否已有旧记录并沿用了发布前最新值；没有旧记录时
                # 本次也不新增
                "retained": bool(
                    err.trip_id in old_trips and err.trip_id in kept
                ),
            }
            for err in parsed.errors
        )
        record = {
            "revision": revision,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "filename": filename,
            "submitted_rows": parsed.row_count,
            "issues": list(issues),
            "missing_id_rows": list(missing_id_rows),
            "applied": list(applied),
            "kept": kept,
            "removed": sorted(removed),
        }
        new_state = {
            "format": STATE_FORMAT,
            "revision": revision,
            "trips": [_trip_to_dict(merged[tid]) for tid in sorted(merged)],
            "revisions": list(current["revisions"]) + [record],
        }

        try:
            write_state_atomic(self.path, new_state)
        except Exception as exc:
            raise PublishFailed(
                f"保存失败，已保留上次结果（修订 {current_revision}）：{exc}",
                current_revision=current_revision,
            ) from exc

        # 只有落盘成功后才更新内存状态；重启后读取同一个状态文件。
        self._state = new_state

        return PublishOutcome(
            revision=revision,
            issues=issues,
            applied=tuple(applied),
            kept=tuple(kept),
            removed=tuple(sorted(removed)),
            missing_id_rows=missing_id_rows,
            submitted_rows=parsed.row_count,
            filename=filename,
        )
