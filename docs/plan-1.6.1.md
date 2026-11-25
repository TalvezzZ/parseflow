# ParseFlow v1.6.1 技术方案：渲染层解耦

状态：实现完成（2026-11-25）。阶段 0 门禁通过；渲染层契约与注册表；PyMuPDF / PaddleOCR / PPT / Excel 表示生成全部迁移（字节等价回归）；导出层改为调用渲染层；页面级图片引用；工作簿 sheet 分组渲染；manifest schema 身份与版本；CJK 空格清理实测否决并移除。跨页表格无生产者，暂缓。依据见 [DocVortex 能力吸收决策记录](docvortex-adoption.md)。

版本定位：**PATCH**（见 [版本策略](versioning.md)）。本版本无对外新能力：输出格式不变、端点不变、默认链路不变。改的是结构（渲染层解耦）与缺陷（中文空格、跨页表格），外加向后兼容的附加字段。因此不升 MINOR。

## 目标与边界

- 建立 `app/render/` 层：输入 `DocumentResult`，输出 `RenderArtifact`；渲染器按格式注册，新增格式不改解析代码。
- `DocumentResult.representations` 降级为兼容缓存，权威来源为渲染层；老任务仍走旧路径，不重算。
- 拆分 `generate_document_exports`：排版归渲染层，字节预算、暂存目录、原子发布、可杀 worker 全部保留。
- 不改变 v1 REST/MCP 契约；不新增对外输出格式；不引入外部引擎运行时依赖。
- 质量修复仅限：中文生成空格、跨页表格续接语义、结果 manifest schema 字段。
- 协议纪律：仅重构不改协议不升版本；改变 `pages`/`blocks`/`tables` 字段语义必须升 `schema_version` 并留迁移说明。

## 阶段 0：可行性验证（门禁，未通过则阻断 v1.7.0）

**结论（2026-11-24）：已通过**，7/7 格式由外部构造的 `MiddleJson` 渲染成功，脚本见 [`scripts/spike_docvortex_render.py`](../scripts/spike_docvortex_render.py)，结果见 [决策记录第 6 节](docvortex-adoption.md#6-阶段-0-验证结论2026-11-24通过)。以下为原验证口径。

脚本 `scripts/spike_docvortex_render.py`，不进入产品依赖：

1. 手工构造最小 `MiddleJson`（1 页、1 段、1 表、1 图），调用 `docvortex.render.render()`；
2. 逐格式尝试 markdown / html / latex / docx / epub / pdf / structured_content；
3. 记录安装体积、依赖树、失败原因；结论写入决策记录第 6 节。

通过条件：不读源文件可渲染；至少 6 种格式成功；docx 公式为 OMML、html 公式可被 MathJax 识别（LaTeX 定界符）。
不通过则：v1.7.0 改为自研渲染器（python-docx / reportlab / markdown-it），公式链路单独立项。

## 阶段 1A：渲染层

```text
app/render/contracts.py   RenderRequest / RenderArtifact / RenderOutput / ExportResult
app/render/registry.py    renderer 注册表，按 output_format 选择，缺失时显式报错
app/render/markdown.py    现有 markdown 生成逻辑（自各 Skill 迁入）
app/render/text.py        plain_text 与中文排版规则
app/render/json.py        structured_content 投影
```

- `PdfParseSkill` / `WordParseSkill` / `PptParseSkill` / `TextParseSkill` / Excel 不再自行拼 markdown，统一调用渲染层。
- 渲染失败必须显式返回错误码，不得静默回退到空字符串；无可用渲染器时如实返回不支持。
- 渲染只读 `DocumentResult`，不持有 Skill、Provider 或文件句柄。

**迁移边界（实施中确认）**：渲染层只接管**可从文档结构派生**的表示。以下情况保留在 Provider 内，不强行下沉：

- 解析器直接产出的 markdown（pdf-inspector、Mammoth 的 HTML→Markdown）：这是解析结果本身，渲染层只做透传。
- 带页面级图片引用的 markdown（PaddleOCR 每页追加 `![第 N 页图片](路径)`）：需要渲染器支持按页分组图片后才能下沉，暂缓。
- 源文本围栏（text/raw 来源）：与文件后缀、围栏长度相关，属于读取语义，不属于排版。
- 单图 OCR（`image_ocr`）：图片链接模板是 `![图片区域](路径)`，无页码前缀，且只有一页。若强行套用页面级引用会改变文案与结构，收益不足，暂不下沉。
- 音视频（`ffmpeg`）：表示由字幕轨与时间轴直接构成，不是页面/块投影，属于解析结果本身，不下沉。

判定规则：若下沉会导致输出字节变化或需要引入 Provider 私有数据，则该表示暂不下沉，并在 Provider 内保留注释说明原因。

**实施发现（2026-11-24）**：

- Excel 已迁移：新增 `body_source="sheet"`，按 `blocks[].metadata.sheet` 分组，标题 `## {name}` + 每行 `- {row}`，空表跳过；纯文本在无页面时回退块拼接。
- PaddleOCR 已迁移：新增 `page_asset_links`，按页归组图片并在页尾追加引用。
- PaddleOCR 的按页图片引用能力已落地：`RenderOptions.page_asset_links` + 链接模板，按 `page_number`/`occurrences` 归组，缺相对路径的图片跳过并告警。空正文 + 有图时**刻意复现** 1.6.0 的前导分隔符以保证字节等价。
- CJK 空格清理曾接入全局策略，实测误删率 96.2% 后**整体移除**（含 `app/render/policy.py` 与配置项），结论见 1C-1。
- 跨页表格续接（`continues_prev`）目前**没有生产者**：现有 PDF Provider 不产出分页 `tables`（pdf-inspector 的表格在 markdown 内），Excel/PPT 表格不跨页。该项缺少数据来源，暂缓到出现真实跨页表格来源后再做，避免先加死字段。

## 阶段 1B：导出层拆分

`app/tasks/exports.py` 保留为导出编排，排版交给渲染层：

- 保留：`ExportLimitExceeded`、组合字节预算、暂存目录 + `replace` 原子发布、失败清理。
- 保留：`app/skills/excel/export_worker.py` 的可杀 worker 执行路径。
- 移除：内联的 markdown/文本拼接与表格 markdown 生成。
- `generate_document_exports(document, workspace, max_bytes)` 签名不变，内部改为调用渲染层。

## 阶段 1C：质量修复

### 1C-1 中文生成空格：已否决并删除（2026-11-25）

**决定：不做，代码已移除。** 不是暂缓，是取消。

**实测过程**：用 14.4 万字符真实中文 PDF（公告、报告、教程）逐字符提取几何数据，评估"汉字+空格+汉字"能否识别抽取器生成的空格。

| 类型 | 数量 | 占比 | 典型 | 间隙 |
|---|---:|---:|---|---|
| 跨行/跨列字符流 | 29 | 55.8% | `点␣二` | -462pt（负值） |
| 大间隙 ≥0.20em（表格列边界） | 21 | 40.4% | `面␣措` | 2.07pt |
| 微间隙 <0.12em（真生成空格） | 2 | 3.8% | `某␣个` | 1.01pt |

**误删率 96.2%。**

端到端验证（`安全隐患信息公告 (第35期).pdf`，真实 PyMuPDF Provider）：

```text
不处理：一、管理方面 措施     ← 正确，保留表格列边界
执行删除：一、管理方面措施     ← 错误，跨列文本被合并
```

**为什么取消而不是修**：

1. 字符形态无法区分三种来源，必须引入字符几何（同基线、`gap/font_size`）与结构（是否在表格单元格内）；这属于 PDF 版面分析能力，不是渲染层该承担的事。
2. 即使加上几何条件，收益上限也很低：真生成空格密度仅约 0.14 处/万字符，而修复它需要引入版面感知，投入与收益严重不成比例。
3. 风险不对称：漏删只是多一个空格，误删会把两列文本粘连，属数据损坏。

**已移除**：`is_generated_cjk_space`、`strip_generated_cjk_spaces`、`RenderOptions.cjk_generated_space_removal`、`app/render/policy.py`、`RENDER_CJK_SPACE_REMOVAL` 配置项、对应测试与实测脚本。

**保留的认知**：中文 PDF 的空格至少有三个来源（生成空格、表格列边界、跨行字符流拼接），只看字符序列无法判别。将来若要做，必须基于版面几何，且应作为独立能力立项。

2. **跨页表格**：暂缓（无生产者，见下方实施发现）。
3. **schema 版本**：`result/manifest.json` 增加 `schema`（`parseflow.result`）与 `schema_version`（当前 1）；新增 `schemas/result-manifest-v1.json` 快照；旧 manifest 缺字段时按 1 读取。

## 执行顺序

1. 阶段 0 验证并记录结论。
2. 落 `app/render/contracts.py` 与 `registry.py`，先迁 markdown/text/json，保持输出字节级等价。
3. 逐个 Skill 切换到渲染层，每个 Skill 单独提交并跑该格式回归。
4. 拆分导出层，保持导出文件清单与预算行为不变。
5. 落地三项质量修复，补 `schemas/result-manifest-v1.json`。
6. 同步版本、OpenAPI、配置与迁移说明；跑全量后端与前端验证。

## 验收

- 现有全部受支持格式的 markdown/纯文本输出与 1.6.0 字节级等价（除中文空格修复的预期差异，差异需有计数与样例）。
- 新增一种输出格式只需实现一个 renderer，不修改任何 Skill。
- 渲染层无文件系统写入；导出层无排版逻辑。
- 中文 PDF 样例的行内多余空格消除，英文词间空格与真实空格保留。
- 跨页表格在 markdown 中合并为单表，JSON 保留原始分表与续接标记。
- 新 manifest 含 schema 身份；旧任务与旧 manifest 可读；未知字段读写后不丢失。
- 后端全量测试通过，前端单测与构建通过，导出文件清单与 1.6.0 一致。
