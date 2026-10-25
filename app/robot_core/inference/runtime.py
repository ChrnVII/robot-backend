from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import replace
from threading import RLock
from typing import Dict, Protocol, Sequence

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
        with self._lock:
            cached = self._cached(request)
            if cached is not None:
                return cached
            response = self._predict_once(request)
            self._remember(request, response)
            return response

    def predict_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResponse]:
        """批量推理：同 capability 的请求合并成一次模型调用，返回与入参同序。

        adapter 未实现 ``predict_batch`` 时自动退化为逐帧调用，因此只实现
        ``predict`` 的适配器（含测试替身）同样可用。
        """
        requests = list(requests)
        if not requests:
            return []
        if len(requests) == 1:
            return [self.predict(requests[0])]
        responses: list[InferenceResponse | None] = [None] * len(requests)
        with self._lock:
            pending: Dict[str, list[tuple[int, InferenceRequest]]] = {}
            for index, request in enumerate(requests):
                cached = self._cached(request)
                if cached is not None:
                    responses[index] = cached
                    continue
                pending.setdefault(request.capability, []).append((index, request))
            for capability, items in pending.items():
                produced = self._predict_group(capability, [request for _, request in items])
                for (index, request), response in zip(items, produced):
                    responses[index] = response
                    self._remember(request, response)
        return [item for item in responses if item is not None]

    def _predict_group(self, capability: str, group: list[InferenceRequest]) -> list[InferenceResponse]:
        """对同一 capability 的一批请求做一次模型调用（能批则批，不能批则逐帧）。"""
        adapter = self.adapters.get(capability)
        if adapter is None:
            return [self._unavailable(request, None) for request in group]
        outcomes: list[InferenceResponse | None] = [None] * len(group)
        ready: list[int] = []
        for index, request in enumerate(group):
            rejection = self._rejection(request, adapter)
            if rejection is not None:
                outcomes[index] = rejection
            else:
                ready.append(index)
        batch_predict = getattr(adapter, "predict_batch", None)
        if len(ready) > 1 and callable(batch_predict):
            try:
                produced = list(batch_predict([group[index] for index in ready]))
            except Exception:  # noqa: BLE001 - 批量失败退化为逐帧，避免整批一起坏
                produced = []
            if len(produced) == len(ready):
                for index, response in zip(ready, produced):
                    outcomes[index] = self._normalize(adapter, group[index], response)
                return [item for item in outcomes if item is not None]
        for index in ready:
            outcomes[index] = self._execute(adapter, group[index])
        return [item for item in outcomes if item is not None]

    # ---------- 缓存 ---------- #

    @staticmethod
    def _signature(request: InferenceRequest) -> tuple[str, ...]:
        return (request.capability, request.asset_id, request.task_run_id,
                request.model_id or "", request.model_version or "")

    def _cached(self, request: InferenceRequest) -> InferenceResponse | None:
        cached = self._responses.get(request.idempotency_key)
        if not cached:
            return None
        if cached[0] != self._signature(request):
            return InferenceResponse(request.request_id, request.capability, "invalid_input", "none",
                                     error_stage="idempotency",
                                     suggested_action="use a new idempotency key for different input",
                                     trace_id=request.trace_id)
        self._responses.move_to_end(request.idempotency_key)
        return cached[1]

    def _remember(self, request: InferenceRequest, response: InferenceResponse) -> None:
        self._responses[request.idempotency_key] = (self._signature(request), response)
        self._responses.move_to_end(request.idempotency_key)
        while len(self._responses) > self.idempotency_cache_size:
            self._responses.popitem(last=False)

    # ---------- 校验与执行 ---------- #

    def _predict_once(self, request: InferenceRequest) -> InferenceResponse:
        if request.deadline_at and parse_utc(request.deadline_at) <= parse_utc(utc_now()):
            return self._timeout(request)
        adapter = self.adapters.get(request.capability)
        if adapter is None:
            return self._unavailable(request, None)
        rejection = self._rejection(request, adapter)
        if rejection is not None:
            return rejection
        return self._execute(adapter, request)

    def _rejection(self, request: InferenceRequest, adapter: ModelAdapter) -> InferenceResponse | None:
        """返回「不该进入模型」的响应；None 表示可以正常推理。"""
        if request.deadline_at and parse_utc(request.deadline_at) <= parse_utc(utc_now()):
            return self._timeout(request)
        if request.model_id and request.model_id != adapter.model_id:
            return self._unavailable(request, adapter, "requested model is not active")
        if request.model_version and request.model_version != adapter.model_version:
            return self._unavailable(request, adapter, "requested model version is not active")
        return None

    def _execute(self, adapter: ModelAdapter, request: InferenceRequest) -> InferenceResponse:
        started = time.perf_counter()
        started_at = utc_now()
        try:
            response = adapter.predict(request)
        except MemoryError:
            return InferenceResponse(request.request_id, request.capability, "resource_exhausted",
                                     "unknown", error_stage="predict",
                                     suggested_action="reduce workload or restart inference-worker",
                                     trace_id=request.trace_id)
        except Exception:  # noqa: BLE001 - 适配器异常统一降级为 internal_error
            return InferenceResponse(request.request_id, request.capability, "internal_error",
                                     "unknown", error_stage="predict",
                                     suggested_action="inspect inference-worker logs",
                                     trace_id=request.trace_id)
        return self._normalize(adapter, request, response, started=started, started_at=started_at)

    @staticmethod
    def _normalize(adapter: ModelAdapter, request: InferenceRequest, response: InferenceResponse,
                   *, started: float | None = None, started_at: str | None = None) -> InferenceResponse:
        elapsed = round((time.perf_counter() - started) * 1000, 2) if started is not None else None
        return replace(
            response,
            trace_id=response.trace_id or request.trace_id,
            model_id=response.model_id or adapter.model_id,
            result_schema=response.result_schema or adapter.result_schema,
            started_at=response.started_at or started_at or utc_now(),
            latency_ms=response.latency_ms or elapsed or 0.0,
        )

    @staticmethod
    def _timeout(request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(request.request_id, request.capability, "timeout", "none",
                                 error_stage="queue",
                                 suggested_action="submit a request with a future deadline",
                                 trace_id=request.trace_id)

    @staticmethod
    def _unavailable(request: InferenceRequest, adapter: ModelAdapter | None,
                     message: str = "install an approved model package") -> InferenceResponse:
        return InferenceResponse(request.request_id, request.capability, "capability_unavailable",
                                 adapter.model_version if adapter is not None else "none",
                                 error_stage="resolve", suggested_action=message,
                                 trace_id=request.trace_id,
                                 model_id=adapter.model_id if adapter is not None else None)

    def stop(self) -> None:
        for adapter in self.adapters.values():
            adapter.unload()
