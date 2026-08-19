# Parse Agent 方案 0.0.1

## 1. 版本说明

本文件描述 Parse Agent 的第一版架构方案，版本为 `0.0.1`。

> 该方案已在 `0.1.0` 迭代中落地为：普通解析使用 `pdf-inspector.process_pdf`，OCR Provider 保持可插拔。

本版本的目标是建立一个可插拔的文档解析集成层，而不是自行实现 PDF、OCR、Office 或音视频解析算法。

项目负责统一接口、能力发现、解析路由、失败处理、结果标准化和 Agent 编排；具体解析能力由外部开源实现提供。

## 2. 目标流程

```text
文件输入
  → 文件类型识别
  → Agent 制定任务计划
  → 选择文件类型 Skill
  → Skill 选择具体 Provider
  → 执行解析
  → 结果校验
  → 失败降级或切换 Provider
  → 输出统一文档结果
```

LangChain 用于 Agent 的计划和编排。确定性的文件检测、OCR 判断、Provider 选择和结果校验不交给 LLM 猜测。

## 3. 核心抽象

### 3.1 Agent

Agent 负责：

- 理解用户目标
- 制定任务计划
- 选择顶层 Skill
- 安排多个 Skill 的执行顺序
- 汇总任务结果

Agent 不直接判断 PDF 是否需要 OCR，也不直接选择具体解析库，不绕过 Skill 的校验和安全策略。

### 3.2 Skill

Skill 表示稳定的业务能力，对 Agent 暴露。第一版规划的公共 Skill 为：

```text
pdf.parse
office.parse
image.parse
audio.transcribe
video.transcribe
document.summary
semantic.chunking
```

第一版优先实现 `pdf.parse`。

### 3.3 Provider / Adapter

Provider 表示一个具体的开源实现，Adapter 负责把外部库或服务接入统一接口。

例如：

```text
pdf.normal.pymupdf
pdf.normal.pdfplumber
pdf.ocr.mineru
pdf.ocr.paddleocr
pdf.ocr.rapidocr
```

Provider 不直接暴露给 Agent，而由对应的文件类型 Skill 内部选择。Provider 可以替换、增加或通过配置切换，而不改变 Skill 的公共接口。

## 4. PDF 解析设计

### 4.1 公共入口

Agent 只选择：

```text
pdf.parse
```

`pdf.parse` 内部负责：

1. 调用 `pdf-inspector`
2. 判断普通解析或 OCR 模式
3. 根据配置和能力选择 Provider
4. 执行解析
5. 校验结果质量
6. 失败时切换 fallback Provider
7. 转换为统一 `DocumentResult`

### 4.2 默认路由

```text
text_based  → 普通解析 Provider
scanned     → OCR Provider
image_based → OCR Provider
mixed       → OCR Provider
检测异常     → OCR Provider
```

第一阶段不做 PDF 页级混合解析。`mixed` PDF 整体交给 OCR 能力更完整的 Provider，页级路由留到后续版本。

### 4.3 Provider 选择顺序

```text
请求中显式指定
  → 应用配置指定
  → 默认 Provider
  → fallback Provider
```

显式指定的 Provider 仍需经过能力校验；不支持当前文件或解析模式时应明确失败，而不是静默切换。

## 5. 可插拔 Provider

Provider 至少需要声明：

- 名称和版本
- 支持的文件类型
- 解析模式：`normal`、`ocr` 或 `both`
- 支持的输出能力
- 依赖信息
- 优先级或选择权重

抽象接口示意：

```python
class ParserProvider(Protocol):
    name: str
    version: str
    capabilities: set[str]

    async def parse(self, context: ParseContext) -> ProviderResult:
        ...
```

第一版先使用显式注册方式：

```python
registry.register(PymupdfProvider())
registry.register(MineruProvider())
```

待接口稳定后，再考虑使用 Python entry points 或独立插件包实现自动发现。

## 6. 统一结果模型

所有解析 Provider 都必须转换为统一结果：

```text
DocumentResult
├── document_id
├── source_file
├── document_type
├── pages
├── blocks
├── tables
├── images
├── metadata
├── parser
├── warnings
├── quality
└── provenance
```

统一结果至少能够表达：

- 页级内容
- 文本块、标题和列表
- 表格
- 图片引用或图片产物
- 来源页码和坐标
- 实际使用的 Provider
- 解析警告
- 文本量、页数和完整度等质量指标

Provider 特有能力放入 `extensions: dict[str, Any]`，避免为了统一接口丢失实现差异。

## 7. 失败和降级

```text
选择主 Provider
  → 执行
  → 校验结果
  → 质量不合格
      → 切换 fallback Provider
  → 仍然失败
      → 返回 failed 或 partial
```

需要区分以下错误：

- Provider 执行失败
- Provider 超时
- 结果质量不足
- 文件损坏
- 外部依赖不可用
- Provider 不支持当前能力

结果中必须保留错误码、警告、执行记录和实际使用的 Provider。

## 8. 0.0.1 实现范围

第一版只实现一条完整的 PDF 垂直链路：

```text
本地 PDF
  → pdf-inspector
  → PDF 路由
  → 普通解析 Provider / OCR Provider
  → 统一 DocumentResult
```

包含：

- `pdf.parse` 公共 Skill
- Provider 抽象接口
- Provider 注册表
- `pdf-inspector` 集成
- 普通解析 Provider 接口
- OCR Provider 接口
- 至少一个普通解析适配器
- 至少一个 OCR 适配器
- Provider 配置切换
- fallback 机制
- 结果质量校验
- 路由、切换、失败和结果校验测试
- LangChain Agent 基础编排入口

即使没有 LLM 配置，确定性的 PDF 解析流程也应能够运行。Agent 是编排能力，不应成为基础解析链路的硬依赖。

## 9. 暂不实现

以下内容不属于 `0.0.1`：

- 对象存储
- 任务队列
- 数据库持久化
- 分布式执行
- PDF 页级混合解析
- 全部 Office、音视频能力
- 复杂插件自动安装
- 生产级权限、鉴权和配额
- 完整知识库入库流程

## 10. 建议目录结构

```text
app/
├── agent/
│   ├── factory.py
│   └── planner.py
├── orchestration/
│   ├── executor.py
│   └── router.py
├── documents/
│   ├── models.py
│   └── results.py
├── skills/
│   ├── base.py
│   ├── registry.py
│   └── pdf/
│       ├── skill.py
│       ├── policy.py
│       ├── providers.py
│       └── adapters/
│           ├── inspector.py
│           ├── pymupdf.py
│           └── mineru.py
└── config.py
```

## 11. 验收标准

`0.0.1` 完成的判断标准：

- Agent 只需要选择 `pdf.parse`
- `pdf.parse` 能自动调用 `pdf-inspector`
- 不同 PDF 类型能进入不同解析模式
- 普通解析和 OCR Provider 可以通过配置切换
- 主 Provider 失败后可以切换备用 Provider
- 所有 Provider 输出相同的 `DocumentResult`
- 结果中包含检测证据、实际 Provider、警告和质量指标
- 没有 LLM 配置时，确定性 PDF 流程仍可运行
- 路由、Provider 切换、失败处理和结果校验都有自动化测试

## 12. 核心原则

```text
Agent 选择能力
Skill 编排流程
Provider 提供实现
Adapter 负责集成
统一模型负责输出
```
