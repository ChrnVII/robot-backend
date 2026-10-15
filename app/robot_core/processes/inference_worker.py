from __future__ import annotations

from dataclasses import asdict

from app.robot_core.common.config import load_config
from app.robot_core.contracts import InferenceRequest
from app.robot_core.inference import InferenceRuntime, TestModelAdapter, build_configured_adapters
from app.ipc import JsonSocketServer

from .common import check_output, configure_logging, parser, run


def main() -> None:
    args = parser("inference-worker").parse_args()
    config = load_config(args.config)
    configure_logging(config, "inference-worker")
    adapters = build_configured_adapters(config)
    runtime = InferenceRuntime(adapters)
    runtime.start()
    if args.check:
        check_output("inference-worker", config, {
            "capabilities": list(runtime.adapters),
            "productionModels": sum(1 for value in runtime.adapters.values() if not isinstance(value, TestModelAdapter)),
        })
        return

    async def handler(request):
        if request.get("operation") == "health":
            return {
                "service": "inference-worker", "state": "ok",
                "models": [
                    {"capability": capability, "model_id": adapter.model_id,
                     "model_version": adapter.model_version,
                     "model_sha256": getattr(adapter, "model_sha256", None),
                     "adapter_version": getattr(adapter, "adapter_version", "1"),
                     "result_schema": adapter.result_schema, "health": adapter.health()}
                    for capability, adapter in runtime.adapters.items()
                ],
            }
        if request.get("operation") != "predict":
            raise ValueError("unsupported operation")
        prediction = InferenceRequest.from_dict(request)
        return asdict(runtime.predict(prediction))

    try:
        run(JsonSocketServer(config.path("ipc", "inference_worker"), handler).serve_forever)
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
