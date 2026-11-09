# Parse Agent 方案 0.1.0：PDF 普通解析闭环

## 目标

完成第一条可运行的 PDF 普通解析链路：

```text
本地 PDF 路径
  → pdf-inspector 检测
  → text_based 路由
  → 根据 PDF_NORMAL_PROVIDER 选择普通解析 Provider
      ├── pdf.normal.pdf-inspector：文本、Markdown、版面和表格
      └── pdf.normal.pymupdf：文本和嵌入图片，无 OCR
  → 统一 DocumentResult
  → API 返回
```

## 实现范围

- 新增 `POST /api/v1/parse/pdf`
- 请求使用服务本地可访问的 PDF 路径
- 使用 `pdf-inspector.detect_pdf` 判断 PDF 类型
- `text_based` 解析由 `PDF_NORMAL_PROVIDER` 选择 `pdf.normal.pdf-inspector` 或 `pdf.normal.pymupdf`
- pdf-inspector Provider 使用 `extract_pages_markdown` 保留页面边界
- PyMuPDF Provider 提取原生文本与嵌入栅格图片，图片保存为 artifact 并记录页码、坐标和格式；不使用 OCR
- 输出统一页面、文本块、图片块、图片资产、Markdown、解析器信息和质量指标
- 解析失败返回统一 `SkillResult` 错误
- 非文本型 PDF 路由至已配置的 OCR Provider；未启用 OCR 时返回明确的 `ocr_not_configured` 错误

## 暂不包含

- MinerU 云端 API 连接
- 文件上传、对象存储和任务队列
- PDF 页级混合路由

非文本 PDF 路由到已启用的 OCR Provider；PyMuPDF 普通解析不执行 OCR。

## 请求示例

```json
{
  "file_id": "demo-1",
  "path": "/data/demo.pdf",
  "filename": "demo.pdf"
}
```

## 验收标准

- 文本型 PDF 能通过 API 返回 `success`
- 结果包含页级文本和统一解析器信息
- 检测异常、文件不存在和解析失败均有明确错误
- 选择 PyMuPDF Provider 时，嵌入栅格图能写入 artifact，并在 `DocumentResult.images` 与图片块中返回页码、坐标
- `pdf.parse` 的公共接口不依赖具体 Provider 实现
