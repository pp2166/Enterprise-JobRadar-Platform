# Failure Task Management 设计

## 1. 设计目标

Failure Task Management 是 CrawlRun 第一阶段之后的第二阶段能力，目标是在已有运行记录、状态回写、自动建档、列表和详情查询的基础上，补齐失败任务的人工处置、活动任务防重复、执行超时和卡死恢复。

本阶段只围绕 CrawlRun 失败管理展开，不引入新的任务系统，不改变现有 Celery 任务外部参数，不拆分服务。

## 2A 手动重试

### 目标

- 只允许对 failed CrawlRun 发起手动重试
- 原失败记录保持不变
- 创建新的 CrawlRun
- 新记录使用新的 celery_task_id
- 新记录保存原运行记录 ID，形成重试链路

### 建议新增字段

- retry_of_run_id：nullable，自关联 CrawlRun.id
- trigger_type：api、manual、scheduled、direct

### 建议接口

POST /admin/crawl-runs/{run_id}/retry

### 规则

- failed 可以重试
- queued、running、retrying、succeeded 不允许重试
- 非法状态返回 HTTP 409
- 不存在返回 HTTP 404
- 成功返回新的 CrawlRun
- 分发失败时，新记录进入 failed
- 旧记录不能被修改
- 不能复用旧 celery_task_id

### 行为说明

手动重试不改变原 failed 记录。系统根据原记录的 source 创建一条新的 queued CrawlRun，生成新的 Celery task id，并通过 apply_async 分发。新记录的 retry_of_run_id 指向原记录，trigger_type 记录为 manual。

如果新任务分发失败，只标记新记录 failed，并记录分发失败原因；原失败记录仍保持原样。

## 2B 同源任务防重复

### 目标

- 在管理 API、手动重试和定时调度等正常入口中，尽力避免同一个 source 同时产生多个 queued、running、retrying 任务。
- POST /admin/crawl、手动重试、Celery Beat 和 crawl_all 等正常调度入口都必须执行活动任务检查
- 冲突返回 HTTP 409
- 响应中说明当前活动 run_id

### 第一版方案

第一版属于应用层 best-effort 防重，只使用 PostgreSQL 查询检查，不使用 Redis 锁。

活动状态定义为：

- queued
- running
- retrying

管理 API、手动重试、Celery Beat、crawl_all 应统一经过共享的调度逻辑，在创建 CrawlRun 和发布 Celery 任务前检查活动记录。

创建新 CrawlRun 前，按 source 查询是否存在上述活动状态记录。如果存在，则拒绝创建新任务并返回 HTTP 409，响应 detail 中说明当前活动 run_id。

直接 crawl_source.delay() 只作为内部兼容入口，不作为推荐的业务调度入口。

### 并发竞争风险

查询和创建之间存在并发竞争窗口：两个请求可能同时查询到没有活动任务，然后分别创建 queued 记录。

第一版接受这个风险，因为实现简单、可测试、无需额外基础设施。因此第一版不能保证严格最多一个活动任务。真正强一致防重需要后续升级为：

- PostgreSQL advisory lock
- PostgreSQL 部分唯一索引

推荐在进入多 Worker、高并发生产形态前升级为数据库级约束。

## 2C 抓取超时

### 目标

- 单次爬取不能无限执行
- 第一版使用 Celery soft_time_limit 和 time_limit
- 超时时尽量把 CrawlRun 标记为 failed
- error_message 明确记录 timeout
- 不实现不同来源的动态超时配置

### 第一版方案

在 crawl_source Celery task 上配置 soft_time_limit 和 time_limit。soft_time_limit 触发时，Worker 捕获超时异常并尽量调用 mark_crawl_run_failed，将状态更新为 failed，error_message 写入 timeout 相关信息，finished_at 设置为当前时间。

time_limit 作为硬兜底，防止任务继续占用 Worker。硬超时可能导致进程被终止，无法保证一定完成数据库回写，因此需要与 2D 卡死任务恢复配合。

### 非目标

第一版不实现按 source 的动态超时配置，不引入外部配置中心，也不做复杂的 per-crawler timeout 策略。

## 2D 卡死任务恢复

### 目标

- Worker 异常退出后，running 或 retrying 记录可能长期不结束
- 增加定时扫描任务
- 第一版根据 started_at 判断，started_at 为空时使用 created_at；updated_at 留作后续精度优化
- 把过期活动记录标记为 failed
- error_message 记录 stale crawl run 或 worker timeout

### 当前限制

当前 CrawlRun 没有 updated_at，因此不能直接知道 retrying 最后一次状态写入时间。

第一版明确采用以下判断方案：

- running：使用 started_at 判断是否过期
- retrying：优先使用 started_at 判断是否过期
- started_at 为空的 queued 或 retrying：使用 created_at 判断是否过期

这意味着 retrying 的过期判断不够精确：如果某条记录多次 retrying，但 started_at 保留首次执行时间，扫描任务可能更早把它判定为过期。为降低误判，第一版 retrying 的过期阈值应明显大于单次任务超时时间和 Celery retry delay。

后续更稳妥的方案是在 CrawlRun 增加 updated_at，所有状态变更都刷新 updated_at，扫描任务再基于 updated_at 判断过期。

### 扫描任务

新增定时扫描任务，例如 recover_stale_crawl_runs。它查询 queued、running、retrying 中超过阈值的记录，并将其标记为 failed。

建议 error_message：

- stale crawl run
- worker timeout

第一版只做保守恢复，不自动重新分发任务。

## 推荐实施顺序

1. 数据模型字段设计
2. 数据库迁移
3. 手动重试 Service
4. 手动重试 API
5. 同源活动任务查询
6. API 和重试防重复
7. Celery 超时
8. 卡死任务扫描
9. 单元测试
10. Docker E2E
11. 文档与分支合并

## 状态机约束

- failed 可以手动重试
- succeeded 不能手动重试
- queued 不能手动重试
- running 不能手动重试
- retrying 不能手动重试
- 原失败记录不修改
- 新记录从 queued 开始
- retry_of_run_id 指向原记录
- 手动重试失败后，可以再次对新失败记录发起重试
- 不实现复杂树形重试关系，只保留直接父记录

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

## 第一版冻结决策

### retry_of_run_id

- nullable
- 使用指向 crawl_runs.id 的自关联外键
- 增加索引
- 不做级联删除
- 第一版不必建立复杂 ORM relationship

### trigger_type

- 使用 String(32)
- 不使用数据库 Enum
- 第一版允许值：
  - api
  - manual
  - scheduled
  - direct
- api：POST /admin/crawl
- manual：手动重试
- scheduled：Celery Beat 和 crawl_all
- direct：Worker 为直接 crawl_source.delay() 自动建档时使用

### HTTP 409 活动任务冲突 detail

```json
{
  "code": "ACTIVE_CRAWL_RUN_EXISTS",
  "message": "active crawl run exists for source: <source>",
  "source": "<source>",
  "active_run_id": <run_id>
}
```

### HTTP 409 不可重试状态 detail

```json
{
  "code": "CRAWL_RUN_NOT_RETRYABLE",
  "message": "crawl run is not retryable: <run_id>",
  "run_id": <run_id>,
  "status": "<status>"
}
```

### Celery 第一版超时

- soft_time_limit：120 秒
- time_limit：150 秒
- SoftTimeLimitExceeded 不进入普通自动重试，尽量直接记录 failed 和 timeout
- 硬超时无法保证数据库回写，由 2D 卡死恢复兜底

### 卡死扫描第一版阈值

- 20 分钟
- running/retrying 优先根据 started_at
- started_at 为空的 queued/retrying 根据 created_at
- 第一版暂不增加 updated_at
- updated_at 保留为后续精度优化
