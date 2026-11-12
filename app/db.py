"""SQLite 连接与建表。

时间在库内一律存为 Unix 纪元整数秒（UTC），避免字符串比较与时区歧义。
所有写操作都在短事务内完成，并设置 busy_timeout 以支持 Web 线程与
后台工作线程并发访问。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS readings (
    turbine        TEXT    NOT NULL,
    measured_at    INTEGER NOT NULL,
    available_at   INTEGER NOT NULL,
    temperature    REAL    NOT NULL,
    PRIMARY KEY (turbine, measured_at, available_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_pick
    ON readings (turbine, available_at, measured_at);

CREATE TABLE IF NOT EXISTS imports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('accepted', 'rejected')),
    rows_total     INTEGER NOT NULL,
    rows_inserted  INTEGER NOT NULL,
    reason         TEXT,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    filename          TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (
                          status IN ('queued', 'running', 'completed', 'failed')),
    total_rows        INTEGER NOT NULL DEFAULT 0,
    processed_rows    INTEGER NOT NULL DEFAULT 0,
    output_offset     INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    input_path        TEXT NOT NULL,
    output_path       TEXT NOT NULL,
    header_path       TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
"""


def get_db_path() -> str:
    return os.environ.get("APP_DB_PATH", "/data/app.db")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path or get_db_path(),
        timeout=30.0,
        isolation_level=None,  # 显式事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
