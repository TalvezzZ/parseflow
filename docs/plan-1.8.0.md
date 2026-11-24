# ParseFlow v1.8.0 技术方案：资产投递（图片对外访问）

状态：方案（未实现）。版本定位：**MINOR**（新增端点 + 新增配置项，见 [版本策略](versioning.md)）。

本版本解决"解析结果里的图片只能在本地取"的问题：给图片一个**有时效、可对外访问**的地址；没有对象存储时也能用；需要内联时按需取 base64，且不撑爆响应。

## 目标与边界

- 图片元数据中新增 `asset_id`、`url`、`url_expires_at`、`inline_url`，均为**附加字段**，不改变既有字段语义。
- 提供两种取用方式：签名 URL 直接取二进制；`format=data-uri` 取 base64。
- 默认不返回任何 base64；结果信封、任务 JSON、`result/document.json` 中**永远不含 base64**。
- 本版本不实现对象存储同步；`S3CompatibleProvider` 只保留接口位置，不接线、不宣称可用。
- 不改变 v1 既有契约；`path`/`artifact_dir` 等绝对路径继续在公开响应中剥离。

## 现状（已具备的基础）

- 图片条目已有 `sha256`、`relative_path`、`content_type`、`width/height`、`size_bytes`、`page_number`。
- `_public_document()` 已剥离 `path`、`artifact_dir` 等绝对路径。
- 已有受鉴权的资源读取链路：`resolve_result_resource(workspace, storage_key)` 拒绝越界与符号链接。
- 任务 artifact workspace 是图片的权威存储，路径形如 `data/artifacts/{task_id}/files/images/*.png`。

## 三个必须避开的约束

1. **base64 不进结果。** `bounded_preview` 总预算 256KB 且会截断长字符串，一张 200KB 图片的 base64 会挤掉 `pages`/`tables`。
2. **任务 JSON 是持久化的。** base64 进结果会写入 `data/tasks/{id}.json` 与 `result/document.json`，磁盘与查询成本一起放大。
3. **列表接口重复发送。** 任务列表、结果查询、MCP 每次调用都会带上，必须保持每条百字节级。

## 投递模式

| 模式 | 结果字段 | 单条体积 | 适用 |
|---|---|---|---|
| `none` | 仅 `asset_id` 与既有元数据 | ~250B | 批量查询、任务列表 |
| `url`（默认） | `url` + `url_expires_at` | ~350B | 前端 `<img>`、外部系统 |
| `inline` | **不在结果中**，走独立端点 | 按需 | MCP/LLM、离线导出 |

`ASSET_DELIVERY` 控制结果里是否带 URL；`inline` 不是结果模式，而是端点参数。

## URL 提供者

```text
AssetUrlProvider (协议)
├─ SignedLocalProvider    本地文件 + HMAC 签名 URL（本版本实现）
└─ S3CompatibleProvider   OSS/COS/MinIO/R2 预签名（仅保留接口，未接线）
```

签名算法：`sig = base64url(HMAC-SHA256(secret, "{task_id}|{asset_id}|{exp}"))[:32]`。
`secret` 取 `ASSET_SIGNING_SECRET`，留空时复用 `API_KEY`；两者皆空时**不签发 URL**（返回 `asset_url_unavailable`），不降级为无签名地址。

## 端点

```text
GET /api/v1/tasks/{task_id}/assets/{asset_id}?exp={epoch}&sig={signature}
    -> 200 二进制，Content-Type 取自图片元数据，Cache-Control: private, max-age={ttl}

GET /api/v1/tasks/{task_id}/assets/{asset_id}?exp=…&sig=…&format=data-uri
    -> 200 {"data_uri": "data:image/png;base64,…", "content_type": …, "size_bytes": …}
```

`asset_id` 形如 `sha256-<hex>`。解析顺序：校验签名与过期 → 在任务结果中按 `sha256` 找 `relative_path` → `resolve_result_resource` 取文件 → 校验大小预算。

错误码：`asset_signature_invalid`、`asset_url_expired`、`asset_not_found`、`asset_too_large_for_inline`、`asset_url_unavailable`。

## 预算

| 配置 | 默认 | 超限行为 |
|---|---:|---|
| `ASSET_LIST_MAX` | 100 | 截断并置 `images_truncated=true`，同时给出 `images_total` |
| `ASSET_INLINE_MAX_BYTES` | 524288 | 413 `asset_too_large_for_inline` |
| `ASSET_INLINE_MAX_COUNT` | 10 | 413（按请求计；本版本单资产端点用不到，为将来批量端点保留） |
| `ASSET_URL_TTL_SECONDS` | 900 | 过期返回 403 `asset_url_expired` |

## 安全红线

1. 签名 URL 必须校验 `exp` 与 `sig`，两者任一不通过即拒绝。
2. `relative_path` 必须经 `resolve_result_resource` 解析，禁止直接拼接 `storage_key`。
3. 预签名/签名 URL **不写入持久化结果**，只在响应时即时生成。
4. 不提供无签名公开地址；对象存储若接入，bucket 必须私有且仅走预签名。
5. `asset_id` 使用 `sha256`，不额外暴露内部目录结构。

## 配置项

```text
ASSET_DELIVERY=url|none        # 默认 url；none 时结果只给 asset_id
ASSET_SIGNING_SECRET=          # 留空复用 API_KEY
ASSET_URL_TTL_SECONDS=900
ASSET_LIST_MAX=100
ASSET_INLINE_MAX_BYTES=524288
ASSET_INLINE_MAX_COUNT=10
```

## 实施顺序

1. `app/assets/delivery.py`：签名签发/校验、`AssetUrlProvider` 协议、`SignedLocalProvider`、data URI 编码与预算检查。
2. `GET /api/v1/tasks/{task_id}/assets/{asset_id}` 二进制与 data-uri 两个分支。
3. 结果投影注入 `asset_id`/`url`/`url_expires_at`/`inline_url`/`images_total`（在 `_public_document` 之后注入，不改 Provider）。
4. 配置项、`.env.example`、能力声明。
5. 测试：签名通过/篡改/过期、路径穿越、超大图、无 sha256 的图片、`ASSET_DELIVERY=none`、列表截断计数。

## 验收

- 默认响应中图片条目含 `asset_id` 与签名 `url`，体积不超过 ~400B/条；不含任何 base64。
- 签名 URL 可直接被浏览器 `<img>` 使用；篡改 `asset_id`/`exp`/`sig` 均返回 403，过期返回 403。
- `format=data-uri` 在预算内返回合法 data URI，超预算返回 413 且不产生部分内容。
- `ASSET_DELIVERY=none` 时结果不含 URL，其余字段不变。
- 结果信封、任务 JSON、`result/document.json` 中检索不到 `data:image`。
- 既有测试全部通过；新增端点与签名逻辑有独立测试覆盖。
