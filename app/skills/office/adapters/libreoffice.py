import asyncio
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.documents.models import ConversionResult, ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class LibreOfficeProvider(Provider):
    """通过 LibreOffice headless 执行格式转换。"""

    manifest = ProviderManifest(
        name="office.convert.libreoffice",
        version="0.3.0",
        kind="file_converter",
        modes=["convert"],
        capabilities=["doc_to_docx", "xls_to_xlsx", "ppt_to_pptx", "office_to_pdf"],
    )

    supported_conversions = {
        ".doc": {"docx", "pdf"},
        ".rtf": {"docx", "pdf"},
        ".docx": {"doc", "pdf", "html"},
        ".xls": {"xlsx", "pdf"},
        ".xlsx": {"xls", "pdf", "html"},
        ".xlsm": {"xlsx", "pdf"},
        ".ppt": {"pptx", "pdf"},
        ".pptx": {"ppt", "pdf"},
        ".odt": {"docx", "pdf"},
        ".ods": {"xlsx", "pdf"},
        ".odp": {"pptx", "pdf"},
    }

    def __init__(self, command: str = "soffice", timeout_seconds: int = 300) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._convert_sync, context)

    def _convert_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        target_format = str(context.metadata.get("target_format", "")).lower().lstrip(".")
        allowed_targets = self.supported_conversions.get(source.suffix.lower(), set())

        if target_format not in allowed_targets:
            return self._failed(
                "unsupported_conversion",
                f"不支持转换: {source.suffix.lower()} → {target_format or '<empty>'}",
            )

        command_path = shutil.which(self.command) or self.command
        output_dir = self._output_dir(context)
        output_dir.mkdir(parents=True, exist_ok=True)
        expected_path = output_dir / f"{source.stem}.{target_format}"
        if expected_path.exists():
            return self._failed("output_exists", f"目标文件已存在，不覆盖原有结果: {expected_path}")

        profile_dir = Path(tempfile.mkdtemp(prefix="parse-agent-libreoffice-profile-"))
        started = time.perf_counter()
        command = [
            command_path,
            "--headless",
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--convert-to",
            target_format,
            "--outdir",
            str(output_dir),
            str(source),
        ]
        timeout = int(context.metadata.get("timeout_seconds", self.timeout_seconds))

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return self._failed("provider_unavailable", f"未找到 LibreOffice 命令: {self.command}")
        except subprocess.TimeoutExpired:
            return self._failed("conversion_timeout", f"转换超过 {timeout} 秒")
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)

        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "LibreOffice 转换失败").strip()
            return self._failed("conversion_failed", message)
        if not expected_path.is_file():
            output = (completed.stdout or completed.stderr or "").strip()
            return self._failed("output_missing", f"LibreOffice 未生成目标文件: {output}")

        duration_ms = int((time.perf_counter() - started) * 1000)
        conversion = ConversionResult(
            source_file=context.file,
            source_format=source.suffix.lower().lstrip("."),
            target_format=target_format,
            target_path=str(expected_path),
            size_bytes=expected_path.stat().st_size,
            duration_ms=duration_ms,
            provider={"name": self.manifest.name, "version": self.manifest.version},
        )
        return ProviderResult(
            status="success",
            provider_name=self.manifest.name,
            data={"conversion": conversion.model_dump()},
            metrics={"size_bytes": conversion.size_bytes, "duration_ms": conversion.duration_ms},
        )

    @staticmethod
    def _output_dir(context: ParseContext) -> Path:
        configured = context.metadata.get("output_dir")
        if configured:
            return Path(str(configured))
        return Path(tempfile.mkdtemp(prefix="parse-agent-converted-"))

    @staticmethod
    def _failed(code: str, message: str) -> ProviderResult:
        return ProviderResult(
            status="failed",
            provider_name="office.convert.libreoffice",
            error=ProviderError(code=code, message=message, retryable=False),
        )
