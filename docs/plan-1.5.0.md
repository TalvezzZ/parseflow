# ParseFlow 1.5.0 技术方案：运行可靠性、容量验证与供应链发布

## 1. 目标

在保持当前单机、单进程任务架构的前提下，让任务提交/关闭/恢复行为可预测，让运维有足够指标和容量基线，并让版本镜像可追溯、可验证。v1.5.0 不通过盲目调高 Worker 数来承诺吞吐，也不引入分布式队列。

## 2. 当前已有基础

- CI 已执行后端 pytest、前端单测/生产构建、真实浏览器 E2E 和 Docker runtime smoke。
- 任务 JSON 持久化；启动时恢复 queued 任务，并将遗留活动任务标为 interrupted。
- `/health`、`/ready`、`/metrics`、结构化日志和存储清理已有基础实现。
- Docker 使用单个 Uvicorn Worker，限制 CPU/内存并默认 loopback 暴露。

## 3. 交付范围

### 3.1 供应链与发布

- GitHub Actions 使用不可变 commit SHA；Python CI 按 `uv.lock` 冻结安装。
- 对源依赖和构建镜像执行高/严重级别漏洞扫描，忽略暂不可修复的条目。
- 版本 Tag 必须与 `pyproject.toml` 版本一致，且所有 CI 门禁成功后才发布 GHCR 镜像。
- BuildKit 为镜像生成 SBOM/Provenance attestation；发布后对从 BuildKit metadata 得到的**同一 digest**执行容器 smoke、Trivy 镜像扫描和 Sigstore keyless 签名/验证，并核对版本 Tag 仍指向该 digest。
- Dependabot 定期检查 Actions、Python、npm 和 Docker 依赖。

### 3.2 单进程任务生命周期

- 使用启动锁避免无 ASGI lifespan 预热时并发首次提交重复创建 Worker。
- 队列准入检查、持久化和入队在单进程 admission lock 下完成，防止并发请求突破等待队列上限。
- 用数据目录操作系统进程锁拒绝多个服务进程共享同一 `DATA_DIR`。
- `TASK_SHUTDOWN_GRACE_SECONDS` 生效：停止接单、等待活动任务完成；超时后取消 Worker 并给有限清理时间，活动任务标记为可显式重试的 `interrupted`，等待中的 queued 记录保留供下次启动恢复。若 Runner 吞掉取消，`stop()` 有界报错且保留数据目录锁直至进程退出。
- Docker `stop_grace_period` 应大于任务关闭宽限期与取消清理宽限期之和，额外留出至少 10 秒给进程退出。
- 移除无运行时语义的 `TASK_QUEUE_ENABLED` 示例配置，避免出现“设置了但不生效”的选项。

### 3.3 指标与容量验证

- Prometheus 新增固定桶 `parseflow_task_queue_wait_seconds` 与按有限终态标签区分的 `parseflow_task_duration_seconds` Histogram。
- 新增队列拒绝及启动恢复计数；所有 label 使用有限枚举，不包含 task/file ID、文件名或错误消息；HTTP method 超出白名单时折叠为 `OTHER`。
- `/metrics` 的任务状态和数据目录大小扫描移至线程并短时缓存；缓存过期时每个 event loop 仅运行一个刷新任务，避免并发抓取导致目录扫描 stampede。
- 新增手动容量脚本，向真实 API 提交指定 fixture，统计终态分布、吞吐、排队 P50/P95、执行 P50/P95、429 和超时。
- 不在 CI 中设易抖动的性能阈值；基准需在目标 Docker 资源约束和代表性格式下重复测量。

### 3.4 运维手册

- 记录单进程边界、健康/ready/metrics 解释、队列满、任务 interrupted、优雅关闭、数据卷备份/恢复、容量验证和签名镜像校验操作。
- 恢复演练至少验证一次：数据卷还原、任务记录恢复、artifact 下载和 `/ready` 正常。

## 4. 验收标准

- 并发提交风暴下运行任务数不超过配置 Worker 数、等待任务数不超过队列上限，超额提交收到 429。
- 多个 manager 在不同异步任务中启动同一数据目录时，只有一个能获取进程锁。
- 优雅关闭测试覆盖活动任务可在宽限期内完成、Runner 忽略取消时有界报错且保留锁语义、关闭等待进行中的恢复、超时任务标记 interrupted、队列任务重启后恢复。
- 指标测试验证 Histogram 桶累计、HTTP method 和任务状态固定低基数 label、并发 cache miss 仅触发一次扫描、正确 Prometheus exposition 和缓存刷新。
- 完整 pytest、前端单测/构建、浏览器 E2E、Docker runtime smoke 和依赖/镜像扫描通过。
- 版本 Tag 不匹配项目版本或任一门禁失败时，不发布镜像。
- 发布镜像包含 SBOM/Provenance，签名验证成功；运维手册中的容量脚本和恢复步骤有实际验证记录。

## 5. 非目标

- 不在本版本增加 PostgreSQL/Redis 分布式队列、对象存储或多 Worker 执行。
- 不把压测脚本输出解释为通用 RPS 承诺；容量数字只适用于明确记录的硬件/文件类型/解析模式。
- 不把同步线程超时误称为强制终止；外部解析进程/线程的取消边界需继续独立验证和记录。

## 6. 当前执行状态

代码与文档已落地；本轮当前工作区后端 `uv run pytest` 重跑为 211 passed、1 skipped，完整浏览器 E2E（真实 API 主流程及 PaddleOCR 校对）为 2 passed，前端单测 18 passed 且生产构建通过。首次全量测试中的 Office XLS 集成测试偶发 interrupted，单模块与全套重跑均通过。隔离本地 API 的 8 个纯文本容量 smoke 全部成功，仅用于工具链检查，不作为容量承诺。本机 Docker daemon 不可用，Docker runtime smoke、Linux CI 的 Trivy/精确 digest 发布验证及目标环境容量与恢复演练仍待执行。
