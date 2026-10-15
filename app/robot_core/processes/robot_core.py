from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from app.robot_core.common.config import load_config
from app.robot_core.contracts import DeviceCommand
from app.robot_core.core import RobotCore
from app.robot_core.devices import (
    LegacyEnvironmentSensorDriver, NetworkCameraControlSettings, NetworkCameraDriver,
    SimulatedDeviceDriver,
)
from app.ipc import JsonSocketServer
from app.robot_core.storage import PlatformStore

from .common import check_output, configure_logging, parser, run


def build(config):
    store = PlatformStore(config.database_url, pool_options=config.database_pool_options)
    devices = {name: SimulatedDeviceDriver(name) for name in ("motion", "battery", "rfid", "track", "environment", "lift", "light", "camera")}
    devices["environment"] = SimulatedDeviceDriver("environment", {
        "temperature_c": 24.8,
        "humidity_percent": 68.5,
        "ammonia_ppm": 12.3,
        "illuminance_lux": 320.0,
    })
    if config.values["runtime"]["mode"] == "hardware":
        camera = config.values.get("devices", {}).get("camera", {})
        control = dict(camera.get("control", {}))
        if not control:
            control = camera
        else:
            # Keep credentials in the camera section so one environment-backed
            # secret configures both capture and PTZ control.
            for key in ("username", "password"):
                if key not in control and key in camera:
                    control[key] = camera[key]
        devices["camera"] = NetworkCameraDriver(NetworkCameraControlSettings.from_mapping(control))
        environment = config.values.get("devices", {}).get("environment", {})
        devices["environment"] = LegacyEnvironmentSensorDriver(
            base_url=str(environment.get("base_url", "http://127.0.0.1:9010")),
            port_name=str(environment.get("port_name", "/dev/ttyS0")),
            timeout_seconds=float(environment.get("timeout_seconds", 2.0)),
        )
    safety = config.values["safety"]
    core = RobotCore(store, devices, lease_ttl_ms=int(safety["lease_ttl_ms"]),
                     max_motion_distance_m=float(safety["max_motion_distance_m"]))
    core._poll_interval_seconds = float(
        config.values.get("devices", {}).get("environment", {}).get("poll_interval_seconds", 5.0)
    )
    return core


def main() -> None:
    args = parser("robot-core").parse_args()
    config = load_config(args.config)
    configure_logging(config, "robot-core")
    core = build(config)
    if args.check:
        check_output("robot-core", config, {"database": config.database_display_url, "devices": list(core.devices)})
        core.store.close()
        return

    async def serve() -> None:
        await core.start()

        async def handler(request):
            operation = request.get("operation")
            if operation == "health":
                return await core.health()
            if operation == "device.state":
                return core.device_state(str(request["device_id"]))
            if operation == "lease.acquire":
                return asdict(core.leases.acquire(str(request["holder"]), str(request["mode"])))
            if operation == "lease.release":
                core.leases.release(str(request["lease_id"]), str(request.get("reason", "released")))
                return {"released": True}
            if operation == "emergency.stop":
                await core.emergency_stop(str(request.get("reason", "requested")), str(request["trace_id"]))
                return {"state": core.state}
            if operation == "asset.register":
                asset = dict(request["asset"])
                core.store.register_asset(asset)
                return {"asset_id": asset["asset_id"], "registered": True}
            if operation == "inspection.persist":
                return core.store.persist_inference_result(
                    original_asset=request["original_asset"], result_asset=request.get("result_asset"),
                    inference=request["inference"], detections=request.get("detections", []))
            if operation == "command":
                now = datetime.now(timezone.utc)
                ttl = int(config.values["safety"]["default_command_ttl_ms"])
                command = DeviceCommand(
                    target=str(request["target"]), action=str(request["action"]), payload=dict(request.get("payload", {})),
                    issuer=str(request["issuer"]), lease_id=str(request["lease_id"]), issued_at=str(request.get("issued_at", now.isoformat())),
                    expires_at=str(request.get("expires_at", (now + timedelta(milliseconds=ttl)).isoformat())),
                    idempotency_key=str(request["idempotency_key"]), trace_id=str(request["trace_id"]),
                )
                return asdict(await core.execute(command))
            raise ValueError("unsupported operation")

        server = JsonSocketServer(config.path("ipc", "robot_core"), handler)
        try:
            await server.serve_forever()
        finally:
            await server.close()
            await core.stop()
            core.store.close()

    run(serve)


if __name__ == "__main__":
    main()
