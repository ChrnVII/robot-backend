"""实时检测线程（可选）：最新帧 → YOLO → WebSocket JSON 叠加框。

设计取舍：**只做实时叠加，不落库**。平台的检测记录已由 inference-worker 落到
PostgreSQL（inference_runs / inference_detections），这里再写一份文件会出现两套账。
需要留档时保持既有巡检链路不变。

坐标一律归一化（0~1），前端按显示尺寸缩放即可，与分辨率无关。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.video.codec import Timeline
from app.video.pipeline import CameraBroadcaster, FrameSlot

logger = logging.getLogger(__name__)


class DetectionThread(threading.Thread):
    """从最新帧槽位取帧做推理，结果经广播器推给浏览器。"""

    def __init__(
        self,
        slot: FrameSlot,
        broadcaster: CameraBroadcaster,
        *,
        stop_event: threading.Event,
        model_path: str,
        imgsz: int = 640,
        conf: float = 0.35,
        iou: float = 0.5,
        every: int = 1,
        timeline: Timeline | None = None,
    ) -> None:
        super().__init__(name="camera-detector", daemon=True)
        self.slot = slot
        self.broadcaster = broadcaster
        self._stop = stop_event
        self.model_path = model_path
        self.imgsz = max(64, int(imgsz))
        self.conf = float(conf)
        self.iou = float(iou)
        self.every = max(1, int(every))
        self.timeline = timeline or Timeline()

        self.fatal: str = ""
        self.model: Any = None
        self.index = 0
        self.inferred = 0
        self.infer_ms = 0.0

    def _load(self) -> bool:
        try:
            from ultralytics import YOLO  # noqa: PLC0415 - 可选依赖，惰性导入
        except Exception as exc:  # noqa: BLE001 - 装不上或装坏了都不该拖垮进程
            self.fatal = "ultralytics_missing"
            logger.error("未安装 ultralytics（%s），实时检测已禁用；画面链路不受影响", exc)
            return False
        if not self.model_path:
            self.fatal = "model_not_configured"
            logger.error("未配置检测模型（ROBOT_CAMERA_STREAM_MODEL），实时检测已禁用")
            return False
        if not Path(self.model_path).exists():
            self.fatal = "model_missing"
            logger.error("找不到模型 %s，实时检测已禁用", self.model_path)
            return False
        try:
            self.model = YOLO(self.model_path)
        except Exception:  # noqa: BLE001
            self.fatal = "model_load_failed"
            logger.exception("加载模型 %s 失败，实时检测已禁用", self.model_path)
            return False
        logger.info("摄像头预览检测模型已加载：%s", self.model_path)
        return True

    def run(self) -> None:
        if not self._load():
            return
        while not self._stop.is_set():
            item = self.slot.take(timeout=0.5)
            if item is None:
                continue
            frame, pts_ms, src_pts_ms = item
            self.index += 1
            if self.every > 1 and self.index % self.every:
                continue
            try:
                image = frame.to_ndarray(format="bgr24")
            except Exception:  # noqa: BLE001 - 个别帧转换失败跳过即可
                logger.debug("帧转 ndarray 失败", exc_info=True)
                continue

            started = time.perf_counter()
            record = self._infer(image, pts_ms, src_pts_ms)
            self.infer_ms = (time.perf_counter() - started) * 1000.0
            self.inferred += 1
            self.broadcaster.publish({"t": "det", **record})
        logger.info("摄像头预览检测线程退出（推理 %d 次）", self.inferred)

    def _infer(self, image: Any, pts_ms: int, src_pts_ms: int | None) -> dict[str, Any]:
        height, width = int(image.shape[0]), int(image.shape[1])
        boxes: list[dict[str, Any]] = []
        try:
            results = self.model.predict(
                image, imgsz=self.imgsz, conf=self.conf, iou=self.iou, verbose=False
            )
            if results:
                result = results[0]
                names = getattr(result, "names", {}) or {}
                xyxy = result.boxes.xyxy.cpu().numpy()
                confidence = result.boxes.conf.cpu().numpy()
                classes = result.boxes.cls.cpu().numpy().astype(int)
                for index in range(len(classes)):
                    x1, y1, x2, y2 = (float(value) for value in xyxy[index][:4])
                    class_id = int(classes[index])
                    boxes.append(
                        {
                            "c": class_id,
                            "n": str(names.get(class_id, class_id)),
                            "p": round(float(confidence[index]), 4),
                            "x": [
                                round(x1 / width, 5), round(y1 / height, 5),
                                round(x2 / width, 5), round(y2 / height, 5),
                            ],
                        }
                    )
        except Exception:  # noqa: BLE001 - 单帧推理失败不应该中断线程
            logger.exception("摄像头预览推理失败")

        return {
            # pts：到达时钟（与 WebSocket 帧头同轴，前端按此做最近邻匹配）
            "pts": pts_ms,
            # src_pts：摄像头原始时间戳（保留给后续可能的留档对帧）
            "src_pts": src_pts_ms,
            "i": self.index,
            "w": width,
            "h": height,
            # age：这一帧到达 → 本条记录产出（含推理）耗时
            "age": max(0, self.timeline.now_ms() - pts_ms),
            "boxes": boxes,
        }

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "fatal": self.fatal,
            "inferred": self.inferred,
            "inferMs": round(self.infer_ms, 2),
        }
