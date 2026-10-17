from __future__ import annotations

import asyncio

from app.contracts import InspectionRequestWire, Op
from app.robot_core.common.config import load_config
from app.robot_core.inspection import InspectionService
from app.ipc import JsonSocketServer
from app.robot_core.storage import PlatformStore
from app.robot_core.storage.runs import InspectionRunStore
from app.robot_core.sensing.camera_pool import CameraPool
from app.video.inspection_preview import InspectionPreview
from app.robot_core.inference import InferenceRuntime, build_configured_adapters

from .common import check_output, configure_logging, parser, run
from .inspection_engine import run as run_inspection
from .sensor_worker import build_service, handle_request


class PerceptionWorker:
    """Own capture, one lazily loaded model runtime, task execution and evidence."""

    def __init__(self, config, *, initialize: bool = True):
        self.config = config
        self.store = PlatformStore(config.database_url_for("perception"), pool_options=config.database_pool_options,
                                   initialize=initialize)
        self.cameras = CameraPool(simulated=config.values["runtime"]["mode"] == "simulated")
        self.runtime = None
        self.preview = InspectionPreview()
        self.sensor = build_service(config, self.store, self.cameras)
        self.inspection = InspectionService(config, self._run, InspectionRunStore(self.store))

    def _run(self, config, **kwargs):
        if kwargs.get("pipeline") == "inference" and self.runtime is None:
            runtime = InferenceRuntime(build_configured_adapters(config))
            try:
                runtime.start()
            except BaseException:
                runtime.stop()
                raise
            self.runtime = runtime
        return run_inspection(config, **kwargs, inference_runtime=self.runtime, preview=self.preview)

    def close(self):
        self.inspection.close()
        if self.runtime is not None:
            self.runtime.stop()
        self.cameras.close()
        self.store.close()


async def handle_inspection_request(service: InspectionService, request: dict) -> dict:
    operation = str(request.get("operation", ""))
    if operation == Op.HEALTH:
        return service.health()
    request = InspectionRequestWire.from_mapping(request).to_payload()
    if operation == Op.TASK_START:
        return await asyncio.to_thread(service.start, request)
    if operation == Op.TASK_STOP:
        return await asyncio.to_thread(service.stop, str(request["task_run_id"]))
    if operation == Op.TASK_STATUS:
        task_run_id = request.get("task_run_id")
        return await asyncio.to_thread(service.status, str(task_run_id) if task_run_id else None)
    if operation == Op.TASK_DETECTIONS:
        return await asyncio.to_thread(service.detections,
            str(request["task_run_id"]),
            offset=int(request.get("offset", 0)),
            limit=int(request.get("limit", 50)),
        )
    raise ValueError("unsupported operation")


def main() -> None:
    args = parser("inspection-worker").parse_args()
    config = load_config(args.config)
    configure_logging(config, "inspection-worker")
    worker = PerceptionWorker(config, initialize=not args.check)
    service = worker.inspection
    if args.check:
        from app.migrations import check_database
        check_database(worker.store.database_url)
        check_output("inspection-worker", config, {
            "socket": str(config.path("ipc", "inspection_worker")),
            "outputRoot": str(service.output_root),
        })
        worker.close()
        return

    async def handler(request):
        return await handle_inspection_request(service, request)

    async def serve():
        inspection_server = JsonSocketServer(config.path("ipc", "inspection_worker"), handler)
        sensor_server = JsonSocketServer(config.path("ipc", "sensor_worker"),
                                        lambda request: handle_request(worker.sensor, request))
        try:
            # Reserve both sockets before recovery; accept requests only after
            # recovery finishes, so a reconnect cannot race task reconciliation.
            await inspection_server.start(start_serving=False)
            await sensor_server.start(start_serving=False)
            await worker.preview.start(config.path("ipc", "inspection_worker"))
            await asyncio.to_thread(service.recover)
            await asyncio.gather(inspection_server.serve_forever(), sensor_server.serve_forever())
        finally:
            await inspection_server.close()
            await sensor_server.close()
            await asyncio.to_thread(worker.close)
            await worker.preview.close()

    run(serve)


if __name__ == "__main__":
    main()
