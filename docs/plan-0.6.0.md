# Parse Agent 方案 0.6.0-A：文件即任务的无模型自动规划

## 用户入口

主入口只要求上传一个文件：

```http
POST /api/v1/parse
Content-Type: multipart/form-data
```

必填：`file`。可选：`goal`、`data_id`、`callback`。

调用方不传 `path`、`file_id`、`skill_name`、`provider`、`plan_id`。上传服务内部创建受控文件记录和 `parse.intent` 任务，立即返回 `task_id`。

## 生命周期

```text
上传文件 → queued → planning → running → succeeded | partial | failed
```

任务是外层生命周期容器；计划是任务内部产物：

```text
TaskRecord
├── request
├── ParsePlan
│   └── PlanStep[]
├── result
└── callback
```

## RuleBasedPlanner

第一版无需 LLM，按文件后缀选择已注册的顶层 Skill 或 Pipeline：

```text
.doc/.xls/.ppt → office.parse_pipeline
.docx → word.parse
.xlsx/.xlsm → excel.parse
.pptx → ppt.parse
.rtf → rtf.parse
.txt/.md/.csv/.tsv/.html/.xml → text.parse
音视频 → audio.prepare / video.prepare
```

只允许生成已注册顶层能力；Planner 不调用 Provider、不执行 shell、不绕过受控文件存储。

## 执行与结果

Worker 内生成计划并写回 `TaskRecord.plan`，再执行唯一顶层 PlanStep。任务查询、callback 都包含计划、步骤状态和最终结果。旧格式转换、复杂 RTF 路由等内部细节仍由相应 Skill 或 Pipeline 负责。

## MCP

- `preview_parse_plan`：预览规则路由，不执行；
- `submit_parse_intent`：对 MCP 可访问的本地路径提交自动任务。

## 非目标

- LLM Planner；
- 多步骤 DAG、并行步骤、动态重规划；
- 持久化 Task/Plan；
- 运行中取消；
- Agent 直接调用 Provider。
