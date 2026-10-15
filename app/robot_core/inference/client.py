from __future__ import annotations

import asyncio
from typing import Any, Mapping

from app.robot_core.contracts import InferenceRequest, InferenceResponse
from app.ipc import JsonSocketClient


class InferenceServiceClient:
    """Client for the single inference-worker entry point."""

    def __init__(self, socket_path: str, *, timeout_seconds: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def predict_async(self, request: InferenceRequest) -> InferenceResponse:
        result = await JsonSocketClient(self.socket_path, self.timeout_seconds).call({"operation": "predict", **request.as_dict()})
        return InferenceResponse.from_dict(result)

    def predict(self, request: InferenceRequest) -> InferenceResponse:
        return asyncio.run(self.predict_async(request))

    async def health_async(self) -> Mapping[str, Any]:
        return await JsonSocketClient(self.socket_path, self.timeout_seconds).call({"operation": "health"})

    def health(self) -> Mapping[str, Any]:
        return asyncio.run(self.health_async())
