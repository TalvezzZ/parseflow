# ParseFlow 1.4.0 技术方案：供应链、部署交付与发布自动化

## 目标

将已冻结的 v1 契约转化为可审计、可复现、可安全交付的发行流程。该版本优先完善 CI/CD、镜像和发布证据，不改变业务解析模型。

## 主要交付

### CI 门禁

- 后端、前端、OpenAPI/MCP/schema drift、Docker Compose、wheel/sdist 和文档链接检查。
- Linux AMD64 Docker 构建与运行时 smoke。
- Playwright Chromium E2E 以及失败截图、trace、后端日志归档。
- 依赖与镜像漏洞扫描；高危/严重漏洞默认阻断。

### 供应链证据

- 生成 SPDX 或 CycloneDX SBOM。
- 发布 wheel/sdist checksums、镜像 digest、provenance。
- 使用 GitHub OIDC 与 Cosign keyless 签名镜像、SBOM 和 provenance。
- 安全例外集中于 `security/allowlist.yaml`，并要求 CVE、理由、批准人、追踪 issue 和过期时间。

### 镜像与 Release

- 发布精确版本、minor 和 latest GHCR tag。
- 从 registry 按 digest 拉取后执行独立 smoke。
- GitHub Release 附 changelog、迁移说明、checksums、SBOM、digest 与验证命令。
- publish workflow 使用受保护 Environment；PR workflow 只读且不具备发布凭据。

### 部署文档

- 本机/可信内网 Docker 部署、备份/恢复、升级/回滚、存储容量、OCR 模型预热、日志和 Prometheus 抓取说明。
- 明确公网暴露需要额外身份认证、TLS、网络策略与运维责任，不承诺自动安全隔离。

## 完成定义

- 每个正式 tag 可通过自动化门禁产出可验证 wheel、镜像、SBOM、签名和 release evidence。
- Linux AMD64 镜像在受限容器配置下实际完成 health/ready/metrics/上传/artifact smoke。
- 发布者和部署者可按文档复现验证与回滚。
