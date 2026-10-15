from __future__ import annotations

from app.robot_core.common.config import load_config
from app.robot_core.contracts import CaptureRequest
from app.ipc import JsonSocketClient, JsonSocketServer
from app.robot_core.sensing import (
    AudioSettings,
    CameraSettings,
    HttpJpegCameraProvider,
    NetworkCameraSettings,
    OpenCVCameraProvider,
    RtspCameraProvider,
    SensorService,
    SoundDeviceAudioProvider,
)
from app.robot_core.storage import MediaStore

from .common import check_output, configure_logging, parser, run


def main() -> None:
    args = parser("sensor-worker").parse_args()
    config = load_config(args.config)
    configure_logging(config, "sensor-worker")
    # sensor-worker owns media files; robot-core remains the business database writer.
    media = MediaStore(config.path("storage", "media_root"), None, config.robot_id)
    core = JsonSocketClient(config.path("ipc", "robot_core"))
    simulated = lambda request: f"simulated:{request.device_id}:{request.request_id}".encode("utf-8")
    providers = {"camera": simulated, "thermal": simulated, "audio": simulated}
    camera_config = config.values.get("devices", {}).get("camera", {})
    audio_config = config.values.get("devices", {}).get("audio", {})
    if config.values["runtime"]["mode"] == "hardware":
        camera_transport = str(camera_config.get("transport", "v4l2")).lower()
        if camera_transport == "http":
            providers["camera"] = HttpJpegCameraProvider(
                NetworkCameraSettings.from_mapping(camera_config)
            ).capture
        elif camera_transport == "rtsp":
            providers["camera"] = RtspCameraProvider(
                NetworkCameraSettings.from_mapping(camera_config)
            ).capture
        elif camera_transport == "v4l2":
            providers["camera"] = OpenCVCameraProvider(
                CameraSettings.from_mapping(camera_config)
            ).capture
        else:
            raise ValueError(f"unsupported camera transport: {camera_transport}")
        providers["audio"] = SoundDeviceAudioProvider(
            AudioSettings.from_mapping(audio_config)
        ).capture
    service = SensorService(media, providers)
    if args.check:
        check_output("sensor-worker", config, {"mediaRoot": str(media.root), "providers": list(providers)})
        return

    async def handler(request):
        if request.get("operation") == "health":
            return {"service": "sensor-worker", "state": "ok", "providers": list(providers)}
        if request.get("operation") != "capture":
            raise ValueError("unsupported operation")
        capture = CaptureRequest(str(request["task_run_id"]), str(request["waypoint_id"]), str(request["device_id"]), str(request["media_type"]), parameters=dict(request.get("parameters", {})))
        asset = service.capture(capture)
        try:
            await core.call({"operation": "asset.register", "asset": asset})
        except Exception:
            media.discard(asset)
            raise
        return asset

    run(JsonSocketServer(config.path("ipc", "sensor_worker"), handler).serve_forever)


if __name__ == "__main__":
    main()
