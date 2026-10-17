"""One MKV, one original encoded track per camera, one disk owner thread."""
from __future__ import annotations
import queue
import threading
import time
from fractions import Fraction


class RecordingChannel:
    def __init__(self, owner, camera_id):
        self.owner, self.camera_id = owner, camera_id
        self.origin = None
        self.offset = 0
        self.session = 0

    def submit(self, packet, template):
        source = packet.dts if packet.dts is not None else packet.pts
        tb = packet.time_base or template.time_base
        if source is None or not tb:
            raise RuntimeError('摄像头数据包缺少时间戳')
        if self.origin is None:
            if not packet.is_keyframe:
                return
            self.origin = float(source * tb)
            self.offset = time.monotonic() - self.owner.started
            self.session += 1
        self.owner.offer((self.camera_id, bytes(packet), packet.pts, packet.dts,
                          packet.duration, tb, bool(packet.is_keyframe), template,
                          self.origin, self.offset))

    def timestamp(self, src_pts):
        return dict(src_pts=src_pts, video_pts=None if src_pts is None or self.origin is None else
                    round(src_pts - self.origin * 1000 + self.offset * 1000), session=self.session,
                    track_index=self.owner.track_indices.get(self.camera_id))

    def finish(self):
        # Ensure the writer has cloned the template before the input container closes.
        done = threading.Event()
        self.owner.offer(('barrier', done))
        while not done.wait(.2):
            self.owner.check()
        self.origin = None


class InspectionRecorder:
    def __init__(self, path, camera_ids, max_queue=2400):
        self.path = path
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
            self.error = '录像写盘积压，任务已停止'
            raise RuntimeError(self.error) from exc

    def close(self):
        while self._thread.is_alive():
            try:
                self._queue.put(None, timeout=.2)
                break
            except queue.Full:
                continue
        self._thread.join()
        self.check()

    def _run(self):
        out = None
        streams, signatures, last_dts = {}, {}, {}
        pending, pending_bytes = [], 0
        try:
            import av
            out = av.open(str(self.path), 'w', format='matroska')
            ready = False
            while True:
                item = self._queue.get()
                if item is None:
                    if not ready:
                        raise RuntimeError('未收到全部所选摄像头的关键帧，录像未就绪')
                    break
                if item[0] == 'barrier':
                    item[1].set()
                    continue
                key, raw, pts, dts, duration, tb, is_key, template, origin, offset = item
                signature = (template.codec_context.name, template.codec_context.width,
                             template.codec_context.height, bytes(template.codec_context.extradata or b''))
                if key not in streams:
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
                    if len(streams) != len(self.camera_ids):
                        continue
                    out.start_encoding()
                    ready = True
                    batch, pending = pending, []
                else:
                    batch = [item]
                for key, raw, pts, dts, duration, tb, is_key, _, origin, offset in batch:
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
        except Exception as exc:
            self.error = f'录像失败：{exc}'
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception as exc:
                    self.error = f'录像收尾失败：{exc}'
