"""状态上报：分组采集、整帧覆盖、单组失败不拖垮整帧。

这里的断言都对应一条"页面在骗人"的故障：

- link/media 组读媒体统计，统计对象坏一次（曾经的 `MediaStats.snapshot` 读了
  不存在的属性）就会让整帧状态采集失败——页面从此停在最后一次状态上，
  而日志里只有一行被吞掉的 AttributeError。
- 因此每个组必须是独立失败域：一个组坏，其余组照发。
"""

from __future__ import annotations

import time

import pytest

from app.cloud_agent.media import MediaManager, MediaSession
from app.cloud_agent.state_reporter import StateReporter

pytestmark = pytest.mark.no_database


class FakeBackend:
    def __init__(self, health: dict | None = None) -> None:
        self.health = health if health is not None else {
            "state": "idle",
            "devices": {
                "battery": {"health": "ok", "values": {"percent": 76}},
                "camera": {"health": "ok", "values": {}},
            },
        }
        self.calls = 0

    async def robot_state(self) -> dict:
        self.calls += 1
        return self.health


class FakeOutbox:
    async def stats(self) -> dict:
        return {"lag": 12}


class BrokenMedia:
    """media.snapshot() 直接抛：模拟一个坏掉的统计实现。"""

    def snapshot(self) -> dict:
        raise AttributeError("'MediaStats' object has no attribute 'gate'")


def make_reporter(media=None, outbox=None) -> tuple[StateReporter, list[dict]]:
    published: list[dict] = []
    reporter = StateReporter(
        FakeBackend(),  # type: ignore[arg-type]
        media if media is not None else MediaManager(None),  # type: ignore[arg-type]
        published.append,
        outbox=outbox,
    )
    return reporter, published


async def test_frame_has_all_groups_and_monotonic_version():
    media = MediaManager(None)  # type: ignore[arg-type]
    session = MediaSession(None, "med-1", "", "low")  # type: ignore[arg-type]
    session.stats.frames = 5
    media._sessions["med-1"] = session

    reporter, _ = make_reporter(media=media, outbox=FakeOutbox())
    first = await reporter.build()
    second = await reporter.build()

    assert first["state_version"] < second["state_version"]
    assert first["observed_at"] <= second["observed_at"]
    assert first["epoch"]
    # robot / camera / link / media 四组齐备
    assert set(first) >= {"robot", "camera", "link", "media"}
    assert first["robot"]["battery"] == 76
    assert first["camera"]["online"] is True
    assert first["link"]["event_lag_seq"] == 12
    assert first["media"]["sessions"] == 1


async def test_media_group_is_absent_without_sessions():
    """没有媒体会话时不带 media 组：报一个"0 会话"没有任何信息量。"""
    reporter, _ = make_reporter()
    frame = await reporter.build()

    assert "media" not in frame


async def test_a_broken_group_does_not_kill_the_frame():
    """单组采集失败只让该组缺席——不能让整帧消失（状态是"只留最新"的）。"""
    reporter, _ = make_reporter(media=BrokenMedia())  # type: ignore[arg-type]

    frame = await reporter.build()

    assert frame["robot"]["battery"] == 76
    assert "link" not in frame        # 坏掉的组缺席
    assert "media" not in frame


async def test_tick_skips_collection_while_disconnected():
    """断线期间不采集：不做无用的本地请求，也不推进版本。"""
    backend = FakeBackend()
    reporter = StateReporter(backend, MediaManager(None), lambda _p: None,  # type: ignore[arg-type]
                             client_connected=lambda: False)

    await reporter._tick()

    assert backend.calls == 0
    assert reporter.version == 0


async def test_tick_publishes_when_connected():
    reporter, published = make_reporter()
    reporter._connected = lambda: True

    await reporter._tick()

    assert len(published) == 1
    assert published[0]["state_version"] >= 1
    assert reporter.sent == 1
    assert reporter.last_sent_at is not None
    assert time.time() - reporter.last_sent_at < 5
