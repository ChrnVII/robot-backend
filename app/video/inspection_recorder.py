"""One camera, one MKV, one original encoded track, one disk owner thread."""
from __future__ import annotations
import queue
import threading
import time
from fractions import Fraction
from pathlib import Path

#: 收尾等待上限：磁盘挂起时不能把任务永远卡在 stopping。
CLOSE_TIMEOUT_SECONDS = 30.0


class RecordingChannel:
    def __init__(self, owner, camera_id):
        self.owner, self.camera_id = owner, camera_id
        self.origin = None
        self.offset = 0
        self.session = 0
        self._lock = threading.Lock()
        self._arrival_anchor = None

    def submit(self, packet, template):
        arrived_at = time.monotonic()
        source = packet.dts if packet.dts is not None else packet.pts
        tb = packet.time_base or template.time_base
        if source is None or not tb:
            raise RuntimeError('摄像头数据包缺少时间戳')
        if self.origin is None:
            if not packet.is_keyframe:
                return False
            self.origin = float(source * tb)
            self.offset = time.monotonic() - self.owner.started
            self.session += 1
        self.owner.offer((self.camera_id, bytes(packet), packet.pts, packet.dts,
                          packet.duration, tb, bool(packet.is_keyframe), template,
                          self.origin, self.offset))
        with self._lock:
            self._arrival_anchor = (arrived_at,
                float((packet.pts if packet.pts is not None else packet.dts) * tb) - self.origin + self.offset,
                self.session)
        return True

    def arrival_timestamp(self, arrived_at, max_age=1.0):
        """Approximate cross-stream correlation; never mix main/sub source PTS.

        Network/encoder latency differs between streams. This is a host arrival
        estimate, not frame-exact synchronization. Gaps have no video timestamp.
        """
        with self._lock:
            anchor = self._arrival_anchor
        valid = anchor is not None and abs(arrived_at - anchor[0]) <= max_age
        return dict(video_pts=max(0, round((anchor[1] + arrived_at - anchor[0]) * 1000)) if valid else None,
                    session=anchor[2] if valid else None,
                    track_index=self.owner.track_indices.get(self.camera_id),
                    video_time_basis='host_arrival_estimate')

    def finish(self):
        with self._lock:
            self._arrival_anchor = None
        # Ensure the writer has cloned the template before the input container closes.
        done = threading.Event()
        self.owner.offer(('barrier', done))
        while not done.wait(.2):
            self.owner.check()
        self.origin = None


class InspectionRecorder:
    """把一路摄像头的主码流原始包转封装成一个单轨 MKV。

    每个任务里每个摄像头各有一个实例（一个文件、一个写盘线程），互不影响：
    某一路没收到关键帧只会让该路的 ``video.mkv`` 无法收尾，不影响其他路。
    """

    def __init__(self, path, camera_ids, max_queue=2400):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.camera_ids = list(camera_ids)
        self.started = time.monotonic()
        self.error = ''
        self.written = 0
        self.track_indices = {}
        self._queue = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, name='inspection-mkv', daemon=True)
        self.channels = {key: RecordingChannel(self, key) for key in camera_ids}
        self._thread.start()

    def check(self):
        if self.error:
            raise RuntimeError(self.error)

    def offer(self, item):
        self.check()
        try:
            self._queue.put_nowait(item)
        except queue.Full as exc:
            self.error = '录像写盘积压，录像已停止'
            raise RuntimeError(self.error) from exc

    def close(self, timeout: float = CLOSE_TIMEOUT_SECONDS):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._thread.is_alive():
            try:
                self._queue.put(None, timeout=.2)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    self.error = self.error or '录像收尾超时，写盘线程未能及时退出'
                    return
                continue
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            self.error = self.error or '录像收尾超时（磁盘可能已挂起）'
        self.check()

    def _write(self, av, out, streams, signatures, last_dts, item):
        key, raw, pts, dts, duration, tb, is_key, _, origin, offset = item
        packet = av.Packet(raw)
        # All tracks use the task monotonic timeline; preserve PTS-DTS (B frames).
        packet.pts = round((float((pts if pts is not None else dts) * tb) - origin + offset) * 1000000)
        packet.dts = round((float((dts if dts is not None else pts) * tb) - origin + offset) * 1000000)
        if packet.dts <= last_dts.get(key, -1):
            raise RuntimeError(f'{key} 摄像头时间戳回退，录像已停止')
        last_dts[key] = packet.dts
        packet.duration = round(float((duration or 0) * tb) * 1000000)
        packet.time_base = Fraction(1, 1000000)
        packet.is_keyframe = is_key
        packet.stream = streams[key]
        out.mux(packet)
        self.written += 1

    def _start_if_ready(self, out, streams, pending):
        """收到该路首包就开录；返回需要先写出去的积压包。"""
        if not pending or not self.camera_ids:
            return None
        if len(streams) < len(self.camera_ids):
            return None
        out.start_encoding()
        return pending

    def _run(self):
        out = None
        streams, signatures, last_dts = {}, {}, {}
        pending, pending_bytes = [], 0
        try:
            import av
            out = av.open(str(self.path), 'w', format='matroska')
            ready = False
            while True:
                # 每轮都判断一次能否开录，而不是等下一个数据包到达时才判断
                # （此时队列可能已空，barrier 会一直等不到 start_encoding）。
                block = self._start_if_ready(out, streams, pending)
                if block is not None:
                    ready = True
                    pending, pending_bytes = [], 0
                    for entry in block:
                        self._write(av, out, streams, signatures, last_dts, entry)
                item = self._queue.get()
                if item is None:
                    if not ready:
                        raise RuntimeError('未收到关键帧，录像未就绪')
                    break
                if item[0] == 'barrier':
                    item[1].set()
                    continue
                key, raw, pts, dts, duration, tb, is_key, template, origin, offset = item
                signature = (template.codec_context.name, template.codec_context.width,
                             template.codec_context.height, bytes(template.codec_context.extradata or b''))
                if key not in streams:
                    if ready:
                        raise RuntimeError(f'{key} 在录像开始后才出现，请重新启动任务')
                    creator = getattr(out, 'add_stream_from_template', None)
                    stream = creator(template) if creator else out.add_stream(template=template)
                    stream.metadata['title'] = key
                    streams[key], signatures[key] = stream, signature
                    self.track_indices[key] = stream.index
                elif signatures[key] != signature:
                    raise RuntimeError(f'{key} 编码或分辨率发生变化，请重新启动任务')
                if not ready:
                    pending.append(item)
                    pending_bytes += len(raw)
                    if pending_bytes > 128 * 1024 * 1024:
                        raise RuntimeError('等待摄像头关键帧超出录像缓冲容量')
                    continue
                self._write(av, out, streams, signatures, last_dts, item)
        except Exception as exc:
            self.error = f'录像失败：{exc}'
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception as exc:
                    self.error = f'录像收尾失败：{exc}'
