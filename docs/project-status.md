# JobRadar 项目状态

## 项目定位

JobRadar 是一个基于 FastAPI、Celery、Redis 和 PostgreSQL 的分布式岗位采集与任务可观测平台。

项目基于开源项目 Talash 进行二次开发，保留原项目 MIT License，并在后续 README 中明确说明上游来源。

## 当前仓库

- 本地目录：D:\CrawlerOps\upstream-talash
- 当前分支：main
- 当前冻结点：33aff5a
- origin：https://github.com/pp2166/Enterprise-JobRadar-Platform
- upstream：https://github.com/iamrahulroyy/talash

## 已完成

- 安装项目依赖
- 运行上游自动化测试
- 修复 Windows Docker 挂载覆盖 Linux 虚拟环境的问题
- 修复 asyncpg 无法一次执行多条初始化 SQL 的问题
- 关闭 Docker API 的 uvicorn reload，避免目录监控权限错误
- 启动 PostgreSQL、Redis、FastAPI 和 Celery Worker
- 验证 FastAPI 健康接口
- 验证 PostgreSQL 查询接口
- 验证 Redis 与 Celery 任务分发
- 真实采集 RemoteOK 岗位数据
- 将修改推送到自己的 GitHub 仓库

## CrawlRun 第一阶段

CrawlRun 第一阶段已完成。

CrawlRun 第一阶段已经合并并推送至 origin/main。

当前已支持：

- 创建 CrawlRun 运行记录
- queued、running、retrying、succeeded、failed 状态流转
- Worker 根据 Celery task id 回写生命周期状态
- Celery Beat 自动建档
- crawl_all 自动建档
- crawl_source.delay 自动建档
- 管理 API 手动触发时创建并复用运行记录
- 列表查询
- 详情查询
- source 筛选
- status 筛选
- source + status 联合筛选
- 分页查询

## 测试与真实验收

- 全量 pytest：210 passed
- API 专项：51 passed
- Ruff：通过
- git diff --check：通过
- Docker RemoteOK E2E：通过
- 当前冻结点：33aff5a

Docker RemoteOK E2E 验收结果：

- run_id：1
- source：remoteok
- status：succeeded
- attempt_count：1
- received：100
- inserted：5
- updated：95
- duplicates：0
- Worker 实际任务耗时约 2.81 秒

说明 inserted=5、updated=95 是因为数据库中此前已经存在 RemoteOK 数据，不是异常。

## 下一阶段：Failure Task Management

下一阶段聚焦失败任务管理：

- 手动重试
- 同源任务防重
- 超时
- Worker 异常退出恢复

## 已知非阻塞项

- Celery Worker 当前在容器内以 root 用户启动，会产生 SecurityWarning
- WeWorkRemotely 尚未完成真实 E2E
- 失败重试的真实网络异常场景尚未人为注入验证
- Worker 突然退出后的运行记录恢复属于后续 Failure Task Management 范围

## 暂时不做

- Agent
- RAG
- Kafka
- Flink
- Kubernetes
- 微服务化

## 当前结论

CrawlRun 运行记录、Worker 生命周期回写、定时任务自动建档、列表与详情查询均已完成，可进入分支合并阶段。
