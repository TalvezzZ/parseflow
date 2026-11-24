# DocVortex 能力吸收决策记录

状态：分析完成（2026-11-24）；阶段 0 可行性验证**通过**（见第 6 节）。

本文记录对 [myhloli/DocVortex](https://github.com/myhloli/DocVortex) 的评估结论、吸收范围与不吸收范围，作为 1.6.1–1.8.0 各阶段方案的依据。结论基于其公开 README（中/英）、`pyproject.toml`、`docs/ARCHITECTURE.md`、`docs/JSON_PROTOCOL.md`、`docs/sdk-0.4.md`、PyPI 元数据与仓库结构；**未实跑其基准，未逐行审阅其 Rust 实现**。

## 1. 对方画像（公开事实）

| 维度 | 事实 |
|---|---|
| 形态 | Python 库 + `docvortex` CLI + SDK + 自带 agent skill；**无服务端、无 UI、无任务队列、无 MCP** |
| 输入 | 20 种：PDF、DOC/DOCX/RTF、PPT/PPTX、XLS/XLSX/CSV/TSV、ODT/ODS/ODP、EPUB、HTML/HTM/SHTML、MHTML/MHT、OFD |
| 输出 | 7 种：markdown、html、latex、docx、epub、pdf、structured_content |
| 中间表示 | `ModelJson` / `MiddleJson`（schema 2.0，含 `schema` 身份与 `extensions` 保留），**与 MinerU 共用同一线格式** |
| 渲染 | 公开门面 `docvortex.render.render(MiddleJson) -> RenderOutput`；`render_artifact` 返回 `RenderArtifact`；`export` 负责素材物化与落盘 |
| OCR | **不做**；扫描件返回 `pdf_ocr_unsupported`，要求用户自行安装 MinerU |
| 公式 | 强：`pylatexenc` / `latex2mathml` / `mathml2omml` / `ziamath`，LaTeX↔MathML↔OMML 往返 |
| PDF 结构 | Tagged PDF / PDF-UA 语义 + 阅读顺序；导出支持 `original`/`reflow` 布局 |
| 性能 | Rust 原生扩展（setuptools-rust、PyO3、借用 pypdfium2 函数指针），自称 2.93×、峰值 RSS −31%（自测公开语料，未第三方复现） |
| 分发 | 仅平台 ABI3 wheel，无 sdist；Linux glibc 2.28+ / Windows x64+ARM64 / macOS arm64（0.6 起弃 Intel Mac） |
| 工程治理 | `PUBLIC_API` 静态清单 + 契约测试（42 模块 / 343 符号 / 1508 方法签名冻结）；私有协议号（当前 36）；`tests/` 含 native 契约与 revision benchmark；`tools/` 含发布验证 |
| 生态 | 93 star / 18 fork；2026-09-05 建仓，约 2.5 个月 77 个 release（0.6.26） |

**关键判断**：DocVortex 不是普通转换器，而是 **MinerU 文档内核的独立化**——`ModelJson`/`MiddleJson` 是双方共用的线格式协议，渲染被做成公开门面。因此其渲染能力**可被外部构造的 IR 直接调用**，这是"借力"而非"参考"的前提。

## 2. 与 ParseFlow 的定位差异

- DocVortex 是**解析与渲染引擎**（library/CLI/SDK）。
- ParseFlow 是**解析服务平台**（HTTP 服务 + 工作台 + 持久化任务 + 文件化结果 + MCP + 可观测性）。

两者不是替代关系。ParseFlow 的护城河在任务、存储、校对、审计、多模态与 MCP 服务层；解析与渲染引擎应保持**可替换**。

## 3. 吸收清单

### 3.1 直接借力（引入依赖，隔离为 optional extra）

| # | 能力 | 解决的问题 | 落点 |
|---|---|---|---|
| A1 | 7 格式渲染门面 | 当前只能出 md/txt/json/csv | `app/render/renderers/docvortex.py` |
| A2 | 数学公式链路（LaTeX/MathML/OMML） | 公式在 DOCX/HTML/LaTeX 中不可用 | 随 A1 到位 |
| A3 | `is_generated_cjk_space` 判据 | 中文 PDF 抽取的字间多余空格 | `app/render/text.py` |
| A4 | 表格续接 `continues_prev` + 表格区域恢复 | 跨页表格被切断且无合并语义 | `DocumentResult.tables[]` |
| A5 | ODF/EPUB/OFD/MHTML 输入分析 | 格式清单四个空洞（OFD 对政企尤重） | 新增 Provider 适配器 |
| A6 | Tagged PDF / PDF-UA 语义与阅读顺序 | 多栏、脚注、跨页段落顺序不稳 | `pdf.normal.docvortex` 可选 Provider |

A3 为单函数、无重依赖，可独立于 A1 先行验证；A1 是收益最大项。

### 3.2 设计借鉴（不引入依赖）

| # | 设计 | 我们的现状 | 动作 |
|---|---|---|---|
| B1 | 分析 / 渲染 / 落盘三层解耦，层间只传可序列化枢纽 | 解析与 representation 生成揉在 Skill 内 | 1.6.1 抽 `app/render/` |
| B2 | `render`（内存产物）与 `export`（落盘+素材+哈希）分离 | `generate_document_exports` 一函数全包 | 1.6.1 拆分，保留预算与原子发布 |
| B3 | 协议带 `schema` 身份与版本号 | `result/manifest.json` 无 schema 字段 | 1.6.1 增字段 + schema 快照 |
| B4 | 未知字段不丢弃（`extensions` 原样保留） | 有 `extensions` 字段，无保留契约 | 1.6.1 契约测试 |
| B5 | 静态公开 API 清单 + 签名冻结契约测试 | Provider/Skill 仅口头契约 | 1.9.0 |
| B6 | 重构不改协议、改协议必升版本 | 无判定规则 | 1.6.1 写入方案约束 |
| B7 | Bundle 载入后可再导出新格式 | 有 `result/manifest.json`，但老任务不能补导出 | 1.7.0 随 A1 成立 |
| B8 | 原生边界纪律（纯计算不含业务、桥接只搬运、借用不接管、GIL 显式释放） | 无原生扩展，暂无此问题 | 未来上原生时执行 |
| B9 | 基准测试与发布验证工具进仓库 | 有 305 测试，缺质量基准 | 1.7.0 建 `tests/benchmarks/` |

### 3.3 明确不吸收

1. **不做 OCR、甩给外部**：PaddleOCR + 版面切图 + 校对视图是差异点，不自废。
2. **只做库不做服务**：服务层是 ParseFlow 的价值所在。
3. **高频破坏性迭代**（2.5 个月 77 个 release、废弃环境变量）：与 1.x 契约承诺冲突。
4. **重依赖进主依赖**（numpy/reportlab/onnxruntime/lxml/ziamath）：必须 optional extra 隔离，避免镜像膨胀。

## 4. 风险与前置条件

| 风险 | 说明 | 应对 |
|---|---|---|
| 技术赌点 | 能否用我们自己的数据构造合法 `MiddleJson` 并在不重开源文件的前提下渲染 | 阶段 0 门禁验证；不通过则 v1.7.0 降级为自研 renderer |
| 许可 | README 写 MIT，`pyproject.toml` 的 `license` 字段为 `MIT AND Apache-2.0 AND BSD-3-Clause` | 仅以 optional extra 方式依赖，由用户自行安装；随包分发前须法务确认 |
| 版本漂移 | 0.6.x 高频迭代且废弃过环境变量 | 锁定小版本 + 契约测试 + 升级需过基准 |
| 平台收窄 | 无 sdist、弃 Intel Mac、不支持 free-threaded | 不进入默认安装路径；Docker 内固定平台 |
| 依赖体积 | 主依赖含 numpy/reportlab/onnxruntime 等 | optional extra；记录安装体积基线 |

## 5. 阶段划分与文档索引

版本号按 [版本策略](versioning.md) 确定：内部重构与修复走 PATCH，新增对外能力走 MINOR；共享依赖与验收面的工作合并交付，不拆成多个 MINOR。

| 阶段 | 内容 | 版本 | 级别 | 文档 |
|---|---|---|---|---|
| 0 | 可行性验证（门禁） | 1.6.1 内 | — | [plan-1.6.1](plan-1.6.1.md) |
| 1 | 渲染层解耦 + 中文/表格质量修复 + schema 版本 | 1.6.1 | PATCH | [plan-1.6.1](plan-1.6.1.md) |
| 2 | 富格式导出（7 格式） | 1.7.0 | MINOR | [plan-1.7.0](plan-1.7.0.md) |
| 3 | 可选解析引擎接入 + 三方基准 | 1.7.0（与阶段 2 合并） | MINOR | [plan-1.7.0](plan-1.7.0.md) |
| 4 | 格式覆盖扩展 + 公开契约冻结 + 分发（按需） | 1.9.0（未排期） | MINOR | [plan-1.9.0](plan-1.9.0.md) |

阶段 2 与阶段 3 合并的原因：两者共用同一个 optional extra、同一套 `DocumentResult ↔ MiddleJson` 映射代码与同一套验收环境，拆分只会制造两次发布而无新增交付价值。

## 6. 阶段 0 验证结论（2026-11-24，通过）

**结论：通过。** 在不读取任何源文档的前提下，用外部构造的 `MiddleJson` 成功渲染出全部 7 种格式，技术赌点解除，v1.7.0 按借力方案推进（不改自研路线）。

验证脚本：[`scripts/spike_docvortex_render.py`](../scripts/spike_docvortex_render.py)。环境：docvortex 0.6.26、Python 3.12.12、macOS arm64、独立 venv（未进入项目依赖）。注意：本机直连 `files.pythonhosted.org` 超时，安装走清华镜像。

### 6.1 结果

| 格式 | 字节 | 耗时 | 结构抽查 |
|---|---:|---:|---|
| markdown | 473 | 74 ms | 表格 / 标题 / 图片 ✓ |
| html | 14 020 | 103 ms | 表格 ✓ 公式 ✓ 图片 ✓ |
| latex | 1 664 | 4 ms | `\begin{document}` / `longtable` / `frac` ✓ |
| docx | 38 342 | 115 ms | OMML ✓ `w:tbl` ✓ `word/media` ✓ |
| epub | 3 664 | 7 ms | `application/epub+zip` ✓ nav ✓ |
| structured_content | 1 282 | 1 ms | 顶层键完整 ✓ |
| pdf | 13 001 | 417 ms | `%PDF-` ✓ |

`MiddleJson` 的 `to_dict()` / `from_dict()` 往返一致。

### 6.2 接入约束（实测，映射层必须满足）

1. 顶层块必须带 `index`；**子块 `index` 必须等于父块 `index`**（如 `table_body` 与 `table`）。
2. `file_suffix="pdf"` 时顶层块必须带**归一化 bbox（0..1）**；像素坐标被拒绝。无几何信息的纯文本来源无法直接声明为 pdf。
3. 图片只接受 `data:image/<type>;base64,...` 或**相对** sidecar 路径；绝对路径被安全校验拒绝。我们的 artifact 是绝对路径，必须转换。
4. `MiddleJson` 必须显式给 `is_full_document`。
5. 表格正文是 **HTML 字符串**（`TableBodyBlock.content`），不是单元格数组。
6. PDF 导出缺少 `extensions.docvortex_layout` 时回退 `reflow` 并记录诊断；要保留原始版式必须带该扩展。
7. HTML 的公式是 **LaTeX 定界符 + `docvortex-math` 类**（供 MathJax 客户端渲染），不是服务端 MathML；LaTeX 表格用 `longtable` + `\cellcolor`，渲染需 `colortbl` 宏包。

### 6.3 体积

site-packages 共 **281 MB / 93 个包**；docvortex 自身 22 MB，onnxruntime 85 MB、numpy 34 MB、lxml 20 MB、fontTools 20 MB、Pillow 13 MB、reportlab 8.8 MB。即使只用渲染也会拉入 onnxruntime，因此必须走 optional extra 隔离，不能进主依赖。
