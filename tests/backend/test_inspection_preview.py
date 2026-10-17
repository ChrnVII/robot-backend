import asyncio
import json
import struct
import pytest
from app.video.inspection_preview import InspectionPreview
from app.video.pipeline import CameraBroadcaster
from app.video.codec import pack_frame, FLAG_KEY

pytestmark = pytest.mark.no_database


def test_binary_bridge_isolates_cameras_and_disconnects():
    async def scenario():
        bridge = InspectionPreview()
        bridge.loop = asyncio.get_running_loop()
        # TCP only in this test; production binds a private Unix socket.
        server = await asyncio.start_server(bridge._serve, '127.0.0.1', 0)
        channels = {key: CameraBroadcaster() for key in ['cam01', 'cam02']}
        for channel in channels.values():
            channel.set_codec('avc1.42001E')
        bridge.bind('run', channels)
        port = server.sockets[0].getsockname()[1]
        async def connect(camera):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(json.dumps({'task_run_id': 'run', 'camera_id': camera}).encode() + b'\n')
            await writer.drain()
            return reader, writer
        async def read(reader):
            size = struct.unpack('!I', await asyncio.wait_for(reader.readexactly(4), 2))[0]
            return await reader.readexactly(size)
        first, one = await connect('cam01')
        second, two = await connect('cam02')
        assert (await read(first)).startswith(b'T')
        assert (await read(second)).startswith(b'T')
        channels['cam01'].broadcast(pack_frame(FLAG_KEY, 10, 1) + b'one')
        channels['cam02'].broadcast(pack_frame(FLAG_KEY, 20, 1) + b'two')
        assert (await read(first)).endswith(b'one')
        assert (await read(second)).endswith(b'two')
        bridge.clear()
        assert json.loads((await read(first))[1:])['t'] == 'stop'
        one.close(); two.close()
        await one.wait_closed(); await two.wait_closed()
        await bridge.close()
        server.close()
        await server.wait_closed()
        assert all(channel.client_count == 0 for channel in channels.values())
    asyncio.run(scenario())
