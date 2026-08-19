# Parse Agent 方案 0.2.0：DOCX 普通解析闭环

## 目标

完成第一条 Word 文档解析链路：

```text
本地 DOCX
  → Mammoth
  → HTML
  → Markdown / 纯文本 / 结构化 blocks
  → 图片产物
  → 统一 DocumentResult
  → API 返回
```

## 实现范围

- 新增 `word.parse` 公共 Skill
- 新增 `word.normal.mammoth` Provider
- 新增 `POST /api/v1/parse/docx`
- 输出 HTML、Markdown 和纯文本三种 representation
- 输出标题、段落、列表、表格、图片 blocks
- 将 DOCX 图片保存为独立 artifact，并记录相对路径
- 支持 Provider 配置切换和 fallback 接口

## 暂不包含

- `.doc` 老格式转换
- LibreOffice 转换
- 页面级分页和精确坐标
- 修订、批注、目录域和复杂 Word 布局还原
- DOCX 图片上传对象存储

## 验收标准

- DOCX 能通过 API 返回 `success`
- 结果同时包含 HTML、Markdown 和纯文本
- 标题、正文、列表、表格能够进入统一 blocks
- 图片能够落盘并出现在 `DocumentResult.images`
- Provider 不可用或解析失败时返回明确错误
- 不影响现有 PDF 普通解析链路
