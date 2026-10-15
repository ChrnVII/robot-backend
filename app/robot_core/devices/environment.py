from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Mapping, Optional
from urllib import error, request

from app.robot_core.common import utc_now
from app.robot_core.contracts import CommandResult, DeviceState, HealthState


class EnvironmentSensorError(RuntimeError):
    """Raised when the legacy environment sensor cannot provide a valid sample."""


class LegacyEnvironmentSensorDriver:
    """Read the old robot's LTWY sensor through the Java HTTP service.

    The Java service remains the only process that owns /dev/ttyS0. This adapter
    deliberately uses HTTP so the new framework cannot compete for the serial
    port during migration.
    """

    FIELDS = {
        "temperature": "temperature_c",
        "humidity": "humidity_percent",
        "ammonia": "ammonia_ppm",
        "illuminance": "illuminance_lux",
    }

    def __init__(
        self,
        device_id: str = "environment",
        base_url: str = "http://127.0.0.1:9010",
        port_name: str = "/dev/ttyS0",
        timeout_seconds: float = 2.0,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        self.port_name = port_name
        self.timeout_seconds = timeout_seconds
        self._opener = opener or request.urlopen
        self._opened = False
        self._last_state: Optional[DeviceState] = None

    def _post(self, path: str, *, require_mapping: bool = True) -> Mapping[str, Any]:
        payload = json.dumps({"portName": self.port_name}).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except (OSError, error.URLError, TimeoutError) as exc:
            raise EnvironmentSensorError(f"legacy sensor request failed: {exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnvironmentSensorError("legacy sensor returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise EnvironmentSensorError("legacy sensor response must be an object")
        code = value.get("code")
        if code is not None and str(code) != "200":
            raise EnvironmentSensorError(str(value.get("msg") or "legacy sensor rejected request"))
        data = value.get("data", value)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                if require_mapping:
                    raise EnvironmentSensorError("legacy sensor data is not valid JSON") from exc
                return {"message": data}
        if not isinstance(data, dict):
            raise EnvironmentSensorError("legacy sensor data must be an object")
        return data

    @staticmethod
    def _number(value: Any, name: str) -> float:
        if isinstance(value, bool) or value is None:
            raise EnvironmentSensorError(f"missing environment field: {name}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise EnvironmentSensorError(f"invalid environment field: {name}") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise EnvironmentSensorError(f"invalid environment field: {name}")
        return number

    def _read_sync(self) -> DeviceState:
        data = self._post("/sensorData/detectLtwy")
        values = {
            target: self._number(data.get(source), source)
            for source, target in self.FIELDS.items()
        }
        return DeviceState(self.device_id, HealthState.OK, utc_now(), self.base_url, values)

    async def _run_blocking(self, func: Callable[[], Any]) -> Any:
        """Run urllib without blocking the event loop on Python 3.8+."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func)

    async def open(self) -> None:
        # Opening is idempotent in the legacy service.
        await self._run_blocking(
            lambda: self._post("/sensorData/sensorOpen", require_mapping=False)
        )
        self._opened = True

    async def self_test(self) -> DeviceState:
        return await self.read_state()

    async def read_state(self) -> DeviceState:
        if not self._opened:
            try:
                await self.open()
            except EnvironmentSensorError as exc:
                return DeviceState(
                    self.device_id, HealthState.UNAVAILABLE, utc_now(), self.base_url,
                    dict(self._last_state.values) if self._last_state else {},
                    quality="invalid", raw_fault_code=str(exc)[:256],
                )
        try:
            state = await self._run_blocking(self._read_sync)
        except EnvironmentSensorError as exc:
            state = DeviceState(
                self.device_id, HealthState.UNAVAILABLE, utc_now(), self.base_url,
                dict(self._last_state.values) if self._last_state else {},
                quality="invalid", raw_fault_code=str(exc)[:256],
            )
        if state.health == HealthState.OK:
            self._last_state = state
        return state

    async def execute(self, command: Any) -> CommandResult:
        return CommandResult(command.command_id, "rejected", reject_reason="environment_read_only")

    async def close(self) -> None:
        if self._opened:
            try:
                await self._run_blocking(
                    lambda: self._post("/sensorData/sensorClose", require_mapping=False)
                )
            except EnvironmentSensorError:
                pass
        self._opened = False
