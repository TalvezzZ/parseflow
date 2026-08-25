# ParseFlow 0.8.1 技术方案：安全与部署加固

## 1. 目标

`0.8.1` 加固 `0.8.0` 的文件任务架构，不新增破坏性业务 API。核心是建立统一解析预算、容器最小权限、存储维护和真实 Docker 发布门禁。

## 2. 威胁模型

不可信输入包括文件名、MIME、文件内容、压缩包内部路径和解析器生成的 artifact。重点防范：

- ZIP/OOXML 解压炸弹、超大对象和病态结构。
- PDF/PPT/图片渲染造成 CPU/内存耗尽。
- XML 实体、深度和节点攻击。
- FFmpeg/LibreOffice 处理恶意文件。
- 路径穿越、符号链接和跨任务读取。
- 任务/结果无限增长导致磁盘耗尽。
- 容器被攻破后的权限、网络和持久化范围扩大。

安全承诺限定为单机/可信内网；本版本不提供多租户隔离。

## 3. 统一解析预算

新增 `app/security/budgets.py`：

```python
class ParseBudget(BaseModel):
    max_input_bytes: int
    max_output_bytes: int
    timeout_seconds: int
    max_archive_entries: int | None
    max_uncompressed_bytes: int | None
    max_pages: int | None
    max_nodes: int | None
    max_pixels: int | None
```

`BudgetRegistry` 按 Skill/Provider 返回预算，Context 传递截止时间和取消 token。统一错误码：

```text
input_too_large
archive_entry_limit_exceeded
archive_uncompressed_limit_exceeded
page_limit_exceeded
node_limit_exceeded
pixel_limit_exceeded
output_limit_exceeded
execution_timeout
resource_budget_exceeded
```

错误不得触发原文 fallback。

## 4. 各格式控制

- PDF：页数、对象/页面渲染像素、OCR 总像素和输出字符数。
- DOCX/XLSX/PPTX：先扫描 ZIP central directory，拒绝绝对路径、`..`、超条目、超解压总量和异常压缩比。
- Excel：保留语义单元格、行列和 sheet 限制。
- PPTX：限制 slide、shape、图片数和单个媒体大小。
- XML/HTML：限制输入、深度、节点、属性和输出字符；继续使用 defusedxml。
- 图片：在完整解码前读取 dimensions 并限制总像素。
- LibreOffice/FFmpeg：超时、输出目录总量、单产物大小和子进程退出清理。

所有 Provider 在写 artifact 时经 `ArtifactWriter` 计数，而非直接写任意目录。

## 5. 子进程治理

新增 `app/runtime/processes.py`：

- 使用参数数组，禁止 shell。
- 独立 process group/session。
- 捕获并限制 stdout/stderr 大小。
- deadline 到期 terminate，grace 后 kill 整组进程。
- 取消任务时复用相同终止路径。
- 工作目录为任务专属临时目录。
- 环境变量使用 allowlist，不继承密钥。

Linux 可选使用 `resource.setrlimit` 或容器 cgroup 约束 CPU、地址空间和文件大小；不可用时保留 warning 和测试替身。

## 6. 容器加固

Compose 目标：

```yaml
read_only: true
cap_drop: [ALL]
security_opt:
  - no-new-privileges:true
tmpfs:
  - /tmp:size=1g,mode=1777
pids_limit: 256
mem_limit: 4g
cpus: 2.0
```

仅 `/data` 和模型缓存可写。应用和 Nginx 使用非 root 用户；Nginx 使用高位内部端口。模型下载应在构建或显式初始化阶段完成，运行期下载策略写入文档。

Nginx 限制：请求体大小、上传速率、并发连接、超时和响应安全头。公网身份认证仍由受支持的外部代理承担。

出站网络：默认不宣称完全禁网；记录 OCR 模型下载需求。稳定部署建议预置模型后限制运行容器出站。

## 7. 存储维护

新增 `StorageJanitor`：

- 基于任务终态和 TTL 清理任务、文件和 artifact。
- 活动任务、锁定任务和共享引用不得删除。
- 清理 `.tmp`、过期锁和孤立目录。
- 删除采用任务级锁和两阶段 tombstone。
- 达到软阈值记录告警，达到硬阈值拒绝上传。
- 每次清理记录数量、字节和失败原因。

提供只读检查命令：

```text
parseflow storage inspect
parseflow storage verify
```

修复/清理必须显式指定，不能在检查中静默删除。

## 8. 健康与就绪

- `/health`：进程存活，不检查昂贵依赖。
- `/ready`：存储可读写、任务仓库可加载、Worker 已启动、剩余磁盘高于硬阈值。
- 可选 `/api/v1/capabilities`：报告 LibreOffice、FFmpeg、OCR 是否可用，不暴露路径和版本敏感信息。

Docker healthcheck 使用 `/ready`。

## 9. CI 设计

新增 jobs：

1. `backend-unit`：Python 3.12、全量测试。
2. `frontend-build`：锁定 Node 版本、`npm ci`、构建。
3. `security-static`：依赖审计、secret scan、Dockerfile lint。
4. `docker-amd64-smoke`：构建 full 镜像并启动。
5. `hostile-fixtures`：预算和恶意输入测试。

Docker smoke：

```text
启动 → /ready → Web 首页
→ 上传 TXT/PDF → 轮询任务 → 下载 artifact
→ MCP 未授权拒绝 → 授权 initialize 成功
→ 匿名请求不能因 Nginx 注入密钥获得权限
→ 重启容器 → terminal 任务仍可查
```

工具依赖测试在含 LibreOffice/FFmpeg 的 full image 中不得 skip；OCR 实际模型测试可分 nightly，普通 CI 使用 Provider mock + 镜像导入 smoke。

## 10. 测试矩阵

- 每种预算边界：limit-1、limit、limit+1。
- ZIP path traversal、异常压缩比、条目数和总解压量。
- 深层 XML/HTML、超大图片维度和 PDF 页数。
- 子进程超时、取消、僵尸进程和超量输出。
- 磁盘软/硬阈值、TTL、孤立目录和并发清理。
- read-only root 下完整解析链路。
- 容器 capability、运行用户、可写挂载和端口检查。
- `/health` 与 `/ready` 在依赖故障时语义正确。

## 11. 实施顺序

1. 定义预算模型、错误码和 ArtifactWriter。
2. 为 ZIP/XML/图片/PDF 增加前置检查。
3. 统一 LibreOffice/FFmpeg 子进程管理。
4. 实现 StorageJanitor 和磁盘阈值。
5. 拆分 health/readiness/capabilities。
6. 加固 Dockerfile、Compose 和 Nginx。
7. 增加 hostile fixtures 与 Docker AMD64 smoke CI。
8. 更新部署、安全和容量规划文档。

## 12. 完成定义

- 所有公开格式有输入、输出、时间和结构预算。
- 预算错误不会被 fallback 绕过。
- 外部进程可超时、取消且无残留。
- 容器以非 root、最小 capability 和受控可写目录运行。
- 存储可自动清理并在容量不足前拒绝新任务。
- Docker AMD64 完整镜像在 CI 中真实启动并完成解析/MCP smoke。
