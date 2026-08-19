# Parse Agent 方案 0.4.1：Office 统一解析 Pipeline 与 MCP

## 1. 目标

将 Office 格式转换和对应解析串联为一个可追踪 Pipeline，并通过 MCP 向外部 Agent 暴露稳定的顶层工具。

```text
.doc / .xls / .ppt
  → office.convert
  → word.parse / excel.parse / ppt.parse
  → PipelineResult
```

## 2. Pipeline

新增：

```text
POST /api/v1/pipeline/parse-office
```

Pipeline 路由：

| 输入 | 转换 | 解析 |
|---|---|---|
| `.doc` | `.docx` | `word.parse` |
| `.xls` / `.xlsm` | `.xlsx` | `excel.parse` |
| `.ppt` | `.pptx` | `ppt.parse` |
| `.docx` | 无 | `word.parse` |
| `.xlsx` / `.xlsm` | 无或内部标准化 | `excel.parse` |
| `.pptx` | 无 | `ppt.parse` |

结果包含：

- 源文件；
- 每一步的名称、状态、输入/输出路径、Provider、耗时；
- 转换产物；
- 下游解析结果；
- 警告与错误；
- 总耗时。

核心模型：

```text
PipelineResult
├── source_file
├── steps[]
│   ├── name
│   ├── status
│   ├── input_path
│   ├── output_path
│   ├── provider
│   └── duration_ms
├── result
├── warnings
└── error
```

## 3. MCP

新增 MCP Server：

```text
app/mcp_server.py
```

启动：

```bash
uv sync
uv run parse-agent-mcp
```

当前暴露三个顶层工具：

- `list_skills`：列出 Skill 能力和 Provider；
- `execute_skill`：执行指定顶层 Skill，不直接暴露 Provider；
- `parse_office_pipeline`：执行 Office 转换后自动解析 Pipeline。

MCP Server 默认使用 stdio 传输，适合 Claude Desktop、Cursor 等 MCP 客户端。MCP 工具与 REST API 共享同一个 `SkillExecutor`、Registry 和 Pipeline，不重复实现解析逻辑。

## 4. 非目标

- 不在 MCP 层直接调用 `ffmpeg`、LibreOffice、openpyxl 或 python-pptx；
- 不通过 HTTP 回调本地 REST API；
- 不在本版本引入任务队列和持久化；
- 长耗时任务仍沿用当前同步执行模型，任务化将在后续版本处理。

## 5. 验收

- `.xls`、`.ppt` 可经单次 Pipeline 请求完成转换与解析；
- Pipeline 结果能定位转换或解析失败阶段；
- MCP Server 能注册并列出三个核心工具；
- Pipeline、MCP 和已有 PDF/DOCX/Excel/PPT/媒体测试全部通过。
