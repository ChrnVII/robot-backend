from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import replace
from threading import RLock
from typing import Dict, Protocol

from app.robot_core.common import parse_utc, utc_now
from app.robot_core.contracts import InferenceRequest, InferenceResponse


class ModelAdapter(Protocol):
    capability: str
    model_id: str
    model_version: str
    result_schema: str

    def load(self) -> None: ...
    def warmup(self) -> None: ...
    def predict(self, request: InferenceRequest) -> InferenceResponse: ...
    def health(self) -> str: ...
    def unload(self) -> None: ...


class TestModelAdapter:
    """Deterministic M1 test adapter. It is never reported as a production model."""

    capability = "platform_smoke_test"
    model_id = "platform_smoke_test"
    model_version = "test-0"
    result_schema = "platform-smoke-test.v1"

    def load(self) -> None:
        self.loaded = True

    def warmup(self) -> None:
        if not getattr(self, "loaded", False):
            raise RuntimeError("adapter not loaded")

    def predict(self, request: InferenceRequest) -> InferenceResponse:
        if not getattr(self, "loaded", False):
            return InferenceResponse(request.request_id, request.capability, "model_unhealthy", self.model_version, error_stage="load", suggested_action="load model package", trace_id=request.trace_id, model_id=self.model_id)
        return InferenceResponse(request.request_id, request.capability, "ok", self.model_version, result={"testAdapter": True}, evidence_asset_ids=(request.asset_id,), trace_id=request.trace_id, model_id=self.model_id, result_schema=self.result_schema)

    def health(self) -> str:
        return "ok" if getattr(self, "loaded", False) else "unavailable"

    def unload(self) -> None:
        self.loaded = False


class InferenceRuntime:
    def __init__(self, adapters: Dict[str, ModelAdapter], idempotency_cache_size: int = 1024):
        self.adapters = adapters
        self.idempotency_cache_size = max(1, int(idempotency_cache_size))
        self._lock = RLock()
        self._responses: OrderedDict[str, tuple[tuple[str, ...], InferenceResponse]] = OrderedDict()

    def start(self) -> None:
        for adapter in self.adapters.values():
            adapter.load()
            adapter.warmup()

    def predict(self, request: InferenceRequest) -> InferenceResponse:
        signature = (request.capability, request.asset_id, request.task_run_id, request.model_id or "", request.model_version or "")
        with self._lock:
            cached = self._responses.get(request.idempotency_key)
            if cached:
                if cached[0] != signature:
                    return InferenceResponse(request.request_id, request.capability, "invalid_input", "none", error_stage="idempotency", suggested_action="use a new idempotency key for different input", trace_id=request.trace_id)
                self._responses.move_to_end(request.idempotency_key)
                return cached[1]
            response = self._predict_once(request)
            self._responses[request.idempotency_key] = (signature, response)
            self._responses.move_to_end(request.idempotency_key)
            while len(self._responses) > self.idempotency_cache_size:
                self._responses.popitem(last=False)
            return response

    def _predict_once(self, request: InferenceRequest) -> InferenceResponse:
        if request.deadline_at and parse_utc(request.deadline_at) <= parse_utc(utc_now()):
            return InferenceResponse(request.request_id, request.capability, "timeout", "none", error_stage="queue", suggested_action="submit a request with a future deadline", trace_id=request.trace_id)
        adapter = self.adapters.get(request.capability)
        if adapter is None:
            return InferenceResponse(request.request_id, request.capability, "capability_unavailable", "none", error_stage="resolve", suggested_action="install an approved model package", trace_id=request.trace_id)
        if request.model_id and request.model_id != adapter.model_id:
            return InferenceResponse(request.request_id, request.capability, "capability_unavailable", adapter.model_version, error_stage="resolve", suggested_action="requested model is not active", trace_id=request.trace_id, model_id=adapter.model_id)
        if request.model_version and request.model_version != adapter.model_version:
            return InferenceResponse(request.request_id, request.capability, "capability_unavailable", adapter.model_version, error_stage="resolve", suggested_action="requested model version is not active", trace_id=request.trace_id, model_id=adapter.model_id)
        started = time.perf_counter()
        started_at = utc_now()
        try:
            response = adapter.predict(request)
            return replace(response, trace_id=response.trace_id or request.trace_id, model_id=response.model_id or adapter.model_id, result_schema=response.result_schema or adapter.result_schema, started_at=response.started_at or started_at, latency_ms=response.latency_ms or round((time.perf_counter() - started) * 1000, 2))
        except MemoryError:
            return InferenceResponse(request.request_id, request.capability, "resource_exhausted", "unknown", error_stage="predict", suggested_action="reduce workload or restart inference-worker", trace_id=request.trace_id)
        except Exception:
            return InferenceResponse(request.request_id, request.capability, "internal_error", "unknown", error_stage="predict", suggested_action="inspect inference-worker logs", trace_id=request.trace_id)

    def stop(self) -> None:
        for adapter in self.adapters.values():
            adapter.unload()
