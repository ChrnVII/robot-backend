from __future__ import annotations

from app.robot_core.common.config import load_config
from app.robot_core.inspection import InspectionService
from app.ipc import JsonSocketServer

from .common import check_output, configure_logging, parser, run
from .realtime_demo import run as run_inspection


def main() -> None:
    args = parser("inspection-worker").parse_args()
    config = load_config(args.config)
    configure_logging(config, "inspection-worker")
    service = InspectionService(config, run_inspection)
    if args.check:
        check_output("inspection-worker", config, {
            "socket": str(config.path("ipc", "inspection_worker")),
            "outputRoot": str(service.output_root),
        })
        return

    async def handler(request):
        operation = str(request.get("operation", ""))
        if operation == "health":
            return service.health()
        if operation == "latest_frame":
            return service.latest_frame(
                str(request["task_run_id"]),
                str(request["camera_id"]) if request.get("camera_id") else None,
            )
        if operation == "latest_frames":
            return service.latest_frames(str(request["task_run_id"]))
        if operation == "start":
            return service.start(request)
        if operation == "stop":
            return service.stop(str(request["task_run_id"]))
        if operation == "status":
            task_run_id = request.get("task_run_id")
            return service.status(str(task_run_id) if task_run_id else None)
        if operation == "detections":
            return service.detections(
                str(request["task_run_id"]),
                offset=int(request.get("offset", 0)),
                limit=int(request.get("limit", 50)),
            )
        if operation == "resolve_image":
            return service.resolve_image(str(request["task_run_id"]), str(request["image_id"]))
        raise ValueError("unsupported operation")

    run(JsonSocketServer(config.path("ipc", "inspection_worker"), handler).serve_forever)


if __name__ == "__main__":
    main()
