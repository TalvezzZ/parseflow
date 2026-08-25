# ParseFlow 1.0.0 技术方案：稳定契约与自动发布

## 1. 目标

`1.0.0` 不以新增格式为目标，而是冻结 REST、远程 MCP、任务状态、持久化 schema 和发布流程。内部 Provider 仍可演进，但公开契约必须遵守语义化版本策略。

不作为 1.0 前置：多租户、分布式队列、对象存储、ASR、知识加工。

## 2. REST v1 契约

所有公开业务接口位于 `/api/v1`：

```text
POST /api/v1/tasks/parse
GET  /api/v1/tasks
GET  /api/v1/tasks/{task_id}
GET  /api/v1/tasks/{task_id}/events
POST /api/v1/tasks/{task_id}/cancel
POST /api/v1/tasks/{task_id}/retry
DELETE /api/v1/tasks/{task_id}
GET  /api/v1/files/{file_id}
GET  /api/v1/files/{file_id}/content
GET  /api/v1/tasks/{task_id}/artifacts
GET  /api/v1/tasks/{task_id}/artifacts/{artifact_id}
```

明确禁止公开字段：

```text
path, output_dir, callback, source_path, target_path, artifact_path
```

Pydantic 公开模型使用 `extra="forbid"`。内部模型与公开 DTO 不继承，避免路径字段被意外序列化。

## 3. 统一响应和错误

成功：

```json
{
  "data": {},
  "meta": {"api_version": "v1", "request_id": "req_..."}
}
```

失败：

```json
{
  "error": {
    "code": "task_not_found",
    "message": "Task was not found.",
    "retryable": false,
    "details": {}
  },
  "meta": {"api_version": "v1", "request_id": "req_..."}
}
```

冻结内容：HTTP status、error code、必填字段、字段类型、状态含义、ID 前缀和 UTC RFC3339 时间格式。message 可改善或本地化，但 machine semantics 不变。

错误注册表集中在 `app/contracts/errors.py`，测试确保每个 code 只有一个默认 HTTP 映射和文档条目。

## 4. 任务语义和幂等

稳定状态：

```text
queued, planning, running,
succeeded, partial, failed,
cancelled, interrupted
```

- 重试始终创建新 task_id 并记录 `retry_of`。
- 原任务记录不可被重试覆盖。
- 不自动重跑 interrupted。
- 删除先 tombstone，物理清理由保留策略执行。

`POST /tasks/parse` 支持 `Idempotency-Key`：相同 key + 相同内容/参数返回原任务；相同 key + 不同内容返回 `409 idempotency_conflict`。只持久化 key hash，不记录明文；默认窗口 24 小时。

## 5. Remote MCP v1

远程工具固定为：

```text
list_skills
preview_parse_plan
submit_file_id
get_task
cancel_task
list_artifacts
```

所有输入只使用 file_id/task_id/artifact_id 和有限业务元数据。返回包含 `contract_version: "1.0"`。工具名称、必填输入、输入类型或结果主结构变化属于 breaking change。

本地 stdio MCP 是独立 server/profile，路径工具显式以 `local_` 命名，不注册到远程实例。

## 6. 规范文件

提交机器可读规范：

```text
schemas/task-record-v1.json
schemas/file-record-v1.json
schemas/artifact-manifest-v1.json
schemas/mcp-remote-v1.json
openapi/openapi-v1.json
```

CI 从应用生成规范，与仓库快照比较。规范变化必须人工审查并按兼容性规则分类。

## 7. 持久化 schema 与迁移

任务、文件和 manifest 都包含整数 `schema_version`，与应用版本解耦。

```text
app/storage/schema.py
app/storage/migrations/registry.py
app/storage/migrations/v0_to_v1.py
app/storage/recovery.py
```

迁移要求：

1. 顺序、确定性、可重复执行。
2. 写入前备份原记录。
3. 使用原子替换。
4. 每个迁移有旧记录、已迁移、损坏、未来版本 fixture。
5. 损坏记录 quarantine，不阻止服务启动。
6. 未知未来 schema 不自动修改。
7. 提供 dry-run 和报告。

运维命令：

```text
parseflow-storage verify
parseflow-storage migrate --dry-run
parseflow-storage backup
parseflow-storage restore
```

## 8. 兼容性政策

### SemVer

- Patch：不改变公开契约的 bug/security/doc 修复。
- Minor：新增可选字段、工具、格式、warning/artifact kind。
- Major：删除/重命名、类型变化、语义变化、可选改必填。

### v1.x 保证

- 不删除/重命名 REST endpoint 和远程 MCP tool。
- 不删除必填响应字段，不改变 JSON 类型。
- 不改变稳定 status/error code 和 ID 前缀含义。
- 可增加可选字段；客户端必须忽略未知可选字段。
- 不允许重新引入服务器路径。

### 弃用

OpenAPI/MCP 标记 deprecated，提供迁移文档和 changelog，维持至少一个 minor 且不少于 90 天；仅在 major 移除。紧急安全撤回必须发布 advisory 和替代方案。

## 9. 契约测试

```text
tests/contracts/
  test_openapi_snapshot.py
  test_rest_payloads.py
  test_error_registry.py
  test_mcp_remote_tools.py
  test_schema_migrations.py
  fixtures/
  snapshots/
```

断言：

- 所有公开路径位于 `/api/v1`。
- OpenAPI/MCP schema 无禁止字段。
- success/error envelope 一致。
- 状态、错误、ID 和时间符合规范。
- MCP tool list 和输入 schema 快照稳定。
- migration 幂等，损坏记录隔离，未来 schema 保留。
- 每个支持格式具有成功、无效、超预算和恶意输入 fixture。

## 10. 版本单一来源

新增 `app/version.py` 或从 package metadata 读取版本。FastAPI、MCP、Docker label 和前端构建注入同一 release version。`scripts/release/check_version.py` 验证：

```text
pyproject.toml
uv.lock
web package/branding
FastAPI
MCP
Docker OCI labels
CHANGELOG
Git tag
```

任一不一致阻止发布。

## 11. CI 与 Release

工作流：

```text
.github/workflows/ci.yml
.github/workflows/release.yml
.github/workflows/verify-release.yml
.github/workflows/nightly-security.yml
```

PR 门禁：后端、前端、契约、MCP、AMD64 Docker、E2E、容器策略、依赖安全和文档一致性。

Tag `v1.0.0` 发布步骤：

1. 校验 tag 与全部版本来源。
2. 重跑完整门禁。
3. 构建 Python wheel/sdist，并在干净环境验证入口。
4. 构建 `linux/amd64` Docker 镜像。
5. 生成 SPDX/CycloneDX SBOM、依赖和镜像扫描报告、provenance。
6. 高危/严重漏洞无批准例外则阻止发布。
7. 使用 GitHub OIDC + Cosign keyless 签名镜像、SBOM 和 provenance。
8. 发布 GHCR 精确版本、minor 和 latest tag。
9. 创建 GitHub Release，附 changelog、迁移指南、checksums、SBOM、digest 和验证命令。
10. 从 registry 按 digest 拉取并执行独立 smoke。

Release 权限最小化；PR 只读，publish 使用受保护 Environment 和人工批准。

## 12. 漏洞例外

`security/allowlist.yaml` 每项必须包含 CVE、组件、理由、批准人、跟踪 issue 和到期日。到期例外自动使 CI 失败；禁止无期限忽略高危漏洞。

## 13. 文档交付

```text
docs/api/v1.md
docs/mcp.md
docs/migration/v0-to-v1.md
docs/deployment.md
docs/backup-restore.md
docs/release.md
SECURITY.md
CHANGELOG.md
LICENSE
```

覆盖安装、认证边界、轮询、错误码、备份恢复、升级、回滚和安全报告。

## 14. 实施顺序

1. 建立 contracts/errors/version 模块。
2. 生成并冻结 OpenAPI、MCP 和持久化 schema。
3. 实现迁移 registry、verify/migrate/backup/restore。
4. 加入幂等和最终任务语义。
5. 建立契约和兼容性测试。
6. 统一版本来源。
7. 完成 v0→v1 迁移、部署、安全和运维文档。
8. 建立 tag release、SBOM、扫描、签名和发布后验证。
9. 发布 RC，执行恢复/升级/回滚演练。
10. 满足全部门禁后发布 `v1.0.0`。

## 15. 完成定义

- REST/OpenAPI、Remote MCP、schema 和文档一致且有快照保护。
- 公开接口只接受文件流和不透明 ID，绝不接受/返回服务器路径。
- 数据迁移、quarantine、备份、恢复和回滚经过真实演练。
- v1.x 兼容/弃用政策已发布。
- Release 自动产出版本化镜像、SBOM、扫描报告、provenance 和签名。
- 发布镜像按 digest 拉取后通过独立 E2E。
- 项目所有版本字符串一致，无未批准高危/严重漏洞。
