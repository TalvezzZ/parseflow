# ParseFlow v1.7.0 技术方案：富格式导出与可选解析引擎

状态：方案（未实现）。依赖 [v1.6.1](plan-1.6.1.md) 的渲染层与阶段 0 验证通过。依据见 [DocVortex 能力吸收决策记录](docvortex-adoption.md)。

版本定位：**MINOR**（见 [版本策略](versioning.md)）。新增 6 种输出格式与 1 个可选 PDF Provider，均向后兼容。两项能力共享同一个 optional extra 与同一套映射代码，属于同一交付物，**合并在一个 MINOR 内交付，不拆分为多个版本**。

## 目标与边界

- 一次解析、多种导出：Markdown、HTML、LaTeX、DOCX、EPUB、PDF、结构化 JSON，并支持对已完成的旧任务补导出，无需重解析。
- 公式口径按实测校正：DOCX 为 OMML，HTML 为 LaTeX 定界符 + `docvortex-math` 类（供 MathJax 客户端渲染，**不是服务端 MathML**），LaTeX 为公式源码。
- 可选解析引擎：`pdf.normal.docvortex` 作为**可选** Provider，默认值不变。
- 两项能力共用 `parseflow[rich-export]` extra；未安装时能力如实降级，不伪装成功。
- 不改变 v1 既有字段语义；新增导出入口必须受 `API_KEY` 保护且不泄露服务器路径。
- 富格式依赖不进入主依赖；镜像默认不含该 extra。
- 导出受 `TASK_MAX_RESULT_SIZE_MB` 与组合字节预算约束；超限显式报错，不静默截断。
- 不替换 PaddleOCR：扫描件仍走既有 OCR 路由。

---

## 阶段 2：富格式导出

### 渲染器接入

`app/render/renderers/docvortex.py` 实现 v1.6.1 定义的 renderer 接口：

```text
DocumentResult -> MiddleJson -> docvortex.render.render() -> RenderOutput
```

- 映射：`pages`→`PageInfo`，`blocks`→`Block`/`Span`，`tables`→表格块（含 `continues_prev` 续接），`images`→资产引用。
- 资产：把我们 artifact 中的图片按引用物化到临时素材目录并校验 SHA256；缺失素材报错，不静默丢图。
- 映射失败或渲染异常必须显式降级到内置 markdown 并记录 warning，不得返回空结果。
- 依赖锁定小版本（`docvortex>=0.6.26,<0.7`）；升级需重跑导出基准与契约测试。
- 安装体积基线：site-packages 约 281 MB / 93 个包（docvortex 22 MB、onnxruntime 85 MB、numpy 34 MB）。即使只用渲染也会拉入 onnxruntime，必须走 optional extra。

### IR 映射约束（阶段 0 实测，必须满足）

1. 顶层块必须带 `index`；子块 `index` 必须等于父块 `index`。
2. 声明 `file_suffix="pdf"` 时顶层块必须带归一化 bbox（0..1）；无几何信息的来源需改判来源类型或合成 bbox，不能伪造像素坐标。
3. 图片只接受 `data:image/...;base64` 或相对 sidecar 路径；我们的 artifact 绝对路径必须转换后传入。
4. `MiddleJson` 必须显式给 `is_full_document`。
5. 表格正文是 HTML 字符串，不是单元格数组；我们的 `tables[].rows` 需先序列化为 HTML 表。
6. PDF 导出缺 `extensions.docvortex_layout` 时回退 reflow；需要原始版式时须带该扩展，否则结果应标注为语义重排。

### 旧任务补导出

- 从任务 artifact 的 `result/document.json` 重建 IR；不重开源文件、不重跑解析。
- 缺失完整 IR 的历史任务返回明确的不支持原因（如仅有有界预览），不伪造结果。
- 补导出产物作为新 artifact 发布，沿用既有 `storage_key` 与下载鉴权。

### 对外入口

- 任务级导出：`GET /api/v1/tasks/{task_id}/exports?format=docx|epub|pdf|html|latex|markdown|structured_content`，返回 artifact 引用而非内联字节。
- 能力声明：能力接口如实列出当前可用格式；未安装 extra 时明确标注原因。
- MCP 侧复用同一能力查询与导出入口，保持 opaque ID 语义。

---

## 阶段 3：可选解析引擎与质量基准

### Provider 适配

`app/skills/pdf/adapters/docvortex.py`：

- 实现 `Provider.parse(context) -> ProviderResult`，把 `MiddleJson` 映射为 `DocumentResult`（**复用阶段 2 的反向映射**，不维护两套逻辑）。
- `pdf_structure=auto|off` 透传；Tagged PDF / PDF-UA 语义结果原样放入 `DocumentResult.extensions["docvortex_pdf_structure"]`，不丢原始角色与块关联。
- 失败、超时、不可用均返回带错误码的 `ProviderResult`，由既有 fallback 机制降级到 `pdf.normal.pymupdf` 或 `pdf-inspector`。
- 既有 `app/skills/pdf/adapters/mineru.py` 保持"边界已定义、未接线"状态，本版本不改其结论。
- 默认 Provider 保持 `pdf.normal.pdf-inspector`；切换默认需另行评审并附基准证据与回滚开关。

### 解析质量基准

新增 `scripts/benchmark_pdf_providers.py` 与 `tests/benchmarks/`：

- 固定语料清单：`data/` 内真实教材 PDF（原生文字）+ 扫描件样本 + 多栏/跨页表格样本；清单入仓，样本文档不入仓。
- 指标：每页耗时、峰值 RSS、表格结构正确率（人工真值子集）、公式还原率、中文空格脏率、失败率、输出体积。
- 结果写入 `docs/pdf-provider-benchmark.md`，只报实测范围与环境，不做跨机推断。
- 基准不进 PR 门禁，进 nightly；报告随版本归档。

---

## 执行顺序

1. 实现 `DocumentResult → MiddleJson` 映射与最小渲染（markdown/html），与内置渲染器做等价对照。
2. 接入 docx/epub/pdf/latex，逐格式验证结构与公式。
3. 接资产物化与哈希校验；补缺失素材的失败路径。
4. 开放导出端点与能力声明，接 `API_KEY` 与审计；打通旧任务补导出。
5. 复用映射实现 Provider 适配与降级路径，打通 `PDF_NORMAL_PROVIDER` 选择与能力声明。
6. 建语料清单与基准脚本，先跑基线留档，再跑三方对照并出报告。
7. 补 optional extra 打包、镜像开关、配置、文档与迁移说明。

## 验收

**导出（A 组）**

- 同一任务可导出 7 种格式；DOCX 中公式为可编辑 OMML，HTML 为 LaTeX 定界符（MathJax 可渲染），LaTeX 为公式源码。
- EPUB 可在主流阅读器打开且章节层级正确；PDF 正文可选择、表格为可选中文字的结构表；HTML 公式为 LaTeX 定界符（MathJax 可渲染）；LaTeX 表格可编译（需 `longtable`、`colortbl`）。
- 旧任务可补导出且不读取源文件；仅有预览的任务如实报不支持。
- 未安装 `rich-export` 时能力列表如实降级，导出请求返回明确错误码。
- 导出体积、耗时、峰值内存有实测记录；超限路径返回显式错误；无路径泄露。

**引擎（B 组）**

- 三个 PDF Provider 可通过配置切换，任一失败可按既有策略降级且原因可查。
- 扫描件仍进入 OCR 路由，不被新 Provider 路径吞掉。
- `extensions` 中的结构信息在解析、存储、导出全链路保留。
- 基准报告含环境、语料规模、逐指标实测值与局限说明。
- 默认 Provider 未变更；如需变更，附基准证据与回滚方式。

**共同**

- 后端全量测试、前端单测与构建通过；新增失败与降级路径有测试覆盖。
