from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from app.robot_core.common.config import PlatformConfig

from .heat_stress import HeatStressAdapter
from .package import ModelPackage
from .runtime import ModelAdapter, TestModelAdapter


AdapterBuilder = Callable[[ModelPackage, Mapping[str, Any], Path], ModelAdapter]


class ModelAdapterRegistry:
    def __init__(self) -> None:
        self._builders: dict[str, AdapterBuilder] = {}

    def register(self, model_id: str, builder: AdapterBuilder) -> None:
        if not model_id or model_id in self._builders:
            raise ValueError(f"invalid or duplicate model adapter: {model_id}")
        self._builders[model_id] = builder

    def create(self, model_id: str, package: ModelPackage, settings: Mapping[str, Any], media_root: Path) -> ModelAdapter:
        try:
            return self._builders[model_id](package, settings, media_root)
        except KeyError as error:
            raise ValueError(f"no ModelAdapter registered for: {model_id}") from error


def _heat_stress_builder(package: ModelPackage, settings: Mapping[str, Any], media_root: Path) -> ModelAdapter:
    return HeatStressAdapter(
        package, media_root=media_root, device=str(settings.get("device", "cpu")),
        imgsz=int(settings.get("imgsz", 640)), conf=float(settings.get("conf", 0.25)),
        iou=float(settings.get("iou", 0.7)),
        cpu_threads=settings.get('cpu_threads', 4),
    )


def default_registry() -> ModelAdapterRegistry:
    registry = ModelAdapterRegistry()
    registry.register("heat_stress", _heat_stress_builder)
    return registry


def build_configured_adapters(config: PlatformConfig) -> dict[str, ModelAdapter]:
    models = config.values.get("models", {})
    settings = models.get("heat_stress", {})
    adapters: dict[str, ModelAdapter] = {}
    if isinstance(settings, Mapping) and settings.get("enabled", False):
        package_value = settings.get("package")
        if package_value:
            package = ModelPackage.load(config.source.parent / str(package_value), expected_model_id="heat_stress")
        else:
            artifact = config.source.parent / str(settings.get("path", ""))
            package = ModelPackage.from_artifact(
                artifact, model_id="heat_stress", version=settings.get("version"),
                adapter_version=str(settings.get("adapter_version", "1")),
                result_schema=str(settings.get("result_schema", "object-detection.v1")),
            )
        media_root = config.path("storage", "media_root")
        adapters["heat_stress_detection"] = default_registry().create("heat_stress", package, settings, media_root)
    elif bool(models.get("allow_test_adapter", False)):
        adapter = TestModelAdapter()
        adapters[adapter.capability] = adapter
    else:
        raise RuntimeError("no verified production model package configured")
    return adapters
