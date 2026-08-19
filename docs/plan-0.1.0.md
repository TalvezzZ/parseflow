# Parse Agent 方案 0.1.0：PDF 普通解析闭环

## 目标

完成第一条可运行的 PDF 普通解析链路：

```text
本地 PDF 路径
  → pdf-inspector 检测
  → text_based 路由
  → pdf-inspector.process_pdf
  → 统一 DocumentResult
  → API 返回
```

## 实现范围

- 新增 `POST /api/v1/parse/pdf`
- 请求使用服务本地可访问的 PDF 路径
- 使用 `pdf-inspector.detect_pdf` 判断 PDF 类型
- `text_based` 使用 `pdf-inspector.process_pdf` 普通解析
- 使用 `extract_pages_markdown` 保留页面边界
- 输出统一页面、文本块、Markdown、解析器信息和质量指标
- 解析失败返回统一 `SkillResult` 错误
- 非文本型 PDF 返回明确的 `ocr_not_supported` 错误

## 暂不包含

- 扫描 PDF 的 OCR 执行
- MinerU 云端 API 连接
- 文件上传、对象存储和任务队列
- PDF 页级混合路由

非文本 PDF 当前不会进入 OCR Provider，而是返回明确的 `ocr_not_supported` 错误，避免误把普通解析当成 OCR 结果。

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
- 不使用 PyMuPDF
- `pdf.parse` 的公共接口不依赖具体 Provider 实现
