# Parse Agent 迭代版本计划

## 1. 当前基线

当前项目版本为 `0.3.0`，已具备以下经过真实 API 集成测试验证的能力：

- 文本型 PDF：`pdf.parse`，使用 `pdf-inspector`。
- DOCX：`word.parse`，使用 Mammoth，输出 HTML、Markdown、纯文本、表格与图片 artifact。
- Office 转换：`office.convert`，使用 LibreOffice。
- 已验证的真实链路：PDF 解析、DOCX 解析、DOCX → PDF、DOC → DOCX → 解析。

当前全量测试基线：`16 passed`。

## 2. 产品演进原则

```text
先做可验证的文件解析闭环。
再做格式转换后的自动编排。
再覆盖 OCR 与复杂文件。
随后建设任务化、存储和 Agent 编排能力；知识加工暂不纳入当前计划。
```

每一版本均应满足：

- 有明确输入格式、输出模型与错误码。
- 有真实文件和 API 集成测试，不只依赖 Mock。
- 保留实际 Provider、转换步骤、警告和质量指标。
- 不将未实现能力伪装为成功结果。

## 3. 版本计划总览

| 版本 | 主题 | 主要交付 | 完成判定 |
|---|---|---|---|
| `0.3.1` | 当前能力加固 | 真实 fixtures、API 测试、错误语义与 artifact 约定 | 三条既有链路可重复真实验收 |
| `0.4.0` | Excel/PPT 解析 | `excel.parse`、`ppt.parse`、旧格式自动预处理 | ✅ 已完成：原生与 `.xls` 回转真实测试通过；PPTX 原生链路已验证 |
| `0.4.1` | Office 工作流与 MCP | 统一 PipelineResult、Pipeline API、MCP Tools | ✅ 已完成：转换/解析串联与 MCP 工具测试通过 |
| `0.4.2` | 音视频预处理 MVP | FFprobe 探测、FFmpeg 音轨提取/标准化、字幕导出 | 音频/视频产出可供后续 ASR 消费的 artifact |
| `0.4.3` | Excel 解析加固 | OOXML ZIP 预检、语义单元格流式扫描、样式污染防护 | ✅ 已完成：污染 Used Range 真实 API 回归通过 |
| `0.5.0` | 轻量任务化服务 | 内存 FIFO 队列、并发控制、任务 API、callback | ✅ 已完成：单进程异步任务与回调测试通过 |
| `0.5.1` | 无模型平台加固 | 文件上传、artifact 下载、可观测性、轻量格式解析 | ✅ 已完成：上传/安全/文本格式真实 API 测试通过 |
| `0.5.2` | XML/RTF 直接解析 | 安全 XML 结构提取、RTF 正文文本提取 | ✅ 已完成：XML/RTF 真实 API 测试通过 |
| `0.5.3` | RTF 智能路由 | 复杂度评分、质量 fallback、RTF → DOCX → Word | ✅ 已完成：直接/转换双路径真实 API 测试通过 |
| `0.6.0` | 无模型自动规划 | 文件即任务、RuleBasedPlanner、任务内 ParsePlan、MCP 计划工具 | ✅ 已完成：单文件自动路由与计划执行测试通过 |
| `0.7.0` | PDF OCR（后置） | OCR Provider、扫描/混合 PDF 路由与质量校验 | 非文本 PDF 可真实提取内容 |
| — | 知识加工 | 不纳入当前版本计划 | 待业务需求明确后重新立项 |

## 4. 0.3.1：当前能力加固

### 目标

将现有 PDF、DOCX、Office 转换能力从“实现存在”提升到“可稳定验收”。

### 工作项

- 固化真实 PDF、DOCX、Office 测试 fixture。
- 增加不存在文件、错误后缀、Provider 不存在、LibreOffice 不可用、超时等 API 测试。
- 定义 artifact 输出目录、清理与保留策略。
- 统一 `quality_insufficient`、`provider_unavailable`、`conversion_timeout` 等错误语义。
- 建立 CI 基线：`uv run pytest`。

### 验收

- PDF、DOCX、DOC → DOCX、DOCX → PDF 均通过真实 API 测试。
- 失败路径有稳定错误码。
- 全量测试持续通过。

## 5. 0.4.0：Excel 与 PowerPoint 解析

完整方案见：[方案 0.4.0](plan-0.4.0.md)。

### 目标

新增：

```text
excel.parse
ppt.parse
```

并支持：

```text
.xls / .xlsm → .xlsx → excel.parse
.ppt → .pptx → ppt.parse
```

### 里程碑 A：Excel

- 接入 `openpyxl` Provider。
- 输出 Sheet、稀疏单元格、连续表格区域、公式、合并范围、图片 artifact、Markdown 与纯文本。
- 实施 ZIP/OOXML 预检、样式污染防护、解析预算和 `partial` 截断。
- 真实验证 `.xlsx` 和 `.xls → .xlsx → 解析`。

### 里程碑 B：PowerPoint

- 接入 `python-pptx` Provider。
- 提取幻灯片、文本、表格、图片与 Group Shape。
- 输出页级 `DocumentPage`、Markdown、纯文本和图片 artifact。
- 真实验证 `.pptx` 与 `.ppt → .pptx → 解析`。

### 验收

- `/api/v1/skills` 返回两个新 Skill。
- Excel 和 PPT 的直接格式、旧格式自动转换链路均可运行。
- 大范围样式污染 Excel 不会导致无限扫描或内存耗尽。
- 新增真实集成测试与既有测试全部通过。

## 6. 0.4.1：Office 转换后自动解析工作流

### 目标

将“转换”和“解析”从两个独立操作扩展为可追踪的多步 Pipeline。

```text
源文件
  → 转换（可选）
  → 对应解析 Skill
  → PipelineResult
```

### 工作项

- 抽取 `OfficeConversionService`，供 `office.convert` 与解析 Skill 共同复用。
- 定义 `PipelineResult`，包含每步输入、输出、耗时、警告、错误和 Provider。
- 支持显式选择“只转换”或“转换后解析”。
- 统一 artifact 路径、生命周期与 provenance。

### 验收

- `.doc`、`.xls`、`.ppt` 均能以单次请求完成转换加解析。
- 任一步失败时能定位失败阶段和原因。
- 结果包含完整的转换与解析执行记录。

## 7. 0.5.0：轻量任务化服务

完整方案见：[方案 0.5.0](plan-0.5.0.md)。

### 已交付

- 单进程内存 `asyncio.Queue` FIFO 队列。
- `TASK_MAX_CONCURRENT_EXECUTIONS` 最大执行任务数配置。
- `TASK_QUEUE_MAX_SIZE` 等待队列容量与友好 `429` 拒绝。
- `skill.execute` 与 `office.parse_pipeline` 异步任务。
- 任务提交、查询、取消排队任务和队列指标 API。
- `data_id` 业务标识与可选 `callback` 终态通知。
- HTTP 200 回调成功判定；无签名、无重试，回调失败不改变任务执行结果。
- 完成任务 TTL 与周期清理。

### 本阶段边界

任务状态只存在当前进程内；服务重启后不会恢复排队、执行中或已完成任务。文件上传、对象存储、数据库、Redis、分布式 Worker、宕机恢复与运行中强制取消保留至业务需要时再单独立项。

### 验收

- 最大同时执行数受配置限制，额外任务 FIFO 排队。
- 队列满时返回“当前待处理任务较多，请稍后重试”。
- 调用方可轮询任务状态，或用 callback 获取终态结果。

## 8. 0.6.0：Agent 计划与多 Skill 编排

### 目标

让 LangChain Agent 成为顶层能力选择与计划编排层，而不是基础解析链路依赖。

### 工作项

- 定义结构化 `ParsePlan`、`PlanStep` 与执行状态模型。
- 暴露 Agent 计划/执行 API。
- Agent 只调用顶层 Skill，不接触 Provider。
- 支持多步骤串联、条件分支、重试和汇总。
- 无模型配置时，确定性解析 API 必须继续正常工作。

### 验收

- Agent 可针对不同文件类型选择合适 Skill。
- 多步骤计划可执行、可审计、可重放。
- Agent 失败不影响直接调用确定性 Skill。

## 9. 0.7.0：PDF OCR 与质量路由（后置）

### 定位

PDF OCR 保留为后续解析能力增强，不作为当前 Excel/PPT、工作流、任务化和 Agent 阶段的前置依赖。启动条件是：已有文件任务、artifact 和 Provider 部署的稳定运行基础，且业务已确认扫描 PDF 的处理量与质量目标。

### 工作项

- 实现并注册 OCR Provider，例如 MinerU 的本地或服务化适配器。
- 按 `text_based`、`scanned`、`image_based`、`mixed` 路由。
- 将 `mixed` 第一版按整份 OCR 处理，后续再考虑页级混合路由。
- 定义文本量、页数、完整度等质量门槛。
- Provider 失败或质量不足时执行 fallback。

### 验收

- 扫描 PDF 与图片型 PDF 能真实返回 OCR 内容。
- 文本型 PDF 仍优先走普通解析。
- 结果包含检测证据、实际 Provider、尝试记录、警告和质量指标。

## 10. 不纳入当前路线图：知识加工

以下能力暂不安排版本和研发资源：

```text
document.summary
semantic.chunking
document.tagging
metadata.enrichment
```

原因是当前优先目标为文件解析覆盖、转换编排、任务化和 Agent 调度。待解析结果模型、任务系统和明确的业务消费场景稳定后，再单独评估知识加工的输入规范、模型成本、质量指标和索引方案。

## 11. 优先级与依赖关系

```text
0.3.1
  → 0.4.0
    → 0.4.1
      → 0.5.0
        → 0.6.0
          → 0.7.0（OCR，后置）

知识加工不在当前版本链路中。
```

近期最高优先级为：

```text
0.4.0-A：excel.parse + openpyxl + 安全的稀疏解析 + 真实 API 测试
```

原因：它复用现有 Office 转换和统一结果模型，业务价值高，且能先验证“转换后自动解析”的内部编排设计。
