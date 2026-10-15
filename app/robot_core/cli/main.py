import argparse
import asyncio
import json

from app.robot_core.common.config import load_config
from app.ipc import JsonSocketClient


def main() -> None:
    parser = argparse.ArgumentParser(prog="robotctl")
    parser.add_argument("--config")
    parser.add_argument("service", choices=("robot_core", "sensor_worker", "inference_worker", "sync_agent"))
    parser.add_argument("operation", choices=("health",))
    args = parser.parse_args()
    config = load_config(args.config)
    result = asyncio.run(JsonSocketClient(config.path("ipc", args.service)).call({"operation": args.operation}))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

