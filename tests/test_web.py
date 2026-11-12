"""Web 层端到端验证。

环境有真实 Flask 时直接使用；无 Flask 时通过最小 stub 验证路由契约
（stub 只在本地无 pip 环境使用，交付容器装的是真实 Flask/gunicorn）。
"""
import io
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    import flask  # noqa: F401
except ImportError:
    sys.path.insert(0, "/tmp/flaskstub")

from app.main import create_app  # noqa: E402

ARCHIVE = (
    "机组,测量时刻,可用时刻,温度\n"
    "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,42.5\n"
    "WT-01,2026-11-01T10:05:00Z,2026-11-01T10:20:00Z,43.1\n"
).encode("utf-8")

CONFLICT = (
    "机组,测量时刻,可用时刻,温度\n"
    "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,99.9\n"
).encode("utf-8")

SAMPLES = (
    "样本号,机组,预测时刻,故障标签\n"
    "S001,WT-01,2026-11-01T10:30:00Z,0\n"
    "S002,WT-01,2026-11-01T10:12:00Z,1\n"
    "S003,WT-09,2026-11-01T10:30:00Z,0\n"
).encode("utf-8")


def main():
    tmp = tempfile.mkdtemp()
    os.environ["APP_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["APP_DB_PATH"] = os.path.join(tmp, "app.db")
    app = create_app()
    c = app.test_client()

    # 首页与健康检查
    r = c.get("/health")
    assert r.status_code == 200 and r.json["status"] == "ok"
    r = c.get("/")
    assert r.status_code == 200 and b"<!DOCTYPE html>" in r.body

    # 正常导入
    r = c.post("/api/imports", files={"file": _Up("ok.csv", ARCHIVE)})
    assert r.status_code == 200, r.body
    assert r.json["rows_inserted"] == 2

    # 同键异值整批拒绝
    r = c.post("/api/imports", files={"file": _Up("bad.csv", CONFLICT)})
    assert r.status_code == 422 and "同键异值" in r.json["error"]
    imports = c.get("/api/imports").json
    assert [i["status"] for i in imports] == ["rejected", "accepted"]

    # 样本校验失败（重复样本号）
    bad_samples = SAMPLES + b"S001,WT-01,2026-11-01T10:30:00Z,0\n"
    r = c.post("/api/jobs", files={"file": _Up("dup.csv", bad_samples)})
    assert r.status_code == 422 and "重复" in r.json["error"]

    # 受理任务
    r = c.post("/api/jobs", files={"file": _Up("s.csv", SAMPLES)})
    assert r.status_code == 202
    job_id = r.json["job_id"]

    # 后台线程处理（真实/stub 下都轮询）
    deadline = time.time() + 15
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json
        if job["status"] == "completed":
            break
        time.sleep(0.2)
    assert job["status"] == "completed", job
    assert job["processed_rows"] == 3 and job["downloadable"] is True

    # 下载
    r = c.get(f"/api/jobs/{job_id}/download")
    assert r.status_code == 200
    lines = r.body.decode("utf-8").strip().splitlines()
    assert len(lines) == 4
    assert lines[1].endswith("43.1")          # 测量最新 + 可用最新
    assert lines[2].endswith("42.5")          # 10:12 预测只能用 10:00 那条
    assert lines[3].endswith(",,,")           # 无匹配留空

    # 不存在的任务
    assert c.get("/api/jobs/deadbeef/download").status_code == 404

    # 任务列表
    jobs = c.get("/api/jobs").json
    assert len(jobs) == 1 and jobs[0]["id"] == job_id

    shutil.rmtree(tmp, ignore_errors=True)
    print("WEB E2E OK")
    print("\n输出 CSV:")
    print(r.body.decode("utf-8"))


class _Up:
    def __init__(self, name, data):
        self.filename = name
        self.stream = io.BytesIO(data)


if __name__ == "__main__":
    main()
