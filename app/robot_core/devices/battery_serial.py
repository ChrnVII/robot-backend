"""串口直读电池 BMS（Modbus-RTU）：电压 / 电流 / 容量 / SOC / 电芯电压。

迁移背景：电量此前由旧程序 ``com.dust.icore.IcoreBatteryQl`` 经 ``/dev/ttyS5``
读取，再通过 9000 端口的 Java 应用对外提供，9100 的 robot-gateway 只是代理。
旧程序下线后整条链路失效，新后端的 ``battery`` 只能由模拟驱动顶替（曾长期
上报一个编造的常数）。本驱动直接接管 ``/dev/ttyS5``，模拟通道则不再提供任何
电量读数——读不到就留空，让页面显示横杠而不是假数。

寄存器表来自 2026-10-26 真机读帧（CRC 校验通过，且与旧程序反汇编出的
``ReadHoldingRegistersRequest(1, 0, 18)`` / ``(1, 0, 35)`` 一致）：

======  ========  ======  ==================
寄存器   原始值    换算    含义
======  ========  ======  ==================
0       4731      ÷100    总电压 47.31V
1       13        ÷10     总电流 1.3A
2       1471      ÷100    当前容量 14.71Ah
3       3600      ÷100    总容量 36.00Ah（铭牌 36Ah）
4       3644      ÷1000   最高电芯电压 3.644V
5       3638      ÷1000   最低电芯电压 3.638V
6       0x6329    低字节   SOC 41%（高字节 99 疑似 SOH）
21..33  3638~3644 ÷1000   各电芯电压
======  ========  ======  ==================

三条与 ``environment_serial`` 相同的设计约束：

1. **串口单一 owner**：``/dev/ttyS5`` 不能有第二个进程在轮询。
2. **缺库不拖垮 robot-core**：``pyserial`` 延迟导入。
3. **可自愈**：打不开时每轮 ``read_state`` 重试，占用进程退出后自动恢复。

第四条是 BMS 特有的：**只读**。本驱动只发 FC03 读请求，绝不写寄存器——
BMS 的写命令可以改保护参数甚至触发校准，误发代价远高于传感器。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from app.robot_core.common import utc_now
from app.robot_core.contracts import CommandResult, DeviceCommand, DeviceState, HealthState

from .environment_serial import crc16, with_crc

logger = logging.getLogger(__name__)

FUNCTION_READ_HOLDING = 0x03

#: BMS 一次要读 35 个寄存器（旧程序分两次读 18 + 35，实际 35 已覆盖 18）。
_MAX_REGISTERS_PER_READ = 64

#: 电芯电压合理区间（mV），用于过滤未接电芯返回的 0。
_CELL_MV_RANGE = (1500, 4500)


class BatterySerialError(RuntimeError):
    """串口或 Modbus 交互失败。"""


class _PortFailure(BatterySerialError):
    """端口级故障（写失败/设备拔出），需要丢弃句柄后重开。"""


# --------------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class BatteryRegisterMap:
    """寄存器布局。默认值即 2026-10-26 实测确认的映射。"""

    voltage: int = 0
    current: int = 1
    remaining_capacity: int = 2
    total_capacity: int = 3
    cell_voltage_max: int = 4
    cell_voltage_min: int = 5
    soc: int = 6
    first_cell: int = 21
    cell_count: int = 13

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "BatteryRegisterMap":
        values = values or {}
        if not isinstance(values, Mapping):
            raise ValueError("devices.battery.registers must be a mapping")
        defaults = cls()
        parsed = {}
        for name in ("voltage", "current", "remaining_capacity", "total_capacity",
                     "cell_voltage_max", "cell_voltage_min", "soc", "first_cell", "cell_count"):
            raw = values.get(name, getattr(defaults, name))
            try:
                parsed[name] = int(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"devices.battery.registers.{name} must be an integer") from error
            if name == "cell_count":
                if not 0 <= parsed[name] <= 32:
                    raise ValueError("devices.battery.registers.cell_count must be between 0 and 32")
            elif not 0 <= parsed[name] <= 0xFFFF:
                raise ValueError(f"devices.battery.registers.{name} must be between 0 and 0xFFFF")
        return cls(**parsed)


@dataclass(frozen=True)
class BatterySerialSettings:
    port: str = "/dev/ttyS5"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout_seconds: float = 0.6
    max_retries: int = 1
    slave: int = 1
    register: int = 0x0000
    count: int = 35
    soc_mode: str = "capacity"
    registers: BatteryRegisterMap = BatteryRegisterMap()

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "BatterySerialSettings":
        values = values or {}
        if not isinstance(values, Mapping):
            raise ValueError("devices.battery must be a mapping")
        port = str(values.get("port", cls.port)).strip()
        if not port:
            raise ValueError("devices.battery.port must not be empty")
        baudrate = int(values.get("baudrate", cls.baudrate))
        if not 300 <= baudrate <= 921600:
            raise ValueError("devices.battery.baudrate must be between 300 and 921600")
        bytesize = int(values.get("bytesize", cls.bytesize))
        if bytesize not in {5, 6, 7, 8}:
            raise ValueError("devices.battery.bytesize must be 5, 6, 7 or 8")
        parity = str(values.get("parity", cls.parity)).strip().upper()
        if parity not in {"N", "E", "O"}:
            raise ValueError("devices.battery.parity must be N, E or O")
        stopbits = float(values.get("stopbits", cls.stopbits))
        if stopbits not in {1, 1.5, 2}:
            raise ValueError("devices.battery.stopbits must be 1, 1.5 or 2")
        timeout = float(values.get("timeout_seconds", cls.timeout_seconds))
        if not 0 < timeout <= 5:
            raise ValueError("devices.battery.timeout_seconds must be greater than 0 and at most 5")
        retries = int(values.get("max_retries", cls.max_retries))
        if not 0 <= retries <= 5:
            raise ValueError("devices.battery.max_retries must be between 0 and 5")
        slave = int(values.get("slave", cls.slave))
        if not 1 <= slave <= 247:
            raise ValueError("devices.battery.slave must be between 1 and 247")
        register = int(values.get("register", cls.register))
        if not 0 <= register <= 0xFFFF:
            raise ValueError("devices.battery.register must be between 0 and 0xFFFF")
        count = int(values.get("count", cls.count))
        if not 1 <= count <= _MAX_REGISTERS_PER_READ:
            raise ValueError(f"devices.battery.count must be between 1 and {_MAX_REGISTERS_PER_READ}")
        soc_mode = str(values.get("soc_mode", cls.soc_mode)).strip().lower()
        if soc_mode not in {"capacity", "register"}:
            raise ValueError("devices.battery.soc_mode must be 'capacity' or 'register'")
        registers = BatteryRegisterMap.from_mapping(values.get("registers"))
        highest = max(registers.voltage, registers.current, registers.remaining_capacity,
                      registers.total_capacity, registers.cell_voltage_max,
                      registers.cell_voltage_min, registers.soc)
        if highest >= count:
            raise ValueError(
                f"devices.battery.registers refer to register {highest} but count is {count}"
            )
        if registers.cell_count and registers.first_cell + registers.cell_count - 1 >= count:
            raise ValueError(
                f"cell registers {registers.first_cell}..{registers.first_cell + registers.cell_count - 1} "
                f"exceed count {count}"
            )
        return cls(
            port=port, baudrate=baudrate, bytesize=bytesize, parity=parity, stopbits=stopbits,
            timeout_seconds=timeout, max_retries=retries, slave=slave, register=register,
            count=count, soc_mode=soc_mode, registers=registers,
        )

    @property
    def source(self) -> str:
        return f"{self.port}@{self.baudrate}"


# --------------------------------------------------------------------------- 解析


def build_read_request(slave: int, register: int, count: int) -> bytes:
    if not 1 <= slave <= 247:
        raise ValueError("slave must be between 1 and 247")
    if not 1 <= count <= _MAX_REGISTERS_PER_READ:
        raise ValueError(f"count must be between 1 and {_MAX_REGISTERS_PER_READ}")
    if not 0 <= register <= 0xFFFF:
        raise ValueError("register must be between 0 and 0xFFFF")
    body = bytes((slave, FUNCTION_READ_HOLDING,
                  register >> 8, register & 0xFF, count >> 8, count & 0xFF))
    return with_crc(body)


def parse_read_response(frame: bytes, slave: int, count: int) -> list[int]:
    """解析 FC03 响应，返回寄存器原始值（大端 uint16）。"""
    if len(frame) >= 5 and frame[1] == (0x03 | 0x80):
        raise BatterySerialError(f"slave{slave} modbus_exception_{frame[2]}")
    expected = 5 + 2 * count
    if len(frame) < expected:
        raise BatterySerialError(f"slave{slave} short_response({len(frame)}/{expected})")
    frame = frame[:expected]
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise BatterySerialError(f"slave{slave} crc_mismatch")
    if frame[0] != slave:
        raise BatterySerialError(f"slave{slave} address_mismatch({frame[0]})")
    if frame[1] != 0x03:
        raise BatterySerialError(f"slave{slave} function_mismatch({frame[1]:#04x})")
    if frame[2] != 2 * count:
        raise BatterySerialError(f"slave{slave} byte_count_mismatch({frame[2]})")
    return [
        int.from_bytes(frame[3 + index * 2: 5 + index * 2], "big")
        for index in range(count)
    ]


def decode_registers(registers: list[int], registers_map: BatteryRegisterMap,
                     soc_mode: str = "capacity") -> dict[str, Any]:
    """把原始寄存器翻译成业务字段。

    SOC 默认按「当前容量 ÷ 总容量」计算：它只依赖两个已确认的刻度，不受位域
    布局假设影响；``soc_mode=register`` 时改用寄存器低字节（同一块电池上两者
    相差不到 0.1 个百分点，见模块文档）。
    """
    voltage = registers[registers_map.voltage] / 100.0
    current = registers[registers_map.current] / 10.0
    remaining = registers[registers_map.remaining_capacity] / 100.0
    total = registers[registers_map.total_capacity] / 100.0
    cell_max = registers[registers_map.cell_voltage_max] / 1000.0
    cell_min = registers[registers_map.cell_voltage_min] / 1000.0
    soc_register = registers[registers_map.soc] & 0xFF

    percent: Optional[float] = None
    if soc_mode == "register" and 0 <= soc_register <= 100:
        percent = float(soc_register)
    elif total > 0 and 0 <= remaining <= total * 1.05:
        percent = remaining / total * 100.0
    elif 0 <= soc_register <= 100:
        percent = float(soc_register)
    if percent is not None:
        percent = round(min(100.0, max(0.0, percent)), 1)

    values: dict[str, Any] = {
        "voltage_v": round(voltage, 2),
        "current_a": round(current, 2),
        "remaining_capacity_ah": round(remaining, 2),
        "total_capacity_ah": round(total, 2),
        "cell_voltage_max_v": round(cell_max, 3),
        "cell_voltage_min_v": round(cell_min, 3),
        "soc_register_percent": soc_register,
    }
    if percent is not None:
        # 前端 robotStateToTelemetry 读的就是 batteryPercent —— 改名会静默丢数。
        values["battery_percent"] = percent

    cells = []
    if registers_map.cell_count:
        window = registers[registers_map.first_cell:registers_map.first_cell + registers_map.cell_count]
        cells = [round(raw / 1000.0, 3) for raw in window if _CELL_MV_RANGE[0] <= raw <= _CELL_MV_RANGE[1]]
    if cells:
        values["cell_voltage_avg_v"] = round(sum(cells) / len(cells), 3)
        values["cell_count_active"] = len(cells)
    return values


# --------------------------------------------------------------------------- 驱动


class ModbusSerialBatteryDriver:
    """通过 485 直读电池 BMS。

    与 ``SimulatedDeviceDriver`` 实现同一契约（open/self_test/read_state/execute/
    close），``RobotCore`` 与前端无需区分。
    """

    def __init__(
        self,
        settings: Optional[BatterySerialSettings],
        *,
        device_id: str = "battery",
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
        device_id: str = "battery",
        serial_factory: Optional[Callable[..., Any]] = None,
    ) -> "ModbusSerialBatteryDriver":
        """配置非法时不抛异常：只让电池设备 unavailable，不影响其它设备启动。"""
        try:
            settings = BatterySerialSettings.from_mapping(values)
        except (TypeError, ValueError) as error:
            logger.error("battery serial config invalid: %s", error)
            return cls(None, device_id=device_id, serial_factory=serial_factory,
                       config_error=f"config_invalid: {error}")
        return cls(settings, device_id=device_id, serial_factory=serial_factory)

    @property
    def config_error(self) -> Optional[str]:
        return self._config_error

    # -- 串口句柄 ---------------------------------------------------------
    def _open_port(self) -> Any:
        if self._port is not None:
            return self._port
        if self.settings is None:
            raise BatterySerialError(self._config_error or "battery serial is not configured")
        factory = self._serial_factory
        if factory is None:
            try:
                import serial  # noqa: PLC0415 - 延迟导入：缺库只影响本设备
            except ImportError as error:  # pragma: no cover - 部署环境缺库
                raise BatterySerialError("pyserial 未安装（pip install pyserial）") from error
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
        except Exception as error:  # noqa: BLE001 - pyserial 异常类型随平台变化
            self._port = None
            raise BatterySerialError(
                f"{settings.port} 打不开（是否被其它进程占用？）: {error}"
            ) from error
        logger.info("battery serial opened: %s", settings.source)
        return self._port

    def _drop_port(self, reason: str) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响重连
                pass
            logger.warning("battery serial closed: %s", reason)

    # -- 一帧读取 ---------------------------------------------------------
    def _read_exact(self, port: Any, expected: int) -> bytes:
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

    def _read_registers(self, port: Any) -> list[int]:
        settings = self.settings
        request = build_read_request(settings.slave, settings.register, settings.count)
        expected = 5 + 2 * settings.count
        last_error = "no_response"
        for _ in range(settings.max_retries + 1):
            port.reset_input_buffer()
            try:
                port.write(request)
                port.flush()
            except Exception as error:  # noqa: BLE001 - 设备拔出/驱动异常
                raise _PortFailure(f"串口写入失败: {error}") from error
            frame = self._read_exact(port, expected)
            try:
                return parse_read_response(frame, settings.slave, settings.count)
            except BatterySerialError as error:
                last_error = str(error)
        raise BatterySerialError(f"从站{settings.slave} {last_error}")

    # -- 状态 -------------------------------------------------------------
    def _fault(self, reason: str, health: HealthState = HealthState.UNAVAILABLE) -> DeviceState:
        source = self.settings.source if self.settings is not None else "battery-serial"
        return DeviceState(
            self.device_id, health, utc_now(), source, dict(self._last_values),
            quality="invalid", raw_fault_code=str(reason)[:256],
        )

    def _read_sync(self) -> DeviceState:
        if self.settings is None:
            return self._fault(self._config_error or "battery serial is not configured")
        settings = self.settings
        with self._lock:
            try:
                port = self._open_port()
            except BatterySerialError as error:
                return self._fault(str(error))
            try:
                registers = self._read_registers(port)
            except _PortFailure as error:
                self._drop_port(str(error))
                return self._fault(str(error))
            except BatterySerialError as error:
                # 有历史读数时降级（页面仍显示上次值并标记 stale），首次失败则 unavailable。
                health = HealthState.DEGRADED if self._last_values else HealthState.UNAVAILABLE
                return self._fault(str(error), health)

        values = decode_registers(registers, settings.registers, settings.soc_mode)
        state = DeviceState(
            self.device_id, HealthState.OK, utc_now(), settings.source, values,
            quality="valid", raw_fault_code=None,
        )
        self._last_values = dict(values)
        return state

    # -- DeviceDriver 契约 -------------------------------------------------
    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            try:
                await loop.run_in_executor(None, self._open_port)
            except BatterySerialError as error:
                # 打不开不致命：read_state 每轮重试，占用进程退出后自愈。
                logger.warning("battery serial open deferred: %s", error)

    async def self_test(self) -> DeviceState:
        return await self.read_state()

    async def read_state(self) -> DeviceState:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sync)

    async def execute(self, command: DeviceCommand) -> CommandResult:
        return CommandResult(command.command_id, "rejected", reject_reason="battery_read_only")

    async def close(self) -> None:
        with self._lock:
            self._drop_port("closed")
