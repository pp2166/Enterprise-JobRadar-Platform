# Failure Task Management 设计与实现状态

## 1. 阶段目标

Failure Task Management 是 CrawlRun 第一阶段之后的任务治理能力。它围绕分布式采集任务补充运行状态追踪、失败重试、同源防重、软硬超时和卡死任务恢复，形成从调度、执行到失败治理的完整任务生命周期。

本阶段已经完成：

- 2A：手动失败重试，完成
- 2B：同源活动任务防重，完成
- 2C：Celery 软硬超时治理，完成
- 2D：stale CrawlRun 恢复，完成

本阶段没有引入新的任务系统，没有拆分微服务，也没有声明强一致调度语义。

## 2A 手动重试，完成

### 数据字段

CrawlRun 增加了两个重试元数据字段：

- `retry_of_run_id`：nullable，自关联 `crawl_runs.id`，记录直接父任务。
- `trigger_type`：`String(32)`，允许 `api`、`manual`、`scheduled`、`direct`。

第一版不建立复杂 ORM relationship，不做树形重试关系，只保留直接父记录。

### Service

`create_retry_crawl_run(session, *, run_id, celery_task_id)` 已实现：

- 先查询父 CrawlRun。
- 不存在时抛出 `CrawlRunNotFoundError`。
- 只有父记录 `status == "failed"` 才允许重试。
- 非 failed 状态抛出 `CrawlRunNotRetryableError`。
- 成功时创建新的 queued CrawlRun。
- 新记录复用父记录 `source`。
- 新记录使用新的 `celery_task_id`。
- 新记录写入 `trigger_type="manual"`。
- 新记录写入 `retry_of_run_id=父记录.id`。
- 原 failed 父记录保持不变。
- Service 不生成 UUID，不调用 Celery。

### API

已提供：

`POST /admin/crawl-runs/{run_id}/retry`

返回新的 `CrawlRunOut`。异常语义为：

- 父记录不存在：HTTP 404，`detail == "crawl run not found: <run_id>"`。
- 父记录不可重试：HTTP 409，结构化 `CRAWL_RUN_NOT_RETRYABLE`。
- 存在同源活动任务：HTTP 409，结构化 `ACTIVE_CRAWL_RUN_EXISTS`。
- 新任务分发失败：新记录标记为 failed，原父记录不变，HTTP 503。

不可重试状态 detail：

```json
{
  "code": "CRAWL_RUN_NOT_RETRYABLE",
  "message": "crawl run is not retryable: <run_id>",
  "run_id": <run_id>,
  "status": "<status>"
}
```

活动任务冲突 detail：

```json
{
  "code": "ACTIVE_CRAWL_RUN_EXISTS",
  "message": "active crawl run exists for source: <source>",
  "source": "<source>",
  "active_run_id": <run_id>
}
```

## 2B 同源活动任务防重，完成

### 活动状态

以下状态视为活动任务：

- `queued`
- `running`
- `retrying`

`succeeded` 和 `failed` 不属于活动状态。

### Service

已实现：

- `find_active_crawl_run(session, *, source)`：按 source 精确匹配，只查询活动状态，按 `created_at DESC, id DESC` 返回最新一条。
- `create_crawl_run_if_inactive(...)`：创建前调用 `find_active_crawl_run`，发现活动记录时抛出 `ActiveCrawlRunExistsError`，否则复用 `create_crawl_run`。

`create_retry_crawl_run` 的检查顺序为：

1. 查询父记录。
2. 检查父记录是否为 failed。
3. 查询同源活动任务。
4. 无活动任务时创建 manual 重试记录。

因此非 failed 父记录会优先返回 `CRAWL_RUN_NOT_RETRYABLE`，不会被活动冲突掩盖。

### API 接入

`POST /admin/crawl` 已接入 `create_crawl_run_if_inactive`。

未指定 source 的批量触发会先对 `registry.names()` 中所有来源做预检查：任一来源存在活动记录时立即返回 HTTP 409，不创建任何 CrawlRun，也不调用 Celery 分发。预检查后真正创建时仍再次调用 `create_crawl_run_if_inactive`，减少预检查与创建之间的竞争窗口。

手动重试 API 也捕获 `ActiveCrawlRunExistsError`，使用同一套 `ACTIVE_CRAWL_RUN_EXISTS` detail。

### Dispatcher、Beat 和 crawl_all

已新增共享 Dispatcher：

`app.workers.tasks.dispatch_crawl_source`

数据流：

```text
Celery Beat / crawl_all
-> dispatch_crawl_source
-> _dispatch_crawl_source
-> create_crawl_run_if_inactive(trigger_type="scheduled")
-> crawl_source.apply_async(args=[source], task_id=<预生成 task id>)
-> Worker 根据相同 celery_task_id 复用 CrawlRun
```

冲突时 Dispatcher 返回：

```json
{
  "source": "<source>",
  "status": "skipped",
  "reason": "active crawl run exists",
  "active_run_id": <run_id>
}
```

分发失败时 Dispatcher 将新记录标记为 failed，`error_message` 写入 `dispatch failed: <异常文本>`，并返回：

```json
{
  "source": "<source>",
  "status": "dispatch_failed",
  "run_id": <run_id>
}
```

`crawl_all` 现在只发布 Dispatcher 任务，不直接调用 `crawl_source`，返回值仍为 `registry.names()` 的来源列表。

### trigger_type 语义

- `api`：`POST /admin/crawl` 创建。
- `manual`：`POST /admin/crawl-runs/{run_id}/retry` 创建。
- `scheduled`：Celery Beat 和 `crawl_all` 通过 Dispatcher 创建。
- `direct`：内部兼容入口 `crawl_source.delay()` 未提前创建 CrawlRun 时，由 Worker 自动建档。

直接 `crawl_source.delay()` 保留为内部兼容入口，不作为推荐业务调度入口。

### best-effort 边界

当前同源防重是应用层 best-effort。查询活动记录和创建新记录之间仍存在并发竞争窗口，因此第一版不能保证严格最多一个活动任务。

第一版没有使用：

- Redis 分布式锁
- PostgreSQL advisory lock
- PostgreSQL 部分唯一索引

后续如进入多 Worker 高并发生产形态，可以升级为数据库级锁或部分唯一索引。

## 2C Celery 软硬超时治理，完成

### crawl_source 专属配置

仅真实采集任务 `app.workers.tasks.crawl_source` 配置超时：

- `max_retries=3`
- `default_retry_delay=60`
- `soft_time_limit=120`
- `time_limit=150`

`dispatch_crawl_source`、`recover_stale_crawl_runs_task` 和 `crawl_all` 不配置 soft/hard time limit。

### 普通异常

普通异常保持原有 Celery retry 语义：

- `retries < max_retries`：`_run_crawler_attempt` 将 CrawlRun 标记为 `retrying`，保存普通异常文本，返回 `_RetryCrawl`，同步 task 调用 `self.retry()`。
- `retries >= max_retries`：将 CrawlRun 标记为 `failed`，重新抛出异常。

每次任务真实进入 `mark_crawl_run_running()` 时，`attempt_count += 1`。

Celery retry 复用当前 task id，因此 Worker 按 `celery_task_id` 能继续找到同一条 CrawlRun，不会因为自动重试创建第二条记录。

### 软超时

`SoftTimeLimitExceeded` 在 `_run_crawler_attempt()` 的爬取和入库 try/except 内被优先捕获，不进入普通 `Exception` 重试分支。

软超时策略：

- 直接标记当前 CrawlRun 为 `failed`。
- `error_message == "crawl soft time limit exceeded after 120 seconds"`。
- 重新抛出 `SoftTimeLimitExceeded`。
- 不调用 `mark_crawl_run_retrying()`。
- 不调用 `self.retry()`。
- 不新增第二条 CrawlRun。

### 硬超时

`time_limit=150` 作为硬兜底。硬超时可能终止 Worker 子进程，Python except/finally 不可靠，因此无法保证数据库回写。此类遗留状态由 2D stale recovery 兜底。

## 2D stale CrawlRun 恢复，完成

### Service

已实现：

`recover_stale_crawl_runs(session, *, stale_before, recovered_at, error_message) -> list[int]`

该 Service 使用单条条件 `UPDATE ... RETURNING`，不先查询候选记录，不逐条调用 `mark_crawl_run_failed`。

恢复条件：

- `queued`：`created_at < stale_before`
- `running`：`coalesce(started_at, created_at) < stale_before`
- `retrying`：`coalesce(started_at, created_at) < stale_before`

边界使用严格小于。刚好等于 `stale_before` 的记录不会恢复。

恢复时只修改：

- `status = "failed"`
- `error_message = <传入文本>`
- `finished_at = recovered_at`

保持不变：

- `source`
- `celery_task_id`
- `trigger_type`
- `retry_of_run_id`
- `attempt_count`
- `started_at`
- `created_at`
- `received`
- `inserted`
- `updated`
- `duplicates`

返回值是成功恢复的 CrawlRun ID，按数字升序排列。重复执行具备幂等性：第二次不会再次修改已经变成 failed 的记录，返回 `[]`。

### Celery 扫描任务

已新增：

`app.workers.tasks.recover_stale_crawl_runs`

同步 task wrapper 只调用一次 `asyncio.run(_recover_stale_crawl_runs())`。

helper 行为：

- 先调用 `_ensure_schema()`。
- `recovered_at` 默认为 `datetime.now(timezone.utc)`。
- `stale_before = recovered_at - timedelta(minutes=20)`。
- 调用 `recover_stale_crawl_runs` Service。
- 返回 JSON 可序列化结构：

```json
{
  "status": "completed",
  "recovered_count": 3,
  "recovered_run_ids": [1, 2, 3]
}
```

Service 或数据库异常不会被吞掉，也不会伪造成 completed。

### Beat 配置

Celery Beat 每 5 分钟扫描一次：

```python
"recover-stale-crawl-runs": {
    "task": "app.workers.tasks.recover_stale_crawl_runs",
    "schedule": crontab(minute="*/5"),
}
```

原来源调度保持不变：

- `remoteok`：`dispatch_crawl_source`，`minute="*/30"`。
- `weworkremotely`：`dispatch_crawl_source`，`minute="5-59/30"`。

## Schema 机制

项目当前不存在：

- `alembic.ini`
- `migrations` 目录
- Alembic migration head

当前采用：

- `app/schema.py::init_schema()`
- SQLAlchemy `Base.metadata.create_all`
- PostgreSQL 专用幂等 DDL

`init_schema()` 在 PostgreSQL 下会幂等补充 CrawlRun 字段和约束：

- `ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS retry_of_run_id INTEGER NULL`
- `ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(32) NOT NULL DEFAULT 'api'`
- 建立外键 `fk_crawl_runs_retry_of_run_id`
- 建立索引 `ix_crawl_runs_retry_of_run_id`

外键使用 PostgreSQL DO block 检查 `pg_constraint`，并捕获 `duplicate_object`，以便 API 与 Worker 并发启动时重复执行仍安全。

这是“幂等 Schema 升级”，不是完整数据库迁移框架。

## 状态机约束

- `failed` 可以手动重试。
- `succeeded` 不能手动重试。
- `queued` 不能手动重试。
- `running` 不能手动重试。
- `retrying` 不能手动重试。
- 原 failed 记录不修改。
- 新手动重试记录从 `queued` 开始。
- `retry_of_run_id` 指向直接父记录。
- 手动重试失败后，可以再次对新的 failed 记录发起重试。
- 不实现复杂树形重试关系，只保留直接父记录。

## 已验证结果

- Ruff：通过。
- CrawlRun 专项：68 passed。
- API 专项：51 passed。
- Worker 专项：39 passed。
- 全量 pytest：299 passed。
- PostgreSQL 真实验收：通过。

## 后续可强化项

- 将同源防重升级为 PostgreSQL advisory lock 或部分唯一索引。
- 增加 `updated_at`，让 retrying 的 stale 判断更精确。
- 给 `mark_crawl_run_succeeded` 增加终态条件保护，避免晚到旧任务覆盖 stale recovery 后的 failed。
- 处理硬超时后可能发生的 broker 重新投递和旧 Worker 晚到问题。

## 本阶段不做

- WebSocket
- 任务取消
- 删除运行记录
- Flower
- Redis 分布式锁
- Kubernetes
- 多 Worker 心跳系统
- 无限自动重试
- Agent
- RAG
- Kafka
- Flink
- 微服务拆分
