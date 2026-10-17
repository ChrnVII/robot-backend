"""Local binary IPC bridge. The API authenticates clients before proxying."""
import asyncio
import json
import struct
from pathlib import Path

MAX_PACKET = 16 * 1024 * 1024


class InspectionPreview:
    def __init__(self):
        self.task_id = None
        self.channels = {}
        self.loop = None
        self.server = None
        self.clients = set()

    async def start(self, path):
        self.loop = asyncio.get_running_loop()
        self.path = Path(str(path) + '.video')
        # The worker's main IPC lock is already held before this is called.
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        self.path.chmod(0o600)

    def bind(self, task_id, channels):
        self.task_id, self.channels = task_id, channels
        for channel in channels.values():
            channel.attach_loop(self.loop)

    def clear(self):
        for channel in self.channels.values():
            channel.close_clients()
        self.task_id, self.channels = None, {}

    async def close(self):
        self.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)
        tasks = list(self.clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        client = channel = receive = pending = None
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 5))
            channel = self.channels.get(request.get('camera_id')) if request.get('task_run_id') == self.task_id else None
            if channel is None:
                raise ValueError('任务预览不可用')
            client = channel.add_client()
            async def send(item):
                data = b'T' + item.encode() if isinstance(item, str) else b'B' + item
                writer.write(struct.pack('!I', len(data)) + data)
                await asyncio.wait_for(writer.drain(), 5)
            if channel.config_message:
                await send(channel.config_message)
            receive = asyncio.create_task(reader.read(1))
            while True:
                pending = asyncio.create_task(client.queue.get())
                done, _ = await asyncio.wait((receive, pending), return_when=asyncio.FIRST_COMPLETED)
                if receive in done:
                    break
                item = pending.result()
                await send(item)
                if isinstance(item, str) and json.loads(item).get('t') == 'stop':
                    break
        except (ValueError, OSError, asyncio.TimeoutError):
            pass
        finally:
            if channel is not None and client is not None:
                channel.remove_client(client)
            tasks = [t for t in (receive, pending) if t is not None]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.clients.discard(task)
