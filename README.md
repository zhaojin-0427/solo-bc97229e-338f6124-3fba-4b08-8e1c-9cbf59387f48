# 风机故障模型 · 训练数据工程服务

按**预测当时可获得**的温度生成训练表，杜绝事后补传值泄漏。Flask + SQLite 单容器实现，
提供网页控制台、归档导入、后台关联任务、进度查看与结果下载。

## 业务规则

### 监测归档导入（CSV：机组 / 测量时刻 / 可用时刻 / 温度）

- 前三列（机组、测量时刻、可用时刻）为业务键；
- **同键同值**：已存在则复用，不重复插入；
- **同键异值**：整批拒绝，本文件一行都不入库；
- 任意行残缺、字段数不符、时间非法（非 UTC 整秒）、温度非数值：整批拒绝；
- 拒绝与接受均在「导入记录」中可查，拒绝带具体原因。

### 运维样本任务（CSV：唯一样本号 / 机组 / 预测时刻 / 故障标签）

- 上传即受理为后台任务，**受理时样本输入原样落盘、从此固定**；任务执行期间可继续导入
  归档补传，补传既不改变在途任务的输入，也不会重算已完成任务；
- 每个样本只使用**同机组**且 `测量时刻 <= 预测时刻`、`可用时刻 <= 预测时刻` 的读数：
  1. 先取**测量时刻最新**者；
  2. 测量时刻并列时取**可用时刻最新**者；
  3. 无匹配读数则温度及两个时刻列留空；
- 按样本原序输出 CSV，保留全部样本字段，追加 `测量时刻,可用时刻,温度` 三列。

### 重启与一致性

- 任务进度（已处理行数 + 输出字节偏移）与任务输入、输出文件均持久化在挂载卷；
- 进程重启自动恢复 `queued/running` 任务，按偏移截断 `.partial` 后续跑，
  同一任务只产出一份完整文件，结果与一次跑完逐字节一致；
- 输出先写 `output.csv.partial`，全部行完成后原子换名为 `output.csv`；
  **残缺文件在任何状态下都不可下载**（接口返回 409）。

### 内存与容量

- 归档导入：磁盘临时表暂存、逐行流式写入；
- 样本处理：逐行读取、逐行查询、增量写出，每 200 行提交一次检查点；
- 实测 10 万归档行 + 10 万样本规模，进程常驻内存增量约 12 MB，与总行数无关。

## 快速开始

```bash
docker compose up -d --build
# 浏览器打开
open http://localhost:8000      # macOS
# 或手动访问 http://localhost:8000
```

一键脚本演示完整流程（合法导入、整批拒绝、复用、后台任务、下载、补传不影响结果）：

```bash
./demo.sh
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页控制台 |
| GET | `/health` | 健康检查 |
| POST | `/api/imports` | 上传归档 CSV（multipart 字段 `file`） |
| GET | `/api/imports` | 导入历史与拒绝原因 |
| POST | `/api/jobs` | 上传样本 CSV，受理后台任务，返回 `job_id` |
| GET | `/api/jobs` | 任务列表与进度 |
| GET | `/api/jobs/<id>` | 单任务详情 |
| GET | `/api/jobs/<id>/download` | 下载完整输出（未完成返回 409） |

示例：

```bash
curl -F "file=@samples/archive_ok.csv" http://localhost:8000/api/imports
curl -F "file=@samples/samples_demo.csv" http://localhost:8000/api/jobs
curl http://localhost:8000/api/jobs/<job_id>
curl -OJ http://localhost:8000/api/jobs/<job_id>/download
```

## 时间格式

统一 UTC 整秒，接受 `2026-11-01T10:00:00Z`、`2026-11-01T18:00:00+08:00`
（自动换算 UTC）与 `2026-11-01 10:00:00`（裸时间按 UTC）；
小数秒一律拒绝。库内以纪元整数秒存储与比较。

## 示例数据（`samples/`）

- `archive_ok.csv`：含同机组同测量时刻、不同可用时刻的并列场景；
- `archive_conflict.csv`：与已入库数据同键异值，演示整批拒绝；
- `archive_backfill.csv`：事后补传，演示不影响已完成任务；
- `samples_demo.csv`：覆盖正常取数、并列破并列、可用时刻超窗、无机组匹配等场景。

## 测试

核心逻辑仅依赖标准库即可运行（无需 Flask）：

```bash
python3 -I tests/test_core.py         # 导入/取数/输出/续跑规则（11 项）
python3 -I tests/test_concurrency.py  # 并发补传 + 重启续跑 + 万行规模
python3 -I tests/test_web.py          # Web 路由端到端（有真实 Flask 时用真实 Flask）
```

## 目录结构

```
app/
  db.py        SQLite 连接、建表、短事务
  util.py      UTC 整秒解析、中英文表头归一
  importer.py  归档流式导入、暂存校验、同键异值整批拒绝
  jobs.py      样本受理固定输入、取数规则、后台线程、检查点与重启续跑
  main.py      Flask 路由、下载防护、页面
  templates/index.html  单页控制台
samples/       演示 CSV
tests/         测试
Dockerfile, docker-compose.yml, requirements.txt
```

## 部署说明

- gunicorn 单 worker + 8 线程：后台调度线程与 Web 线程同进程协作；
  任务持久化完全依赖 SQLite 与挂载卷 `/data`，不依赖进程内存；
- 多实例水平扩展不在本方案范围（单 worker 是避免同任务被多进程重复领取的有意约束）；
- `docker compose down -v` 会同时清除数据卷。
