# Failure Task Management 最终验收

## 验收基线

- 分支：`feat/failure-task-management`
- 提交：`d5655bc feat: schedule stale crawl run recovery`
- 日期：2026-07-16
- Python/测试运行方式：`uv run pytest`
- 工作区：干净
- 验收范围：Failure Task Management 2A 到 2D

## 自动化测试

- 本阶段相关文件 Ruff 检查：通过
- 仓库级 `uv run ruff check .` 当前存在既有问题：
  `tests/test_crawlers_edge.py` 中 `NormalizedJob` 未使用，错误码 `F401`
- 该问题不由本阶段文档修改引入，合并前单独处理
- CrawlRun：68 passed
- API：51 passed
- Worker：39 passed
- 全量：299 passed
- `git diff --check`：通过

## PostgreSQL Schema 验收

项目当前没有 `alembic.ini`、`migrations` 目录或 Alembic migration head。Schema 验收使用当前仓库支持的 `app/schema.py::init_schema()` 幂等升级路径。

验收方式：

- 使用 Docker Compose 启动 PostgreSQL 16。
- 创建独立临时数据库。
- 连续执行 `init_schema()` 两次。
- 不修改业务数据库数据。

验收结果：

- `init_schema()` 重复执行成功。
- `crawl_runs.retry_of_run_id` 存在。
- `crawl_runs.trigger_type` 存在。
- 外键 `fk_crawl_runs_retry_of_run_id` 存在。
- 索引 `ix_crawl_runs_retry_of_run_id` 存在。
- PostgreSQL 下 `recover_stale_crawl_runs` 的 `UPDATE ... RETURNING` 可执行。

## 业务能力验收

### 手动重试

已验证：

- 创建 failed 父记录。
- 调用 `create_retry_crawl_run`。
- 新子记录 `status == "queued"`。
- 新子记录 `trigger_type == "manual"`。
- 新子记录 `retry_of_run_id` 指向父记录 ID。
- 新子记录 `source` 与父记录一致。
- 原 failed 父记录保持不变。

### 同源活动防重

已验证：

- 创建同源 queued 活动记录。
- 再调用 `create_crawl_run_if_inactive` 时抛出 `ActiveCrawlRunExistsError`。
- 异常中的 `source` 正确。
- 异常中的 `active_run_id` 正确。
- 数据库没有新增记录。

当前活动状态定义：

- `queued`
- `running`
- `retrying`

当前实现仍是应用层 best-effort，不声明强一致。

### stale recovery

已验证以下超过 20 分钟的活动记录会被恢复：

- `queued`
- `running`
- `retrying`

同时验证以下记录不会被恢复：

- 未超过阈值的 `queued`
- 旧 `succeeded`
- 旧 `failed`

恢复结果：

- 只恢复三条 stale 活动记录。
- 返回 ID 升序。
- 状态变成 `failed`。
- `error_message == "stale crawl run recovered after 20 minutes"`。
- `finished_at` 等于传入的 `recovered_at`。
- `source`、`celery_task_id`、`trigger_type`、`retry_of_run_id`、`attempt_count`、`started_at`、`created_at`、`received`、`inserted`、`updated`、`duplicates` 保持不变。

### 幂等恢复

已验证：

- 第一次执行返回被恢复的 CrawlRun ID。
- 第二次使用相同 stale 条件再次执行返回 `[]`。
- 已经恢复为 failed 的记录不会被再次覆盖。

### API 409/404

使用测试客户端和 monkeypatch 隔离 Celery 分发，未向 broker 发布真实抓取任务。

已验证：

- 数据库已有同源 queued 记录时，`POST /admin/crawl {"source": "remoteok"}` 返回 HTTP 409。
- HTTP 409 detail：

```json
{
  "code": "ACTIVE_CRAWL_RUN_EXISTS",
  "message": "active crawl run exists for source: remoteok",
  "source": "remoteok",
  "active_run_id": 1
}
```

- 冲突时没有新增 CrawlRun。
- 非 failed 父记录调用 `POST /admin/crawl-runs/{run_id}/retry` 返回 HTTP 409，`code == "CRAWL_RUN_NOT_RETRYABLE"`。
- 不存在父记录调用 `POST /admin/crawl-runs/999999/retry` 返回 HTTP 404。

### Celery timeout 配置

`crawl_source`：

- `max_retries == 3`
- `default_retry_delay == 60`
- `soft_time_limit == 120`
- `time_limit == 150`

`dispatch_crawl_source`：

- 没有 `soft_time_limit`
- 没有 `time_limit`

`recover_stale_crawl_runs_task`：

- task name：`app.workers.tasks.recover_stale_crawl_runs`
- 没有自动重试
- 没有 `soft_time_limit`
- 没有 `time_limit`

### Beat 配置

`remoteok`：

- task：`app.workers.tasks.dispatch_crawl_source`
- args：`("remoteok",)`
- minute：`"*/30"`

`weworkremotely`：

- task：`app.workers.tasks.dispatch_crawl_source`
- args：`("weworkremotely",)`
- minute：`"5-59/30"`

stale recovery：

- task：`app.workers.tasks.recover_stale_crawl_runs`
- minute：`"*/5"`
- 没有 args 和 kwargs

## 隔离与清理

- 未访问 RemoteOK、WeWorkRemotely 或其他外部招聘网站。
- API 验收中 Celery 分发已 mock。
- `acceptance-failure-management-` 前缀数据已清理。
- 临时 PostgreSQL 数据库已删除。
- 临时 PostgreSQL 容器已停止。

## 已知限制与后续可强化项

以下限制不代表当前功能失败，而是第一版实现的明确边界：

1. 同源防重仍是应用层 best-effort，查询与创建之间存在并发窗口。
2. 第一版没有 Redis 锁、PostgreSQL advisory lock 或部分唯一索引。
3. 当前 CrawlRun 没有 `updated_at`，`retrying` 使用第一次 `started_at` 判断 stale。
4. 硬超时可能无法执行 Python 清理逻辑。
5. stale 恢复后，旧 Worker 或 `acks_late` 重新投递的任务可能再次运行。
6. `mark_crawl_run_succeeded` 当前没有终态条件保护，晚到旧任务可能覆盖恢复后的 failed。
7. 当前 Schema 升级依赖 `init_schema()`，不是 Alembic 版本化迁移体系。

## 验收结论

Failure Task Management 2A 到 2D 已完成。当前实现覆盖手动失败重试、同源活动防重、Celery 软硬超时和 stale CrawlRun 恢复，并通过自动化测试与真实 PostgreSQL 验收。
