# ParseFlow 运维手册

本手册针对当前的**单进程、本地持久化**部署。任务记录能在重启后恢复，但任务仓库和 Worker 不是分布式实现；不要通过增加 Uvicorn Worker 或运行多个服务副本来扩容。

## 部署边界与配置

默认 Docker 资源限制为 2 CPU、4 GiB 内存；默认任务执行并发为 2，等待队列最多 100 个任务。队列满时解析提交会返回 HTTP 429。调整并发前先按下方步骤做压测，不要把 Worker 数直接当作吞吐承诺。

关键配置：

```dotenv
TASK_MAX_CONCURRENT_EXECUTIONS=2
TASK_QUEUE_MAX_SIZE=100
TASK_DEFAULT_TIMEOUT_SECONDS=1800
TASK_SHUTDOWN_GRACE_SECONDS=30
TASK_SHUTDOWN_CANCEL_GRACE_SECONDS=2
PARSEFLOW_STOP_GRACE_PERIOD=45s
```

Docker 停止宽限期应大于任务完成宽限期与取消清理宽限期之和，并额外留出至少 10 秒给 API 与容器进程退出。服务启动时会独占锁定 `DATA_DIR`；如果另一进程已使用同一目录，启动会失败。不要删除活动服务的数据目录中的 `.parseflow.lock` 文件。

## 健康检查与指标

- `GET /health`：进程存活，不代表任务 Worker 一定可用。
- `GET /ready`：检查任务仓库、可写存储、剩余空间、Worker、Skill Registry 和生产环境所需外部工具；未就绪时返回 503。
- `GET /metrics`：Prometheus 指标，包括 HTTP 请求、任务状态、队列深度、存储大小、任务排队等待与执行时长、队列拒绝及启动恢复计数。
- `GET /api/v1/metrics`：JSON 诊断视图。

`/metrics` 的任务状态与磁盘占用快照最多缓存 15 秒；任务执行直方图和拒绝/恢复计数是进程内累计值，服务重启后会归零。任务状态本身以磁盘任务记录为准。

建议告警：

- `/ready` 持续 503；
- `parseflow_task_queue_depth` 长期接近配置上限；
- `parseflow_task_queue_rejections_total{reason="full"}` 增长；
- 任务失败/中断计数持续增长；
- `/ready` 剩余空间接近硬阈值，或 Docker 容器持续触及内存限制。

## 关闭、重启与任务恢复

正常停止时，服务先停止接收新解析任务，再在 `TASK_SHUTDOWN_GRACE_SECONDS` 内等待正在执行的任务完成；超时后取消 Worker，并最多再等待 `TASK_SHUTDOWN_CANCEL_GRACE_SECONDS` 做清理。等待队列中的任务保留为 `queued`，下次启动时重新入队；超过第一段宽限期的活动任务会标记为 `interrupted`，需要通过任务 API 显式重试，不会自动重跑。Python 协程的取消是协作式的；若 Runner 吞掉取消且仍未退出，`stop()` 会在第二段宽限期后失败返回，服务保留 `DATA_DIR` 进程锁直到容器/进程退出，Compose 的 stop grace 最终会强制终止进程。发生此错误后不要在同一进程内尝试重启 ASGI lifespan；应让容器/进程退出，再启动新进程。

推荐操作：

```bash
docker compose stop
# 检查日志和任务状态后再启动
docker compose up -d
```

不要使用 `docker compose down -v` 做普通重启；该命令会删除任务与上传数据所在的持久卷。

## 队列满或任务没有进展

1. 查询 `/api/v1/metrics` 和 `/metrics`，区分运行中的任务与排队任务。
2. 429 表示等待队列已满；等待任务完成后再提交，或在压测验证后调整队列容量。
3. 检查 `/ready`、容器 CPU/内存和剩余磁盘空间；OCR、Office 转换和媒体处理的耗时/资源特征不同，应分别观察。
4. `interrupted` 任务先检查是否有未完成产物，再显式重试；不要把增加队列容量当作增加处理能力。

## 备份与恢复

停服或对数据卷做一致性快照后，备份整个 `DATA_DIR`，至少包含上传文件、任务 JSON、任务事件和 artifacts。OCR 模型缓存卷可单独备份；不备份时，首次 OCR 处理可能需要重新获取模型。

恢复时先恢复到隔离的新数据卷，使用**一个**服务进程启动，确认 `/ready` 正常、队列恢复结果符合预期并能下载既有 artifact，再切回生产流量。定期演练恢复，不要只验证备份文件存在。

## 容量验证

使用非敏感、具有代表性的 fixture；文本、普通 PDF、扫描 PDF/OCR、Office 文件分别测试，不要只用一个小文本文件推算整体能力。

```bash
# 低负载基线
uv run python scripts/load_test.py \
  --base-url http://127.0.0.1:8080 \
  --file ./fixtures/representative.pdf \
  --jobs 2 --submit-concurrency 2

# 观察排队、429 与资源变化；不要据此自动提高生产并发
uv run python scripts/load_test.py \
  --base-url http://127.0.0.1:8080 \
  --file ./fixtures/representative.pdf \
  --jobs 8 --submit-concurrency 4
```

每次记录版本、文件类型/大小、解析模式、任务数、终态分布、排队 P50/P95、执行 P50/P95、吞吐和容器峰值 CPU/内存。脚本只报告结果，不替项目承诺固定 RPS；生产并发上限应以目标部署环境中的重复测试结果为准。

## 发布与镜像校验

版本 Tag 触发 CI 全门禁；发布任务要求 Tag 去掉 `v` 后与 `pyproject.toml` 版本一致。BuildKit 将 SBOM/Provenance 与镜像一起发布，发布任务随后对 metadata 返回的同一 digest 执行容器 smoke 和 Trivy 扫描，确认版本 Tag 仍指向该 digest 后才签名。部署时优先使用经校验的 digest，而不是可变标签。

```bash
cosign verify \
  --certificate-identity="https://github.com/<owner>/<repo>/.github/workflows/ci.yml@refs/tags/v<version>" \
  --certificate-oidc-issuer=https://token.actions.githubusercontent.com \
  ghcr.io/<owner>/<repo>@sha256:<digest>
```

## 本轮本地负载 smoke

以隔离临时数据目录启动未容器化本地 API，使用 860 字节纯文本 fixture、8 个任务和 4 路提交并发：8/8 succeeded，吞吐 50.073 tasks/s，排队 P50/P95 为 0.004/0.015 秒，执行 P50/P95 为 0.013/0.039 秒。该单次文本 smoke 只用于验证脚本与任务链路可运行；不代表容器、OCR、Office 或目标硬件容量，调高生产并发前仍需按上文在目标部署环境重复测量。
