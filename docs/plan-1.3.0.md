# ParseFlow 1.3.0 技术方案：工作台体验、可访问性与浏览器 E2E

## 目标

将现有工程工作台提升为适合日常审核与运营的任务中心，同时不把 localStorage 作为任务事实来源。服务端任务 API 仍是唯一权威数据源。

## 主要交付

### 任务中心

- 完整任务历史筛选：状态、文件类型、日期、Provider、错误码和文本查询。
- 任务详情支持深链接 `/tasks/{task_id}`，刷新后恢复。
- 终态、partial、failed、cancelled、interrupted、deleted/expired 的专属提示和恢复动作。
- 在 UI 中接入 cancel、retry、delete，并遵守服务端状态约束。

### 结果体验

- Artifact 图片预览与懒加载；其他类型安全下载。
- 大表格分页/虚拟滚动、列头、caption、CSV/JSON 导出。
- JSON 折叠、搜索、复制；HTML/XML 继续采用 sandboxed iframe。
- 执行详情展示 plan、timeline、provider、warning、metrics 和 error code。

### 上传与轮询

- capability/config 端点驱动文件后缀和大小提示。
- 上传进度、取消上传、稳定错误码映射、request ID 显示。
- 运行任务快速轮询、长任务退避、visibility 恢复立即刷新、网络失败 stale 状态提示。

### 可访问性

- aria-live 用于上传、错误、复制、终态和后台状态变更。
- 失败聚焦错误摘要，新任务创建后聚焦标题。
- Tabs 完整支持左右键、Home/End、tablist/tab/tabpanel 关系。
- 关键状态不只使用颜色；移动端不隐藏任务和服务状态。

## 测试与完成定义

- Vitest/RTL 覆盖 API 错误归一化、时间线、状态、artifact、结果 tabs、分页与键盘行为。
- Playwright Chromium 使用真实后端验证上传、轮询、刷新/深链接、retry、delete、artifact 下载。
- axe 检查核心流程无 critical/serious 问题。

## 实施说明

任务历史筛选和 cursor 分页全部由 `/api/v1/tasks` 提供，浏览器 URL 只保存可分享的筛选状态，不保存任务事实。`/api/v1/capabilities` 提供允许后缀、上传/结果大小预算和支持的筛选字段。Playwright 启动隔离的真实 FastAPI 后端和 Vite 前端，Chromium 流程覆盖上传、后台轮询、深链接刷新与焦点、artifact 下载、重试、删除和 axe；CI 明确安装浏览器，不依赖开发机环境。
