from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from app.contracts import (
    CommandWire,
    DeviceQueryWire,
    EmergencyResetWire,
    EmergencyStopWire,
    LeaseAcquireWire,
    LeaseReleaseWire,
    Op,
)
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
    store = PlatformStore(config.database_url_for("core"), pool_options=config.database_pool_options, initialize=False)
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
                     max_motion_distance_m=float(safety["max_motion_distance_m"]),
                     audit_directory=config.path("storage", "media_root").parent / "emergency-audit")
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
        from app.migrations import check_database
        check_database(core.store.database_url)
        check_output("robot-core", config, {"database": config.database_display_url, "devices": list(core.devices)})
        core.store.close()
        return

    async def serve() -> None:
        await core.start()

        async def handler(request):
            operation = request.get("operation")
            if operation == Op.HEALTH:
                return await core.health()
            if operation == Op.DEVICE_STATE:
                return core.device_state(str(request["device_id"]))
            if operation == Op.LEASE_ACQUIRE:
                lease = LeaseAcquireWire.from_mapping(request)
                return asdict(await asyncio.to_thread(core.leases.acquire, lease.holder, lease.mode))
            if operation == Op.LEASE_RELEASE:
                release = LeaseReleaseWire.from_mapping(request)
                await asyncio.to_thread(core.leases.release, release.lease_id, release.reason)
                return {"released": True}
            if operation == Op.EMERGENCY_STOP:
                stop = EmergencyStopWire.from_mapping(request)
                await core.emergency_stop(stop.reason, stop.trace_id)
                return {"state": core.state}
            if operation == Op.EMERGENCY_RESET:
                reset = EmergencyResetWire.from_mapping(request)
                return await core.reset_emergency(reset.lease_id, reset.trace_id, reset.reason)
            if operation == Op.DEVICE_QUERY:
                query = DeviceQueryWire.from_mapping(request)
                return await core.query(query.device_id, query.query, dict(query.params))
            if operation == Op.ASSET_REGISTER:
                asset = dict(request["asset"])
                await asyncio.to_thread(core.store.register_asset, asset)
                return {"asset_id": asset["asset_id"], "registered": True}
            if operation == Op.INSPECTION_PERSIST:
                return await asyncio.to_thread(core.store.persist_inference_result,
                    original_asset=request["original_asset"], result_asset=request.get("result_asset"),
                    inference=request["inference"], detections=request.get("detections", []))
            if operation == Op.COMMAND:
                now = datetime.now(timezone.utc)
                ttl = int(config.values["safety"]["default_command_ttl_ms"])
                wire = CommandWire.from_mapping(
                    request,
                    default_issued_at=now.isoformat(),
                    default_expires_at=(now + timedelta(milliseconds=ttl)).isoformat(),
                )
                command = DeviceCommand(
                    target=wire.target, action=wire.action, payload=dict(wire.payload),
                    issuer=wire.issuer, lease_id=wire.lease_id,
                    issued_at=wire.issued_at, expires_at=wire.expires_at,
                    idempotency_key=wire.idempotency_key, trace_id=wire.trace_id,
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
