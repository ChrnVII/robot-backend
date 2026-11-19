"""按扩展名推断 Content-Type。

录像产物是 MKV、音频是 WAV、抓图是 JPG，前端下载/预览都靠这个头判断类型。
放在公共模块里，避免 inspection / missions 两处各写一份而漂移。
"""

from __future__ import annotations

from pathlib import Path

_MIME_TYPES = {
    ".wav": "audio/wav",
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
}


def mime_for(name: str | Path) -> str:
    """扩展名 → MIME；未知类型一律 application/octet-stream（触发下载而不是误解析）。"""
    return _MIME_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
