# ParseFlow 1.4.0 技术方案：存储生命周期治理与 OCR 校对补完

## 目标

修复 `v1.3.1` 暴露出的两个真实缺陷：任务删除后本地存储不回收，以及公开结果中残留服务器绝对路径；同时把上一个提交只做了一半的 OCR 页面预览补成可用、可测的校对能力。本版本不引入新的解析模型，也不改变已冻结的 v1 契约语义（仅做收紧：移除内部字段）。

供应链与发布自动化（SBOM、签名、GHCR、publish workflow）顺延为 `v1.5.0`，待本版本稳定后单独立项。

## 背景：为什么这些是缺口

### 存储不回收

`PersistentTaskManager.delete()` 只删除任务记录与事件：

```python
await self.repository.delete(task_id)
await self.events.delete(task_id)
```

既不删除 `data/artifacts/{task_id}/`，也不删除 `data/files/{file_id}/`。`LocalFileStore.delete()` 只在提交失败时被调用。`StorageJanitor` 仅处理超过 TTL 的任务，手动 DELETE 不在覆盖范围内。

在真实运行数据上已观察到：

| 目录 | 数量 | 说明 |
|---|---|---|
| `data/tasks/*.json` | 38 | 现存任务记录 |
| `data/artifacts/*/` | 426 | 其中 388 个已无对应任务记录 |
| `data/files/*/` | 584 | 上传源文件，从未回收 |

### 公开结果泄露服务器路径

`_public_document()` 的屏蔽集合是：

```python
forbidden = {"path", "output_dir", "source_path", "target_path", "artifact_path", "callback"}
```

**不包含 `artifact_dir`**。而 `mammoth.py`、`openpyxl.py`、`python_pptx.py` 都把 `artifact_dir` 写进 `document.metadata`，因此公开任务记录里会出现：

```json
"metadata": {"artifact_dir": "/Users/<user>/.../data/artifacts/task_xxx/files", ...}
```

38 条现存记录中有 13 条受影响。这违反 `docs/features.md` 与 roadmap 5.1 节"公开接口不暴露服务器真实路径"的既定决策。

### OCR 校对只完成了一半

`v1.3.1` 的提交让 Provider 在页面级输出 `width`/`height`/`preview`，并新增了前端 `OcrProofing` 组件，但：

- 前端 `hasOcrOverlay()` 硬编码 `document_type === 'image'`，而扫描 PDF 的 OCR 结果是 `document_type === 'pdf'`，其 block 同样带 `bbox` 与 `confidence` —— **扫描 PDF 有坐标却没有校对入口**；
- 前端读取的是文档级 `document.metadata.width/height`，而 PDF 的尺寸写在 `pages[i]` 上，因此 PDF 即使放开类型判断也无法渲染；
- 多页 PDF 没有页切换 UI；
- 后端 preview 字段没有任何测试断言，前端画布绘制逻辑零覆盖。

## 主要交付

### 1. 存储生命周期治理

- 删除终态任务时，在同一操作中回收 `data/artifacts/{task_id}/`、对应上传源文件目录与事件文件；
- 回收失败不得让删除接口失败：记录失败并保持任务记录已删除的一致性；
- 保留"运行中任务不可删除"的既有约束；
- `StorageJanitor` 增加孤儿回收：对不存在对应任务记录的 artifact 目录与上传文件目录进行保守清理（需有安全时间阈值，避免与正在提交的任务竞争）；
- 清理动作写入可观测计数，便于运维核对。

### 2. 公开结果去除服务器路径

- 将 `artifact_dir` 加入公开投影的屏蔽集合；
- 增加契约测试：对包含 `artifact_dir`/`output_dir`/`path` 等键的文档执行公开投影后，序列化结果不得包含绝对路径；
- 确认 `tests/test_release_http_safety.py` 与 `tests/test_tasks.py` 中"公开输入无 path/callback/output_dir"的既有断言继续通过。

### 3. OCR 校对补完

- 校对入口判定改为"结果中存在带 `bbox` 的文本块"，不再限定 `document_type`；
- 前端尺寸与预览图来源改为页面级 `pages[i].width/height` 与 `pages[i].preview`，文档级 `metadata` 仅作回退；
- 增加多页切换控件，页面切换时同步更新标注与重建版面；
- 去掉公开结果中的 `relative_path`，只保留展示所需的 `filename`；
- 修正 `preview.content_type`：图片 OCR 不应退化为 `application/octet-stream`；
- 无可用预览图时给出明确说明，而不是空白区域。

### 4. 交互可访问性

- 解析方式选择器（`aria-haspopup="listbox"`）补齐键盘模式：上下方向键移动、Home/End 跳转、Escape 关闭并把焦点还给触发按钮、选择后关闭并回焦；
- 保持与既有 Tabs 相同的可访问性标准。

## 测试与完成定义

- 后端：
  - 删除任务后 artifact 目录、上传文件目录、事件文件均不再存在；
  - 删除运行中任务仍返回冲突错误；
  - 孤儿目录回收在超过安全阈值后被清理，未超阈值时保留；
  - 公开投影对 `artifact_dir` 等内部键的屏蔽有断言；
  - OCR preview 的生成、`content_type` 与"无 artifact 目录时不写 preview"有断言。
- 前端：
  - 校对视图在图片与扫描 PDF 两类结果上均可渲染；
  - 多页切换更新选中页与标注；
  - 画布绘制逻辑可测试（不依赖真实 canvas 实现时需明确降级路径）；
  - 选择器方向键/Home/End/Escape 行为有单测。
- 浏览器 E2E：覆盖一次 OCR 结果的校对页签打开与页面切换。
- 全量 `uv run pytest`、`npm test`、`npm run build` 通过；OpenAPI 快照随契约变更更新。

## 不在本版本范围

- SBOM、Cosign 签名、GHCR 发布、publish workflow（顺延 `v1.5.0`）；
- 多用户权限、对象存储、分布式队列；
- ASR 实际转写；
- LLM 规划接入（`create_parse_agent` 的取舍单独决策）。
