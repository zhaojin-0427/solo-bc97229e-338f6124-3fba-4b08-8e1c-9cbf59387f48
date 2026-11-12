#!/usr/bin/env bash
# 端到端演示：构建启动 -> 导入归档（含整批拒绝案例）-> 发起任务 ->
# 轮询进度 -> 下载输出 -> 演示补传不影响在途/已完成任务。
set -euo pipefail

BASE="http://localhost:8000"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> 1/6 构建并启动容器"
docker compose up -d --build
echo "等待健康检查..."
for i in $(seq 1 30); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then echo "服务已就绪"; break; fi
  sleep 1
done

echo "==> 2/6 导入合法归档 archive_ok.csv"
curl -fsS -F "file=@$HERE/samples/archive_ok.csv" "$BASE/api/imports"; echo

echo "==> 3/6 导入同键异值文件 archive_conflict.csv（预期 422 整批拒绝）"
curl -sS -o /tmp/conflict_resp.json -w "HTTP %{http_code}\n" \
  -F "file=@$HERE/samples/archive_conflict.csv" "$BASE/api/imports" || true
cat /tmp/conflict_resp.json; echo

echo "==> 再次导入相同合法文件（预期 rows_inserted=0，全部复用）"
curl -fsS -F "file=@$HERE/samples/archive_ok.csv" "$BASE/api/imports"; echo

echo "==> 4/6 上传运维样本并受理后台任务"
JOB=$(curl -fsS -F "file=@$HERE/samples/samples_demo.csv" \
  "$BASE/api/jobs" | sed -E 's/.*"job_id":"([^"]+)".*/\1/')
echo "任务 ID: $JOB"

echo "==> 5/6 轮询进度直到完成"
while true; do
  RESP=$(curl -fsS "$BASE/api/jobs/$JOB")
  echo "  $RESP"
  echo "$RESP" | grep -q '"status": *"completed"' && break
  echo "$RESP" | grep -q '"status": *"failed"' && { echo "任务失败"; exit 1; }
  sleep 1
done

echo "==> 6/6 下载完整输出"
curl -fsS "$BASE/api/jobs/$JOB/download" -o "/tmp/training_$JOB.csv"
echo "已保存 /tmp/training_$JOB.csv，内容："
cat "/tmp/training_$JOB.csv"

echo
echo "==> 补传演示：任务完成后导入 archive_backfill.csv，已完成输出保持不变"
curl -fsS -F "file=@$HERE/samples/archive_backfill.csv" "$BASE/api/imports"; echo
echo "重新下载仍为同一内容（completed 任务不重算）："
curl -fsS "$BASE/api/jobs/$JOB/download" | diff - "/tmp/training_$JOB.csv" \
  && echo "一致 ✔"

echo
echo "演示完成。浏览器打开 $BASE 可查看网页控制台。"
echo "停止并清理：docker compose down -v"
