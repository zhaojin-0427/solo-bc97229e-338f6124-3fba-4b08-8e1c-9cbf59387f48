"""Flask 应用入口与路由。

页面：
  GET  /                 单页控制台（导入归档 / 提交样本 / 进度 / 下载）
接口：
  POST /api/imports      上传监测归档 CSV
  GET  /api/imports      导入历史（含拒绝原因）
  POST /api/jobs         上传运维样本 CSV，受理后台任务
  GET  /api/jobs         任务列表与进度
  GET  /api/jobs/<id>    单任务详情
  GET  /api/jobs/<id>/download   仅完成且完整的输出可下载
  GET  /health           容器健康检查
"""
from __future__ import annotations

import os

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

from .db import init_db
from .importer import ImportRejected, import_archive
from .jobs import (
    SampleValidationError,
    create_job,
    ensure_worker_started,
)
from .util import format_utc_second


def create_app() -> Flask:
    app = Flask(__name__)
    data_dir = os.environ.get("APP_DATA_DIR", "/data")
    db_path = os.environ.get("APP_DB_PATH", os.path.join(data_dir, "app.db"))
    os.makedirs(os.path.join(data_dir, "jobs"), exist_ok=True)
    app.config["DATA_DIR"] = data_dir
    app.config["DB_PATH"] = db_path
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 单次上传上限 512MB

    init_db(db_path)

    def _job_to_dict(row) -> dict:
        total = row["total_rows"] or 0
        done = row["processed_rows"] or 0
        return {
            "id": row["id"],
            "filename": row["filename"],
            "status": row["status"],
            "total_rows": total,
            "processed_rows": done,
            "progress": round(done / total, 4) if total else 0.0,
            "error": row["error"],
            "created_at": format_utc_second(row["created_at"]),
            "updated_at": format_utc_second(row["updated_at"]),
            "downloadable": row["status"] == "completed"
            and os.path.exists(row["output_path"])
            and not os.path.exists(row["output_path"] + ".partial"),
        }

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    # -------------------------------------------------------- 归档导入
    @app.post("/api/imports")
    def post_import():
        file = request.files.get("file")
        if file is None or file.filename == "":
            return jsonify(error="请选择归档 CSV 文件"), 400
        try:
            result = import_archive(file.stream, file.filename, db_path)
        except ImportRejected as exc:
            return jsonify(error=str(exc), status="rejected"), 422
        return jsonify(result), 200

    @app.get("/api/imports")
    def list_imports():
        from .db import connect

        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM imports ORDER BY id DESC LIMIT 100"
            ).fetchall()
        finally:
            conn.close()
        return jsonify(
            [
                {
                    "id": r["id"],
                    "filename": r["filename"],
                    "status": r["status"],
                    "rows_total": r["rows_total"],
                    "rows_inserted": r["rows_inserted"],
                    "reason": r["reason"],
                    "created_at": format_utc_second(r["created_at"]),
                }
                for r in rows
            ]
        )

    # -------------------------------------------------------- 样本任务
    @app.post("/api/jobs")
    def post_job():
        file = request.files.get("file")
        if file is None or file.filename == "":
            return jsonify(error="请选择运维样本 CSV 文件"), 400
        try:
            job_id = create_job(file.stream, file.filename, data_dir, db_path)
        except SampleValidationError as exc:
            return jsonify(error=str(exc)), 422
        ensure_worker_started(app)
        return jsonify(job_id=job_id, status="queued"), 202

    @app.get("/api/jobs")
    def list_jobs():
        from .db import connect

        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        finally:
            conn.close()
        return jsonify([_job_to_dict(r) for r in rows])

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        from .db import connect

        conn = connect(db_path)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        return jsonify(_job_to_dict(row))

    @app.get("/api/jobs/<job_id>/download")
    def download_job(job_id: str):
        from .db import connect

        conn = connect(db_path)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        partial = row["output_path"] + ".partial"
        # 残缺文件（未完成 / 只剩 partial / 状态不符）一律不可下载
        if row["status"] != "completed" or not os.path.exists(row["output_path"]) \
                or os.path.exists(partial):
            abort(409, description="输出文件尚未完成，残缺文件不可下载")
        return send_file(
            row["output_path"],
            as_attachment=True,
            download_name=f"training_{job_id}.csv",
            mimetype="text/csv",
        )

    # 进程启动即恢复重启前未完成的任务（gunicorn 单 worker 多线程）
    ensure_worker_started(app)
    return app


if __name__ == "__main__":
    # 本地直接运行（python -m app.main）；容器内由 gunicorn 加载 app.main:app
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
