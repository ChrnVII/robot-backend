"""预览转封装：MKV → MP4（copy）与路径围栏。

不依赖数据库，纯文件级单元测试。用 PyAV 现场生成一个极小的 MKV 当素材，
避免把二进制夹具放进仓库。
"""

from __future__ import annotations

import numpy as np
import pytest

av = pytest.importorskip("av")

from app.common.exceptions import AppError  # noqa: E402
from app.services import media_preview  # noqa: E402

# 纯文件级用例：不需要 PostgreSQL，标记后 conftest 会跳过建库 fixture。
pytestmark = pytest.mark.no_database


@pytest.fixture()
def sample(tmp_path, monkeypatch):
    """一个只属于当前用例的受管目录，避免碰到真实的 data/。"""
    camera_dir = tmp_path / "cam01"
    camera_dir.mkdir(parents=True)
    movie = camera_dir / "video.mkv"
    container = av.open(str(movie), "w", format="matroska")
    try:
        stream = container.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 160, 120, "yuv420p"
        for index in range(20):
            frame = av.VideoFrame.from_ndarray(
                np.full((120, 160, 3), index * 8, dtype=np.uint8), format="rgb24"
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()

    monkeypatch.setattr(media_preview, "ALLOWED_ROOTS", (tmp_path,))
    monkeypatch.setattr(media_preview, "CACHE_DIR", tmp_path / "cache")
    return movie


def test_preview_remuxes_mkv_to_playable_mp4(sample):
    output = media_preview.preview_file(str(sample))

    assert output.suffix == ".mp4"
    assert output.is_file() and output.stat().st_size > 0
    # 转出来的必须是能读的 h264，否则前端 <video> 一样没画面
    with av.open(str(output)) as container:
        stream = container.streams.video[0]
        assert stream.codec_context.name == "h264"
        assert (stream.width, stream.height) == (160, 120)


def test_preview_cache_is_reused(sample):
    first = media_preview.preview_file(str(sample))
    stamp = first.stat().st_mtime_ns

    second = media_preview.preview_file(str(sample))

    assert second == first
    assert second.stat().st_mtime_ns == stamp, "第二次应命中缓存而不是重新转封装"


def test_preview_rejects_path_outside_managed_roots(tmp_path, sample):
    outside = tmp_path.parent / "not-managed.mkv"
    outside.write_bytes(b"not a real video")
    try:
        with pytest.raises(AppError) as error:
            media_preview.preview_file(str(outside))
        assert error.value.code == "ARTIFACT_FORBIDDEN"
    finally:
        outside.unlink(missing_ok=True)


def test_preview_rejects_non_video(sample):
    other = sample.with_suffix(".wav")
    other.write_bytes(b"RIFF")
    with pytest.raises(AppError) as error:
        media_preview.preview_file(str(other))
    assert error.value.code == "PREVIEW_UNSUPPORTED"
