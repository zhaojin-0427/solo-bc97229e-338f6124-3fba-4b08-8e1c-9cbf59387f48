"""运维样本后台任务：按预测时刻可获得的温度生成训练表。

每个样本仅使用同机组、测量时刻与可用时刻均不晚于预测时刻的读数；
先取测量时刻最新者，再以可用时刻最新者破并列；无匹配则温度留空。

设计要点：
  * 受理时样本 CSV 原样落盘，任务输入从此固定，之后的归档补传不改变输入；
  * 逐行流式处理，内存占用不随样本/归档总行数增长；
  * 输出增量写入 *.partial，每批在同一事务里落库进度（已处理行数、
    输出字节偏移）；进程重启后按偏移截断续跑，同一任务只产出一份完整文件；
  * 全部行完成后原子 rename 为正式输出，未完成文件永不提供下载。
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid

from .db import connect, transaction
from .util import (
    format_utc_second,
    normalize_headers,
    parse_utc_second,
    require_columns,
)

SAMPLE_REQUIRED = ["sample_id", "turbine", "predicted_at", "fault_label"]
APPENDED_COLUMNS = ["测量时刻", "可用时刻", "温度"]
CHECKPOINT_BATCH = 200

_worker_lock = threading.Lock()


class SampleValidationError(ValueError):
    pass


# ---------------------------------------------------------------- 受理校验
def validate_and_save_samples(source, job_dir: str) -> dict:
    """流式校验样本 CSV 并原样保存，返回元信息。内存恒定。"""
    os.makedirs(job_dir, exist_ok=True)
    input_path = os.path.join(job_dir, "input.csv")
    header_path = os.path.join(job_dir, "header.json")

    stream = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")
    reader = csv.reader(stream)
    try:
        raw_header = next(reader)
    except StopIteration:
        raise SampleValidationError("样本文件为空，缺少表头")
    try:
        columns = normalize_headers(raw_header)
        require_columns(columns, SAMPLE_REQUIRED, "样本 CSV ")
    except csv.Error as exc:
        raise SampleValidationError(str(exc)) from exc
    idx = {name: i for i, name in enumerate(columns)}

    total_rows = 0
    seen_ids: set[str] = set()
    with open(input_path, "w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(raw_header)
        for line_no, row in enumerate(reader, start=2):
            if not row or all((c is None or c.strip() == "") for c in row):
                continue
            if len(row) != len(columns):
                raise SampleValidationError(
                    f"第 {line_no} 行字段数为 {len(row)}，期望 {len(columns)}"
                )
            sample_id = row[idx["sample_id"]].strip()
            turbine = row[idx["turbine"]].strip()
            if not sample_id:
                raise SampleValidationError(f"第 {line_no} 行样本号为空")
            if not turbine:
                raise SampleValidationError(f"第 {line_no} 行机组为空")
            if sample_id in seen_ids:
                raise SampleValidationError(f"样本号重复: {sample_id}")
            seen_ids.add(sample_id)
            try:
                parse_utc_second(
                    row[idx["predicted_at"]], f"第 {line_no} 行预测时刻"
                )
            except ValueError as exc:
                raise SampleValidationError(str(exc)) from exc
            writer.writerow(row)
            total_rows += 1

    if total_rows == 0:
        raise SampleValidationError("样本文件没有任何有效数据行")

    with open(header_path, "w", encoding="utf-8") as fh:
        json.dump(raw_header, fh, ensure_ascii=False)

    return {
        "input_path": input_path,
        "header_path": header_path,
        "raw_header": raw_header,
        "total_rows": total_rows,
    }


def create_job(source, filename: str, data_dir: str, db_path: str | None = None) -> str:
    """受理一个任务：固定输入、写入 jobs 行并唤醒工作线程。"""
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(data_dir, "jobs", job_id)
    try:
        meta = validate_and_save_samples(source, job_dir)
    except Exception:
        # 校验失败不允许残留半成品任务目录
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    output_path = os.path.join(job_dir, "output.csv")
    now = int(time.time())
    conn = connect(db_path)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO jobs (id, filename, status, total_rows, processed_rows,"
                " output_offset, error, input_path, output_path, header_path,"
                " created_at, updated_at) VALUES (?, ?, 'queued', ?, 0, 0, NULL,"
                " ?, ?, ?, ?, ?)",
                (
                    job_id,
                    filename,
                    meta["total_rows"],
                    meta["input_path"],
                    output_path,
                    meta["header_path"],
                    now,
                    now,
                ),
            )
    finally:
        conn.close()
    return job_id


# ---------------------------------------------------------------- 取数逻辑
def pick_reading(conn: sqlite3.Connection, turbine: str, predicted_at: int):
    """同机组且两个时刻均不晚于预测时刻；测量最新优先，可用最新破并列。"""
    return conn.execute(
        "SELECT measured_at, available_at, temperature FROM readings "
        "WHERE turbine = ? AND measured_at <= ? AND available_at <= ? "
        "ORDER BY measured_at DESC, available_at DESC LIMIT 1",
        (turbine, predicted_at, predicted_at),
    ).fetchone()


# ---------------------------------------------------------------- 任务执行
def run_job(job_id: str, db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        if job["status"] == "completed":
            return
        # 崩溃恢复的罕见窗口：文件已原子换名但 completed 状态未及提交。
        # 正式输出已存在且无 partial，按文件实际行数核对后补登完成状态。
        if os.path.exists(job["output_path"]) and not os.path.exists(
            job["output_path"] + ".partial"
        ):
            with open(job["output_path"], "r", encoding="utf-8", newline="") as fh:
                line_count = sum(1 for _ in fh) - 1  # 去掉表头
            if line_count == job["total_rows"]:
                with transaction(conn):
                    conn.execute(
                        "UPDATE jobs SET status='completed', processed_rows=?,"
                        " error=NULL, updated_at=? WHERE id=?",
                        (line_count, int(time.time()), job_id),
                    )
                return
        conn.execute(
            "UPDATE jobs SET status='running', updated_at=?, error=NULL WHERE id=?",
            (int(time.time()), job_id),
        )

        partial_path = job["output_path"] + ".partial"
        processed = job["processed_rows"]
        offset = job["output_offset"]

        # 重启续跑：按已提交的字节偏移截断，防止半行污染。
        # 已提交检查点意味着 processed>0；若 processed==0 则必为全新任务。
        partial_exists = os.path.exists(partial_path)
        if partial_exists:
            with open(partial_path, "r+b") as check:
                size = check.seek(0, os.SEEK_END)
                if size > offset:
                    check.truncate(offset)
        mode = "a" if partial_exists else "w"

        with open(job["header_path"], "r", encoding="utf-8") as fh:
            raw_header = json.load(fh)
        columns = normalize_headers(raw_header)
        idx = {name: i for i, name in enumerate(columns)}

        out = open(partial_path, mode, encoding="utf-8", newline="")
        writer = csv.writer(out)
        if not partial_exists:
            writer.writerow(raw_header + APPENDED_COLUMNS)

        batch_processed = 0
        try:
            with open(job["input_path"], "r", encoding="utf-8", newline="") as inp:
                reader = csv.reader(inp)
                next(reader)  # 跳过表头
                for _ in range(processed):
                    next(reader)  # 跳过重启前已完成的行
                for row in reader:
                    if not row or all(
                        (c is None or c.strip() == "") for c in row
                    ):
                        continue
                    turbine = row[idx["turbine"]].strip()
                    predicted_at = parse_utc_second(row[idx["predicted_at"]])
                    reading = pick_reading(conn, turbine, predicted_at)
                    if reading is None:
                        extra = ["", "", ""]
                    else:
                        extra = [
                            format_utc_second(reading["measured_at"]),
                            format_utc_second(reading["available_at"]),
                            repr(reading["temperature"])
                            if isinstance(reading["temperature"], float)
                            else str(reading["temperature"]),
                        ]
                    writer.writerow(row + extra)
                    processed += 1
                    batch_processed += 1

                    if batch_processed >= CHECKPOINT_BATCH:
                        out.flush()
                        os.fsync(out.fileno())
                        new_offset = out.tell()
                        with transaction(conn):
                            conn.execute(
                                "UPDATE jobs SET processed_rows=?, output_offset=?,"
                                " updated_at=? WHERE id=?",
                                (processed, new_offset, int(time.time()), job_id),
                            )
                        batch_processed = 0

            out.flush()
            os.fsync(out.fileno())
            final_offset = out.tell()
        finally:
            out.close()

        # 全部行完成：先原子换名，再标记完成。
        # 若换名后、标记前进程崩溃，重启时 output.csv 已存在而 partial 不存在，
        # 下方续跑逻辑据此直接补登 completed，不会覆盖已完成文件。
        os.replace(partial_path, job["output_path"])
        with transaction(conn):
            conn.execute(
                "UPDATE jobs SET status='completed', processed_rows=?,"
                " output_offset=?, error=NULL, updated_at=? WHERE id=?",
                (processed, final_offset, int(time.time()), job_id),
            )
    except Exception as exc:  # 任务级失败：记录错误，保留部分文件供排查
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                    (f"{type(exc).__name__}: {exc}", int(time.time()), job_id),
                )
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def resume_pending(db_path: str | None = None) -> list[str]:
    """进程启动时恢复所有未完成任务（queued/running），顺序续跑。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','running') "
            "ORDER BY created_at"
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def ensure_worker_started(app) -> None:
    """在应用进程内启动唯一后台调度线程（单进程多线程部署）。"""
    with _worker_lock:
        if getattr(app, "_worker_started", False):
            return
        app._worker_started = True  # type: ignore[attr-defined]

    db_path = app.config["DB_PATH"]
    data_dir = app.config["DATA_DIR"]

    def _loop() -> None:
        # 启动时先继续重启前未完成的任务
        for job_id in resume_pending(db_path):
            _run_guarded(job_id, db_path)
        while True:
            job_id = _claim_next_job(db_path)
            if job_id is None:
                time.sleep(0.5)
                continue
            _run_guarded(job_id, db_path)

    thread = threading.Thread(target=_loop, name="job-worker", daemon=True)
    thread.start()


def _claim_next_job(db_path: str) -> str | None:
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
            return row["id"]
    finally:
        conn.close()


def _run_guarded(job_id: str, db_path: str) -> None:
    try:
        run_job(job_id, db_path)
    except Exception:
        # 错误已落库；调度线程不能因单个任务退出
        pass
