from __future__ import annotations

import asyncio

from app.contracts import Op
from app.robot_core.common.config import load_config
from app.robot_core.contracts import CaptureRequest
from app.ipc import JsonSocketServer
from app.robot_core.sensing import (
    AudioSettings,
    SensorService,
    SoundDeviceAudioProvider,
)
from app.robot_core.storage import MediaStore
from app.robot_core.storage import PlatformStore
from app.robot_core.sensing.camera_pool import CameraPool

from .common import check_output, configure_logging, parser, run


def build_service(config, store: PlatformStore, cameras: CameraPool) -> SensorService:
    media = MediaStore(config.path("storage", "media_root"), store, config.robot_id)
    simulated = lambda request: f"simulated:{request.device_id}:{request.request_id}".encode("utf-8")
    providers = {"camera": simulated, "thermal": simulated, "audio": simulated}
    camera_config = config.values.get("devices", {}).get("camera", {})
    audio_config = config.values.get("devices", {}).get("audio", {})
    providers["camera"] = cameras.capture_provider(camera_config)
    for values in config.values.get("devices", {}).get("cameras", {}).get("items", []):
        if values.get("enabled", True):
            providers[str(values["id"])] = cameras.capture_provider(values)
    if config.values["runtime"]["mode"] == "hardware":
        providers["audio"] = SoundDeviceAudioProvider(
            AudioSettings.from_mapping(audio_config)
        ).capture
    return SensorService(media, providers)


async def handle_request(service: SensorService, request: dict) -> dict:
    if request.get("operation") == Op.HEALTH:
        return {"service": "sensor-worker", "state": "ok", "providers": list(service.providers)}
    if request.get("operation") != Op.CAPTURE:
        raise ValueError("unsupported operation")
    capture = CaptureRequest(str(request["task_run_id"]), str(request["waypoint_id"]),
        str(request["device_id"]), str(request["media_type"]), parameters=dict(request.get("parameters", {})))
    return await asyncio.to_thread(service.capture, capture)


def main() -> None:
    """Optional standalone entry point; the default deployment embeds this service."""
    args = parser("sensor-worker").parse_args()
    config = load_config(args.config)
    configure_logging(config, "sensor-worker")
    store = PlatformStore(config.database_url_for("perception"), pool_options=config.database_pool_options,
                          initialize=not args.check)
    cameras = CameraPool(simulated=config.values["runtime"]["mode"] == "simulated")
    service = build_service(config, store, cameras)
    if args.check:
        from app.migrations import check_database
        check_database(store.database_url)
        check_output("sensor-worker", config, {"mediaRoot": str(service.media.root), "providers": list(service.providers)})
        store.close()
        return

    async def handler(request):
        return await handle_request(service, request)

    server = JsonSocketServer(config.path("ipc", "sensor_worker"), handler)
    async def serve():
        try:
            await server.serve_forever()
        finally:
            await server.close()
    try:
        run(serve)
    finally:
        cameras.close()
        store.close()


if __name__ == "__main__":
    main()
