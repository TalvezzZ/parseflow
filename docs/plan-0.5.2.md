# Parse Agent 方案 0.5.2：XML 与 RTF 直接解析

## 目标

在不依赖 OCR、ASR、LLM 或 LibreOffice 转换的条件下，将 XML 与 RTF 纳入已有 `text.parse` Skill。

## XML

输入：

```text
.xml
```

使用 `defusedxml` 解析，避免普通 XML 解析中外部实体和实体展开风险。

输出：

- `plain_text`：带节点路径的可读文本；
- `markdown`：根节点和节点列表；
- `extensions.xml`：`root_tag` 与包含 `path`、`tag`、`text`、`attributes` 的节点数组；
- `metadata`：根节点与节点数。

## RTF

输入：

```text
.rtf
```

使用 `striprtf` 直接提取正文文本，输出 plain text 和 Markdown。此版本不处理富文本样式保真、图片、表格或嵌入对象，也不加入：

```text
.rtf → LibreOffice → .docx → word.parse
```

该增强 Pipeline 在业务确有复杂 RTF 解析需求时再单独评估。

## 接入

XML 与 RTF 自动通过现有入口可用：

```text
POST /api/v1/parse/text
POST /api/v1/tasks/skill  (skill_name=text.parse)
MCP execute_skill         (skill_name=text.parse)
```

受控上传白名单增加 `.xml`、`.rtf`。

## 验收

- XML 能输出根节点、属性、节点路径和文本；
- RTF 能直接返回可读正文文本；
- XML/RTF REST API 真实集成测试通过；
- 全量回归通过。
