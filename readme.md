# JobRadar：分布式职位聚合与采集任务治理平台

<p align="center">
  <img src="assets/banner.png" alt="JobRadar - Distributed job aggregation and crawl task governance platform" width="100%">
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/pp2166/Enterprise-JobRadar-Platform/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/pp2166/Enterprise-JobRadar-Platform/ci.yml?branch=main&style=for-the-badge&label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License"></a>
</p>

JobRadar 是一个基于 FastAPI、PostgreSQL、Celery 和 Redis 的职位聚合与采集任务治理项目。系统聚合 RemoteOK、WeWorkRemotely 等公开职位来源，支持职位标准化、SimHash 去重、PostgreSQL 全文检索，并围绕 CrawlRun 补充任务生命周期追踪、失败重试、同源防重、任务超时和卡死恢复。

本项目的求职展示重点是：在上游 Talash 的职位聚合基础上，继续实现分布式采集任务的运行可观测性和失败治理能力，让一次采集从调度、派发、执行、重试到异常恢复都有可查询、可追踪的状态闭环。

## 项目定位

JobRadar 面向“远程职位聚合 + 后台采集任务治理”场景：

- 聚合多个公开职位来源，统一标准化为内部职位模型。
- 使用 SimHash 识别近重复职位，减少重复岗位干扰。
- 使用 PostgreSQL FTS 提供搜索、过滤和分页。
- 使用 Celery Beat、Celery Worker 和 Redis 执行异步采集。
- 使用 CrawlRun 记录每次采集任务的状态、统计和错误信息。
- 使用 Failure Task Management 处理失败重试、同源防重、超时和 stale 任务恢复。

## 系统架构

```text
Celery Beat / Admin API / crawl_all
        |
        v
dispatch_crawl_source / POST /admin/crawl
        |
        v
CrawlRun queued -> Redis -> crawl_source Worker
        |
        v
running -> crawler.fetch() -> normalize -> ingest_jobs
        |
        v
succeeded / retrying / failed
        |
        v
GET /admin/crawl-runs / GET /admin/crawl-runs/{run_id}
```

核心数据流：

- `POST /admin/crawl` 或 Celery Beat 触发采集。
- 系统创建 `queued` CrawlRun，并使用预生成的 `celery_task_id` 派发真实采集任务。
- Worker 根据 `celery_task_id` 复用 CrawlRun，进入 `running`。
- 采集完成后写入 `received`、`inserted`、`updated`、`duplicates`。
- 普通异常进入 `retrying` 或最终 `failed`。
- 软超时和 stale recovery 会将异常任务标记为 `failed`。

## 核心能力

### 职位采集与标准化

- RemoteOK crawler：读取公开 JSON feed。
- WeWorkRemotely crawler：读取多个 RSS category feed。
- 每个 crawler 输出 `NormalizedJob`。
- 标准化内容包括 title、company、location、remote、salary、tags、posted_at 等字段。
- 入库使用 `(source, source_id)` upsert，重复采集可以安全更新已有职位。

### SimHash 去重

- 使用 64-bit SimHash 对职位标题、公司和描述进行近重复判断。
- 精确重复通过 `(source, source_id)` upsert 处理。
- 近重复职位根据 Hamming distance 阈值识别并计入 duplicates。

### PostgreSQL 全文检索

- 使用 PostgreSQL `tsvector`、GIN index 和 `ts_rank_cd`。
- 支持 `GET /search` 的关键词、source、company、location、remote、experience、page、page_size 查询。
- 排序结合文本相关性、标题命中加权和时间衰减。

### CrawlRun 可观测性

CrawlRun 记录每次采集运行：

- `run_id`
- `source`
- `status`
- `celery_task_id`
- `retry_of_run_id`
- `trigger_type`
- `attempt_count`
- `received`
- `inserted`
- `updated`
- `duplicates`
- `error_message`
- `created_at`
- `started_at`
- `finished_at`

支持：

- `GET /admin/crawl-runs`
- `GET /admin/crawl-runs/{run_id}`
- source/status 筛选
- page/page_size 分页

### Failure Task Management

Failure Task Management 覆盖 2A 到 2D：

- 手动失败重试
- 同源活动任务防重
- Celery 软硬超时
- stale CrawlRun 恢复

## 失败任务治理

### 手动失败重试

接口：

```text
POST /admin/crawl-runs/{run_id}/retry
```

规则：

- 仅 `failed` 状态允许重试。
- 原 failed CrawlRun 保持不变。
- 重试会创建新的子 CrawlRun。
- 新记录 `trigger_type == "manual"`。
- 新记录 `retry_of_run_id` 指向父任务。
- 新记录使用新的 `celery_task_id`。
- 非 failed 父任务返回结构化 HTTP 409，`code == "CRAWL_RUN_NOT_RETRYABLE"`。

### 同源活动任务防重

活动状态：

- `queued`
- `running`
- `retrying`

覆盖入口：

- `POST /admin/crawl`
- `POST /admin/crawl-runs/{run_id}/retry`
- Celery Beat
- `crawl_all`

实现方式：

- `find_active_crawl_run` 查询同源活动 CrawlRun。
- `create_crawl_run_if_inactive` 在创建前做应用层检查。
- Celery Beat 和 `crawl_all` 统一经过 `dispatch_crawl_source`。
- 冲突返回 HTTP 409 或 Dispatcher skipped 结果。

第一版是应用层 best-effort 防重，不声明强一致。查询和创建之间仍存在并发窗口。直接 `crawl_source.delay()` 保留为内部兼容入口，Worker 找不到预建 CrawlRun 时会自动创建 `trigger_type == "direct"` 的记录。

### Celery 软硬超时

`crawl_source` 专属配置：

- `max_retries == 3`
- `default_retry_delay == 60`
- `soft_time_limit == 120`
- `time_limit == 150`

普通异常：

- 有剩余重试次数时，CrawlRun 进入 `retrying`，并调用 `self.retry()`。
- 重试耗尽时，CrawlRun 进入 `failed`。
- Celery retry 复用当前 task id，Worker 会继续复用同一条 CrawlRun。

软超时：

- `SoftTimeLimitExceeded` 直接标记为 `failed`。
- `error_message == "crawl soft time limit exceeded after 120 seconds"`。
- 不进入普通 retrying/self.retry 路径。

硬超时：

- `time_limit == 150` 用作 Worker 硬兜底。
- 硬超时可能无法执行 Python 清理逻辑，遗留状态由 stale recovery 兜底。

### stale recovery

Celery Beat 每 5 分钟执行：

```text
app.workers.tasks.recover_stale_crawl_runs
```

阈值：

- 20 分钟

判断规则：

- `queued` 使用 `created_at`。
- `running` / `retrying` 优先使用 `started_at`。
- `started_at` 为空时回退 `created_at`。

恢复方式：

- 调用 `recover_stale_crawl_runs`。
- 使用单条条件 `UPDATE ... RETURNING`。
- 将 stale 活动记录标记为 `failed`。
- `error_message == "stale crawl run recovered after 20 minutes"`。
- 返回 `recovered_count` 和 `recovered_run_ids`。
- 重复执行具备幂等性。

## 技术栈

- Python 3.11
- FastAPI
- SQLAlchemy 2.0 async
- Pydantic v2
- PostgreSQL 16
- PostgreSQL FTS
- Redis 7
- Celery 5.6.3
- Docker Compose
- HTTPX
- selectolax
- SimHash
- pytest
- Ruff
- uv

## 快速启动

### Docker Compose

```bash
git clone https://github.com/pp2166/Enterprise-JobRadar-Platform.git
cd Enterprise-JobRadar-Platform
cp .env.example .env
docker compose up --build
```

访问：

- UI: http://localhost:8000/
- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/healthz

触发采集：

```bash
curl -X POST http://localhost:8000/admin/crawl \
     -H "content-type: application/json" \
     -d '{"source":"remoteok"}'
```

### 本地开发

需要本地可访问 PostgreSQL 和 Redis。

```bash
uv sync
cp .env.example .env

uv run task api
uv run task worker
uv run task beat
```

常用命令：

| Command | 说明 |
| --- | --- |
| `uv run task api` | 启动 FastAPI |
| `uv run task worker` | 启动 Celery Worker |
| `uv run task beat` | 启动 Celery Beat |
| `uv run pytest -q` | 运行测试 |
| `uv run ruff check .` | 运行 Ruff |
| `docker compose up --build` | 启动完整环境 |

## API

### `GET /search`

支持参数：

- `q`
- `location`
- `remote`
- `experience`
- `company`
- `source`
- `page`
- `page_size`

返回结构：

```json
{
  "total": 42,
  "page": 1,
  "page_size": 20,
  "results": []
}
```

### `POST /admin/crawl`

请求：

```json
{ "source": "remoteok" }
```

或触发全部来源：

```json
{}
```

响应中的 CrawlRun ID 字段为 `run_id`，与 `CrawlRunOut` 保持一致：

```json
{
  "dispatched": ["remoteok"],
  "runs": [
    {
      "run_id": 1,
      "source": "remoteok",
      "status": "queued",
      "celery_task_id": "generated-task-id",
      "retry_of_run_id": null,
      "trigger_type": "api",
      "attempt_count": 0,
      "received": 0,
      "inserted": 0,
      "updated": 0,
      "duplicates": 0,
      "error_message": null,
      "created_at": "2026-01-01T00:00:00Z",
      "started_at": null,
      "finished_at": null
    }
  ]
}
```

### `GET /admin/crawl-runs`

查询 CrawlRun 列表。

参数：

- `source`
- `status`
- `page`
- `page_size`

响应：

```json
{
  "total": 1,
  "page": 1,
  "page_size": 20,
  "runs": []
}
```

### `GET /admin/crawl-runs/{run_id}`

查询单条 CrawlRun。不存在时返回 HTTP 404：

```json
{
  "detail": "crawl run not found: 999999"
}
```

### `POST /admin/crawl-runs/{run_id}/retry`

对 failed CrawlRun 发起手动重试。成功时返回新的 `CrawlRunOut`。

非 failed 状态返回 HTTP 409：

```json
{
  "detail": {
    "code": "CRAWL_RUN_NOT_RETRYABLE",
    "message": "crawl run is not retryable: 1",
    "run_id": 1,
    "status": "queued"
  }
}
```

同源活动任务冲突返回 HTTP 409：

```json
{
  "detail": {
    "code": "ACTIVE_CRAWL_RUN_EXISTS",
    "message": "active crawl run exists for source: remoteok",
    "source": "remoteok",
    "active_run_id": 2
  }
}
```

### `GET /admin/sources`

```json
{
  "sources": ["remoteok", "weworkremotely"]
}
```

## 自动化测试

当前验证结果：

- CrawlRun 专项：68 passed
- API 专项：51 passed
- Worker 专项：39 passed
- 全量测试：299 passed
- PostgreSQL 真实验收通过

测试覆盖重点：

- Pydantic Schema 与配置
- 职位标准化、去重、入库和搜索
- Admin API 成功、404、409 和 422
- CrawlRun 查询、重试、防重和 stale recovery
- Worker retry、timeout、Dispatcher 和 Beat 配置

## 项目边界和已知限制

- 同源活动任务防重是应用层 best-effort，存在并发窗口。
- 第一版没有 Redis 锁、PostgreSQL advisory lock 或部分唯一索引。
- 当前 CrawlRun 没有 `updated_at`，`retrying` 使用第一次 `started_at` 参与 stale 判断。
- 硬超时可能无法执行 Python 清理逻辑。
- stale recovery 后，旧 Worker 或 `acks_late` 重新投递的任务可能再次运行。
- `mark_crawl_run_succeeded` 当前没有终态条件保护，晚到旧任务可能覆盖恢复后的 failed。
- 当前 Schema 升级依赖 `app/schema.py::init_schema()` 和 PostgreSQL 幂等 DDL，不是 Alembic 版本化迁移体系。
- WeWorkRemotely 尚未进行独立真实 Docker E2E。

## 上游项目与开源许可

JobRadar 基于 Talash 进行二次开发。

- 上游仓库：https://github.com/iamrahulroyy/talash
- 上游项目 Talash 使用 MIT License。
- 本仓库保留原始 LICENSE 和上游归属。
- 本项目的求职亮点是对任务生命周期、失败治理和运行可观测性的后续开发。
- 本项目不声称整个系统从零独立开发。

## License

[MIT](LICENSE)
