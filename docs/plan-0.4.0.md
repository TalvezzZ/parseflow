# Parse Agent 方案 0.4.0：Excel 与 PowerPoint 解析闭环

## 1. 版本目标

在现有 PDF、DOCX 和 Office 转换能力之上，新增 Excel 与 PowerPoint 的原生解析能力，并使旧版 Office 格式在转换后自动进入对应解析链路。

```text
.xls / .xlsm
  → Office 转换为 .xlsx
  → excel.parse
  → 统一 DocumentResult

.xlsx
  → excel.parse
  → 统一 DocumentResult

.ppt
  → Office 转换为 .pptx
  → ppt.parse
  → 统一 DocumentResult

.pptx
  → ppt.parse
  → 统一 DocumentResult
```

本版本的目标是可靠提取可供后续业务消费的结构化内容；不是实现 Excel 或 PowerPoint 的像素级渲染和编辑器级还原。知识加工能力不属于本版本范围。

## 2. 范围与非目标

### 2.1 本版本包含

- 新增 `excel.parse` Skill。
- 新增 `ppt.parse` Skill。
- 使用 `openpyxl` 解析 `.xlsx`。
- 使用 `python-pptx` 解析 `.pptx`。
- `.xls`、`.xlsm` 自动转换为 `.xlsx` 后解析。
- `.ppt` 自动转换为 `.pptx` 后解析。
- 复用 LibreOffice 的转换实现，不通过内部 HTTP 调用公开转换 API。
- 输出 Markdown、纯文本、表格/页面 blocks、图片 artifact 和专有扩展信息。
- 对 Excel 的大文件、样式污染、异常 OOXML 包实施资源限制和可解释的截断策略。
- 覆盖真实文件与 API 集成测试。

### 2.2 本版本不包含

- Excel 公式执行或重算。
- Excel VBA/Macro 执行、保留或安全分析。
- Excel 图表、透视表、条件格式、复杂绘图对象的完整还原。
- PPT/PPTX 动画、转场、音视频、SmartArt、OLE 嵌入对象的完整解析。
- PPTX 母版、主题、布局、坐标、样式的像素级还原。
- 分布式任务队列、对象存储、鉴权和权限模型。

## 3. 核心原则

```text
Skill 负责文件能力和流程编排。
Provider 负责 openpyxl、python-pptx 等具体实现。
ConversionService 负责可复用的旧格式标准化。
DocumentResult 负责稳定的通用输出。
extensions 负责保留 Excel/PPT 专有结构。
```

1. Agent 只选择 `excel.parse` 或 `ppt.parse`，不选择底层库。
2. 旧格式转换是解析 Skill 的内部预处理步骤；用户仍可独立调用 `office.convert`。
3. 解析结果必须保留原始文件、转换产物、实际 Provider 和各步骤尝试记录。
4. 对超大或异常 Excel 优先保障服务可用性，可返回 `partial`，不得无限扫描。
5. 不把格式样式视为业务内容；不依赖 Excel 声明的 Used Range 作为真实数据边界。

## 4. Skill 与 Provider 设计

### 4.1 新增 Skill

```text
excel.parse
ppt.parse
```

建议目录：

```text
app/
├── services/
│   └── office_conversion.py
└── skills/
    ├── excel/
    │   ├── skill.py
    │   └── adapters/openpyxl.py
    └── ppt/
        ├── skill.py
        └── adapters/python_pptx.py
```

### 4.2 Provider Manifest

```text
excel.normal.openpyxl
  kind: spreadsheet_parser
  modes: [normal]
  capabilities:
    - sheets
    - cells
    - formulas
    - merged_cells
    - tables
    - images
    - markdown

ppt.normal.python-pptx
  kind: presentation_parser
  modes: [normal]
  capabilities:
    - slides
    - text
    - tables
    - images
    - group_shapes
    - markdown
```

### 4.3 支持的格式

| Skill | 直接格式 | 自动预处理格式 | 预处理目标 |
|---|---|---|---|
| `excel.parse` | `.xlsx` | `.xls`、`.xlsm` | `.xlsx` |
| `ppt.parse` | `.pptx` | `.ppt` | `.pptx` |

`.xlsm` 被转换为 `.xlsx` 后不保留可执行宏；结果必须包含宏被移除的 warning。

## 5. 旧格式转换编排

### 5.1 内部服务

将 LibreOffice 调用从 `LibreOfficeProvider` 抽取为可复用的 `OfficeConversionService`：

```text
OfficeConvertSkill
  → OfficeConversionService.convert(...)

ExcelParseSkill
  → OfficeConversionService.convert(.xls/.xlsm → .xlsx)
  → OpenpyxlProvider.parse(...)

PptParseSkill
  → OfficeConversionService.convert(.ppt → .pptx)
  → PythonPptxProvider.parse(...)
```

公开 API `POST /api/v1/convert/office` 保持不变。解析 Skill 不得通过 HTTP 再调用该 API。

### 5.2 溯源要求

旧格式解析成功后，`DocumentResult.provenance` 至少包括：

```json
{
  "source_file": "/data/source.xls",
  "conversion": {
    "provider": "office.convert.libreoffice",
    "source_format": "xls",
    "target_format": "xlsx",
    "target_path": "/data/artifacts/source.xlsx",
    "duration_ms": 1200
  },
  "parse_provider": {
    "name": "excel.normal.openpyxl",
    "version": "0.4.0"
  }
}
```

## 6. Excel 解析设计

### 6.1 输出映射

```text
DocumentResult
├── document_type: "xlsx"
├── blocks
│   ├── Sheet 标题（heading）
│   ├── 连续表格区域（table）
│   ├── 独立文本/注释（text）
│   └── 图片引用（image）
├── tables
│   └── sheet_name、range、rows、merged_ranges、headers
├── images
│   └── artifact 路径、所属 Sheet、锚点坐标
├── representations
│   ├── plain_text
│   └── markdown
├── metadata
│   └── Sheet 数、可见 Sheet、工作簿属性
├── quality
│   └── 语义单元格数、表格数、图片数、截断状态
└── extensions.workbook
    └── Sheet、稀疏 cells、公式、合并范围、维度
```

单元格建议使用以下结构，避免丢失公式语义：

```json
{
  "coordinate": "C12",
  "row": 12,
  "column": 3,
  "value": 100,
  "formula": "=SUM(C2:C11)",
  "display_value": 100
}
```

`openpyxl` 不执行公式。应同时尽可能读取公式表达式与工作簿缓存值；缓存值不存在时保留公式并增加 warning，不能伪造计算结果。

### 6.2 空白/样式污染防护

Excel 可能因为整行、整列或整 Sheet 的样式而将声明范围扩大到：

```text
A1:XFD1048576
```

但其中可能没有任何业务数据。解析器必须采用以下流程：

```text
ZIP / OOXML 安全预检
  → 流式扫描 Sheet XML
  → 仅统计有值、公式、批注、超链接、图片或表格锚点的语义单元格
  → 计算 semantic_dimension
  → 使用 openpyxl 读取必要对象
  → 稀疏输出与连续区域聚类
```

不得把以下字段直接作为遍历范围：

```text
worksheet.max_row
worksheet.max_column
worksheet.calculate_dimension()
```

仅有样式、空白边框、字体、背景色的单元格不构成语义内容。

### 6.3 稀疏模型与表格聚类

对于跨度极大的稀疏内容，例如：

```text
A1 = 标题
B1000000 = 备注
```

不得生成一百万行二维数组。应输出稀疏单元格，并只对相邻、合理大小的连续区域生成表格。

合并单元格必须保留范围信息，不得逐格展开：

```json
{
  "range": "A1:D10",
  "anchor": "A1",
  "value": "合并标题"
}
```

### 6.4 Excel 限额

建议新增配置：

```env
EXCEL_MAX_FILE_SIZE_MB=100
EXCEL_MAX_UNCOMPRESSED_SIZE_MB=500
EXCEL_MAX_ZIP_ENTRIES=10000
EXCEL_MAX_SHEET_XML_SIZE_MB=100
EXCEL_MAX_PARSE_SECONDS=300
EXCEL_MAX_SHEETS=30
EXCEL_MAX_SEMANTIC_CELLS=1000000
EXCEL_MAX_SHEET_ROWS=100000
EXCEL_MAX_COLUMNS=1000
EXCEL_MAX_TABLE_CELLS=200000
EXCEL_MAX_IMAGE_COUNT=1000
EXCEL_MAX_MERGED_RANGES=10000
```

超过解析预算时返回：

```text
status: partial
warning: excel_parse_truncated
```

并在 `quality` 和 `provenance` 中记录总量、已解析量、触发限制与原因。

全文件仅包含样式、没有语义内容时返回：

```text
status: failed
error: quality_insufficient
```

## 7. PPT/PPTX 解析设计

### 7.1 输出映射

每张幻灯片映射为一个 `DocumentPage`：

```text
DocumentResult
├── document_type: "pptx"
├── pages
│   └── 每页幻灯片一个 DocumentPage
├── blocks
│   ├── 标题与正文（heading/text）
│   ├── 表格（table）
│   └── 图片（image）
├── tables
│   └── slide_number、shape_id、rows
├── images
│   └── slide_number、shape_id、artifact 路径
├── representations
│   ├── plain_text
│   └── markdown
├── metadata
│   └── slide_count、slide_width、slide_height
└── extensions.presentation
    └── slides、shape 摘要、布局基础信息
```

### 7.2 第一版抽取范围

- 幻灯片顺序与页码。
- 文本框及段落文本。
- 幻灯片表格。
- 图片 artifact。
- Group Shape 内的文本、表格和图片。
- Slide 级 Markdown 与纯文本。

图片必须保存至由任务或请求指定的 artifact 目录，不应只返回内存字节或临时不可追踪路径。

## 8. API 与配置

新增 API：

```text
POST /api/v1/parse/excel
POST /api/v1/parse/ppt
```

二者可复用 `DocumentParseRequest`，由对应 Skill 按文件后缀验证格式。

新增配置：

```env
EXCEL_NORMAL_PROVIDER=excel.normal.openpyxl
EXCEL_FALLBACK_PROVIDERS=
PPT_NORMAL_PROVIDER=ppt.normal.python-pptx
PPT_FALLBACK_PROVIDERS=
```

新增依赖：

```toml
openpyxl>=3.1.5
python-pptx>=1.0.2
Pillow>=10.0.0
```

## 9. 测试与验收

### 9.1 Excel 真实测试

- `.xlsx` 的多 Sheet、标题、数据表、日期、公式和隐藏 Sheet。
- 合并单元格与嵌入图片。
- `.xls → .xlsx → excel.parse` 的真实 LibreOffice 回转。
- `.xlsm → .xlsx` 的宏移除 warning。
- 伪装扩展名、损坏 OOXML 和压缩包限额。
- 超行数、超 Sheet 数、语义单元格超限的 `partial` 结果。
- 声明 Used Range 巨大但只有少量内容的样式污染文件。

### 9.2 PPT 真实测试

- `.pptx` 多页、文本框、表格、图片与 Group Shape。
- `.ppt → .pptx → ppt.parse` 的真实 LibreOffice 回转。
- 图片 artifact 存在并能被结果引用。
- 幻灯片顺序、文本、表格和图片数量正确。
- 空白幻灯片与损坏 OOXML 的处理。

### 9.3 0.4.0 验收标准

- `/api/v1/skills` 返回 `excel.parse` 和 `ppt.parse`。
- `.xlsx` 能返回 Sheet、表格、Markdown、纯文本与专有 workbook 扩展信息。
- `.pptx` 能返回页级文本、表格、图片 artifact 与 Markdown。
- `.xls/.xlsm/.ppt` 能自动转换后解析，并保留完整 conversion provenance。
- Excel 样式污染、合并区域滥用和超大文件不会导致无限扫描或内存耗尽。
- 全部新增能力具有真实文件与 API 集成测试。
- 现有 PDF、DOCX、Office 转换测试全部回归通过。
