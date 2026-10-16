"""Single-stream MKV remux. Disk I/O runs separately from RTSP/preview."""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.common.exceptions import AppError
from app.video.codec import redact_credentials


class CameraRecorder:
    def __init__(self, directory: str, publish, max_queue: int = 600):
        self.directory = Path(directory).resolve()
        self._publish = publish
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._slots = threading.BoundedSemaphore(max(1, max_queue))
        self._thread = None
        self._out = None
        self._stream = None
        self.recording = False
        self.current_file = None
        self.error = ''
        self.written = 0
        self.segments = 0

    def start_worker(self):
        self._thread = threading.Thread(target=self._run, name='camera-recorder', daemon=True)
        self._thread.start()

    def status(self):
        with self._lock:
            return dict(recording=self.recording, currentFile=self.current_file,
                        error=self.error, written=self.written, segments=self.segments)

    def _notify(self):
        self._publish(dict(t='record', **self.status()))

    def start(self):
        with self._lock:
            if not self.recording:
                self.directory.mkdir(parents=True, exist_ok=True)
                if self._thread is None or not self._thread.is_alive():
                    raise AppError('录像服务未运行', code='STREAM_UNAVAILABLE', status_code=409)
                self._queue.put(('close', threading.Event()))
                self.error = ''
                self.recording = True
                self._notify()
        return self.status()

    def finish(self, *, stop=False, shutdown=False):
        done = threading.Event()
        with self._lock:
            if stop:
                self.recording = False
            if self._thread is None:
                return self.status()
            self._queue.put(('shutdown' if shutdown else 'close', done))
        # Commands are ordered with packet admission; stop drains and finalizes immediately.
        if not done.wait(10):
            raise AppError('录像收尾超时', code='RECORDING_TIMEOUT', status_code=503)
        if shutdown:
            self._thread.join(timeout=1)
            self._thread = None
        return self.status()

    def submit(self, packet, template):
        with self._lock:
            if not self.recording:
                return
            if not self._slots.acquire(blocking=False):
                # Do not silently produce corrupt video by dropping arbitrary reference frames.
                self.recording = False
                self.error = '录像写盘积压，已停止；请检查磁盘性能'
                self._queue.put(('close', threading.Event()))
                self._notify()
                return
            self._queue.put(('packet', (bytes(packet), packet.pts, packet.dts,
                                       packet.duration, packet.time_base or template.time_base,
                                       bool(packet.is_keyframe), template)))

    def _close(self):
        out, self._out = self._out, None
        try:
            if out is not None:
                out.close()
        finally:
            self._stream = None
            with self._lock:
                self.current_file = None
            self._notify()

    def _write(self, item):
        import av
        raw, pts, dts, duration, time_base, key, template = item
        if self._out is None:
            if not key:
                return
            name = datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid4().hex[:8] + '.mkv'
            out = av.open(str(self.directory / name), 'w', format='matroska')
            try:
                creator = getattr(out, 'add_stream_from_template', None)
                stream = creator(template) if creator else out.add_stream(template=template)
                out.start_encoding()
            except BaseException:
                out.close()
                raise
            self._out, self._stream = out, stream
            with self._lock:
                self.current_file = name
                self.segments += 1
            self._notify()
        if pts is None and dts is None:
            raise ValueError('摄像头数据包缺少时间戳，录像已停止')
        packet = av.Packet(raw)
        packet.pts = pts if pts is not None else dts
        packet.dts = dts if dts is not None else pts
        packet.duration = duration
        packet.time_base = time_base
        packet.is_keyframe = key
        packet.stream = self._stream
        self._out.mux(packet)
        with self._lock:
            self.written += 1

    def _run(self):
        failed = False
        while True:
            kind, item = self._queue.get()
            try:
                if kind == 'packet':
                    if not failed:
                        self._write(item)
                else:
                    self._close()
                    failed = False
            except Exception as exc:
                with self._lock:
                    self.error = redact_credentials(exc)
                    self.recording = False
                failed = True
                try:
                    self._close()
                except Exception:
                    pass
                self._notify()
            finally:
                if kind == 'packet':
                    self._slots.release()
                else:
                    item.set()
            if kind == 'shutdown':
                return

    def file(self, name):
        path = (self.directory / name).resolve()
        if path.parent != self.directory or path.suffix != '.mkv' or not path.is_file():
            raise AppError('录像不存在', code='NOT_FOUND', status_code=404)
        if name == self.status()['currentFile']:
            raise AppError('录像正在写入，请先停止录像', code='RECORDING_ACTIVE', status_code=409)
        return path

    def files(self):
        rows = []
        for path in self.directory.glob('*.mkv'):
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            rows.append(dict(name=path.name, size=stat.st_size,
                             sizeText=f'{stat.st_size / 1048576:.2f} MB',
                             active=path.name == self.status()['currentFile'],
                             modifiedAt=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()))
        return sorted(rows, key=lambda row: row['modifiedAt'], reverse=True)
