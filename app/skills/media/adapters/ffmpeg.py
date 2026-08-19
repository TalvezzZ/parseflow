import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.documents.models import ParseContext, ProviderError
from app.skills.providers import Provider, ProviderManifest, ProviderResult


class FfmpegMediaPrepareProvider(Provider):
    """使用 FFprobe/FFmpeg 探测媒体并生成可供后续 ASR 使用的标准音频。"""

    manifest = ProviderManifest(
        name="media.prepare.ffmpeg",
        version="0.5.0",
        kind="media_preprocessor",
        modes=["prepare"],
        capabilities=["media_probe", "audio_normalization", "video_audio_extraction", "subtitle_export"],
    )

    def __init__(
        self,
        ffmpeg_command: str = "ffmpeg",
        ffprobe_command: str = "ffprobe",
        timeout_seconds: int = 300,
        max_duration_seconds: int = 14400,
        max_file_size_mb: int = 2048,
    ) -> None:
        self.ffmpeg_command = ffmpeg_command
        self.ffprobe_command = ffprobe_command
        self.timeout_seconds = timeout_seconds
        self.max_duration_seconds = max_duration_seconds
        self.max_file_size_mb = max_file_size_mb

    async def parse(self, context: ParseContext) -> ProviderResult:
        return await asyncio.to_thread(self._prepare_sync, context)

    def _prepare_sync(self, context: ParseContext) -> ProviderResult:
        source = Path(context.file.path)
        kind = str(context.metadata.get("media_kind", ""))
        started = time.perf_counter()
        if kind not in {"audio", "video"}:
            return self._failed("unsupported_media_kind", f"不支持的媒体类型: {kind or '<empty>'}")
        if source.stat().st_size > self.max_file_size_mb * 1024 * 1024:
            return self._failed("media_file_too_large", f"媒体文件超过 {self.max_file_size_mb} MB 限制")

        ffmpeg = shutil.which(self.ffmpeg_command)
        ffprobe = shutil.which(self.ffprobe_command)
        if not ffmpeg or not ffprobe:
            return self._failed(
                "provider_unavailable",
                f"未找到 FFmpeg/FFprobe 命令: {self.ffmpeg_command}, {self.ffprobe_command}",
            )

        try:
            probe = self._probe(ffprobe, source)
        except subprocess.TimeoutExpired:
            return self._failed("media_probe_timeout", "FFprobe 探测超时")
        except Exception as exc:
            return self._failed("media_probe_failed", str(exc))

        duration_seconds = self._duration(probe)
        if duration_seconds is not None and duration_seconds > self.max_duration_seconds:
            return self._failed(
                "media_duration_exceeded",
                f"媒体时长 {duration_seconds:.2f} 秒超过 {self.max_duration_seconds} 秒限制",
            )

        streams = list(probe.get("streams") or [])
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
        if kind == "video" and not audio_streams:
            return self._failed("no_audio_stream", "视频不包含可提取的音轨")
        if kind == "audio" and not audio_streams:
            return self._failed("no_audio_stream", "音频文件不包含可读取的音轨")

        output_dir = self._output_dir(context)
        output_dir.mkdir(parents=True, exist_ok=True)
        audio_stream = self._select_stream(audio_streams, context.metadata.get("audio_stream_index"))
        audio_output = output_dir / f"{source.stem}.normalized.wav"
        if audio_output.exists():
            return self._failed("output_exists", f"目标音频已存在，不覆盖原有结果: {audio_output}")

        command = [
            ffmpeg,
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            f"0:{audio_stream['index']}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_output),
        ]
        try:
            self._run(command, self._timeout(context))
        except subprocess.TimeoutExpired:
            return self._failed("media_processing_timeout", "音频提取或标准化超时")
        except Exception as exc:
            return self._failed("media_processing_failed", str(exc))
        if not audio_output.is_file():
            return self._failed("audio_output_missing", "FFmpeg 未生成标准化音频")

        warnings = ["媒体预处理完成，尚未执行 ASR，结果不包含语音转写文本。"]
        artifacts = [self._artifact(audio_output, "normalized_audio")]
        selected_subtitle = self._select_subtitle(subtitle_streams, context.metadata.get("subtitle_stream_index"))
        if selected_subtitle is not None:
            subtitle_output = output_dir / f"{source.stem}.subtitle-{selected_subtitle['index']}.srt"
            try:
                self._run(
                    [ffmpeg, "-nostdin", "-y", "-i", str(source), "-map", f"0:{selected_subtitle['index']}", str(subtitle_output)],
                    self._timeout(context),
                )
                if subtitle_output.is_file():
                    artifacts.append(self._artifact(subtitle_output, "subtitle"))
                else:
                    warnings.append("检测到字幕轨，但未能导出为 SRT。")
            except Exception as exc:
                warnings.append(f"检测到字幕轨，但导出失败: {exc}")

        duration_ms = int((duration_seconds or 0) * 1000)
        preparation = {
            "media_type": kind,
            "duration_ms": duration_ms,
            "format": probe.get("format", {}).get("format_name"),
            "audio_streams": [self._stream_summary(stream) for stream in audio_streams],
            "subtitle_streams": [self._stream_summary(stream) for stream in subtitle_streams],
            "selected_audio_stream_index": audio_stream["index"],
            "artifacts": artifacts,
            "transcript_available": False,
            "asr_status": "not_requested",
            "provenance": {"provider": self.manifest.model_dump(), "source_path": str(source)},
        }
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProviderResult(
            status="success",
            provider_name=self.manifest.name,
            data={"media": preparation},
            warnings=warnings,
            metrics={"duration_ms": duration_ms, "processing_duration_ms": elapsed_ms, "artifact_count": len(artifacts)},
        )

    def _probe(self, ffprobe: str, source: Path) -> dict[str, Any]:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "FFprobe 探测失败").strip())
        return json.loads(completed.stdout)

    @staticmethod
    def _duration(probe: dict[str, Any]) -> float | None:
        value = probe.get("format", {}).get("duration")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _timeout(self, context: ParseContext) -> int:
        return int(context.metadata.get("timeout_seconds", self.timeout_seconds))

    @staticmethod
    def _select_stream(streams: list[dict[str, Any]], requested_index: Any) -> dict[str, Any]:
        if requested_index is not None:
            requested = int(requested_index)
            for stream in streams:
                if stream.get("index") == requested:
                    return stream
            raise ValueError(f"未找到指定音轨: {requested}")
        for stream in streams:
            if stream.get("disposition", {}).get("default") == 1:
                return stream
        return streams[0]

    @staticmethod
    def _select_subtitle(streams: list[dict[str, Any]], requested_index: Any) -> dict[str, Any] | None:
        if not streams:
            return None
        return FfmpegMediaPrepareProvider._select_stream(streams, requested_index)

    @staticmethod
    def _stream_summary(stream: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": stream.get("index"),
            "codec": stream.get("codec_name"),
            "language": stream.get("tags", {}).get("language"),
            "title": stream.get("tags", {}).get("title"),
            "channels": stream.get("channels"),
            "sample_rate": stream.get("sample_rate"),
            "disposition": stream.get("disposition", {}),
        }

    @staticmethod
    def _artifact(path: Path, kind: str) -> dict[str, Any]:
        return {"kind": kind, "path": str(path), "filename": path.name, "size_bytes": path.stat().st_size}

    @staticmethod
    def _output_dir(context: ParseContext) -> Path:
        configured = context.metadata.get("output_dir")
        if configured:
            return Path(str(configured))
        return Path(tempfile.mkdtemp(prefix=f"parse-agent-media-{context.file.file_id}-"))

    @staticmethod
    def _run(command: list[str], timeout: int) -> None:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "FFmpeg 命令失败").strip())

    def _failed(self, code: str, message: str) -> ProviderResult:
        return ProviderResult(
            status="failed",
            provider_name=self.manifest.name,
            error=ProviderError(code=code, message=message, retryable=False),
        )
