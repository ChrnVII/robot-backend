"""串口环境驱动：用真机抓到的字节做回归向量。

所有请求/响应都来自 2026-10-23 在机器人上 strace 抓到的真实帧（CRC 已逐帧校验），
所以这组用例同时守住两件事：帧格式不错、寄存器表不错。
"""

import asyncio
import unittest
from pathlib import Path

import pytest
import yaml

from app.robot_core.contracts import HealthState
from app.robot_core.devices.environment_serial import (
    EnvironmentSerialSettings,
    ModbusSerialEnvironmentDriver,
    SensorChannel,
    build_read_request,
    crc16,
    parse_read_response,
)

pytestmark = pytest.mark.no_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: 从硬件配置里取的现场参数（与 configs/hardware.yaml 的 devices.environment 保持同步）。
ENVIRONMENT_CONFIG = {
    "transport": "serial",
    "port": "/dev/ttyS0",
    "baudrate": 9600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout_seconds": 0.5,
    "frame_gap_ms": 0,
    "max_retries": 1,
    "sensors": [
        {"slave": 12, "label": "温湿度", "register": 0x0000, "count": 2,
         "fields": [{"offset": 0, "name": "temperature_c", "scale": 0.1},
                    {"offset": 1, "name": "humidity_percent", "scale": 0.1}]},
        {"slave": 2, "label": "光照", "register": 0x0000, "count": 1,
         "fields": [{"offset": 0, "name": "illuminance_lux", "scale": 1}]},
        {"slave": 3, "label": "风速", "register": 0x0063, "count": 1,
         "fields": [{"offset": 0, "name": "wind_speed_mps", "scale": 0.1}]},
    ],
}

#: 真机抓到的请求帧 → 响应帧（strace，fd=247）
REAL_FRAMES = {
    bytes.fromhex("0c0300000002c516"): bytes.fromhex("0c030400f3022cd67d"),   # 24.3℃ / 55.6%RH
    bytes.fromhex("0203000000018439"): bytes.fromhex("02030200efbdc8"),       # 239 lux
    bytes.fromhex("03030063000175f6"): bytes.fromhex("0303020000c184"),       # 0 → 0.0 m/s
}


class FakeSerial:
    """按请求帧回放响应，可按从站模拟丢包/坏 CRC。"""

    def __init__(self, responses, *, drop=(), corrupt=(), fail_write=False):
        self.responses = responses
        self.drop = set(drop)
        self.corrupt = set(corrupt)
        self.fail_write = fail_write
        self.written = []
        self.closed = False
        self._buffer = b""
        self.kwargs = {}

    def reset_input_buffer(self):
        self._buffer = b""

    def write(self, data):
        if self.fail_write:
            raise OSError("serial port disappeared")
        frame = bytes(data)
        self.written.append(frame)
        slave = frame[0]
        if slave in self.drop:
            self._buffer = b""
            return len(frame)
        response = bytearray(self.responses[frame])
        if slave in self.corrupt:
            response[-1] ^= 0xFF
        self._buffer = bytes(response)
        return len(frame)

    def flush(self):
        pass

    def read(self, size):
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def close(self):
        self.closed = True


def make_driver(fake, **overrides):
    config = dict(ENVIRONMENT_CONFIG)
    config.update(overrides)
    return ModbusSerialEnvironmentDriver.from_mapping(config, serial_factory=lambda **_: fake)


def read_state(driver):
    return asyncio.run(driver.read_state())


class ModbusPrimitiveTest(unittest.TestCase):
    def test_request_frames_match_captured_traffic(self):
        for frame in REAL_FRAMES:
            self.assertEqual(build_read_request(frame[0], frame[1],
                                                int.from_bytes(frame[2:4], "big"),
                                                int.from_bytes(frame[4:6], "big")), frame)

    def test_response_parsing(self):
        self.assertEqual(parse_read_response(REAL_FRAMES[bytes.fromhex("0c0300000002c516")], 12, 3, 2), [243, 556])
        self.assertEqual(parse_read_response(REAL_FRAMES[bytes.fromhex("03030063000175f6")], 3, 3, 1), [0])

    def test_crc_mismatch_is_rejected(self):
        frame = bytearray(REAL_FRAMES[bytes.fromhex("0203000000018439")])
        frame[-1] ^= 0xFF
        with self.assertRaisesRegex(Exception, "crc_mismatch"):
            parse_read_response(bytes(frame), 2, 3, 1)

    def test_modbus_exception_is_reported(self):
        body = bytes([0x02, 0x83, 0x02])
        frame = body + bytes([crc16(body) & 0xFF, (crc16(body) >> 8) & 0xFF])
        with self.assertRaisesRegex(Exception, "modbus_exception_2"):
            parse_read_response(frame, 2, 3, 1)


class ModbusSerialDriverTest(unittest.TestCase):
    def test_reads_all_three_channels_like_the_legacy_program(self):
        fake = FakeSerial(REAL_FRAMES)
        state = read_state(make_driver(fake))
        self.assertEqual(state.health, HealthState.OK)
        self.assertEqual(state.values, {
            "temperature_c": 24.3,
            "humidity_percent": 55.6,
            "illuminance_lux": 239.0,
            "wind_speed_mps": 0.0,
        })
        # 请求必须与真机抓到的三帧逐字节一致（顺序也一致）
        self.assertEqual(fake.written, list(REAL_FRAMES))
        self.assertEqual(state.source, "/dev/ttyS0@9600")

    def test_crc_error_on_one_channel_degrades_but_keeps_the_rest(self):
        fake = FakeSerial(REAL_FRAMES, corrupt=(3,))
        state = read_state(make_driver(fake))
        self.assertEqual(state.health, HealthState.DEGRADED)
        self.assertEqual(state.quality, "degraded")
        self.assertIn("temperature_c", state.values)
        self.assertNotIn("wind_speed_mps", state.values)
        self.assertIn("slave3", state.raw_fault_code)

    def test_silent_slave_degrades_without_losing_others(self):
        fake = FakeSerial(REAL_FRAMES, drop=(2,))
        state = read_state(make_driver(fake))
        self.assertEqual(state.health, HealthState.DEGRADED)
        self.assertNotIn("illuminance_lux", state.values)
        self.assertIn("temperature_c", state.values)

    def test_all_channels_down_keeps_last_values(self):
        fake = FakeSerial(REAL_FRAMES)
        driver = make_driver(fake)
        self.assertEqual(read_state(driver).health, HealthState.OK)

        busted = FakeSerial(REAL_FRAMES, drop=(12, 2, 3))
        driver._serial_factory = lambda **_: busted
        driver._drop_port("test")
        state = read_state(driver)
        self.assertEqual(state.health, HealthState.UNAVAILABLE)
        # 保留上次读数：宁可让页面显示旧值 + invalid，也不要跳成空白
        self.assertEqual(state.values["temperature_c"], 24.3)
        self.assertEqual(state.quality, "invalid")

    def test_port_failure_drops_handle_so_next_poll_can_reopen(self):
        fake = FakeSerial(REAL_FRAMES, fail_write=True)
        driver = make_driver(fake)
        self.assertEqual(read_state(driver).health, HealthState.UNAVAILABLE)
        self.assertIsNone(driver._port)
        # 端口恢复后（同一对象继续轮询）应自愈，无需重启进程
        fake.fail_write = False
        self.assertEqual(read_state(driver).health, HealthState.OK)

    def test_open_failure_is_not_fatal(self):
        def broken(**kwargs):
            raise OSError("device is busy")

        driver = ModbusSerialEnvironmentDriver(
            EnvironmentSerialSettings.from_mapping(ENVIRONMENT_CONFIG), serial_factory=broken,
        )
        asyncio.run(driver.open())            # 不抛异常
        state = read_state(driver)            # 每轮都重试打开
        self.assertEqual(state.health, HealthState.UNAVAILABLE)
        self.assertIn("打不开", state.raw_fault_code)

    def test_invalid_config_reports_unavailable_instead_of_crashing(self):
        driver = ModbusSerialEnvironmentDriver.from_mapping(
            {"port": "/dev/ttyS0", "sensors": [{"slave": 0, "register": 0, "count": 1,
                                                "fields": [{"offset": 0, "name": "temperature_c"}]}]},
        )
        state = read_state(driver)
        self.assertEqual(state.health, HealthState.UNAVAILABLE)
        self.assertTrue(state.raw_fault_code.startswith("config_invalid"))


class EnvironmentSerialConfigTest(unittest.TestCase):
    def test_each_invalid_field_is_rejected(self):
        cases = [
            ({"sensors": []}, "at least one channel"),
            ({"baudrate": 0}, "baudrate"),
            ({"parity": "X"}, "parity"),
            ({"stopbits": 3}, "stopbits"),
            ({"timeout_seconds": 0}, "timeout_seconds"),
            ({"sensors": [{"slave": 12, "register": 0, "count": 1, "fields": []}]}, "at least one field"),
            ({"sensors": [{"slave": 12, "register": 0, "count": 1,
                           "fields": [{"offset": 1, "name": "temperature_c"}]}]}, "out of range"),
            ({"sensors": [{"slave": 12, "register": 0, "count": 1,
                           "fields": [{"offset": 0, "name": "unknown_field"}]}]}, "unknown field"),
            ({"sensors": [{"slave": 12, "register": 0, "count": 1,
                           "fields": [{"offset": 0, "name": "temperature_c"}]},
                          {"slave": 12, "register": 1, "count": 1,
                           "fields": [{"offset": 0, "name": "humidity_percent"}]}]}, "duplicate sensor"),
        ]
        for overrides, expected in cases:
            config = dict(ENVIRONMENT_CONFIG)
            config.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, expected):
                    EnvironmentSerialSettings.from_mapping(config)

    def test_shipped_hardware_config_parses(self):
        """守住 configs/hardware.yaml：字段写错会在单测而不是现场暴露。"""
        values = yaml.safe_load((PROJECT_ROOT / "configs" / "hardware.yaml").read_text(encoding="utf-8"))
        environment = values["devices"]["environment"]
        self.assertEqual(environment["transport"], "serial")
        settings = EnvironmentSerialSettings.from_mapping(environment)
        self.assertEqual(
            [(channel.slave, channel.register, channel.count) for channel in settings.channels],
            [(12, 0x0000, 2), (2, 0x0000, 1), (3, 0x0063, 1)],
        )
        names = [field.name for channel in settings.channels for field in channel.fields]
        self.assertEqual(
            names,
            ["temperature_c", "humidity_percent", "illuminance_lux", "wind_speed_mps"],
        )


if __name__ == "__main__":
    unittest.main()
