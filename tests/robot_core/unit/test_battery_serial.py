"""串口电池驱动：用真机读到的字节做回归向量。

帧与寄存器值来自 2026-10-26 在机器人上对 /dev/ttyS5 的只读探测（CRC 已校验，
重算出的帧尾 2b1b 与现场输出一致），寄存器表另与旧程序 IcoreBatteryQl 反汇编出的
ReadHoldingRegistersRequest(1, 0, 18) / (1, 0, 35) 对照过。

这组用例守三件事：帧格式、寄存器表、SOC 口径。
"""

import asyncio
import unittest
from pathlib import Path

import pytest
import yaml

from app.robot_core.contracts import HealthState
from app.robot_core.devices.battery_serial import (
    BatteryRegisterMap,
    BatterySerialSettings,
    ModbusSerialBatteryDriver,
    build_read_request,
    crc16,
    decode_registers,
    parse_read_response,
)

pytestmark = pytest.mark.no_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: 与 configs/hardware.yaml 的 devices.battery 保持同步。
BATTERY_CONFIG = {
    "transport": "serial",
    "port": "/dev/ttyS5",
    "baudrate": 9600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout_seconds": 0.6,
    "max_retries": 1,
    "slave": 1,
    "register": 0x0000,
    "count": 35,
    "soc_mode": "capacity",
    "registers": {
        "voltage": 0, "current": 1, "remaining_capacity": 2, "total_capacity": 3,
        "cell_voltage_max": 4, "cell_voltage_min": 5, "soc": 6,
        "first_cell": 21, "cell_count": 13,
    },
}

#: 真机读数（寄存器原值，count=18 与 count=35 前 18 个完全一致）
REAL_REGISTERS_18 = [4731, 13, 1471, 3600, 3644, 3638, 25385, 21, 5911, 0, 6144, 257, 0, 0, 0, 8192, 49155, 0]
REAL_REGISTERS_35 = REAL_REGISTERS_18 + [0, 0, 0] + [3638, 3640, 3638, 3641, 3640,
                                                     3639, 3640, 3640, 3639, 3644] + [0, 0, 0, 0]


def frame_of(slave, registers):
    """按设备实际返回的格式拼响应帧（字节数 + 大端数据 + CRC）。"""
    body = bytes((slave, 0x03, 2 * len(registers))) + b"".join(
        value.to_bytes(2, "big") for value in registers
    )
    crc = crc16(body)
    return body + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


#: 现场读到的响应帧（重算后与截图里的 2b1b / 0efa 帧尾一致）
REAL_RESPONSE_18 = frame_of(1, REAL_REGISTERS_18)
REAL_RESPONSE_35 = frame_of(1, REAL_REGISTERS_35)


class FakeSerial:
    """按请求回放固定响应，可模拟丢包、坏 CRC、写失败。"""

    def __init__(self, response=b"", *, drop=False, corrupt=False, fail_write=False):
        self.response = response
        self.drop = drop
        self.corrupt = corrupt
        self.fail_write = fail_write
        self.written = []
        self.closed = False
        self._buffer = b""

    def reset_input_buffer(self):
        self._buffer = b""

    def write(self, data):
        if self.fail_write:
            raise OSError("serial port disappeared")
        self.written.append(bytes(data))
        if not self.drop:
            payload = bytearray(self.response)
            if self.corrupt:
                payload[-1] ^= 0xFF
            self._buffer = bytes(payload)
        return len(data)

    def flush(self):
        pass

    def read(self, size):
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def close(self):
        self.closed = True


def make_driver(fake, **overrides):
    config = dict(BATTERY_CONFIG)
    config.update(overrides)
    return ModbusSerialBatteryDriver.from_mapping(config, serial_factory=lambda **_: fake)


def read_state(driver):
    return asyncio.run(driver.read_state())


class ModbusPrimitiveTest(unittest.TestCase):
    def test_request_frame_is_slave_1_start_0_count_35(self):
        # 与旧程序 ReadHoldingRegistersRequest(1, 0, 35) 完全对应
        self.assertEqual(build_read_request(1, 0, 35)[:6], bytes.fromhex("010300000023"))

    def test_request_crc(self):
        request = build_read_request(1, 0, 35)
        self.assertEqual(crc16(request[:-2]), int.from_bytes(request[-2:], "little"))

    def test_response_parsing_18(self):
        self.assertEqual(parse_read_response(REAL_RESPONSE_18, 1, 18), REAL_REGISTERS_18)

    def test_response_parsing_35(self):
        self.assertEqual(parse_read_response(REAL_RESPONSE_35, 1, 35), REAL_REGISTERS_35)

    def test_crc_mismatch_raises(self):
        broken = bytearray(REAL_RESPONSE_35)
        broken[-1] ^= 0xFF
        with self.assertRaises(Exception) as error:
            parse_read_response(bytes(broken), 1, 35)
        self.assertIn("crc_mismatch", str(error.exception))


class DecodeTest(unittest.TestCase):
    def setUp(self):
        self.registers = BatteryRegisterMap()
        self.values = decode_registers(REAL_REGISTERS_35, self.registers, "capacity")

    def test_scaled_fields_match_现场读数(self):
        self.assertEqual(self.values["voltage_v"], 47.31)
        self.assertEqual(self.values["current_a"], 1.3)
        self.assertEqual(self.values["remaining_capacity_ah"], 14.71)
        self.assertEqual(self.values["total_capacity_ah"], 36.0)
        self.assertEqual(self.values["cell_voltage_max_v"], 3.644)
        self.assertEqual(self.values["cell_voltage_min_v"], 3.638)

    def test_percent_is_capacity_ratio(self):
        self.assertEqual(self.values["battery_percent"], 40.9)
        self.assertEqual(self.values["soc_register_percent"], 41)

    def test_negative_current_is_decoded_as_int16(self):
        # 放电时电流寄存器返回 0xFFAC：uint16 会读成 65452（=6545.2 A），
        # int16 才是 -84（=-8.4 A）。
        discharge = list(REAL_REGISTERS_35)
        discharge[1] = 0xFFAC
        values = decode_registers(discharge, self.registers, "capacity")
        self.assertEqual(values["current_a"], -8.4)
        # 同一帧里其它字段不能被符号扩展带偏
        self.assertEqual(values["voltage_v"], 47.31)
        self.assertEqual(values["battery_percent"], 40.9)

    def test_percent_from_register_when_configured(self):
        values = decode_registers(REAL_REGISTERS_35, self.registers, "register")
        self.assertEqual(values["battery_percent"], 41.0)

    def test_cell_voltages_average_ignores_dead_channels(self):
        # 窗口里混入 0（未接电芯）时必须被过滤，否则均值会被拉低
        self.assertEqual(self.values["cell_voltage_avg_v"], 3.64)
        self.assertEqual(self.values["cell_count_active"], 10)

    def test_percent_clamped_and_falls_back_to_register(self):
        broken = list(REAL_REGISTERS_35)
        broken[2] = 0          # 当前容量为 0
        broken[3] = 0          # 总容量为 0，容量比不可用
        values = decode_registers(broken, self.registers, "capacity")
        self.assertEqual(values["battery_percent"], 41.0)


class DriverTest(unittest.TestCase):
    def test_read_state_ok(self):
        fake = FakeSerial(REAL_RESPONSE_35)
        state = read_state(make_driver(fake))
        self.assertIs(state.health, HealthState.OK)
        self.assertEqual(state.quality, "valid")
        self.assertEqual(state.values["battery_percent"], 40.9)
        self.assertEqual(state.source, "/dev/ttyS5@9600")
        self.assertEqual(fake.written, [build_read_request(1, 0, 35)])

    def test_timeout_is_unavailable_on_first_read(self):
        state = read_state(make_driver(FakeSerial(b"", drop=True)))
        self.assertIs(state.health, HealthState.UNAVAILABLE)
        self.assertIn("short_response", state.raw_fault_code or "")

    def test_bad_crc_degrades_but_keeps_last_values(self):
        fake = FakeSerial(REAL_RESPONSE_35)
        driver = make_driver(fake)
        read_state(driver)
        fake.corrupt = True
        state = read_state(driver)
        self.assertIs(state.health, HealthState.DEGRADED)
        self.assertIn("crc_mismatch", state.raw_fault_code or "")
        # 保留上次读数，页面显示 stale 而不是跳成 “—”
        self.assertEqual(state.values["battery_percent"], 40.9)

    def test_write_failure_drops_port_for_reconnect(self):
        fake = FakeSerial(REAL_RESPONSE_35, fail_write=True)
        driver = make_driver(fake)
        state = read_state(driver)
        self.assertIs(state.health, HealthState.UNAVAILABLE)
        self.assertIn("串口写入失败", state.raw_fault_code or "")
        self.assertIsNone(driver._port)

    def test_invalid_config_does_not_crash(self):
        driver = make_driver(FakeSerial(), count=6)   # 寄存器 21 超出 count
        state = read_state(driver)
        self.assertIs(state.health, HealthState.UNAVAILABLE)
        self.assertIn("config_invalid", state.raw_fault_code or "")

    def test_battery_is_read_only(self):
        from datetime import datetime, timedelta, timezone
        from app.robot_core.contracts import DeviceCommand

        driver = make_driver(FakeSerial(REAL_RESPONSE_35))
        now = datetime.now(timezone.utc)
        command = DeviceCommand(
            target="battery", action="battery.reset", payload={}, issuer="ui", lease_id="l",
            issued_at=now.isoformat(), expires_at=(now + timedelta(seconds=5)).isoformat(),
            idempotency_key="k", trace_id="t",
        )
        result = asyncio.run(driver.execute(command))
        self.assertEqual(result.state, "rejected")
        self.assertEqual(result.reject_reason, "battery_read_only")


class SafetyGateTest(unittest.TestCase):
    """低电量门禁：只拦运动/升降的启动，不拦停止，读不到电量时不阻拦。"""

    class Store:
        def save_lease(self, *args, **kwargs):
            pass

        def renew_lease(self, *args, **kwargs):
            pass

        def release_lease(self, *args, **kwargs):
            pass

    def build(self, percent):
        from app.robot_core.control.leases import LeaseManager
        from app.robot_core.control.safety import SafetySupervisor

        leases = LeaseManager(self.Store(), 3000)
        lease = leases.acquire("ui", "manual_local")
        safety = SafetySupervisor(leases, 2.0, 15.0)
        safety.battery_percent_provider = lambda: percent
        return safety, lease

    @staticmethod
    def command(target, action, lease_id, payload=None):
        from datetime import datetime, timedelta, timezone
        from app.robot_core.contracts import DeviceCommand

        now = datetime.now(timezone.utc)
        return DeviceCommand(
            target=target, action=action, payload=payload or {}, issuer="ui", lease_id=lease_id,
            issued_at=now.isoformat(), expires_at=(now + timedelta(seconds=5)).isoformat(),
            idempotency_key="k", trace_id="t",
        )

    def test_motion_blocked_below_threshold(self):
        from app.robot_core.control.safety import CommandRejected

        safety, lease = self.build(9.0)
        with self.assertRaises(CommandRejected) as error:
            safety.validate(self.command("motion", "move", lease.lease_id, {"distance_m": 0.2}))
        self.assertEqual(str(error.exception), "battery_critical")

    def test_lift_blocked_below_threshold(self):
        from app.robot_core.control.safety import CommandRejected

        safety, lease = self.build(9.0)
        with self.assertRaises(CommandRejected) as error:
            safety.validate(self.command("lift", "lift.up", lease.lease_id))
        self.assertEqual(str(error.exception), "battery_critical")

    def test_stop_always_allowed_when_battery_low(self):
        safety, lease = self.build(3.0)
        safety.validate(self.command("motion", "stop", lease.lease_id))
        safety.validate(self.command("lift", "stop", lease.lease_id))

    def test_camera_not_affected(self):
        safety, lease = self.build(3.0)
        safety.validate(self.command("camera", "camera.ptz", lease.lease_id,
                                     {"direct": "up", "speed": 0.3}))

    def test_unknown_battery_does_not_block(self):
        safety, lease = self.build(None)
        safety.validate(self.command("motion", "move", lease.lease_id, {"distance_m": 0.2}))


class HardwareConfigTest(unittest.TestCase):
    def test_hardware_yaml_battery_section_parses(self):
        values = yaml.safe_load((PROJECT_ROOT / "configs" / "hardware.yaml").read_text(encoding="utf-8"))
        battery = values["devices"]["battery"]
        self.assertEqual(battery["transport"], "serial")
        settings = BatterySerialSettings.from_mapping(battery)
        self.assertEqual(settings.port, "/dev/ttyS5")
        self.assertEqual(settings.slave, 1)
        self.assertEqual(settings.count, 35)
        self.assertEqual(settings.registers.total_capacity, 3)

    def test_minimum_battery_percent_configured(self):
        values = yaml.safe_load((PROJECT_ROOT / "configs" / "hardware.yaml").read_text(encoding="utf-8"))
        self.assertGreater(values["safety"]["minimum_battery_percent"], 0)


if __name__ == "__main__":
    unittest.main()
