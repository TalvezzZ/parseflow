# Parse Agent 方案 0.4.2：音视频媒体预处理 MVP

## 1. 目标

在暂不接入 ASR 的前提下，先完成音频和视频的媒体预处理能力，为后续语音识别 Provider 提供统一输入。

```text
音频
  → FFprobe 探测
  → FFmpeg 标准化
  → 16kHz 单声道 PCM WAV artifact

视频
  → FFprobe 探测
  → 选择音轨
  → FFmpeg 提取并标准化音轨
  → 可选导出内嵌字幕轨
  → 音频与字幕 artifact
```

## 2. 新增能力

- `audio.prepare`
- `video.prepare`
- `POST /api/v1/prepare/audio`
- `POST /api/v1/prepare/video`
- Provider：`media.prepare.ffmpeg`

## 3. 组件

- `ffprobe`：读取容器、时长、音轨、字幕轨和媒体元数据。
- `ffmpeg`：提取视频音轨并统一转换为单声道、16kHz、PCM WAV。
- FFmpeg 字幕映射：尝试将可用内嵌字幕导出为 SRT。

本版本不执行 ASR，因此结果必须明确：

```text
transcript_available = false
asr_status = not_requested
```

## 4. 限制与错误

- 支持音频：`.mp3`、`.wav`、`.m4a`、`.aac`、`.flac`、`.ogg`。
- 支持视频：`.mp4`、`.mov`、`.mkv`、`.avi`、`.webm`。
- 默认选择视频的 default 音轨，否则选择第一条音轨。
- 支持请求指定 FFprobe 全局音轨索引和字幕轨索引。
- 检查文件大小、媒体时长和 FFmpeg 执行超时。
- 无音轨、探测失败、命令不可用和产物缺失均返回明确错误码。
- 不覆盖已存在的目标产物。

## 5. 结果结构

```json
{
  "media_type": "video",
  "duration_ms": 1000,
  "audio_streams": [],
  "subtitle_streams": [],
  "selected_audio_stream_index": 1,
  "artifacts": [
    {
      "kind": "normalized_audio",
      "path": ".../source.normalized.wav",
      "size_bytes": 32000
    }
  ],
  "transcript_available": false,
  "asr_status": "not_requested"
}
```

## 6. 验收

- 真实 WAV 可通过 `/api/v1/prepare/audio` 生成 16kHz 单声道 WAV。
- 真实带音轨 MP4 可通过 `/api/v1/prepare/video` 提取标准化音频。
- 带内嵌字幕的 MP4 可导出 SRT artifact。
- 真实 FFmpeg 集成测试与既有测试全部通过。

## 7. 后续增强

后续可在不改变媒体预处理 Provider 输入/产物约定的前提下增加：

```text
audio.transcribe
video.transcribe
```

由 ASR Provider 消费标准化 WAV，并输出带时间戳的文本。ASR 不属于本版本范围。
