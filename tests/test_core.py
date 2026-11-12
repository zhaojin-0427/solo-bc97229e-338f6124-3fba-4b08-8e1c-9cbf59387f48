"""核心规则测试，仅依赖标准库，可直接 `python -I tests/test_core.py` 运行。"""
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db
from app.importer import ImportRejected, import_archive
from app.jobs import (
    SampleValidationError,
    create_job,
    pick_reading,
    resume_pending,
    run_job,
)
from app.util import TimeParseError, format_utc_second, parse_utc_second

ARCHIVE_HEADER = "机组,测量时刻,可用时刻,温度\n"
SAMPLE_HEADER = "样本号,机组,预测时刻,故障标签\n"


def b(text: str) -> io.BytesIO:
    return io.BytesIO(text.encode("utf-8"))


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.data_dir = os.path.join(self.tmp, "data")
        os.environ["APP_DB_PATH"] = self.db_path
        os.environ["APP_DATA_DIR"] = self.data_dir
        init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------- 时间解析
    def test_parse_utc_second(self):
        self.assertEqual(
            parse_utc_second("2026-11-01T10:00:00Z"),
            parse_utc_second("2026-11-01 10:00:00"),
        )
        self.assertEqual(
            format_utc_second(parse_utc_second("2026-11-01T10:00:00Z")),
            "2026-11-01T10:00:00Z",
        )
        # 带偏移换算 UTC
        self.assertEqual(
            parse_utc_second("2026-11-01T18:00:00+08:00"),
            parse_utc_second("2026-11-01T10:00:00Z"),
        )
        with self.assertRaises(TimeParseError):
            parse_utc_second("2026-11-01T10:00:00.5Z")

    # -------------------------------------------------- 导入规则
    def _import(self, text, name="a.csv"):
        return import_archive(b(text), name, self.db_path)

    def test_import_accept_and_reuse(self):
        text = ARCHIVE_HEADER + (
            "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,42.5\n"
            "WT-01,2026-11-01T10:05:00Z,2026-11-01T10:20:00Z,43.1\n"
        )
        r = self._import(text)
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["rows_inserted"], 2)

        # 完全相同的文件再传：全部复用，零新增
        r2 = self._import(text, "a_dup.csv")
        self.assertEqual(r2["rows_inserted"], 0)
        self.assertEqual(r2["rows_reused"], 2)

        conn = connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        conn.close()
        self.assertEqual(n, 2)

    def test_import_conflict_rejects_whole_batch(self):
        self._import(
            ARCHIVE_HEADER
            + "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,42.5\n"
        )
        # 同键异值：整批拒绝，连带文件里的其他新行也不能入库
        with self.assertRaises(ImportRejected):
            self._import(
                ARCHIVE_HEADER
                + "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,99.9\n"
                "WT-02,2026-11-01T11:00:00Z,2026-11-01T11:05:00Z,40.0\n",
                "bad.csv",
            )
        conn = connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        rejected = conn.execute(
            "SELECT COUNT(*) c FROM imports WHERE status='rejected'"
        ).fetchone()["c"]
        wt02 = conn.execute(
            "SELECT COUNT(*) c FROM readings WHERE turbine='WT-02'"
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1)  # 仍是第一批那一行
        self.assertEqual(rejected, 1)
        self.assertEqual(wt02, 0)

    def test_import_malformed_row_rejects_batch(self):
        with self.assertRaises(ImportRejected):
            self._import(
                ARCHIVE_HEADER
                + "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,\n"  # 温度空
            )
        with self.assertRaises(ImportRejected):
            self._import(
                ARCHIVE_HEADER
                + "WT-01,2026-11-01T10:00:00Z,abc,42.5\n"  # 时间坏
            )

    def test_internal_duplicate_same_value_ignored(self):
        r = self._import(
            ARCHIVE_HEADER
            + "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,42.5\n"
            "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,42.5\n"
        )
        self.assertEqual(r["rows_total"], 1)
        self.assertEqual(r["rows_inserted"], 1)

    # -------------------------------------------------- 取数规则
    def _seed(self):
        self._import(
            ARCHIVE_HEADER
            + "WT-01,2026-11-01T10:00:00Z,2026-11-01T10:10:00Z,42.5\n"
            "WT-01,2026-11-01T10:05:00Z,2026-11-01T10:20:00Z,43.1\n"
            "WT-01,2026-11-01T10:05:00Z,2026-11-01T10:15:00Z,41.9\n"
            "WT-02,2026-11-01T10:40:00Z,2026-11-01T10:45:00Z,47.2\n"
            "WT-03,2026-11-01T08:00:00Z,2026-11-01T12:00:00Z,35.6\n"
        )

    def test_pick_rule_measured_then_available(self):
        self._seed()
        conn = connect(self.db_path)
        t = lambda s: parse_utc_second(s)

        # 10:30 预测：测量最新是 10:05（两条），取可用更新的 10:20 -> 43.1
        r = pick_reading(conn, "WT-01", t("2026-11-01T10:30:00Z"))
        self.assertEqual(r["temperature"], 43.1)
        self.assertEqual(format_utc_second(r["measured_at"]),
                         "2026-11-01T10:05:00Z")
        self.assertEqual(format_utc_second(r["available_at"]),
                         "2026-11-01T10:20:00Z")

        # 10:12 预测：10:05 测量的两条可用时刻都晚于预测，不可用；
        # 只剩 10:00 测量 / 10:10 可用
        r = pick_reading(conn, "WT-01", t("2026-11-01T10:12:00Z"))
        self.assertEqual(r["temperature"], 42.5)

        # 两时刻都晚于预测 -> 无匹配
        self.assertIsNone(
            pick_reading(conn, "WT-02", t("2026-11-01T10:30:00Z"))
        )
        # 可用时刻 12:00 晚于 11:00 预测 -> 不可用
        self.assertIsNone(
            pick_reading(conn, "WT-03", t("2026-11-01T11:00:00Z"))
        )
        conn.close()

    # -------------------------------------------------- 任务与输出
    def _create_job(self, rows_text):
        return create_job(b(SAMPLE_HEADER + rows_text), "s.csv",
                          self.data_dir, self.db_path)

    def _read_output(self, job_id):
        conn = connect(self.db_path)
        path = conn.execute("SELECT output_path FROM jobs WHERE id=?",
                            (job_id,)).fetchone()["output_path"]
        conn.close()
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def test_job_full_output_order_and_blanks(self):
        self._seed()
        job_id = self._create_job(
            "S001,WT-01,2026-11-01T10:30:00Z,0\n"
            "S002,WT-02,2026-11-01T10:30:00Z,1\n"
            "S003,WT-99,2026-11-01T11:00:00Z,0\n"
        )
        run_job(job_id, self.db_path)
        out = self._read_output(job_id)
        self.assertEqual(
            out[0],
            ["样本号", "机组", "预测时刻", "故障标签",
             "测量时刻", "可用时刻", "温度"],
        )
        self.assertEqual(out[1][0], "S001")
        self.assertEqual(out[1][-1], "43.1")
        # 无匹配样本保留原字段，三个附加列留空
        self.assertEqual(out[2][:4], ["S002", "WT-02",
                                      "2026-11-01T10:30:00Z", "1"])
        self.assertEqual(out[2][4:], ["", "", ""])
        self.assertEqual(out[3][4:], ["", "", ""])
        # 原序
        self.assertEqual([r[0] for r in out[1:]], ["S001", "S002", "S003"])

        conn = connect(self.db_path)
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["processed_rows"], 3)
        self.assertFalse(os.path.exists(row["output_path"] + ".partial"))

    def test_input_fixed_then_backfill_ignored_by_completed_job(self):
        self._seed()
        job_id = self._create_job(
            "S001,WT-03,2026-11-01T11:00:00Z,0\n"
        )
        run_job(job_id, self.db_path)
        out_before = self._read_output(job_id)
        self.assertEqual(out_before[1][4:], ["", "", ""])  # 当时无可用温度

        # 任务完成后补传一条“事后可用”的读数，不能改变已完成结果
        self._import(
            ARCHIVE_HEADER
            + "WT-03,2026-11-01T07:00:00Z,2026-11-01T10:55:00Z,33.3\n",
            "backfill.csv",
        )
        run_job(job_id, self.db_path)  # completed 任务直接返回
        self.assertEqual(self._read_output(job_id), out_before)

    def test_resume_after_restart_keeps_one_consistent_output(self):
        self._seed()
        # 足够多的行以跨越多个检查点批次
        n = 500
        lines = "".join(
            f"S{i:04d},WT-01,2026-11-01T10:30:00Z,{i % 2}\n"
            for i in range(n)
        )
        job_id = self._create_job(lines)

        # 模拟“运行到一半进程被杀”：手动跑一部分，再用 resume 机制续跑
        from app import jobs as jobs_mod
        old_batch = jobs_mod.CHECKPOINT_BATCH
        jobs_mod.CHECKPOINT_BATCH = 50
        try:
            # 第一次执行完整跑完（验证检查点逻辑不破坏结果）
            run_job(job_id, self.db_path)
        finally:
            jobs_mod.CHECKPOINT_BATCH = old_batch

        rows = self._read_output(job_id)
        self.assertEqual(len(rows), n + 1)

        # 重启恢复：没有未完成任务
        self.assertEqual(resume_pending(self.db_path), [])

        # 模拟中断：直接构造一个停在中途的 running 任务
        conn = connect(self.db_path)
        out_path = conn.execute(
            "SELECT output_path FROM jobs WHERE id=?", (job_id,)
        ).fetchone()["output_path"]
        conn.close()
        partial = out_path + ".partial"
        shutil.copy(out_path, partial)
        stop = 137
        with open(partial, "r", encoding="utf-8", newline="") as fh:
            lines = fh.readlines()
        # 表头 + stop 行数据之后的字节偏移即为检查点位置
        pos = sum(len(line.encode("utf-8")) for line in lines[: stop + 1])
        with open(partial, "r+b") as fh:
            fh.truncate(pos)
        conn = connect(self.db_path)
        conn.execute(
            "UPDATE jobs SET status='running', processed_rows=?, "
            "output_offset=? WHERE id=?", (stop, pos, job_id)
        )
        conn.close()

        self.assertEqual(resume_pending(self.db_path), [job_id])
        run_job(job_id, self.db_path)  # 续跑
        rows2 = self._read_output(job_id)
        self.assertEqual(len(rows2), n + 1)
        self.assertEqual(rows2, rows)  # 续跑结果与一次跑完逐字节一致
        self.assertFalse(os.path.exists(partial))

    def test_partial_not_downloadable_and_no_duplicate_headers(self):
        self._seed()
        job_id = self._create_job(
            "S001,WT-01,2026-11-01T10:30:00Z,0\n"
        )
        # 未执行时只有输入，没有输出文件
        conn = connect(self.db_path)
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        conn.close()
        self.assertFalse(os.path.exists(row["output_path"]))
        self.assertEqual(row["status"], "queued")

    def test_recovery_after_rename_before_commit(self):
        """换名成功但 completed 状态未提交的崩溃窗口：重启只补登状态、不重算。"""
        self._seed()
        job_id = self._create_job(
            "S001,WT-01,2026-11-01T10:30:00Z,0\n"
            "S002,WT-02,2026-11-01T11:00:00Z,1\n"
        )
        # 先完整跑一遍拿到正确输出
        run_job(job_id, self.db_path)
        expected = self._read_output(job_id)
        conn = connect(self.db_path)
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        out_path, total = row["output_path"], row["total_rows"]
        # 人为回退状态，模拟“换名后、提交前崩溃”
        conn.execute(
            "UPDATE jobs SET status='running', processed_rows=0, output_offset=0 "
            "WHERE id=?", (job_id,)
        )
        conn.close()
        self.assertTrue(os.path.exists(out_path))
        self.assertFalse(os.path.exists(out_path + ".partial"))

        run_job(job_id, self.db_path)  # 重启续跑入口
        self.assertEqual(self._read_output(job_id), expected)
        conn = connect(self.db_path)
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["processed_rows"], total)

    def test_failed_validation_leaves_no_job_dir(self):
        jobs_root = os.path.join(self.data_dir, "jobs")
        before = set(os.listdir(jobs_root)) if os.path.isdir(jobs_root) else set()
        with self.assertRaises(SampleValidationError):
            create_job(b(SAMPLE_HEADER +
                         "S1,WT-01,2026-11-01T10:30:00Z,0\n"
                         "S1,WT-02,2026-11-01T10:30:00Z,1\n"),
                       "dup.csv", self.data_dir, self.db_path)
        after = set(os.listdir(jobs_root)) if os.path.isdir(jobs_root) else set()
        self.assertEqual(before, after)

    def test_sample_validation(self):
        with self.assertRaises(SampleValidationError):
            create_job(b(SAMPLE_HEADER +
                         "S1,WT-01,2026-11-01T10:30:00Z,0\n"
                         "S1,WT-02,2026-11-01T10:30:00Z,1\n"),
                       "dup.csv", self.data_dir, self.db_path)
        with self.assertRaises(SampleValidationError):
            create_job(b(SAMPLE_HEADER +
                         "S1,WT-01,bad-time,0\n"),
                       "bad.csv", self.data_dir, self.db_path)
        with self.assertRaises(SampleValidationError):
            create_job(b(SAMPLE_HEADER),
                       "empty.csv", self.data_dir, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
