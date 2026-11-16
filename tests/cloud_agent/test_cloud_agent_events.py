"""状态上报与事件 Outbox。

两条链路各有一处"错了也不会报错、只会悄悄丢数据"的地方，所以单独压：

- 状态：拿不到某个组时必须**不带**那个键，而不是填 0/true。页面对
  "没有数据"和"0%"的处置完全不同。
- 事件：超限丢最旧的之后必须留下 `event.gap`。静默丢弃会让云端以为
  一台设备十小时什么都没发生——那比丢数据更难查。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.cloud_agent.events import EventOutbox, EventUploader
from app.cloud_agent.state_reporter import StateReporter

pytestmark = pytest.mark.no_database


class FakeBackend:
    def __init__(self, health: dict | None = None, error: Exception | None = None) -> None:
        self.health = health
        self.error = error

    async def robot_state(self) -> dict:
        if self.error is not None:
            raise self.error
        return self.health or {}


class FakeMedia:
    def __init__(self, sessions: list[dict] | None = None) -> None:
        self.sessions = sessions or []

    def snapshot(self) -> dict:
        return {"sessions": self.sessions}


HEALTH = {
    "service": "robot-core",
    "state": "idle",
    "devices": {
        "battery": {"device_id": "battery", "health": "ok", "values": {"percent": 76.4}},
        "camera": {"device_id": "camera", "health": "ok", "values": {}},
    },
}


def make_reporter(
    backend: FakeBackend | None = None, media: FakeMedia | None = None
) -> tuple[StateReporter, list[dict]]:
    published: list[dict] = []
    reporter = StateReporter(
        backend or FakeBackend(HEALTH),  # type: ignore[arg-type]
        media or FakeMedia(),  # type: ignore[arg-type]
        published.append,
    )
    return reporter, published


# --- 状态 ---------------------------------------------------------------


async def test_state_groups_are_filled_from_health():
    reporter, _ = make_reporter()

    payload = await reporter.build()

    assert payload["state_version"] >= 1
    assert payload["epoch"]                       # 每次运行一个标识，云端靠它识别重启
    assert payload["robot"] == {"mode": "idle", "battery": 76}
    assert payload["camera"] == {"online": True}
    assert payload["link"]["control_uptime_s"] >= 0
    # 没有媒体会话时不该出现 media 组
    assert "media" not in payload


async def test_version_is_monotonic():
    reporter, _ = make_reporter()

    first = await reporter.build()
    second = await reporter.build()

    assert second["state_version"] == first["state_version"] + 1


async def test_version_survives_a_reconnect():
    """控制连接重建会新建 StateReporter；版本不能跟着归零。

    归零的后果很严重：云端只接受更大的版本，于是重启之后的所有状态帧都会被
    当成旧帧丢掉，页面从此停在最后一个值上，越看越旧。
    """
    first, _ = make_reporter()
    second, _ = make_reporter()          # 等价于重连后新建的 reporter

    a = await first.build()
    b = await second.build()

    assert b["state_version"] > a["state_version"]
    assert a["epoch"] == b["epoch"] == first.epoch


async def test_missing_values_are_omitted_not_zeroed():
    """没有电池设备的机型：不带 battery，而不是报 0%（页面会显示"电量耗尽"）。"""
    health = {"state": "idle", "devices": {"camera": {"health": "ok"}}}
    reporter, _ = make_reporter(FakeBackend(health))

    payload = await reporter.build()

    assert "battery" not in payload["robot"]
    assert payload["robot"]["mode"] == "idle"


async def test_unknown_camera_health_is_not_reported_as_online():
    health = {"state": "idle", "devices": {}}
    reporter, _ = make_reporter(FakeBackend(health))

    payload = await reporter.build()

    assert "camera" not in payload


async def test_backend_failure_drops_groups_but_keeps_link():
    """后端暂时不可用：只少了 robot/camera 两组，link 仍然真实。

    这里**不**保留上一次的 robot 组：整帧共用一个 `observed_at`，
    把旧电量放进新帧会让页面把过期数据当最新的显示。
    """
    reporter, _ = make_reporter(FakeBackend(error=RuntimeError("backend down")))

    payload = await reporter.build()

    assert "robot" not in payload
    assert "camera" not in payload
    assert payload["link"]["control_uptime_s"] >= 0
    assert payload["state_version"] > 0


async def test_no_groups_means_no_frame():
    """一个组都拿不到时不发帧、也不推进版本（避免"版本在涨、内容全空"）。"""
    reporter, published = make_reporter(FakeBackend(error=RuntimeError("backend down")))
    reporter._link_group = lambda now, lag=None: None  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        await reporter.build()

    assert reporter.version == 0
    assert published == []


async def test_media_group_only_when_streaming():
    media = FakeMedia(
        [
            {
                "session_id": "med-1",
                "stats": {"bitrate_kbps": 210, "fps": 8.0, "frames": 100, "reconnects": 2},
            }
        ]
    )
    reporter, _ = make_reporter(media=media)

    payload = await reporter.build()

    assert payload["media"]["preview"] == "streaming"
    assert payload["media"]["bitrate_kbps"] == 210
    assert payload["link"]["uplink_kbps"] == 210
    assert payload["link"]["media_reconnects"] == 2


async def test_tick_publishes_only_when_connected():
    reporter, published = make_reporter()
    reporter._connected = lambda: False  # type: ignore[assignment]

    await reporter._tick()
    assert published == []

    reporter._connected = lambda: True  # type: ignore[assignment]
    await reporter._tick()
    assert len(published) == 1


async def test_event_lag_is_reported_in_link_group(tmp_path):
    """补传落后量只有机器人自己知道（本地还有多少条没发出去）。"""
    outbox = EventOutbox(tmp_path / "events.db")
    await outbox.open()
    for index in range(3):
        await outbox.append("x", {"i": index})

    published: list[dict] = []
    reporter = StateReporter(
        FakeBackend(HEALTH),  # type: ignore[arg-type]
        FakeMedia(),  # type: ignore[arg-type]
        published.append,
        outbox=outbox,
    )

    payload = await reporter.build()

    assert payload["link"]["event_lag_seq"] == 3


# --- 事件 Outbox --------------------------------------------------------


async def make_outbox(tmp_path, **kwargs) -> EventOutbox:
    outbox = EventOutbox(tmp_path / "events.db", **kwargs)
    await outbox.open()
    return outbox


async def test_append_and_read_back(tmp_path):
    outbox = await make_outbox(tmp_path)

    seq = await outbox.append("alert.raised", {"code": "person_intrusion"})
    batch = await outbox.pending()

    assert seq == 1
    assert len(batch) == 1
    assert batch[0]["event_type"] == "alert.raised"
    assert batch[0]["payload"] == {"code": "person_intrusion"}
    assert batch[0]["event_id"].startswith("ev-")


async def test_advance_deletes_acked_rows(tmp_path):
    outbox = await make_outbox(tmp_path)
    for index in range(3):
        await outbox.append("task.progress", {"i": index})

    await outbox.advance(2)

    remaining = await outbox.pending()
    assert [item["event_seq"] for item in remaining] == [3]
    assert (await outbox.stats())["acked_seq"] == 2
    assert (await outbox.stats())["lag"] == 1


async def test_advance_is_monotonic(tmp_path):
    """游标只能前进：乱序到达的 ack 不能把已删除的数据"复活"。"""
    outbox = await make_outbox(tmp_path)
    for index in range(5):
        await outbox.append("x", {"i": index})

    await outbox.advance(4)
    await outbox.advance(2)

    assert (await outbox.acked()) == 4


async def test_pending_respects_cursor(tmp_path):
    outbox = await make_outbox(tmp_path)
    for index in range(4):
        await outbox.append("x", {"i": index})

    batch = await outbox.pending(limit=10, cursor=2)

    assert [item["event_seq"] for item in batch] == [3, 4]


async def test_limit_drops_oldest_and_leaves_a_gap(tmp_path):
    """超限必须留下 `event.gap`：云端要知道"这里有一段缺口"。"""
    outbox = await make_outbox(tmp_path, max_bytes=200)
    for index in range(40):
        await outbox.append("task.progress", {"i": index, "pad": "x" * 60})

    dropped = await outbox.enforce_limits()

    assert dropped > 0
    batch = await outbox.pending(limit=200)
    gap = [item for item in batch if item["event_type"] == "event.gap"]
    assert gap, "丢数据后必须留下缺口事件"
    assert gap[0]["payload"]["dropped"] == dropped
    assert gap[0]["payload"]["reason"] == "outbox_limit"


async def test_flush_without_token_is_a_noop(tmp_path):
    outbox = await make_outbox(tmp_path)
    await outbox.append("x", {})

    class Config:
        token = ""
        agent_id = "b-1"
        http_base = "http://127.0.0.1:1"

    uploader = EventUploader(Config(), outbox)  # type: ignore[arg-type]

    assert await uploader.flush() == 0
    assert len(await outbox.pending()) == 1


async def test_appending_wakes_the_uploader(tmp_path):
    """有新事件要立刻发，而不是等 15s 的空转周期。"""
    outbox = await make_outbox(tmp_path)

    class Config:
        token = "t"
        agent_id = "b-1"
        http_base = "http://127.0.0.1:1"

    uploader = EventUploader(Config(), outbox)  # type: ignore[arg-type]
    uploader.notify()

    assert await asyncio.wait_for(uploader._wake.wait(), 1.0) is True


async def test_uploader_enforces_the_outbox_limit_on_its_first_pass(tmp_path):
    """限容必须真有人调，否则 200MB / 7 天这条上限形同虚设。

    以前 `enforce_limits()` 只被测试调用：一台断网几周的机器会把磁盘写满，
    而且 `event.gap` 永远不会出现——云端看到的是一条"什么都没发生"的完整
    时间线，比明确报缺口更糟。
    """
    outbox = await make_outbox(tmp_path, max_bytes=200)
    for index in range(40):
        await outbox.append("task.progress", {"i": index, "pad": "x" * 60})
    before = (await outbox.stats())["count"]

    class Config:
        token = ""
        agent_id = "b-1"
        http_base = "http://127.0.0.1:1"

    uploader = EventUploader(Config(), outbox)  # type: ignore[arg-type]

    dropped = await uploader.enforce_limits()

    assert dropped > 0
    assert uploader.dropped == dropped
    after = (await outbox.stats())
    assert after["count"] < before
    # 落后量按"表里还剩多少条"算，而不是 max_seq - acked：
    # 否则被裁掉的历史会一直被算成待补传，页面上的数字永远回不到 0。
    assert after["lag"] == after["count"]


async def test_uploader_enforce_is_rate_limited(tmp_path):
    """限容是两条聚合 SQL，不能每 2s 跑一次。"""
    outbox = await make_outbox(tmp_path)

    class Config:
        token = ""
        agent_id = "b-1"
        http_base = "http://127.0.0.1:1"

    uploader = EventUploader(Config(), outbox)  # type: ignore[arg-type]
    await uploader.enforce_limits()          # 第一次一定跑
    uploader._last_enforce = time.time()      # 刚跑过

    calls: list[int] = []
    original = outbox.enforce_limits

    async def counting() -> int:
        calls.append(1)
        return await original()

    outbox.enforce_limits = counting        # type: ignore[assignment]

    assert await uploader.enforce_limits() == 0
    assert calls == []
