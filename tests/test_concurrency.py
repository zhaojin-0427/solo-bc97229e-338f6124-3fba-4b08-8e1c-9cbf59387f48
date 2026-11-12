"""验证：大任务执行期间归档补传可并发写入；模拟重启后任务续跑一致。"""
import io
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    import flask  # noqa: F401
except ImportError:
    sys.path.insert(0, "/tmp/flaskstub")

from app.db import connect, init_db  # noqa: E402
from app.importer import import_archive  # noqa: E402
from app.jobs import create_job, resume_pending, run_job  # noqa: E402

N = 3000


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "app.db")
    data = os.path.join(tmp, "data")
    init_db(db)

    # 基础归档
    base = "机组,测量时刻,可用时刻,温度\n" + "".join(
        f"WT-01,2026-11-01T09:{m:02d}:00Z,2026-11-01T09:{m:02d}:30Z,{30+m}\n"
        for m in range(60)
    )
    import_archive(io.BytesIO(base.encode()), "base.csv", db)

    # 大样本任务
    samples = "样本号,机组,预测时刻,故障标签\n" + "".join(
        f"S{i:05d},WT-01,2026-11-01T10:00:00Z,{i % 2}\n" for i in range(N)
    )
    job_id = create_job(io.BytesIO(samples.encode()), "big.csv", data, db)

    # 在任务跑的同时并发补传归档（验证不相互阻塞）
    errors = []

    def backfill():
        try:
            time.sleep(0.05)
            for k in range(3):
                chunk = "机组,测量时刻,可用时刻,温度\n" + (
                    f"WT-02,2026-11-01T08:0{k}:00Z,2026-11-01T08:0{k}:30Z,20.{k}\n"
                )
                import_archive(io.BytesIO(chunk.encode()),
                               f"back{k}.csv", db)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=backfill)
    t.start()
    run_job(job_id, db)
    t.join()
    assert not errors, errors

    conn = connect(db)
    n_wt02 = conn.execute(
        "SELECT COUNT(*) c FROM readings WHERE turbine='WT-02'"
    ).fetchone()["c"]
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    out_path = job["output_path"]
    conn.close()
    assert n_wt02 == 3, n_wt02
    assert job["status"] == "completed" and job["processed_rows"] == N
    with open(out_path, encoding="utf-8") as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == N + 1

    # 模拟重启：completed 不重复跑；再人为制造中断态验证 resume
    assert resume_pending(db) == []
    conn = connect(db)
    partial = out_path + ".partial"
    shutil.copy(out_path, partial)
    stop = 1234
    with open(partial, "r", encoding="utf-8", newline="") as fh:
        kept = fh.readlines()[: stop + 1]
    pos = sum(len(x.encode()) for x in kept)
    with open(partial, "wb") as fh:
        fh.write("".join(kept).encode())
    conn.execute(
        "UPDATE jobs SET status='running', processed_rows=?, output_offset=? "
        "WHERE id=?", (stop, pos, job_id)
    )
    conn.close()

    # “重启”：新进程视角调用 resume_pending 并续跑
    pending = resume_pending(db)
    assert pending == [job_id]
    run_job(job_id, db)

    with open(out_path, encoding="utf-8") as fh:
        lines2 = fh.read().strip().splitlines()
    assert lines2 == lines
    assert not os.path.exists(partial)
    conn = connect(db)
    final = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    assert final["status"] == "completed" and final["processed_rows"] == N

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"CONCURRENCY + RESTART OK (rows={N}, output lines={len(lines2)})")


if __name__ == "__main__":
    main()
