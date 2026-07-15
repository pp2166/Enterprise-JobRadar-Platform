# CrawlRun 真实端到端验收

## 验收环境

- Docker Compose
- FastAPI
- PostgreSQL 16
- Redis 7
- Celery 5.6.3
- RemoteOK
- 当前功能分支：feat/crawl-run-records
- 代码冻结点：d5a25af

## 验收链路

POST /admin/crawl
→ PostgreSQL 创建 queued CrawlRun
→ Redis
→ Celery Worker
→ running
→ RemoteOK 抓取
→ 数据标准化和写入
→ succeeded
→ GET 详情和列表查询

## 实际验收结果

- run_id：1
- source：remoteok
- status：succeeded
- attempt_count：1
- received：100
- inserted：5
- updated：95
- duplicates：0
- error_message：null
- created_at、started_at、finished_at 均非空
- Worker 实际任务耗时约 2.81 秒

说明 inserted=5、updated=95 是因为数据库中此前已经存在 RemoteOK 数据，不是异常。

## API 验收

已验证：

- POST /admin/crawl
- GET /admin/crawl-runs/{run_id}
- GET /admin/crawl-runs
- source 筛选
- status 筛选
- source + status 联合筛选
- 分页返回结构

## 状态闭环

已支持：

- queued
- running
- retrying
- succeeded
- failed

## 触发方式

均能创建或复用 CrawlRun：

- 管理 API 手动触发
- Celery Beat
- crawl_all
- crawl_source.delay

其中管理 API 已完成真实 Docker E2E；Celery Beat、crawl_all 和 crawl_source.delay 的自动建档能力由自动化测试覆盖，尚未分别进行独立的真实 Docker E2E。

## 测试结果

- 全量 pytest：210 passed
- API 专项：51 passed
- Ruff：通过
- git diff --check：通过
- 真实 Docker E2E：通过

## 已知非阻塞项

- Celery Worker 在容器内以 root 用户启动，会产生 SecurityWarning
- WeWorkRemotely 尚未完成真实 E2E
- 失败重试的真实网络异常场景尚未人为注入验证
- Worker 突然退出后的运行记录恢复属于后续 Failure Task Management 范围

## 验收结论

CrawlRun 运行记录、Worker 生命周期回写、定时任务自动建档、列表与详情查询均已完成，可进入分支合并阶段。
