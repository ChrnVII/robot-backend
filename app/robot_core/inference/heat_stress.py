from __future__ import annotations

import logging
import time
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.robot_core.contracts import InferenceRequest, InferenceResponse

from .package import ModelPackage

logger = logging.getLogger(__name__)

#: 单次批量推理最多合并多少帧；超过部分由调用方再排一批。
MAX_BATCH_SIZE = 8


class HeatStressAdapter:
    """Versioned adapter for the heat-stress detection model."""

    capability = "heat_stress_detection"

    def __init__(self, package: ModelPackage | str | Path, *, media_root: str | Path | None = None, device: str = "cpu", imgsz: int = 640, conf: float = 0.25, iou: float = 0.7, cpu_threads: int = 4, expected_sha256: str | None = None) -> None:
        self.package = package if isinstance(package, ModelPackage) else ModelPackage.from_artifact(package, model_id="heat_stress", result_schema="object-detection.v1")
        if expected_sha256 and self.package.sha256.lower() != expected_sha256.lower():
            raise ValueError("heat-stress model SHA-256 mismatch")
        self.model_id = self.package.model_id
        self.model_version = self.package.version
        self.result_schema = self.package.result_schema
        self.model_sha256 = self.package.sha256
        self.adapter_version = self.package.adapter_version
        self.model_path = self.package.artifact
        self.media_root = Path(media_root).expanduser().resolve() if media_root else None
        self.device, self.imgsz, self.conf, self.iou = device, int(imgsz), float(conf), float(iou)
        if isinstance(cpu_threads, bool) or not isinstance(cpu_threads, int) or not 1 <= cpu_threads <= 64:
            raise ValueError('models.heat_stress.cpu_threads must be an integer between 1 and 64')
        available = getattr(os, 'process_cpu_count', os.cpu_count)() or 1
        self.cpu_threads = min(cpu_threads, max(1, available - 2))
        self.model: Any = None
        self.names: Mapping[int, str] = {}

    def load(self) -> None:
        if not self.model_path.is_file():
            raise FileNotFoundError(f"heat-stress model not found: {self.model_path}")
        # 设备自检放在建模型之前：配置成 GPU 而本机没有 CUDA 时直接失败，
        # 而不是静默改用 CPU 运行（现场只能从检测帧率上间接发现）。
        self._require_device()
        try:
            from ultralytics import YOLO
        except ImportError as error:
            raise RuntimeError("install the inference and media dependencies before loading the model") from error
        self.model = YOLO(str(self.model_path))
        if self.device.lower() == 'cpu':
            self._configure_cpu_threads()
            # AutoBackend can reset torch's pool during setup; reapply before prediction.
            self.model.add_callback('on_predict_start', self._configure_cpu_threads)
        logger.info("热应激检测模型就绪：path=%s device=%s imgsz=%s", self.model_path, self.device, self.imgsz)
        self.names = {int(key): str(value) for key, value in dict(getattr(self.model, "names", {})).items()}

    def warmup(self) -> None:
        if self.model is None:
            raise RuntimeError("heat-stress model is not loaded")
        try:
            import numpy as np
        except ImportError as error:
            raise RuntimeError("numpy is required for model warmup") from error
        self.model.predict(source=np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8), imgsz=self.imgsz, conf=self.conf, iou=self.iou, device=self.device, verbose=False)

    def predict(self, request: InferenceRequest) -> InferenceResponse:
        started = time.perf_counter()
        if self.model is None:
            return self._unhealthy(request)
        try:
            result = self._predict_one(self._source_for_request(request))
            return self._ok(request, result, round((time.perf_counter() - started) * 1000, 2))
        except (FileNotFoundError, ValueError) as error:
            return self._invalid(request, error)

    def predict_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResponse]:
        """一次模型调用推理多帧（每路一帧），返回顺序与 ``requests`` 一一对应。

        Ultralytics 的 ``predict(source=[img, ...])`` 会把多张图合成一个 batch，
        CPU 上通常比逐帧快 2-3 倍，GPU 上收益更大。任何一帧的输入非法只影响
        该帧的结果；整批模型调用异常时退化为逐帧，避免一帧坏掉拖垮整批。
        """
        requests = list(requests)
        if not requests:
            return []
        if len(requests) == 1:
            return [self.predict(requests[0])]
        if self.model is None:
            return [self._unhealthy(request) for request in requests]

        started = time.perf_counter()
        results: list[InferenceResponse | None] = [None] * len(requests)
        sources: list[tuple[int, Any]] = []
        for index, request in enumerate(requests):
            try:
                sources.append((index, self._source_for_request(request)))
            except (FileNotFoundError, ValueError) as error:
                results[index] = self._invalid(request, error)
        if not sources:
            return [item for item in results if item is not None]

        images = [image for _, image in sources]
        try:
            predictions = self.model.predict(source=images, imgsz=self.imgsz, conf=self.conf,
                                             iou=self.iou, device=self.device, verbose=False)
        except Exception:  # noqa: BLE001 - 批量失败不能让整批一起丢
            logger.warning("批量推理失败，退化为逐帧推理", exc_info=True)
            for index, image in sources:
                try:
                    results[index] = self._ok(requests[index], self._predict_one(image),
                                              round((time.perf_counter() - started) * 1000, 2))
                except (FileNotFoundError, ValueError) as error:
                    results[index] = self._invalid(requests[index], error)
            return [item for item in results if item is not None]

        latency = round((time.perf_counter() - started) * 1000, 2)
        for position, (index, _image) in enumerate(sources):
            result = predictions[position] if position < len(predictions) else None
            payload = self._result_dict(result) if result is not None else {"detections": [], "count_by_label": {}}
            results[index] = self._ok(requests[index], payload, latency, batch_size=len(sources))
        return [item for item in results if item is not None]

    # ---------- 响应构造 ---------- #

    def _unhealthy(self, request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(request.request_id, request.capability, "model_unhealthy",
                                 self.model_version, error_stage="load",
                                 suggested_action="load heat-stress model", trace_id=request.trace_id,
                                 model_id=self.model_id, model_sha256=self.model_sha256)

    def _invalid(self, request: InferenceRequest, error: BaseException) -> InferenceResponse:
        return InferenceResponse(request.request_id, request.capability, "invalid_input",
                                 self.model_version, error_stage="input", suggested_action=str(error),
                                 trace_id=request.trace_id, model_id=self.model_id,
                                 model_sha256=self.model_sha256)

    def _ok(self, request: InferenceRequest, result: Mapping[str, Any], latency: float,
            *, batch_size: int | None = None) -> InferenceResponse:
        payload = {**result, "latency_ms": latency}
        if batch_size:
            payload["batch_size"] = batch_size
        return InferenceResponse(request.request_id, request.capability, "ok", self.model_version,
                                 result=payload, evidence_asset_ids=(request.asset_id,),
                                 trace_id=request.trace_id, model_id=self.model_id,
                                 model_sha256=self.model_sha256, latency_ms=latency,
                                 result_schema=self.result_schema)

    def _require_device(self) -> None:
        """校验配置的推理设备在本机真实可用；不满足就报错而不是静默跑 CPU。"""
        device = str(self.device or "cpu").strip().lower()
        if device in {"", "cpu"}:
            return
        try:
            import torch
        except ImportError as error:
            raise RuntimeError(f"models.heat_stress.device={self.device} 需要 torch，当前环境未安装") from error
        if device.startswith("cuda") or device.isdigit():
            if not torch.cuda.is_available():
                raise RuntimeError(
                    f"MODEL_DEVICE=models.heat_stress.device={self.device} 要求 CUDA，"
                    f"但 torch.cuda.is_available()=False（torch={getattr(torch, '__version__', 'unknown')}）。"
                    "请安装与显卡驱动匹配的 CUDA 版 torch，或把 device 设为 cpu。"
                )
            index = int(device.split(":")[-1]) if ":" in device else (int(device) if device.isdigit() else 0)
            available = torch.cuda.device_count()
            if index >= available:
                raise RuntimeError(
                    f"models.heat_stress.device={self.device} 超出本机可见 GPU 数量（{available} 块）"
                )
            logger.info("热应激检测使用 GPU：cuda:%d（可见 %d 块）", index, available)
            return
        if device == "mps" and not getattr(getattr(torch, "backends", None), "mps", None):
            raise RuntimeError("models.heat_stress.device=mps，但当前 torch 未提供 MPS 后端")

    def _configure_cpu_threads(self, predictor=None) -> None:
        import torch
        import cv2
        if torch.get_num_threads() != self.cpu_threads:
            torch.set_num_threads(self.cpu_threads)
        if cv2.getNumThreads() != 1:
            cv2.setNumThreads(1)

    def annotate(self, image: Any, result: Mapping[str, Any]) -> Any:
        return annotate_detections(image, result)

    def health(self) -> str:
        return "ok" if self.model is not None else "unavailable"

    def unload(self) -> None:
        self.model = None

    def _predict_one(self, source: Any) -> dict[str, Any]:
        results = self.model.predict(source=source, imgsz=self.imgsz, conf=self.conf, iou=self.iou, device=self.device, verbose=False)
        return self._result_dict(results[0]) if results else {"detections": [], "count_by_label": {}}

    def _result_dict(self, result: Any) -> dict[str, Any]:
        detections: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        boxes = getattr(result, "boxes", None)
        if boxes is not None:
            for class_id, score, bbox in zip(self._tolist(getattr(boxes, "cls", None)), self._tolist(getattr(boxes, "conf", None)), self._tolist(getattr(boxes, "xyxy", None))):
                class_id = int(class_id)
                label = self.names.get(class_id, f"class_{class_id}")
                counts[label] = counts.get(label, 0) + 1
                detections.append({"class_id": class_id, "label": label, "confidence": round(float(score), 6), "bbox_xyxy": [round(float(value), 2) for value in bbox[:4]]})
        return {"detections": detections, "count_by_label": counts}

    @staticmethod
    def _tolist(value: Any) -> list[Any]:
        if value is None:
            return []
        for method in ("detach", "cpu"):
            candidate = getattr(value, method, None)
            if callable(candidate):
                value = candidate()
        converter = getattr(value, "tolist", None)
        if callable(converter):
            value = converter()
        if isinstance(value, list):
            return value
        try:
            return list(value)
        except TypeError:
            return [value]

    def _source_for_request(self, request: InferenceRequest) -> Any:
        source = request.context.get("image")
        if source is not None:
            return source
        image_path = request.context.get("image_path")
        if image_path is None:
            if self.media_root is None:
                raise ValueError("InferenceRequest must provide context.image_path or configure media_root")
            matches = list(self.media_root.rglob(f"{request.asset_id}.*"))
            if not matches:
                raise FileNotFoundError(f"media asset not found: {request.asset_id}")
            return matches[0]
        if self.media_root is None:
            raise ValueError("media_root is required when image_path is provided")
        resolved = Path(str(image_path)).expanduser().resolve()
        if self.media_root != resolved and self.media_root not in resolved.parents:
            raise ValueError("inference image_path is outside media_root")
        if not resolved.is_file():
            raise FileNotFoundError(f"media asset not found: {request.asset_id}")
        return resolved


def annotate_detections(image: Any, result: Mapping[str, Any]) -> Any:
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("opencv-python is required for annotated inspection frames") from error
    annotated = image.copy()
    for detection in result.get("detections", []):
        bbox = detection.get("bbox_xyxy")
        if not bbox or len(bbox) < 4:
            continue
        left, top, right, bottom = [int(value) for value in bbox[:4]]
        label = f"{detection.get('label', 'unknown')} {float(detection.get('confidence', 0.0)):.2f}"
        cv2.rectangle(annotated, (left, top), (right, bottom), (0, 0, 255), 2)
        cv2.putText(annotated, label, (left, max(20, top - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return annotated
