import io
import unittest
from datetime import date
from pathlib import Path

from app.parser import parse_version
from app.web import SAMPLE_DIR, create_app

OLD_CSV = (
    "班次号,类型,分钟数,常规开始日,常规结束日,星期,临时加开日期,停运日期\n"
    "A1,到站,1380,2026-11-01,2026-12-31,一二三四五,,\n"
    "D1,发车,1410,2026-11-01,2026-12-31,一二三四五六日,,\n"
)

NEW_CSV = (
    "班次号,类型,分钟数,常规开始日,常规结束日,星期,临时加开日期,停运日期\n"
    "A1,到站,1380,2026-11-01,2026-12-31,一二三四五,,\n"
    "D1,发车,1410,2026-11-01,2026-12-31,一二三四五六日,,2026-11-20\n"
)

BAD_CSV = (
    "班次号,类型,分钟数,常规开始日,常规结束日,星期,临时加开日期,停运日期\n"
    "A1,停靠,-5,2026-11-01,2026-12-31,一,,\n"
)


def post(client, old, new, start="2026-11-18", end="2026-11-25", wait="30"):
    return client.post(
        "/check",
        data={
            "old_csv": (io.BytesIO(old.encode("utf-8")), "old.csv"),
            "new_csv": (io.BytesIO(new.encode("utf-8")), "new.csv"),
            "range_start": start,
            "range_end": end,
            "max_wait": wait,
        },
        content_type="multipart/form-data",
    )


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_index_renders(self):
        resp = self.client.get("/")
        assert resp.status_code == 200
        assert "换版" in resp.get_data(as_text=True)

    def test_check_reports_lost_connection(self):
        resp = post(self.client, OLD_CSV, NEW_CSV)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "失去接驳" in html
        assert "A1" in html
        assert "2026-11-20" in html
        assert "D1" in html

    def test_bad_csv_returns_row_errors_without_conclusion(self):
        resp = post(self.client, BAD_CSV, NEW_CSV)
        assert resp.status_code == 422
        html = resp.get_data(as_text=True)
        assert "暂不出结论" in html
        assert "类型应为" in html
        # 出错时不得出现结果结论
        assert "失去接驳的到站班次" not in html

    def test_form_validation_error(self):
        resp = post(self.client, OLD_CSV, NEW_CSV, start="2026-11-30", end="2026-11-01")
        assert resp.status_code == 400
        assert "核查开始日期不能晚于结束日期" in resp.get_data(as_text=True)

    def test_sample_download(self):
        resp = self.client.get("/samples/old.csv")
        assert resp.status_code == 200
        try:
            parsed = parse_version("s", resp.data)
            assert parsed.errors == []
        finally:
            resp.close()

    def test_sample_pair_end_to_end(self):
        old_text = (Path(SAMPLE_DIR) / "old.csv").read_text(encoding="utf-8")
        new_text = (Path(SAMPLE_DIR) / "new.csv").read_text(encoding="utf-8")
        resp = post(
            self.client,
            old_text,
            new_text,
            start="2026-11-18",
            end="2026-11-30",
            wait="30",
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "2026-11-20" in html  # D1 停运日 -> A1 首个失去接驳日
