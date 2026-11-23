"""Flask 应用：换版接驳核查 + 班表发布。"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .analyzer import analyze
from .parser import parse_version
from .publishing import (
    PublishError,
    PublishStore,
    RevisionConflict,
    SubmissionRejected,
)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "timetable_state.json"
)


def create_app(state_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["TIMETABLE_STATE_PATH"] = str(
        state_path
        or os.environ.get("TIMETABLE_STATE_PATH")
        or DEFAULT_STATE_PATH
    )
    app.extensions["publish_store"] = PublishStore(app.config["TIMETABLE_STATE_PATH"])

    def store() -> PublishStore:
        return current_app.extensions["publish_store"]

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
        if (
            old_result.header_error
            or new_result.header_error
            or old_result.csv_error
            or new_result.csv_error
            or old_result.errors
            or new_result.errors
        ):
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

    @app.get("/publish")
    def publish_form():
        return render_template(
            "publish.html",
            next_revision=store().revision + 1,
            current_revision=store().revision,
            current_record=store().revision_record(store().revision),
        )

    @app.post("/publish")
    def publish_submit():
        submit_store = store()
        filename = ""
        raw_bytes = b""
        form_errors: list[str] = []

        revision_raw = request.form.get("revision", "").strip()
        revision = None
        try:
            revision = int(revision_raw)
            if revision < 1:
                form_errors.append("修订号应为正整数")
                revision = None
        except ValueError:
            form_errors.append(f"修订号应为正整数：{revision_raw!r}")

        upload = request.files.get("timetable_csv")
        if not upload or not upload.filename:
            form_errors.append("请上传班表 CSV")
        else:
            filename = upload.filename
            raw_bytes = upload.read()

        if form_errors:
            return (
                render_template(
                    "publish.html",
                    form_errors=form_errors,
                    submitted_revision=revision_raw,
                    next_revision=submit_store.revision + 1,
                    current_revision=submit_store.revision,
                    current_record=submit_store.revision_record(submit_store.revision),
                ),
                400,
            )

        try:
            outcome = submit_store.publish(revision, filename, raw_bytes)
        except RevisionConflict as exc:
            return (
                render_template(
                    "publish.html",
                    form_errors=[exc.message],
                    submitted_revision=revision_raw,
                    next_revision=submit_store.revision + 1,
                    current_revision=submit_store.revision,
                    current_record=submit_store.revision_record(submit_store.revision),
                ),
                409,
            )
        except SubmissionRejected as exc:
            return (
                render_template(
                    "publish.html",
                    form_errors=[exc.message],
                    submitted_revision=revision_raw,
                    next_revision=submit_store.revision + 1,
                    current_revision=submit_store.revision,
                    current_record=submit_store.revision_record(submit_store.revision),
                ),
                422,
            )
        except PublishError as exc:
            # 保存失败：内存与磁盘都仍保留上次结果
            return (
                render_template(
                    "publish.html",
                    form_errors=[exc.message],
                    submitted_revision=revision_raw,
                    next_revision=submit_store.revision + 1,
                    current_revision=submit_store.revision,
                    current_record=submit_store.revision_record(submit_store.revision),
                ),
                500,
            )
        return redirect(url_for("publish_result", revision=outcome.revision))

    @app.get("/publish/<int:revision>")
    def publish_result(revision: int):
        record = store().revision_record(revision)
        if record is None:
            abort(404)
        return render_template("publish_result.html", record=record)

    @app.get("/publish/<int:revision>/issues")
    def publish_issues(revision: int):
        record = store().revision_record(revision)
        if record is None:
            abort(404)
        return redirect(url_for("publish_result", revision=revision, _anchor="issues"))

    @app.get("/timetable.csv")
    def timetable_csv():
        csv_text = store().current_csv()
        return Response(
            csv_text,
            content_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="timetable.csv"'
            },
        )

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
