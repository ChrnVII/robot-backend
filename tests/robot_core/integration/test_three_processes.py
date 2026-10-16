"""Linux smoke: real API, two runtime processes, UDS, WebSocket and SIGTERM."""

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import yaml

from app.migrations import upgrade_database

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires Unix domain sockets and SIGTERM")


def test_three_process_http_video_capture_emergency_and_shutdown():
    from websockets.sync.client import connect

    root = Path(__file__).parents[3]
    dsn = os.environ["ROBOT_TEST_DATABASE_URL"]
    upgrade_database(dsn)
    with tempfile.TemporaryDirectory(prefix="robot-smoke-") as temporary:
        directory = Path(temporary)
        config = yaml.safe_load((root / "configs/dev.yaml").read_text())
        config["storage"].update(database_url=dsn, database=dsn, media_root=str(directory / "media"))
        config["inspection"].update(output_root=str(directory / "inspection"), capture_fps=10)
        config["devices"]["camera"].update(width=32, height=24)
        for name in config["ipc"]:
            config["ipc"][name] = str(directory / (name + ".sock"))
        config_path = directory / "config.yaml"
        config_path.write_text(yaml.safe_dump(config))
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ROBOT_DATABASE_URL": dsn,
               "ROBOT_CORE_DATABASE_URL": dsn, "ROBOT_PERCEPTION_DATABASE_URL": dsn,
               "ROBOT_AUTO_MIGRATE": "false", "ROBOT_AUTH_ENABLED": "false",
               "ROBOT_ROBOT_ID": "robot-test", "ROBOT_MODE": "simulated",
               "ROBOT_DEFAULT_ADMIN_USERNAME": "admin", "ROBOT_DEFAULT_ADMIN_PASSWORD": "admin123",
               "ROBOT_ROBOT_CORE_SOCKET": config["ipc"]["robot_core"],
               "ROBOT_SENSOR_WORKER_SOCKET": config["ipc"]["sensor_worker"],
               "ROBOT_INSPECTION_WORKER_SOCKET": config["ipc"]["inspection_worker"]}
        processes, logs = [], []

        def launch(name, arguments):
            log = (directory / (name + ".log")).open("w+")
            logs.append(log)
            process = subprocess.Popen([sys.executable, "-B", "-m", *arguments], env=env, cwd=root,
                                       stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            return process

        def wait_for(predicate, label):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return
                except (OSError, httpx.HTTPError):
                    pass
                assert all(p.poll() is None for p in processes), label
                time.sleep(0.05)
            pytest.fail(label)

        try:
            launch("core", ["app.robot_core.processes.robot_core", "--config", str(config_path)])
            wait_for(lambda: Path(config["ipc"]["robot_core"]).exists(), "core startup")
            launch("perception", ["app.robot_core.processes.inspection_worker", "--config", str(config_path)])
            wait_for(lambda: Path(config["ipc"]["sensor_worker"]).exists(), "perception startup")
            launch("api", ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)])
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                wait_for(lambda: client.get("/healthz").status_code == 200, "API startup")
                capture = client.post("/api/v1/media/captures", json={
                    "taskRunId": "smoke", "waypointId": "point", "deviceId": "camera", "mediaType": "image"})
                assert capture.status_code == 200, capture.text
                stream = client.post("/api/v1/video/streams", json={"streamId": "smoke", "cameraIds": ["cam01"]})
                assert stream.status_code == 200, stream.text
                with connect(f"ws://127.0.0.1:{port}/ws/video/smoke?cameraId=cam01", open_timeout=5) as websocket:
                    for _ in range(20):
                        message = json.loads(websocket.recv(timeout=5))
                        if message["data"]["frame"]["available"]:
                            break
                    assert set(message) == {"type", "data", "timestamp"}
                    assert message["type"] == "video"
                    frame = message["data"]["frame"]
                    assert frame["cameraId"] == "cam01"
                    assert base64.b64decode(frame["jpegBase64"]).startswith(b"\xff\xd8")
                # A duplicate process must not recover/fail this live run or replace its sockets.
                duplicate = subprocess.run([sys.executable, "-B", "-m", "app.robot_core.processes.inspection_worker",
                                            "--config", str(config_path)], env=env, cwd=root,
                                           capture_output=True, timeout=10)
                assert duplicate.returncode != 0
                assert client.get("/api/v1/video/streams/smoke").json()["data"]["state"] == "running"
                stop = client.post("/api/v1/emergency-stop", json={"traceId": "smoke", "reason": "test"})
                assert stop.status_code == 200, stop.text
                assert stop.json()["data"]["state"] == "emergency"
                assert client.delete("/api/v1/video/streams/smoke").status_code == 200
        except BaseException:
            for log in logs:
                log.flush()
                log.seek(0)
                print(log.read())
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for log in logs:
                log.close()
        assert all(process.returncode == 0 for process in processes[:2]), {
            "exit_codes": [p.returncode for p in processes],
            "logs": [p.read_text() for p in directory.glob("*.log")],
        }
        # Uvicorn re-raises SIGTERM after its graceful application shutdown.
        assert processes[2].returncode in {0, -signal.SIGTERM}
        assert "Application shutdown complete" in (directory / "api.log").read_text()
        assert not any(Path(config["ipc"][name]).exists() for name in ("robot_core", "inspection_worker", "sensor_worker"))
