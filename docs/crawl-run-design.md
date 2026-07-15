# CrawlRun 采集运行记录设计

## 1. 要解决的问题

当前系统只能触发 Celery 采集任务，但无法通过 API 查看：

- 某次任务是否正在运行
- 任务是否成功
- 任务采集了多少数据
- 任务为什么失败
- Celery task_id 是什么
- 任务什么时候开始和结束

因此新增 CrawlRun 记录每一次采集任务。

## 2. 数据表

表名：crawl_runs

字段：

- id：主键
- source：采集来源，例如 remoteok
- status：queued、running、retrying、succeeded、failed
- celery_task_id：Celery 任务 ID
- attempt_count：已经执行的次数
- received：采集器返回的数据数量
- inserted：新增数量
- updated：更新数量
- duplicates：重复数量
- error_message：最近一次错误信息
- created_at：任务创建时间
- started_at：任务开始时间
- finished_at：任务结束时间

任务耗时不单独保存，通过 finished_at - started_at 计算。

## 3. 状态流转

正常流程：

queued
→ running
→ succeeded

自动重试流程：

queued
→ running
→ retrying
→ running
→ succeeded

最终失败流程：

queued
→ running
→ retrying
→ failed

## 4. 请求链路

用户调用 POST /admin/crawl
→ FastAPI 校验 source
→ 为每个来源生成 celery_task_id
→ 创建 CrawlRun，状态为 queued、attempt_count 为 0
→ 提交数据库
→ 使用预先生成的 task_id 派发 Celery 任务
→ Worker 根据 run_id 将状态改为 running
→ attempt_count 加 1
→ 执行爬虫
→ 保存 received、inserted、updated、duplicates
→ 状态改为 succeeded
→ 如果可以继续重试，状态改为 retrying
→ 如果达到最大重试次数，状态改为 failed
→ 如果任务派发失败，直接改为 failed 并保存错误

## 5. API

### 触发采集

POST /admin/crawl

返回：

- dispatched：已派发的来源名称
- runs：本次创建的运行记录列表

每个运行记录包含：

- run_id
- source
- status
- celery_task_id

### 查询运行记录

GET /admin/crawl-runs

支持：

- source
- status
- page
- page_size

### 查询单次运行

GET /admin/crawl-runs/{run_id}

## 6. 本阶段暂时不做

- 手动重试接口
- 取消正在运行的任务
- 删除历史记录
- WebSocket 实时进度
- Celery Flower 页面
- 来源启用和停用
- 任务运行锁

这些内容放到后续失败任务管理阶段。

## 7. 预计修改文件

- app/models.py
- app/schemas.py
- app/services/crawl_runs.py
- app/api/admin.py
- app/workers/tasks.py
- tests/test_crawl_runs.py
- docs/project-status.md

## 8. 验收标准

- 调用 POST /admin/crawl 后数据库生成 queued 记录
- 返回 run_id 和 celery_task_id
- Worker 开始时状态变为 running
- 成功后状态变为 succeeded
- received、inserted、updated、duplicates 正确写入
- 最终失败时状态变为 failed，并保存错误信息
- 可以分页查询运行记录
- 可以根据 ID 查询单次运行记录
- 未知来源仍返回 HTTP 400，且不创建记录
- 原有 160 个测试继续通过
- 新增功能测试全部通过
- RemoteOK 真实采集验收通过