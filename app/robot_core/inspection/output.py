"""Bounded asynchronous JSON journal with atomic, complete snapshots."""
from __future__ import annotations
import json
import os
import queue
import threading
import time
from pathlib import Path


class InspectionOutput:
    def __init__(self, directory, metadata):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'inspection.json'
        self.journal = self.root / '.inspection.records'
        self.metadata = dict(metadata)
        self.error = ''
        self._queue = queue.Queue(maxsize=1024)
        self._thread = threading.Thread(target=self._run, name='inspection-json', daemon=True)
        self._snapshot()
        self._thread.start()

    def add(self, record):
        if self.error:
            raise RuntimeError(self.error)
        try:
            self._queue.put_nowait(record)
        except queue.Full as exc:
            raise RuntimeError('检测记录写盘积压，任务已停止') from exc

    def _snapshot(self):
        temporary = self.path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as out:
            out.write('{"metadata":')
            json.dump(self.metadata, out, ensure_ascii=False)
            out.write(',"records":[')
            if self.journal.exists():
                with self.journal.open(encoding='utf-8') as source:
                    first = True
                    for line in source:
                        if not first:
                            out.write(',')
                        out.write(line.strip())
                        first = False
            out.write(']}')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)

    def _run(self):
        last = time.monotonic()
        try:
            with self.journal.open('a', encoding='utf-8') as journal:
                while True:
                    try:
                        record = self._queue.get(timeout=1)
                    except queue.Empty:
                        record = {}
                    if isinstance(record, tuple):
                        self.metadata.update(record[0])
                        journal.flush()
                        self._snapshot()
                        break
                    if record:
                        journal.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
                    if time.monotonic() - last >= 5:
                        journal.flush()
                        self._snapshot()
                        last = time.monotonic()
            self.journal.unlink(missing_ok=True)
        except Exception as exc:
            self.error = f'检测记录写盘失败：{exc}'

    def close(self, metadata):
        while self._thread.is_alive():
            try:
                self._queue.put((dict(metadata),), timeout=.2)
                break
            except queue.Full:
                continue
        self._thread.join()
        if self.error:
            raise RuntimeError(self.error)
