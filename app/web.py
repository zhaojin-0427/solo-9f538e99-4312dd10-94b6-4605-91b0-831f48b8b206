"""Flask 核查页：上传新旧两版 CSV，填写日期范围与最长等车分钟数。"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from flask import Flask, abort, render_template, request, send_from_directory

from .analyzer import analyze
from .parser import parse_version

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/samples/<path:filename>")
    def sample(filename: str):
        if "/" in filename or ".." in filename:
            abort(404)
        if not (SAMPLE_DIR / filename).is_file():
            abort(404)
        return send_from_directory(SAMPLE_DIR, filename, as_attachment=True)

    @app.post("/check")
    def check():
        form_errors: list[str] = []

        old_file = request.files.get("old_csv")
        new_file = request.files.get("new_csv")
        if not old_file or not old_file.filename:
            form_errors.append("请上传旧版 CSV")
        if not new_file or not new_file.filename:
            form_errors.append("请上传新版 CSV")

        range_start = range_end = None
        start_raw = request.form.get("range_start", "").strip()
        end_raw = request.form.get("range_end", "").strip()
        try:
            range_start = date.fromisoformat(start_raw) if start_raw else None
        except ValueError:
            form_errors.append(f"核查开始日期格式应为 YYYY-MM-DD：{start_raw!r}")
        try:
            range_end = date.fromisoformat(end_raw) if end_raw else None
        except ValueError:
            form_errors.append(f"核查结束日期格式应为 YYYY-MM-DD：{end_raw!r}")
        if range_start and range_end and range_start > range_end:
            form_errors.append("核查开始日期不能晚于结束日期")

        max_wait_raw = request.form.get("max_wait", "").strip()
        max_wait = None
        try:
            max_wait = int(max_wait_raw)
            if max_wait < 0:
                form_errors.append("最长等车分钟数不能为负")
                max_wait = None
        except ValueError:
            form_errors.append(f"最长等车分钟数应为非负整数：{max_wait_raw!r}")

        if form_errors:
            return render_template("index.html", form_errors=form_errors), 400

        old_bytes = old_file.read()
        new_bytes = new_file.read()
        old_result = parse_version("旧版", old_bytes)
        new_result = parse_version("新版", new_bytes)

        # 任何一版有解析问题：只标出问题行，不出结论
        if old_result.header_error or new_result.header_error or old_result.errors or new_result.errors:
            return render_template(
                "index.html",
                old_parse=old_result,
                new_parse=new_result,
                old_name=old_file.filename,
                new_name=new_file.filename,
            ), 422

        result = analyze(
            old_result.version,
            new_result.version,
            range_start,
            range_end,
            max_wait,
        )
        return render_template(
            "result.html",
            result=result,
            range_start=range_start,
            range_end=range_end,
            max_wait=max_wait,
            old_name=old_file.filename,
            new_name=new_file.filename,
        )

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
