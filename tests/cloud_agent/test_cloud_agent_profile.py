"""档位门：按 GOP 抽帧。

这里唯一要证明的事：**抽掉的是整段 GOP，不是随机帧**。随机抽帧会让后续
P 帧引用到浏览器从未收到的那一帧——画面花屏，且要等下一个 IDR 才能恢复，
看起来像"网络坏了"，其实是发送端抽错了。
"""

from __future__ import annotations

import pytest

from app.cloud_agent.media import PROFILE_STRIDE, ProfileGate

pytestmark = pytest.mark.no_database

GOP_FRAMES = 4


def frame(*, key: bool = False, pad: int = 0) -> bytes:
    flags = 0x01 if key else 0x00
    return bytes([flags]) + b"\x00" * 8 + bytes([pad])


def run_gops(gate: ProfileGate, gops: int) -> list[tuple[int, bool]]:
    """喂 `gops` 段假 GOP，返回 (第几段, 该段是否被保留)。"""
    kept: list[tuple[int, bool]] = []
    for index in range(1, gops + 1):
        forwarded = 0
        for position in range(GOP_FRAMES):
            if gate.allow(frame(key=position == 0, pad=position)) is not None:
                forwarded += 1
        # 整段保留 = 全部帧都过；整段跳过 = 一帧都不过
        assert forwarded in (0, GOP_FRAMES), "不允许只发一段 GOP 的一部分"
        kept.append((index, forwarded == GOP_FRAMES))
    return kept


def test_high_profile_forwards_everything():
    gate = ProfileGate("high")

    kept = run_gops(gate, 6)

    assert all(value for _, value in kept)
    assert PROFILE_STRIDE["high"] == 1


def test_tiny_profile_keeps_every_fourth_gop():
    gate = ProfileGate("tiny")

    kept = run_gops(gate, 9)

    # 第 1、5、9 段保留
    assert [index for index, value in kept if value] == [1, 5, 9]


def test_first_gop_is_always_kept():
    """新观众必须立刻看到画面，不能等下一个周期。"""
    for profile in ("high", "low", "tiny"):
        gate = ProfileGate(profile)
        assert gate.allow(frame(key=True)) is not None, profile


def test_resumed_gop_is_marked_as_jump():
    """跳过之后的第一个保留段要打 GOP_START，播放器据此知道有缺口。"""
    gate = ProfileGate("tiny")
    first = gate.allow(frame(key=True))
    assert first[0] & 0x08 == 0  # 第一段不打标记

    # 跳过 2、3、4 段
    for _ in range(3):
        for position in range(GOP_FRAMES):
            gate.allow(frame(key=position == 0))

    resumed = gate.allow(frame(key=True))
    assert resumed is not None
    assert resumed[0] & 0x08, "恢复的第一段必须带 GOP_START"


def test_unknown_profile_falls_back_to_low():
    gate = ProfileGate("ultra")

    assert gate.profile == "low"
    assert gate.stride == PROFILE_STRIDE["low"]


def test_set_profile_only_reacts_to_real_changes():
    gate = ProfileGate("low")

    assert gate.set_profile("low") is False
    assert gate.set_profile("bogus") is False
    assert gate.set_profile("tiny") is True
    assert gate.stride == 4


def test_profile_change_resumes_at_next_keyframe():
    """降档后立刻恢复一段：否则用户要等 stride 个 GOP 才看到新画面。"""
    gate = ProfileGate("tiny")
    run_gops(gate, 2)          # 当前处在"跳过"状态
    gate.set_profile("high")

    assert gate.allow(frame(key=True)) is not None
    for position in range(1, GOP_FRAMES):
        assert gate.allow(frame(pad=position)) is not None


def test_keyframe_request_forces_the_next_gop():
    gate = ProfileGate("tiny")
    run_gops(gate, 2)          # 第 2 段被跳过
    assert gate.keeping is False

    gate.request_keyframe()

    assert gate.allow(frame(key=True)) is not None
    assert gate.keeping is True


def test_short_frames_are_ignored():
    gate = ProfileGate("high")
    assert gate.allow(b"\x01") is None


def test_local_preview_url_omits_empty_camera_id():
    """空 cameraId 表示"本地那台球形相机"，不能传成 cameraId=default。"""
    from app.cloud_agent.media import MediaSession

    class Config:
        backend_url = "http://127.0.0.1:8000"
        backend_token = ""

    default_camera = MediaSession(Config(), "med-1", "", "low")  # type: ignore[arg-type]
    explicit = MediaSession(Config(), "med-2", "VAR-1", "low")  # type: ignore[arg-type]

    assert default_camera._local_url() == "ws://127.0.0.1:8000/ws/camera/preview"
    assert explicit._local_url().endswith("/ws/camera/preview?cameraId=VAR-1")
