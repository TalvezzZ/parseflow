# Parse Agent 方案 0.3.0：文件转换闭环

## 目标

增加独立的文件转换能力，为后续文档解析提供格式预处理：

```text
本地 Office 文件
  → office.convert
  → LibreOffice Provider
  → 转换产物
  → 返回统一 ConversionResult
```

## 实现范围

- 新增 `office.convert` Skill
- 新增 `office.convert.libreoffice` Provider
- 新增 `POST /api/v1/convert/office`
- 支持 `doc → docx`
- 支持 `xls/xlsm → xlsx`
- 支持 `ppt → pptx`
- 支持常见 Office → PDF
- 输出到独立目录，不覆盖源文件
- 支持转换超时和明确错误码

## 暂不包含

- 任意格式之间的自由转换
- 图片、音频、视频转换
- 转换任务队列和持久化
- 对象存储上传
- 转换后自动串联解析 Skill

## 请求示例

```json
{
  "file_id": "demo-1",
  "path": "/data/legacy.doc",
  "target_format": "docx",
  "output_dir": "/data/converted"
}
```

## 验收标准

- 能真实调用本机 LibreOffice 完成支持的格式转换
- 不覆盖源文件
- 返回目标文件路径、格式、大小和耗时
- 不支持的格式组合返回明确错误
- LibreOffice 不存在、超时或失败时返回明确错误
- Provider 可以替换，不影响 `office.convert` 接口
