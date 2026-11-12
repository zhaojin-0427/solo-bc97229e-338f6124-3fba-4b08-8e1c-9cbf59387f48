"""监测归档 CSV 导入。

规则：
  * 前三列（机组、测量时刻、可用时刻）为业务键；
  * 同键同值 -> 已存在，复用，不计为新行；
  * 同键异值 -> 整批拒绝（本文件一行都不入库）；
  * 任意行残缺/无法解析 -> 整批拒绝。

内存不随总行数增长：用磁盘临时表暂存，流式逐行写入，
全程只保留单行缓冲。
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import time

from .db import connect, transaction
from .util import normalize_headers, parse_utc_second, require_columns

REQUIRED = ["turbine", "measured_at", "available_at", "temperature"]


class ImportRejected(Exception):
    """整批拒绝，message 为可展示原因。"""


def _open_stream(source) -> tuple[io.TextIOBase, bool]:
    """接受文件路径或已打开的二进制/文本流，返回文本流与是否需要关闭。"""
    if isinstance(source, (str, bytes, os.PathLike)):
        return open(source, "r", encoding="utf-8-sig", newline=""), True
    if isinstance(source, io.TextIOBase):
        return source, False
    if isinstance(source, (io.RawIOBase, io.BufferedIOBase)):
        return io.TextIOWrapper(source, encoding="utf-8-sig", newline=""), False
    # 无 mode 属性的类文件对象（如部分测试替身）按内容嗅探
    data = source.read()
    if isinstance(data, bytes):
        return io.StringIO(data.decode("utf-8-sig")), False
    return io.StringIO(data), False


def import_archive(source, filename: str, db_path: str | None = None) -> dict:
    """导入一个归档 CSV，返回统计字典；失败抛 ImportRejected。"""
    conn = connect(db_path)
    staging_name = f"_stage_import_{os.getpid()}_{int(time.time() * 1_000_000)}"
    stream, should_close = _open_stream(source)
    rows_total = 0
    try:
        reader = csv.reader(stream)
        try:
            raw_header = next(reader)
        except StopIteration:
            raise ImportRejected("文件为空，缺少表头")
        try:
            columns = normalize_headers(raw_header)
            require_columns(columns, REQUIRED, "归档 CSV ")
        except csv.Error as exc:
            raise ImportRejected(str(exc)) from exc
        idx = {name: i for i, name in enumerate(columns)}

        conn.execute(
            f"CREATE TEMP TABLE {staging_name} ("
            "turbine TEXT NOT NULL, measured_at INTEGER NOT NULL, "
            "available_at INTEGER NOT NULL, temperature REAL NOT NULL, "
            "PRIMARY KEY (turbine, measured_at, available_at)"
            ") WITHOUT ROWID"
        )
        insert_stage = (
            f"INSERT INTO {staging_name} "
            "(turbine, measured_at, available_at, temperature) VALUES (?,?,?,?)"
        )

        for line_no, row in enumerate(reader, start=2):
            if not row or all((c is None or c.strip() == "") for c in row):
                continue  # 容忍文件末尾空行
            if len(row) != len(columns):
                raise ImportRejected(
                    f"第 {line_no} 行字段数为 {len(row)}，期望 {len(columns)}，整批拒绝"
                )
            turbine = row[idx["turbine"]].strip()
            if not turbine:
                raise ImportRejected(f"第 {line_no} 行机组为空，整批拒绝")
            try:
                measured_at = parse_utc_second(
                    row[idx["measured_at"]], f"第 {line_no} 行测量时刻"
                )
                available_at = parse_utc_second(
                    row[idx["available_at"]], f"第 {line_no} 行可用时刻"
                )
                temp_raw = row[idx["temperature"]].strip()
                if temp_raw == "":
                    raise ValueError("温度为空")
                temperature = float(temp_raw)
            except (ValueError, OverflowError) as exc:
                raise ImportRejected(f"第 {line_no} 行数据非法（{exc}），整批拒绝")

            rows_total += 1
            try:
                conn.execute(
                    insert_stage, (turbine, measured_at, available_at, temperature)
                )
            except sqlite3.IntegrityError:
                # 文件内部同键出现两次
                existing = conn.execute(
                    f"SELECT temperature FROM {staging_name} "
                    "WHERE turbine=? AND measured_at=? AND available_at=?",
                    (turbine, measured_at, available_at),
                ).fetchone()
                if existing is not None and existing["temperature"] != temperature:
                    raise ImportRejected(
                        f"第 {line_no} 行与文件内同键记录温度不一致"
                        f"（机组={turbine}），整批拒绝"
                    )
                # 同键同值的重复行：忽略
                rows_total -= 1

        if rows_total == 0:
            raise ImportRejected("文件没有任何有效数据行，整批拒绝")

        # 与已入库数据比对：同键异值
        clash = conn.execute(
            f"SELECT s.turbine, s.measured_at, s.available_at, "
            "s.temperature AS new_t, r.temperature AS old_t "
            f"FROM {staging_name} s "
            "JOIN readings r ON r.turbine = s.turbine "
            "AND r.measured_at = s.measured_at "
            "AND r.available_at = s.available_at "
            "WHERE s.temperature <> r.temperature LIMIT 1"
        ).fetchone()
        if clash is not None:
            raise ImportRejected(
                "存在与已归档数据同键异值的记录"
                f"（机组={clash['turbine']}，新温度={clash['new_t']}，"
                f"已存温度={clash['old_t']}），整批拒绝"
            )

        # 同键同值的行不再插入（INSERT OR IGNORE 命中主键即跳过）
        with transaction(conn):
            # 先统计本批真正的新键数（此时尚未写入正式表）
            rows_inserted = conn.execute(
                f"SELECT COUNT(*) AS c FROM {staging_name} s "
                "WHERE NOT EXISTS (SELECT 1 FROM readings r "
                "WHERE r.turbine=s.turbine AND r.measured_at=s.measured_at "
                "AND r.available_at=s.available_at)"
            ).fetchone()["c"]
            conn.execute(
                f"INSERT OR IGNORE INTO readings "
                "(turbine, measured_at, available_at, temperature) "
                f"SELECT turbine, measured_at, available_at, temperature "
                f"FROM {staging_name}"
            )
            conn.execute(
                "INSERT INTO imports (filename, status, rows_total, rows_inserted,"
                " reason, created_at) VALUES (?, 'accepted', ?, ?, NULL, ?)",
                (filename, rows_total, rows_inserted, int(time.time())),
            )
            import_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        return {
            "import_id": import_id,
            "status": "accepted",
            "rows_total": rows_total,
            "rows_inserted": rows_inserted,
            "rows_reused": rows_total - rows_inserted,
        }
    except ImportRejected as exc:
        try:
            conn.execute(
                "INSERT INTO imports (filename, status, rows_total, rows_inserted,"
                " reason, created_at) VALUES (?, 'rejected', ?, 0, ?, ?)",
                (filename, rows_total, str(exc), int(time.time())),
            )
        except sqlite3.Error:
            pass
        raise
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {staging_name}")
        except sqlite3.Error:
            pass
        conn.close()
        if should_close:
            stream.close()
