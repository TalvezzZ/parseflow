# Parse Agent 方案 0.4.3：Excel 样式污染与超大 Used Range 加固

## 1. 背景

Excel 的声明使用范围可能因整行、整列或整 Sheet 的样式残留扩大到：

```text
A1:XFD1048576
```

但真实业务内容可能只有少量单元格。若按 `openpyxl.Worksheet.max_row`、`max_column` 或 `iter_rows()` 全范围遍历，会导致极高 CPU、内存与响应时间风险。

## 2. 目标

在保留现有 Excel 输出格式的前提下，将解析改造为“先预检、后稀疏访问”：

```text
XLSX ZIP 安全预检
  → 流式扫描 worksheet XML
  → 收集有业务语义的单元格坐标
  → openpyxl 仅访问这些坐标
  → 输出 semantic_dimension 与污染告警
```

## 3. 实施计划

### A. OOXML ZIP 预检

新增可配置限制：

- 文件大小；
- ZIP entry 数量；
- 解压总大小；
- 单个 worksheet XML 解压大小；
- Sheet 数量。

超过安全上限时返回明确的 `excel_archive_limit_exceeded`，不加载工作簿。

### B. 语义单元格流式扫描

扫描 `xl/worksheets/*.xml` 中的 `<c>` 节点。仅当单元格包含以下内容时计入语义坐标：

- `<v>` 值；
- `<f>` 公式；
- `<is>` inline string；
- 单元格坐标及其对应的批注/超链接由 openpyxl 的稀疏访问补充。

仅有 `s`（样式）、边框、填充、字体或空 `<c>` 的节点不计入语义内容。

### C. 稀疏访问与输出

- 禁止使用 `sheet.iter_rows()` 扫描声明范围；
- 只按预扫描得到的坐标访问 `sheet[coordinate]`；
- 保留合并范围为范围描述，绝不逐格展开；
- 从语义坐标计算 `semantic_dimension`；
- 若声明范围远大于语义范围，记录 `excel_used_range_polluted` warning；
- 大量稀疏坐标、语义单元格或行/列跨度超限时返回 `partial`，并记录限制原因。

### D. 回归测试

构造真实 XLSX：

```text
声明范围：A1:XFD1048576
真实内容：A1、B2、C3
```

验收：

- API 在合理时间内返回；
- 只输出三个语义单元格；
- 不产生百万行或十亿级单元格扫描；
- 输出 `semantic_dimension=A1:C3`；
- 输出 `excel_used_range_polluted` warning；
- 全量测试通过。

## 4. 非目标

- 公式计算与缓存值重算；
- 完整样式还原；
- Excel 图表、透视表与绘图对象的深度解析；
- 对恶意 ZIP 的病毒扫描。
