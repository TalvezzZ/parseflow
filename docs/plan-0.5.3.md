# Parse Agent 方案 0.5.3：RTF 智能路由与 DOCX Fallback

## 目标

为 RTF 提供独立顶层 `rtf.parse` Skill。在保持简单 RTF 快速直接提取的同时，对带表格、图片、对象等复杂 RTF 自动使用 LibreOffice 转 DOCX 后交给 `word.parse`。

## 路由

```text
.rtf → rtf.parse
  ├─ direct：striprtf → DocumentResult
  └─ convert：LibreOffice → .docx → word.parse → DocumentResult
```

默认 `auto` 模式的复杂度信号：

| 信号 | RTF 控制词 | 分值 |
|---|---|---:|
| 表格 | `\trowd`、`\cell`、`\row`、`\intbl` | 5 |
| 图片 | `\pict`、`\pngblip`、`\jpegblip` | 5 |
| 嵌入对象 | `\object`、`\objdata`、`\objclass` | 8 |
| 字段 | `\field`、`\fldinst`、`\fldrslt` | 3 |
| 绘图 | `\shp`、`\shpinst` | 4 |
| 页眉页脚 | `\header`、`\footer` | 2 |
| 脚注批注 | `\footnote`、`\annotation` | 2 |

默认评分达到 `RTF_DIRECT_MAX_COMPLEXITY_SCORE=5` 时走转换路径。

即使静态特征较简单，直接解析出现失败、无文本或正文字符数与源文件比例低于 `RTF_DIRECT_MIN_TEXT_RATIO` 时，`auto` 模式也会 fallback 到 DOCX 转换。

## 配置

```env
RTF_ROUTING_MODE=auto
RTF_DIRECT_MAX_COMPLEXITY_SCORE=5
RTF_DIRECT_MAX_SIZE_MB=2
RTF_DIRECT_MIN_TEXT_RATIO=0.01
```

- `auto`：按特征评分和质量自动选择；
- `direct`：始终 `striprtf`；
- `convert`：始终 LibreOffice 转 DOCX。

## 可追踪性

结果的 `data.rtf_routing` 和 `document.provenance.rtf_routing` 都包含：

```json
{
  "mode": "auto",
  "signals": ["rtf_table"],
  "complexity_score": 5,
  "selected_path": "convert_to_docx"
}
```

发生质量 fallback 时，额外包含 `direct_parse_quality` 和 `fallback_reason`。转换结果保留 `conversion` provenance，且最终文档仍指向原始 RTF 路径。

## 接入

```text
POST /api/v1/parse/rtf
POST /api/v1/tasks/skill  (skill_name=rtf.parse)
MCP execute_skill         (skill_name=rtf.parse)
```

`text.parse` 仍保留 RTF 直接文本模式，适合调用方明确不希望转换的场景。
