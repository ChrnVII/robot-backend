"""事件链路：本地持久化 Outbox + 批量补传。

事件与状态的差别只有一句话：**状态可以丢，事件不能丢**。所以事件走本地磁盘
而不是内存队列，走独立 HTTP 而不是控制连接——断网十分钟后要补几百上千条，
这批数据一旦占住 C0，用户的"停止"命令就排在补传后面了。

四条设计决定：

1. **SQLite，不用 `robot_core/sync/outbox.py`。** 那个表挂在平台库（Postgres）上，
   面向的是"业务事件发一次"的场景，且按条 ack。这里要的是**批量 + 游标 ack**，
   而且必须在 robot-core 没起来时也能记事件（agent 的独立性优先）。
2. **游标 ack，不逐条 ack。** 服务端回 `acked_seq`，本地删掉 ≤ 它的行。
   逐条 ack 在补传 800 条时是 800 次往返。
3. **限容（200MB / 7 天）时丢最旧的，并写一条 `event.gap`。** 宁可告诉云端
   "这里有一段缺口"，也不要假装完整——缺口的语义是可解释的，静默丢不是。
4. **至少一次投递。** 服务端按 `(agent_id, event_seq)` 去重，重复不报错。
   所以这里宁可重发，也不做"发了就删"。

事件体量小、频率低，用标准库 sqlite3 + `asyncio.to_thread` 足够；不引入
额外依赖，也不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx

from .config import AgentConfig

logger = logging.getLogger("robot-cloud-agent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    event_type     TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    occurred_at    REAL NOT NULL,
    trace_id       TEXT,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: 容量上限：超过就先丢最旧的，并留下一条缺口事件。
MAX_BYTES = 200 * 1024 * 1024
MAX_AGE_SECONDS = 7 * 24 * 3600
#: 一次补传多少条。服务端也是按这个量级设置的校验上限。
BATCH_LIMIT = 50
#: 补传节奏：有积压时 2s 一轮，空转时退到 15s。
FLUSH_INTERVAL = 2.0
IDLE_INTERVAL = 15.0
RETRY_MAX = 60.0
#: 限容检查间隔。它是两条聚合 SQL，不必每轮都跑；但也别太长，
#: 否则一次长时间离线会在两次检查之间把盘写满。
ENFORCE_INTERVAL = 60.0


def new_event_id() -> str:
    return f"ev-{uuid.uuid4().hex[:20]}"


class EventOutbox:
    """本地事件表。所有方法都在线程里跑，不阻塞事件循环。"""

    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._ready = False
        self.dropped = 0

    # --- 初始化 ---------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)
        self._ready = True

    def _open_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # --- 写入 -----------------------------------------------------------

    async def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        trace_id: str = "",
        occurred_at: float | None = None,
    ) -> int | None:
        """记一条事件。返回 event_seq；写失败返回 None（不影响调用方）。"""
        if not self._ready:
            return None
        try:
            return await asyncio.to_thread(
                self._append_sync, event_type, payload, trace_id, occurred_at
            )
        except Exception:  # noqa: BLE001 - 记事件失败不该把业务打挂
            logger.exception("写入本地事件失败：%s", event_type)
            return None

    def _append_sync(
        self,
        event_type: str,
        payload: dict[str, Any],
        trace_id: str,
        occurred_at: float | None,
    ) -> int:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO events (event_id, event_type, occurred_at, trace_id, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    new_event_id(),
                    event_type,
                    float(occurred_at if occurred_at is not None else time.time()),
                    trace_id or None,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            return int(cursor.lastrowid or 0)

    # --- 读取 -----------------------------------------------------------

    async def pending(self, limit: int = BATCH_LIMIT, cursor: int = 0) -> list[dict[str, Any]]:
        if not self._ready:
            return []
        return await asyncio.to_thread(self._pending_sync, limit, cursor)

    def _pending_sync(self, limit: int, cursor: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT event_seq, event_id, event_type, schema_version, occurred_at,"
                " trace_id, payload_json FROM events WHERE event_seq > ?"
                " ORDER BY event_seq LIMIT ?",
                (cursor, max(1, min(limit, 200))),
            ).fetchall()
        return [
            {
                "event_seq": int(row["event_seq"]),
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "schema_version": int(row["schema_version"]),
                "occurred_at": float(row["occurred_at"]),
                "trace_id": row["trace_id"] or "",
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]

    async def stats(self) -> dict[str, Any]:
        if not self._ready:
            return {"count": 0, "acked_seq": 0, "max_seq": 0, "lag": 0, "bytes": 0}
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(MAX(event_seq), 0) AS max_seq,"
                " COALESCE(SUM(LENGTH(payload_json)), 0) AS bytes FROM events"
            ).fetchone()
            acked = self._get_meta(conn, "acked_seq", 0)
        count = int(row["count"])
        return {
            "count": count,
            "max_seq": int(row["max_seq"]),
            "acked_seq": int(acked),
            # 落后量就是"表里还剩多少条没确认"。
            # 不用 `max_seq - acked`：限容会删掉最旧的行却不推进 acked，
            # 那个差值会把已经丢弃的历史一直算成"待补传"，页面上的
            # "落后 N 条"于是永远回不到 0。
            "lag": count,
            "bytes": int(row["bytes"]),
        }

    # --- 游标 -----------------------------------------------------------

    async def acked(self) -> int:
        if not self._ready:
            return 0
        return await asyncio.to_thread(self._acked_sync)

    def _acked_sync(self) -> int:
        with closing(self._connect()) as conn, conn:
            return self._get_meta(conn, "acked_seq", 0)

    async def advance(self, seq: int) -> None:
        """服务端确认到 seq：删掉 ≤ seq 的行，推进游标。

        服务端保证 `acked_seq` 之前一条不缺（它是按收到的最大连续序号回话的），
        所以这里可以安全删除——不需要留窗口重放。
        """
        if not self._ready or seq <= 0:
            return
        await asyncio.to_thread(self._advance_sync, seq)

    def _advance_sync(self, seq: int) -> None:
        with closing(self._connect()) as conn, conn:
            current = self._get_meta(conn, "acked_seq", 0)
            if seq <= current:
                return
            conn.execute("DELETE FROM events WHERE event_seq <= ?", (seq,))
            self._set_meta(conn, "acked_seq", seq)

    # --- 限容 -----------------------------------------------------------

    async def enforce_limits(self) -> int:
        """超限时丢最旧的，并记一条 `event.gap`。返回丢弃条数。"""
        if not self._ready:
            return 0
        dropped = await asyncio.to_thread(self._enforce_sync)
        if dropped:
            self.dropped += dropped
            logger.warning("事件积压超过上限，丢弃最旧的 %d 条并记录 event.gap", dropped)
        return dropped

    def _enforce_sync(self) -> int:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(payload_json)), 0) AS bytes,"
                " COALESCE(MIN(event_seq), 0) AS min_seq, COALESCE(MAX(event_seq), 0) AS max_seq"
                " FROM events"
            ).fetchone()
            boundary = time.time() - MAX_AGE_SECONDS
            old = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(event_seq), 0) AS seq FROM events"
                " WHERE occurred_at < ?",
                (boundary,),
            ).fetchone()
            drop_to = 0
            if int(old["n"]):
                drop_to = int(old["seq"])
            elif int(row["bytes"]) > self.max_bytes or int(row["count"]) > 50000:
                # 超容量：删掉最旧的一半，避免每写一条就触发一次清理。
                keep_from = int(row["min_seq"]) + max(1, int(row["count"]) // 2)
                drop_to = keep_from - 1
            if drop_to <= 0:
                return 0
            lost = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(MIN(event_seq), 0) AS first,"
                " COALESCE(MAX(event_seq), 0) AS last FROM events WHERE event_seq <= ?",
                (drop_to,),
            ).fetchone()
            conn.execute("DELETE FROM events WHERE event_seq <= ?", (drop_to,))
            count = int(lost["n"])
            if count:
                # 缺口本身也是一条事件：云端据此知道"这段时间不完整"，
                # 而不是以为一台设备十小时什么都没发生。
                conn.execute(
                    "INSERT INTO events (event_id, event_type, occurred_at, trace_id,"
                    " payload_json) VALUES (?, 'event.gap', ?, NULL, ?)",
                    (
                        new_event_id(),
                        time.time(),
                        json.dumps(
                            {
                                "dropped": count,
                                "from_seq": int(lost["first"]),
                                "to_seq": int(lost["last"]),
                                "reason": "outbox_limit",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            return count

    # --- meta -----------------------------------------------------------

    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str, default: int) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: int) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


class EventUploader:
    """把 Outbox 里的事件批量送到云端，按游标推进。

    顺带承担**限容**：`EventOutbox.enforce_limits()` 必须有人定期调，否则
    200MB / 7 天这条上限形同虚设——一台断网几周的机器人会把磁盘写满，
    而且 `event.gap` 永远不会出现，云端看到的是一条"什么都没发生"的完整
    时间线，比明确报缺口更糟。
    """

    def __init__(self, config: AgentConfig, outbox: EventOutbox) -> None:
        self.config = config
        self.outbox = outbox
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: 上次限容时刻。0 表示"还没做过"——启动后第一轮就跑一次，
        #: 把上次长时间离线攒下的积压先裁掉。
        self._last_enforce = 0.0
        self.sent = 0
        self.duplicates = 0
        self.dropped = 0
        self.failures = 0
        self.last_error = ""

    # --- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="event-uploader")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def notify(self) -> None:
        """有新事件：立刻醒来发一轮，不等下一个 tick。"""
        self._wake.set()

    async def enforce_limits(self) -> int:
        """按间隔执行一次限容。返回本次丢弃条数。"""
        now = time.time()
        if now - self._last_enforce < ENFORCE_INTERVAL:
            return 0
        self._last_enforce = now
        try:
            dropped = await self.outbox.enforce_limits()
        except Exception as exc:  # noqa: BLE001 - 限容失败不该停掉补传
            logger.warning("事件 Outbox 限容失败：%s", exc)
            return 0
        self.dropped += dropped
        return dropped

    def status(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    # --- 主循环 ---------------------------------------------------------

    async def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            flushed = 0
            try:
                # 先限容再补传：积压已经超限时，先把最旧的裁掉并留下 event.gap，
                # 免得把磁盘写满，也免得补传一堆注定会被裁掉的历史。
                await self.enforce_limits()
                flushed = await self.flush()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 补传失败就退避再来
                self.failures += 1
                self.last_error = str(exc)
                logger.info("事件补传失败：%s", exc)

            delay = FLUSH_INTERVAL if flushed else IDLE_INTERVAL
            if self.last_error and not flushed:
                delay = min(RETRY_MAX, max(delay, backoff))
                backoff = min(backoff * 2, RETRY_MAX)

            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    async def flush(self) -> int:
        """发一轮。返回本轮发出的条数。"""
        if not self.config.token:
            return 0
        acked = await self.outbox.acked()
        batch = await self.outbox.pending(BATCH_LIMIT, cursor=acked)
        if not batch:
            return 0

        body = {
            "agent_id": self.config.agent_id or "",
            "cursor": acked,
            "batch": batch,
        }
        url = f"{self.config.http_base}/internal/events"
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            response = await client.post(url, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

        data = response.json() if response.content else {}
        acked_seq = int(data.get("acked_seq") or 0)
        self.duplicates += int(data.get("duplicates") or 0)
        self.sent += len(batch)
        self.last_error = ""
        if acked_seq > 0:
            await self.outbox.advance(acked_seq)
        return len(batch)
