# ParseFlow 1.2.0 技术方案：解析质量、结果导出与媒体能力

## 目标

在不改变 v1 REST/MCP 契约的前提下，提高已有解析格式的质量可见性、结果可消费性和失败可解释性。重点不是盲目增加格式，而是让用户知道结果来自什么解析链路、可信度如何、如何导出使用。

## 主要交付

### PDF 与 OCR 质量策略

- 识别文本型、扫描型和混合 PDF，记录实际 Provider/fallback 链路。
- 提供质量摘要：文本覆盖、OCR 页数、表格/图片数、截断原因、warning 和失败类别。
- 支持表格优先、文本优先和 OCR 优先等受控目标/策略；服务端仍为最终决策方。

### 统一质量报告

在统一 result envelope 中增加可选 `quality`/`provenance` 信息：

```text
provider chain、input classification、coverage、truncation、warnings、confidence（仅有可靠来源时）
```

禁止伪造 OCR/模型置信度；未知必须明确为未知。

### 结果导出

- 表格导出 CSV、XLSX、JSON、Markdown。
- 文档导出 JSON、Markdown、纯文本。
- 小型导出可同步生成；大型导出作为受控 artifact 异步生成。
- 导出保留来源 task/file/artifact 关联和下载审计语义。

### 图片与 OCR

- 输出页面/区域坐标、旋转修正提示、空白页/低清晰度 warning。
- OCR 结果按页面/块归属，避免与原生 PDF 文本混淆。

### 媒体扩展准备

当前媒体预处理保持不含转写。定义可插拔 ASR Provider 契约、资源预算、语言/时间轴 schema 与 artifact 规则，但仅在具备本地模型和真实测试后再启用实际转写。

## 测试与完成定义

- 为文本 PDF、扫描 PDF、混合 PDF、无效文件、超预算文件提供稳定 fixture。
- 导出格式正确、无路径泄露、artifact 可下载。
- UI 显示 provider、warning、质量和导出入口。
- 不改变 v1 既有字段语义；新增字段均为可选。

## 实施说明

v1.2.0 采用任务完成时的受控同步导出：每个导出文件受 `TASK_MAX_RESULT_SIZE_MB` 限制，超限会产生 warning，不阻塞原始解析结果。公开任务结果新增可选 `quality`/`provenance` 字段；artifact manifest 使用仅服务端可见的相对 `storage_key` 支持嵌套导出下载，公开响应仍只包含 opaque ID、文件名和下载 URL。

媒体转写仍未启用；本版本仅在 `app/skills/media/asr.py` 固化 Provider、语言、时间轴、置信度和时长预算契约。
