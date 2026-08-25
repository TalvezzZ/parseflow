# ParseFlow 0.7.1 技术方案：基础能力收尾与一致性修复

## 1. 目标与边界

`0.7.1` 收口 `v0.7.0` 之后已经开始的 XML、原文兜底和结果页改动，不改变现有任务架构和公开 API。路径型 API、callback 和持久化重构留给 `0.8.0`。

## 2. 当前问题

- XML 已能安全解析，但正文与结构化语义输出混在同一 representation 中。
- `TextParseSkill` 新增的原文格式尚未同步到上传白名单和 Planner。
- 原文 Provider 失败回退逻辑需要安全错误分类，不能仅依赖字符串前缀。
- `OfficeParsePipeline` 把文档放在 `PipelineResult.result.document`，前端只读取普通 `SkillResult.data.document`。
- Artifact 页面只有静态卡片，尚未连接安全下载接口。
- 项目稳定标签为 `v0.7.0`，代码内仍有 `0.6.0` 版本字符串。

## 3. 后端设计

### 3.1 文本格式单一来源

新增 `app/skills/text_formats.py`：

```python
STRUCTURED_TEXT_SUFFIXES = {".csv", ".tsv", ".html", ".htm", ".xml", ".rtf"}
RAW_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".log", ".sql", ".js", ".ts", ".css",
}
TEXT_SUFFIXES = STRUCTURED_TEXT_SUFFIXES | RAW_TEXT_SUFFIXES
```

`TextParseSkill`、`RuleBasedPlanner` 和默认配置从同一常量生成，测试断言上传白名单、Planner 和 Skill 三者一致。环境变量仍可缩小上传白名单，但默认值必须覆盖所有公开 route。

### 3.2 RawSourceProvider

Provider 名称：

```text
text.raw.source
```

职责：

1. 使用 `Path.stat()` 检查 `TEXT_MAX_FILE_SIZE_MB`。
2. 读取字节并处理 UTF-8 BOM。
3. 默认采用 UTF-8 严格解码；是否允许替换非法字节由明确配置控制，首版建议非法 UTF-8 返回 `invalid_text_encoding`，避免把二进制伪装成文本。
4. 对 NUL 字节和高比例控制字符执行轻量二进制检测，返回 `binary_content_rejected`。
5. 输出 `plain_text` 和 `markdown`，block 为单个 text block。
6. metadata 记录 `encoding`、`size_bytes` 和 `extraction_mode=raw_source` 或 `raw_source_fallback`。

`raw_source` 表示该后缀本来就使用原文模式；`raw_source_fallback` 表示专用 Parser 失败后降级。

### 3.3 回退策略

不要在 Skill 中使用 `error.code.startswith("xml_unsafe")` 判断安全错误。新增稳定错误分类：

```python
NON_FALLBACK_ERROR_CODES = {
    "unsafe_xml",
    "file_size_exceeded",
    "binary_content_rejected",
    "path_not_found",
    "permission_denied",
}
```

执行规则：

```text
原文后缀 → 直接 RawSourceProvider
结构化后缀 → 专用解析成功则返回专用结果
             → 可回退解析错误时调用 RawSourceProvider
             → 安全/预算/文件错误直接失败
```

CSV/TSV 被截断为 partial 时不回退；只有 failed 才评估回退。显式选择 Provider 时尊重用户选择，不自动换 Provider，除非 `fallback_enabled=true` 且请求契约明确允许。

### 3.4 XML 输出

`PlainTextProvider._parse_xml()` 一次构建最终 `DocumentResult`，不先创建语义正文再覆盖：

- `representations.plain_text`：原始 XML 字符串。
- `representations.markdown`：原始 XML fenced code block或原始字符串，保持既有兼容策略。
- `representations.html`：转义后的独立 HTML 树。
- `extensions.xml`：保留 root tag、节点路径和语义值。
- `blocks`：原文 text block。

继续使用 `defusedxml`；禁止外部实体和危险实体展开。HTML 生成器必须转义标签名、属性名、属性值和文本值。

### 3.5 HTML 输出

HTML Parser 保留：

- `plain_text`：可读正文。
- `markdown`：转换后的 Markdown。
- `html`：清洗后的 HTML。

首版清洗规则：删除 `script`、`object`、`embed`、`iframe`、`base`，移除 `on*` 事件属性；预览仍使用无权限 sandbox iframe。原始 HTML 不直接进入主页面 DOM。

### 3.6 统一结果提取

新增后端或前端单一归一化函数。`0.7.1` 为最小改动，可在 `web/src/result.ts` 实现：

```ts
function documentOf(task: Task): DocumentResult | undefined {
  const execution = task.result?.result
  return execution?.data?.document
      ?? execution?.result?.document
      ?? execution?.document
}
```

同时将 warnings、artifacts、conversion 和 steps 归一化。`0.8.0` 再从后端彻底统一任务结果 envelope。

### 3.7 Artifact 下载

前端只使用服务端下载 URL：

```text
/api/v1/files/{file_id}/artifacts/{encodedRelativePath}
```

Artifact 归一化时从文档、conversion 和 media 结果收集相对路径；不显示绝对路径。下载链接必须对每个路径段进行编码。后端继续使用 `resolve()` + `relative_to()` 阻止目录穿越，并增加符号链接逃逸测试。

## 4. 前端模块拆分

将当前单文件 `web/src/main.tsx` 的新增逻辑拆分为：

```text
web/src/api.ts                 API 请求与错误归一化
web/src/results/normalize.ts   Skill/Pipeline 结果归一化
web/src/results/CopyButton.tsx 复制与降级交互
web/src/results/HtmlPreview.tsx sandbox iframe
web/src/results/Artifacts.tsx  artifact 列表与下载
```

`HtmlPreview` 固定 `sandbox=""`，增加标题和无内容状态。复制成功提示使用 `aria-live`，不只改变按钮文字。

## 5. 配置与版本

- `FILE_ALLOWED_SUFFIXES` 默认值覆盖 `TEXT_SUFFIXES`。
- `README` 支持格式表同步新增后缀。
- Python、Web、FastAPI、MCP、Planner 和 Docker 镜像统一为 `0.7.1`。
- 新增标准 Python `[build-system]` 并验证脚本入口。
- 根目录补充 MIT `LICENSE`。

长期版本单一来源留给 `1.0.0`；本版本增加测试防止多个公开版本字符串不一致。

## 6. 测试设计

### 后端

新增或扩展：

- 每个新增文本后缀的上传、Planner 和 Skill 一致性参数化测试。
- UTF-8、UTF-8 BOM、空文件、非法编码和 NUL 字节。
- 大小限制和错误码。
- XML 原文、HTML representation、危险实体拒绝且不回退。
- HTML 清洗和脚本移除。
- 专用解析成功优先、普通错误回退、安全错误不回退。
- Artifact 路径穿越和符号链接逃逸。

### 前端

`0.7.1` 至少保证 TypeScript 构建，并为 `normalize.ts` 增加轻量 Vitest 可选；完整浏览器测试框架在 `0.9.0` 建立。手工 smoke：

1. XML 原文、HTML 预览和复制。
2. JSON/YAML/LOG 上传与正文。
3. DOC/XLS/PPT Pipeline 结果。
4. Artifact 下载。
5. 无 HTML、无 artifact 和复制失败状态。

## 7. 实施顺序

1. 提取文本后缀常量并贯通配置、Planner、Skill。
2. 完成 RawSourceProvider 解码、二进制检测和错误分类。
3. 重构 XML/HTML 最终结果构建。
4. 增加后端参数化回归测试。
5. 拆分前端结果归一化、复制、HTML 和 Artifact 组件。
6. 修复 Office Pipeline 结果展示。
7. 统一版本号、LICENSE、build-system 和文档。
8. 执行全量测试、Web build 和本地 UI smoke。

## 8. 完成定义

- 所有公开文本后缀都可上传、可规划、可解析。
- 二进制、安全 XML 和超限文件不会被原文兜底绕过。
- Skill 与 Pipeline 结果都能正常展示。
- Artifact 可下载且不暴露/越过存储目录。
- `uv run pytest` 与 `cd web && npm run build` 通过。
- 版本和文档统一为 `0.7.1`，工作区形成一个可审查的发布提交。
