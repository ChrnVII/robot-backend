# Device adapters

Real adapters exist for `battery` (Modbus-RTU over `/dev/ttyS5`, `battery_serial.py`), `environment` (Modbus-RTU over `/dev/ttyS0`, `environment_serial.py`) and `camera` (Hikvision ISAPI, `network_camera.py`). Motion, track, RFID, lift, light and audio still fall back to `SimulatedDeviceDriver`, which reports motion placeholders only — it deliberately provides **no** battery reading, so pages show `—` instead of a fabricated percentage. Each physical channel must have exactly one process owner.

