"""串口直读环境传感器（Modbus-RTU）：温湿度 / 光照 / 风速。

迁移背景：环境数据此前由旧 Java 服务经 HTTP（9010）转发，旧服务下线后那条链路
必然失效（表现为环境面板全空、robot-core 整体 degraded）。本驱动直接接管
``/dev/ttyS0``（9600-8N1），不再依赖任何外部进程。

现场寄存器表来自 2026-10-23 真机抓帧（三帧请求/响应 CRC 全部校验通过）：

======  ========  ====  =========  ====
从站    寄存器    数量  含义       缩放
======  ========  ====  =========  ====
12      0x0000    2     温度/湿度  0.1
2       0x0000    1     光照       1
3       0x0063    1     风速       0.1
======  ========  ====  =========  ====

三条设计约束（都由真机环境倒推而来，改动前请先看理由）：

1. **串口只有一个 owner**：旧采集进程（Tomcat 里的定时任务）必须先停，
   否则两边互相抢回复帧，表现为双方都偶发 CRC 错。
2. **缺库不能拖垮 robot-core**：``pyserial`` 延迟导入；未安装时只有本设备
   unavailable，相机/升降等照常工作。
3. **可自愈**：串口打不开时每次 ``read_state`` 都会重试打开。运维停掉占用
   进程后无需重启 robot-core，下一轮轮询自动恢复。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from app.robot_core.common import utc_now
from app.robot_core.contracts import CommandResult, DeviceCommand, DeviceState, HealthState

logger = logging.getLogger(__name__)

FUNCTION_READ_HOLDING = 0x03
FUNCTION_READ_INPUT = 0x04
_MODBUS_FUNCTIONS = (FUNCTION_READ_HOLDING, FUNCTION_READ_INPUT)

#: 允许上报的字段名——与前端 robotStateToTelemetry 的取值键一一对应，改名会静默丢数。
ALLOWED_FIELD_NAMES = frozenset({
    "temperature_c",
    "humidity_percent",
    "ammonia_ppm",
    "illuminance_lux",
    "wind_speed_mps",
    "co2_ppm",
})

_MAX_REGISTERS_PER_READ = 16


class EnvironmentSerialError(RuntimeError):
    """串口或 Modbus 交互失败。"""


class _PortFailure(EnvironmentSerialError):
    """端口级故障（写失败/设备拔出），需要丢弃句柄后重开。"""


# --------------------------------------------------------------------------- Modbus RTU


def crc16(payload: bytes) -> int:
    """Modbus CRC16（多项式 0xA001，初值 0xFFFF）。"""
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(payload: bytes) -> bytes:
    crc = crc16(payload)
    return payload + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def build_read_request(slave: int, function: int, register: int, count: int) -> bytes:
    if function not in _MODBUS_FUNCTIONS:
        raise ValueError("function must be 0x03 or 0x04")
    if not 1 <= slave <= 247:
        raise ValueError("slave must be between 1 and 247")
    if not 1 <= count <= _MAX_REGISTERS_PER_READ:
        raise ValueError(f"count must be between 1 and {_MAX_REGISTERS_PER_READ}")
    if not 0 <= register <= 0xFFFF:
        raise ValueError("register must be between 0 and 0xFFFF")
    body = bytes((slave, function, register >> 8, register & 0xFF, count >> 8, count & 0xFF))
    return with_crc(body)


def parse_read_response(frame: bytes, slave: int, function: int, count: int) -> list[int]:
    """解析 FC03/FC04 响应，返回寄存器原始值（大端 uint16）。"""
    if len(frame) >= 5 and frame[1] == (function | 0x80):
        # 异常响应只有 5 字节：从站 + (功能码|0x80) + 异常码 + CRC
        raise EnvironmentSerialError(f"slave{slave} modbus_exception_{frame[2]}")
    expected = 5 + 2 * count
    if len(frame) < expected:
        raise EnvironmentSerialError(f"slave{slave} short_response({len(frame)}/{expected})")
    frame = frame[:expected]
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise EnvironmentSerialError(f"slave{slave} crc_mismatch")
    if frame[0] != slave:
        raise EnvironmentSerialError(f"slave{slave} address_mismatch({frame[0]})")
    if frame[1] != function:
        raise EnvironmentSerialError(f"slave{slave} function_mismatch({frame[1]:#04x})")
    if frame[2] != 2 * count:
        raise EnvironmentSerialError(f"slave{slave} byte_count_mismatch({frame[2]})")
    return [
        int.from_bytes(frame[3 + index * 2:5 + index * 2], "big")
        for index in range(count)
    ]


# --------------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class SensorField:
    """一个寄存器到业务字段的映射。"""

    offset: int
    name: str
    scale: float = 1.0
    unit: str = ""

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any], *, count: int) -> "SensorField":
        if not isinstance(values, Mapping):
            raise ValueError("environment.sensors[].fields[] must be a mapping")
        try:
            offset = int(values["offset"])
            name = str(values["name"]).strip()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("environment.sensors[].fields[] requires offset and name") from error
        if not 0 <= offset < count:
            raise ValueError(f"field offset {offset} out of range (count={count})")
        if name not in ALLOWED_FIELD_NAMES:
            raise ValueError(
                f"unknown field name {name!r}; allowed: {', '.join(sorted(ALLOWED_FIELD_NAMES))}"
            )
        scale = float(values.get("scale", 1.0))
        if not scale or scale != scale or scale in (float("inf"), float("-inf")):
            raise ValueError(f"field {name} scale must be a finite non-zero number")
        return cls(offset=offset, name=name, scale=scale, unit=str(values.get("unit", "")))


@dataclass(frozen=True)
class SensorChannel:
    """一个从站的一次读取（一次 Modbus 事务）。"""

    slave: int
    register: int
    count: int
    fields: tuple[SensorField, ...]
    function: int = FUNCTION_READ_HOLDING
    label: str = ""

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SensorChannel":
        if not isinstance(values, Mapping):
            raise ValueError("environment.sensors[] must be a mapping")
        try:
            slave = int(values.get("slave", 0))
            register = int(values.get("register", 0))
            count = int(values.get("count", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("environment.sensors[] slave/register/count must be integers") from error
        if not 1 <= slave <= 247:
            raise ValueError(f"sensor slave {slave} must be between 1 and 247")
        if not 1 <= count <= _MAX_REGISTERS_PER_READ:
            raise ValueError(f"sensor slave {slave} count must be between 1 and {_MAX_REGISTERS_PER_READ}")
        if not 0 <= register <= 0xFFFF:
            raise ValueError(f"sensor slave {slave} register must be between 0 and 0xFFFF")
        function = int(values.get("function", FUNCTION_READ_HOLDING))
        if function not in _MODBUS_FUNCTIONS:
            raise ValueError(f"sensor slave {slave} function must be 0x03 or 0x04")
        raw_fields = values.get("fields") or []
        if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
            raise ValueError(f"sensor slave {slave} fields must be a list")
        fields = tuple(SensorField.from_mapping(item, count=count) for item in raw_fields)
        if not fields:
            raise ValueError(f"sensor slave {slave} must declare at least one field")
        names = [field.name for field in fields]
        if len(set(names)) != len(names):
            raise ValueError(f"sensor slave {slave} declares duplicate field names: {names}")
        return cls(
            slave=slave, register=register, count=count, fields=fields, function=function,
            label=str(values.get("label", "")),
        )


@dataclass(frozen=True)
class EnvironmentSerialSettings:
    port: str = "/dev/ttyS0"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout_seconds: float = 0.5
    frame_gap_ms: int = 50
    max_retries: int = 1
    channels: tuple[SensorChannel, ...] = ()

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "EnvironmentSerialSettings":
        values = values or {}
        port = str(values.get("port", cls.port)).strip()
        if not port:
            raise ValueError("devices.environment.port must not be empty")
        baudrate = int(values.get("baudrate", cls.baudrate))
        if not 300 <= baudrate <= 921600:
            raise ValueError("devices.environment.baudrate must be between 300 and 921600")
        bytesize = int(values.get("bytesize", cls.bytesize))
        if bytesize not in {5, 6, 7, 8}:
            raise ValueError("devices.environment.bytesize must be 5, 6, 7 or 8")
        parity = str(values.get("parity", cls.parity)).strip().upper()
        if parity not in {"N", "E", "O"}:
            raise ValueError("devices.environment.parity must be N, E or O")
        stopbits = float(values.get("stopbits", cls.stopbits))
        if stopbits not in {1, 1.5, 2}:
            raise ValueError("devices.environment.stopbits must be 1, 1.5 or 2")
        timeout = float(values.get("timeout_seconds", cls.timeout_seconds))
        if not 0 < timeout <= 5:
            raise ValueError("devices.environment.timeout_seconds must be greater than 0 and at most 5")
        gap = int(values.get("frame_gap_ms", cls.frame_gap_ms))
        if not 0 <= gap <= 1000:
            raise ValueError("devices.environment.frame_gap_ms must be between 0 and 1000")
        retries = int(values.get("max_retries", cls.max_retries))
        if not 0 <= retries <= 5:
            raise ValueError("devices.environment.max_retries must be between 0 and 5")
        raw_channels = values.get("sensors") or []
        if not isinstance(raw_channels, Sequence) or isinstance(raw_channels, (str, bytes)):
            raise ValueError("devices.environment.sensors must be a list")
        channels = tuple(SensorChannel.from_mapping(item) for item in raw_channels)
        if not channels:
            raise ValueError("devices.environment.sensors must contain at least one channel")
        duplicates = [c.slave for c in channels if [x.slave for x in channels].count(c.slave) > 1]
        if duplicates:
            raise ValueError(f"duplicate sensor slave addresses: {sorted(set(duplicates))}")
        return cls(
            port=port, baudrate=baudrate, bytesize=bytesize, parity=parity, stopbits=stopbits,
            timeout_seconds=timeout, frame_gap_ms=gap, max_retries=retries, channels=channels,
        )

    @property
    def source(self) -> str:
        return f"{self.port}@{self.baudrate}"


# --------------------------------------------------------------------------- 驱动


class ModbusSerialEnvironmentDriver:
    """通过 485 总线直读环境传感器。

    与 ``LegacyEnvironmentSensorDriver`` 实现同一契约（open/self_test/read_state/
    execute/close），因此 ``RobotCore`` 与本仓库其余部分无需区分两者。
    """

    def __init__(
        self,
        settings: Optional[EnvironmentSerialSettings],
        *,
        device_id: str = "environment",
        serial_factory: Optional[Callable[..., Any]] = None,
        config_error: Optional[str] = None,
    ) -> None:
        self.device_id = device_id
        self.settings = settings
        self._serial_factory = serial_factory
        self._config_error = config_error
        self._port: Any = None
        self._lock = threading.RLock()
        self._last_values: dict[str, Any] = {}

    @classmethod
    def from_mapping(
        cls,
        values: Optional[Mapping[str, Any]],
        *,
        device_id: str = "environment",
        serial_factory: Optional[Callable[..., Any]] = None,
    ) -> "ModbusSerialEnvironmentDriver":
        """配置非法时**不抛异常**：让 robot-core 照常启动，只让本设备报 unavailable。

        串口配置写错不该连带相机/升降一起起不来——现场更怕「整机起不来」。
        """
        try:
            settings = EnvironmentSerialSettings.from_mapping(values)
        except (TypeError, ValueError) as error:
            logger.error("environment serial config invalid: %s", error)
            return cls(None, device_id=device_id, serial_factory=serial_factory,
                       config_error=f"config_invalid: {error}")
        return cls(settings, device_id=device_id, serial_factory=serial_factory)

    # -- 串口句柄 ---------------------------------------------------------
    def _open_port(self) -> Any:
        if self._port is not None:
            return self._port
        if self.settings is None:
            raise EnvironmentSerialError(self._config_error or "environment serial is not configured")
        factory = self._serial_factory
        if factory is None:
            try:
                import serial  # noqa: PLC0415 - 延迟导入：缺库只影响本设备
            except ImportError as error:  # pragma: no cover - 部署环境缺库
                raise EnvironmentSerialError(
                    "pyserial 未安装（pip install pyserial）"
                ) from error
            factory = serial.Serial
        settings = self.settings
        try:
            self._port = factory(
                port=settings.port,
                baudrate=settings.baudrate,
                bytesize=settings.bytesize,
                parity=settings.parity,
                stopbits=settings.stopbits,
                timeout=settings.timeout_seconds,
                write_timeout=settings.timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - pyserial 的异常类型随平台变化
            self._port = None
            raise EnvironmentSerialError(
                f"{settings.port} 打不开（是否被其它进程占用？）: {error}"
            ) from error
        logger.info("environment serial opened: %s", settings.source)
        return self._port

    def _drop_port(self, reason: str) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响重连
                pass
            logger.warning("environment serial closed: %s", reason)

    # -- 一帧读取 ---------------------------------------------------------
    def _read_exact(self, port: Any, expected: int) -> bytes:
        """读到 expected 字节或超时为止。

        串口 read() 一次可能只返回部分字节；异常响应只有 5 字节，提前收手，
        避免为一个必然失败的帧白等满一个超时。
        """
        deadline = time.monotonic() + self.settings.timeout_seconds
        buffer = bytearray()
        while len(buffer) < expected and time.monotonic() < deadline:
            chunk = port.read(expected - len(buffer))
            if chunk:
                buffer.extend(chunk)
                if len(buffer) >= 5 and buffer[1] & 0x80:
                    break
            else:
                break
        return bytes(buffer)

    def _read_channel(self, port: Any, channel: SensorChannel) -> dict[str, float]:
        request = build_read_request(channel.slave, channel.function, channel.register, channel.count)
        expected = 5 + 2 * channel.count
        last_error = "no_response"
        for attempt in range(self.settings.max_retries + 1):
            if attempt:
                logger.debug("retrying slave %s (%s/%s)", channel.slave, attempt, self.settings.max_retries)
            port.reset_input_buffer()
            try:
                port.write(request)
                port.flush()
            except Exception as error:  # noqa: BLE001 - 设备拔出/驱动异常
                raise _PortFailure(f"串口写入失败: {error}") from error
            frame = self._read_exact(port, expected)
            try:
                registers = parse_read_response(frame, channel.slave, channel.function, channel.count)
            except EnvironmentSerialError as error:
                last_error = str(error)
                continue
            return {
                field.name: round(registers[field.offset] * field.scale, 3)
                for field in channel.fields
            }
        name = f"从站{channel.slave}({channel.label})" if channel.label else f"从站{channel.slave}"
        raise EnvironmentSerialError(f"{name} {last_error}")

    # -- 状态 -------------------------------------------------------------
    def _fault(self, reason: str, health: HealthState = HealthState.UNAVAILABLE) -> DeviceState:
        source = self.settings.source if self.settings is not None else "environment-serial"
        return DeviceState(
            self.device_id, health, utc_now(), source, dict(self._last_values),
            quality="invalid", raw_fault_code=str(reason)[:256],
        )

    def _read_sync(self) -> DeviceState:
        if self.settings is None:
            return self._fault(self._config_error or "environment serial is not configured")
        settings = self.settings
        with self._lock:
            try:
                port = self._open_port()
            except EnvironmentSerialError as error:
                return self._fault(str(error))

            values: dict[str, Any] = {}
            errors: list[str] = []
            for index, channel in enumerate(settings.channels):
                if index and settings.frame_gap_ms:
                    # 485 半双工：从站释放总线需要时间，帧间不留空会丢下一帧。
                    time.sleep(settings.frame_gap_ms / 1000)
                try:
                    values.update(self._read_channel(port, channel))
                except _PortFailure as error:
                    self._drop_port(str(error))
                    errors.append(str(error))
                    break
                except EnvironmentSerialError as error:
                    errors.append(str(error))

            if not values:
                return self._fault("; ".join(errors) or "no_channel_succeeded")

            health = HealthState.DEGRADED if errors else HealthState.OK
            state = DeviceState(
                self.device_id, health, utc_now(), settings.source, values,
                quality="degraded" if errors else "valid",
                raw_fault_code="; ".join(errors)[:256] or None,
            )
            self._last_values = dict(values)
            return state

    # -- DeviceDriver 契约 -------------------------------------------------
    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            try:
                await loop.run_in_executor(None, self._open_port)
            except EnvironmentSerialError as error:
                # 打不开不算致命：read_state 每轮都会重试，停掉占用进程后自愈。
                logger.warning("environment serial open deferred: %s", error)

    async def self_test(self) -> DeviceState:
        return await self.read_state()

    async def read_state(self) -> DeviceState:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sync)

    async def execute(self, command: DeviceCommand) -> CommandResult:
        return CommandResult(command.command_id, "rejected", reject_reason="environment_read_only")

    async def close(self) -> None:
        with self._lock:
            self._drop_port("closed")
