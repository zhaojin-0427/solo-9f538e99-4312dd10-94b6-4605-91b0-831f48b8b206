import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.parser import parse_version
from app.publishing import PublishStore
from app.web import create_app

HEADER = "班次号,类型,分钟数,常规开始日,常规结束日,星期,临时加开日期,停运日期\n"


def row(trip_id, kind="到站", minute=60, weekdays="一二三四五"):
    return f"{trip_id},{kind},{minute},2026-11-01,2026-12-31,{weekdays},,\n"


def upload(revision, content, filename="timetable.csv"):
    return {
        "revision": str(revision),
        "timetable_csv": (io.BytesIO(content.encode("utf-8")), filename),
    }


class PublishWebTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "state.json"
        self.app = create_app(self.state_path)
        self.client = self.app.test_client()

    def publish(self, revision, content, filename="timetable.csv"):
        return self.client.post(
            "/publish",
            data=upload(revision, content, filename),
            content_type="multipart/form-data",
        )

    def test_publish_page_shows_current_revision_and_rule_summary(self):
        resp = self.client.get("/publish")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("修订 0", html)
        self.assertIn("沿用", html)

    def test_publish_valid_submission_redirects_to_revision_record(self):
        resp = self.publish(3, HEADER + row("A1"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/publish/3", resp.headers["Location"])
        record = self.client.get("/publish/3")
        self.assertEqual(record.status_code, 200)
        html = record.get_data(as_text=True)
        self.assertIn("修订 3 已生效", html)
        self.assertIn("新增 1 个班次", html)

    def test_revision_must_be_higher_than_current(self):
        self.publish(2, HEADER + row("A1"))
        resp = self.publish(2, HEADER + row("A2"))
        self.assertEqual(resp.status_code, 409)
        html = resp.get_data(as_text=True)
        self.assertIn("不高于已生效版本", html)
        # 未生效：当前班表仍是修订 2 的内容
        export = self.client.get("/timetable.csv").get_data(as_text=True)
        self.assertIn("A1", export)
        self.assertNotIn("A2", export)

    def test_header_error_and_unparseable_csv_are_rejected(self):
        bad_header = self.publish(1, "班次号,类型,分钟数\nA1,到站,60\n")
        self.assertEqual(bad_header.status_code, 422)
        self.assertIn("整份拒收", bad_header.get_data(as_text=True))

        broken = self.publish(
            2, HEADER + row("A1") + '"unterminated,60,2026-11-01\n'
        )
        self.assertEqual(broken.status_code, 422)
        self.assertIn("无法完整解析", broken.get_data(as_text=True))

        # 两次拒收都不生效
        self.assertEqual(self.client.get("/publish").status_code, 200)
        record_2 = self.client.get("/publish/2")
        self.assertEqual(record_2.status_code, 404)

    def test_issue_rows_are_listed_in_revision_record(self):
        self.publish(1, HEADER + row("A1", minute=60))
        resp = self.publish(2, HEADER + row("A1", minute=-5) + row("A2"))
        self.assertEqual(resp.status_code, 302)
        html = self.client.get("/publish/2").get_data(as_text=True)
        self.assertIn("问题行（1 条）", html)
        self.assertIn("不能为负", html)
        self.assertIn("沿用发布前最新值", html)
        self.assertIn("新增 1 个班次", html)

    def test_download_current_timetable_csv(self):
        self.publish(1, HEADER + row("A1", minute=1380))
        self.publish(2, HEADER + row("A1", minute=-1) + row("D1", "发车", 1410))
        resp = self.client.get("/timetable.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        text = resp.get_data(as_text=True)
        self.assertIn("A1", text)
        self.assertIn("1380", text)  # 问题行未采用，沿用旧值
        self.assertIn("D1", text)
        parsed = parse_version("导出", resp.data)
        self.assertEqual(parsed.errors, [])

    def test_issue_page_route(self):
        self.publish(1, HEADER + row("A1", minute=-1))
        resp = self.client.get("/publish/1/issues")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("#issues", resp.headers["Location"])

    def test_state_survives_new_app_instance(self):
        self.publish(1, HEADER + row("A1", minute=60))
        self.publish(2, HEADER + row("A1", minute=90) + row("A2"))
        restarted = create_app(self.state_path).test_client()
        html = restarted.get("/publish").get_data(as_text=True)
        self.assertIn("修订 2", html)
        html2 = restarted.get("/publish/2").get_data(as_text=True)
        self.assertIn("替换 1 个班次", html2)
        self.assertIn("新增 1 个班次", html2)

    def test_form_validation(self):
        resp = self.client.post(
            "/publish",
            data={"revision": "0", "timetable_csv": (io.BytesIO(b""), "")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("修订号应为正整数", html)
        self.assertIn("请上传班表 CSV", html)

    def test_unknown_revision_record_404(self):
        resp = self.client.get("/publish/99")
        self.assertEqual(resp.status_code, 404)


class InterleavedPublishTest(unittest.TestCase):
    """两稿交错：先解析的来稿在提交时必须以最新已生效版本为合并基准。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.json"
        self.store = PublishStore(self.path)

    def test_earlier_parsed_submission_keeps_latest_values(self):
        self.store.publish(1, "rev1.csv", (HEADER + row("A1", minute=60)
                                           + row("A2", minute=70)).encode())

        # 修订 3 的来稿先完成解析：A1 有问题行（应沿用），A2 合格
        rev3_parsed = parse_version("修订 3",
                                    (HEADER + row("A1", minute=-5)
                                     + row("A2", minute=200)).encode(),
                                    duplicate_policy="all")
        # 解析后、提交前，修订 2 先发布并改了 A1
        self.store.publish(2, "rev2.csv",
                           (HEADER + row("A1", minute=90)).encode())

        # 修订 3 提交：漏列旧班次 -> A1 沿用修订 2 的最新值 90
        outcome = self.store.publish_parsed(3, "rev3.csv", rev3_parsed)
        self.assertEqual(outcome.kept, ("A1",))
        self.assertEqual(outcome.removed, ())
        trips = {t.trip_id: t for t in self.store.current_trips()}
        self.assertEqual(trips["A1"].minute, 90)
        self.assertEqual(trips["A2"].minute, 200)

    def test_two_valid_revisions_committed_in_reverse_order(self):
        self.store.publish(1, "rev1.csv", (HEADER + row("A1", minute=60)).encode())
        low = parse_version("修订 2",
                            (HEADER + row("A2", minute=70)).encode(),
                            duplicate_policy="all")
        self.store.publish(3, "rev3.csv", (HEADER + row("A3", minute=80)).encode())
        # 修订 3 生效后，修订 2 来稿按规则拒收，不覆盖任何已生效内容
        from app.publishing import RevisionConflict

        with self.assertRaises(RevisionConflict):
            self.store.publish_parsed(2, "rev2.csv", low)
        trips = {t.trip_id for t in self.store.current_trips()}
        self.assertEqual(trips, {"A3"})
        self.assertEqual(self.store.revision, 3)

    def test_slow_validation_does_not_block_other_publish(self):
        import app.publishing as publishing

        original_parse = publishing.parse_version
        slow_started = threading.Event()
        release_slow = threading.Event()

        def slow_parse(label, raw_bytes, *, duplicate_policy="first"):
            if b"S1" in raw_bytes:
                slow_started.set()
                if not release_slow.wait(timeout=5):
                    raise AssertionError("测试未能及时放行慢来稿")
            return original_parse(label, raw_bytes, duplicate_policy=duplicate_policy)

        publishing.parse_version = slow_parse
        self.addCleanup(setattr, publishing, "parse_version", original_parse)

        slow_result = {}

        def slow_submission():
            try:
                slow_result["outcome"] = self.store.publish(
                    9, "slow.csv", (HEADER + row("S1")).encode()
                )
            except Exception as exc:  # pragma: no cover - 测试失败时输出
                slow_result["error"] = exc

        thread = threading.Thread(target=slow_submission)
        thread.start()
        try:
            self.assertTrue(slow_started.wait(timeout=5))
            # 慢来稿仍在解析，另一次发布应能立即完成
            quick = self.store.publish(
                2, "quick.csv", (HEADER + row("Q1")).encode()
            )
            self.assertEqual(quick.revision, 2)
            self.assertEqual(self.store.revision, 2)
        finally:
            release_slow.set()
            thread.join(timeout=10)
        self.assertTrue(thread.is_alive() is False)
        self.assertIsNone(slow_result.get("error"))
        self.assertEqual(slow_result["outcome"].revision, 9)
        # 慢稿在修订 2 生效后才提交：以最新状态为基准合并，新表未列 Q1 故移除
        self.assertEqual(slow_result["outcome"].removed, ("Q1",))
        self.assertEqual({t.trip_id for t in self.store.current_trips()}, {"S1"})


if __name__ == "__main__":
    unittest.main()
