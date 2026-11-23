import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from app.publishing import (
    PublishFailed,
    PublishStore,
    RevisionConflict,
    SubmissionRejected,
)
from app.parser import parse_version

HEADER = "班次号,类型,分钟数,常规开始日,常规结束日,星期,临时加开日期,停运日期\n"


def row(trip_id, kind="到站", minute=60, start="2026-11-01",
        end="2026-12-31", weekdays="一二三四五", added="", suspended=""):
    return f"{trip_id},{kind},{minute},{start},{end},{weekdays},{added},{suspended}\n"


def csv_bytes(*rows):
    return (HEADER + "".join(rows)).encode("utf-8")


class PublishStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.json"
        self.store = PublishStore(self.path)

    def test_first_publish_adds_trips(self):
        outcome = self.store.publish(1, "a.csv", csv_bytes(
            row("A1"), row("D1", "发车", 120),
        ))
        self.assertEqual(outcome.revision, 1)
        self.assertEqual([a["trip_id"] for a in outcome.applied], ["A1", "D1"])
        self.assertEqual(outcome.kept, ())
        self.assertEqual(outcome.removed, ())
        self.assertEqual({t.trip_id for t in self.store.current_trips()}, {"A1", "D1"})

    def test_valid_unique_rows_add_or_replace_and_omitted_removed(self):
        self.store.publish(1, "a.csv", csv_bytes(
            row("A1", minute=60), row("A2", minute=70), row("D1", "发车", 120),
        ))
        outcome = self.store.publish(2, "b.csv", csv_bytes(
            row("A1", minute=90), row("A3", minute=80),
        ))
        self.assertEqual(outcome.applied, (
            {"trip_id": "A1", "action": "replaced"},
            {"trip_id": "A3", "action": "added"},
        ))
        self.assertEqual(outcome.removed, ("A2", "D1"))
        trips = {t.trip_id: t for t in self.store.current_trips()}
        self.assertEqual(set(trips), {"A1", "A3"})
        self.assertEqual(trips["A1"].minute, 90)
        self.assertEqual(trips["A3"].minute, 80)

    def test_duplicate_trip_id_rows_all_reported_and_old_value_kept(self):
        self.store.publish(1, "a.csv", csv_bytes(row("A1", minute=60)))
        outcome = self.store.publish(2, "b.csv", csv_bytes(
            row("A1", minute=90), row("A1", minute=100),
        ))
        self.assertEqual(len(outcome.issues), 2)
        self.assertEqual([i["line_number"] for i in outcome.issues], [2, 3])
        self.assertEqual(outcome.kept, ("A1",))
        self.assertEqual(outcome.applied, ())
        trips = {t.trip_id: t for t in self.store.current_trips()}
        self.assertEqual(trips["A1"].minute, 60)

    def test_duplicate_new_trip_id_is_not_added(self):
        outcome = self.store.publish(1, "a.csv", csv_bytes(
            row("N1", minute=10), row("N1", minute=20),
        ))
        self.assertEqual(outcome.applied, ())
        self.assertEqual(outcome.kept, ())
        self.assertEqual(self.store.current_trips(), ())
        self.assertTrue(all(issue["trip_id"] == "N1" for issue in outcome.issues))

    def test_field_error_row_keeps_old_value_and_lists_reason(self):
        self.store.publish(1, "a.csv", csv_bytes(row("A1", minute=60)))
        outcome = self.store.publish(2, "b.csv", csv_bytes(
            row("A1", minute=-5), row("A2", minute=80),
        ))
        self.assertEqual(len(outcome.issues), 1)
        self.assertEqual(outcome.issues[0]["trip_id"], "A1")
        self.assertEqual(outcome.issues[0]["line_number"], 2)
        self.assertIn("不能为负", outcome.issues[0]["message"])
        self.assertTrue(outcome.issues[0]["retained"])
        self.assertEqual(outcome.kept, ("A1",))
        self.assertEqual(outcome.applied, ({"trip_id": "A2", "action": "added"},))
        trips = {t.trip_id: t for t in self.store.current_trips()}
        self.assertEqual(trips["A1"].minute, 60)
        self.assertEqual(trips["A2"].minute, 80)

    def test_missing_trip_id_row_preserves_all_omitted_old_trips(self):
        self.store.publish(1, "a.csv", csv_bytes(
            row("A1", minute=60), row("A2", minute=70), row("D1", "发车", 120),
        ))
        text = HEADER + row("A1", minute=90) + ",到站,80,2026-11-01,2026-12-31,一,,\n"
        outcome = self.store.publish(2, "b.csv", text.encode("utf-8"))
        self.assertEqual(outcome.missing_id_rows, (3,))
        self.assertEqual(outcome.removed, ())
        self.assertEqual(outcome.kept, ("A2", "D1"))
        trips = {t.trip_id: t for t in self.store.current_trips()}
        self.assertEqual(set(trips), {"A1", "A2", "D1"})
        self.assertEqual(trips["A1"].minute, 90)

    def test_missing_trip_id_row_with_no_omissions_still_commits(self):
        self.store.publish(1, "a.csv", csv_bytes(row("A1", minute=60)))
        text = HEADER + row("A1", minute=90) + ",到站,80,2026-11-01,2026-12-31,一,,\n"
        outcome = self.store.publish(2, "b.csv", text.encode("utf-8"))
        self.assertEqual(outcome.missing_id_rows, (3,))
        self.assertEqual(outcome.removed, ())
        self.assertEqual(self.store.revision, 2)

    def test_blank_physical_rows_do_not_count_as_missing_id(self):
        self.store.publish(1, "a.csv", csv_bytes(row("A1"), row("A2")))
        text = HEADER + row("A1") + "\n   \n,,,\n"
        outcome = self.store.publish(2, "b.csv", text.encode("utf-8"))
        self.assertEqual(outcome.missing_id_rows, ())
        self.assertEqual(outcome.removed, ("A2",))

    def test_lower_or_equal_revision_rejected(self):
        self.store.publish(2, "a.csv", csv_bytes(row("A1")))
        with self.assertRaises(RevisionConflict):
            self.store.publish(2, "b.csv", csv_bytes(row("A2")))
        with self.assertRaises(RevisionConflict):
            self.store.publish(1, "b.csv", csv_bytes(row("A2")))
        self.assertEqual(self.store.revision, 2)
        self.assertEqual({t.trip_id for t in self.store.current_trips()}, {"A1"})

    def test_header_error_rejects_whole_submission(self):
        bad = "班次号,类型,分钟数\nA1,到站,60\n"
        with self.assertRaises(SubmissionRejected):
            self.store.publish(2, "a.csv", bad.encode("utf-8"))
        self.assertEqual(self.store.revision, 0)

    def test_unparseable_csv_rejects_whole_submission(self):
        text = HEADER + 'A1,到站,60,2026-11-01,2026-12-31,一,,,\n"unterminated\n'
        with self.assertRaises(SubmissionRejected) as ctx:
            self.store.publish(1, "a.csv", text.encode("utf-8"))
        self.assertIn("无法完整解析", ctx.exception.message)

    def test_save_failure_keeps_previous_state_on_disk_and_memory(self):
        self.store.publish(1, "a.csv", csv_bytes(row("A1", minute=60)))
        before = self.path.read_text(encoding="utf-8")

        import app.publishing as publishing

        def boom(path, state):
            raise OSError("disk full")

        original_write = publishing.write_state_atomic
        publishing.write_state_atomic = boom
        try:
            with self.assertRaises(PublishFailed) as ctx:
                self.store.publish(2, "b.csv", csv_bytes(row("A1", minute=90)))
        finally:
            publishing.write_state_atomic = original_write
        self.assertEqual(ctx.exception.current_revision, 1)
        self.assertEqual(self.store.revision, 1)
        self.assertEqual(self.store.current_trips()[0].minute, 60)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        with self.assertRaises(RevisionConflict):
            self.store.publish(1, "c.csv", csv_bytes(row("A2")))

    def test_state_survives_reload(self):
        self.store.publish(1, "a.csv", csv_bytes(row("A1", minute=60)))
        self.store.publish(2, "b.csv", csv_bytes(row("A1", minute=-1)))
        reloaded = PublishStore(self.path)
        self.assertEqual(reloaded.revision, 2)
        self.assertEqual({t.trip_id: t.minute for t in reloaded.current_trips()}, {"A1": 60})
        record = reloaded.revision_record(2)
        self.assertIsNotNone(record)
        self.assertEqual(record["issues"][0]["trip_id"], "A1")
        self.assertEqual(record["filename"], "b.csv")

    def test_export_round_trips_through_parser(self):
        self.store.publish(1, "a.csv", csv_bytes(
            row("A1", minute=1380),
            row("D1", "发车", 1470, weekdays="一三五",
                added="2026-11-21;2026-12-24", suspended="2026-11-20"),
        ))
        exported = self.store.current_csv()
        parsed = parse_version("导出", exported.encode("utf-8"), duplicate_policy="all")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.header_error, None)
        self.assertEqual(len(parsed.valid_trips), 2)
        trips = {t.trip_id: t for t in parsed.valid_trips}
        self.assertEqual(trips["D1"].minute, 1470)
        self.assertEqual(trips["D1"].schedule.weekdays, frozenset({1, 3, 5}))
        self.assertEqual(sorted(trips["D1"].schedule.added),
                         [date(2026, 11, 21), date(2026, 12, 24)])
        # 重新发布导出的班表不应丢失任何班次
        outcome = self.store.publish(2, "roundtrip.csv", exported.encode("utf-8"))
        self.assertEqual(outcome.removed, ())
        self.assertEqual({t.trip_id for t in self.store.current_trips()}, {"A1", "D1"})

