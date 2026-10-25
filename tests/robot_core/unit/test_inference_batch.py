"""推理设备自检与批量推理：配置错要当场报错，批处理要能真正合并成一次调用。"""
import sys
from types import SimpleNamespace

import pytest

from app.robot_core.contracts import InferenceRequest
from app.robot_core.inference import InferenceRuntime, TestModelAdapter
from app.robot_core.inference.heat_stress import HeatStressAdapter

pytestmark = pytest.mark.no_database


class BatchAdapter(TestModelAdapter):
    """记录每次批量调用里的帧数，用来确认「一批只调一次模型」。"""

    def __init__(self):
        self.batches = []
        self.single = 0

    def predict(self, request):
        self.single += 1
        return super().predict(request)

    def predict_batch(self, requests):
        self.batches.append(len(requests))
        return [super(BatchAdapter, self).predict(request) for request in requests]


def _adapter_without_cuda(tmp_path, monkeypatch, device):
    path = tmp_path / 'best.pt'
    path.write_bytes(b'fixture')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda: False, device_count=lambda: 0)))
    return HeatStressAdapter(path, device=device)


def test_gpu_device_fails_fast_without_cuda(tmp_path, monkeypatch):
    adapter = _adapter_without_cuda(tmp_path, monkeypatch, '0')
    with pytest.raises(RuntimeError, match='CUDA'):
        adapter.load()


def test_cuda_index_beyond_visible_devices_fails(tmp_path, monkeypatch):
    path = tmp_path / 'best.pt'
    path.write_bytes(b'fixture')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda: True, device_count=lambda: 1)))
    with pytest.raises(RuntimeError, match='GPU 数量'):
        HeatStressAdapter(path, device='cuda:3').load()


def test_cpu_device_needs_no_torch(tmp_path, monkeypatch):
    """device=cpu 不该因为环境里没有 torch/CUDA 就被拒绝。"""
    path = tmp_path / 'best.pt'
    path.write_bytes(b'fixture')
    monkeypatch.delitem(sys.modules, 'torch', raising=False)
    adapter = HeatStressAdapter(path, device='cpu')
    adapter._require_device()          # 不抛异常即可


def test_predict_batch_merges_one_adapter_call():
    adapter = BatchAdapter()
    runtime = InferenceRuntime({adapter.capability: adapter})
    runtime.start()
    requests = [InferenceRequest(adapter.capability, f'asset-{index}', 'run-1') for index in range(3)]
    responses = runtime.predict_batch(requests)
    assert adapter.batches == [3]
    assert [response.request_id for response in responses] == [request.request_id for request in requests]
    assert all(response.status == 'ok' for response in responses)


def test_predict_batch_falls_back_without_batch_support():
    adapter = TestModelAdapter()
    runtime = InferenceRuntime({adapter.capability: adapter})
    runtime.start()
    requests = [InferenceRequest(adapter.capability, f'asset-{index}', 'run-1') for index in range(2)]
    responses = runtime.predict_batch(requests)
    assert len(responses) == 2
    assert all(response.status == 'ok' for response in responses)


def test_predict_batch_keeps_order_with_a_broken_frame():
    class PickyAdapter(BatchAdapter):
        def predict_batch(self, requests):
            self.batches.append(len(requests))
            return [super(BatchAdapter, self).predict(request) for request in requests]

    adapter = PickyAdapter()
    runtime = InferenceRuntime({adapter.capability: adapter})
    runtime.start()
    adapter.batches.clear()
    first = InferenceRequest(adapter.capability, 'asset-1', 'run-1')
    second = InferenceRequest(adapter.capability, 'asset-2', 'run-1', model_id='other-model')
    responses = runtime.predict_batch([first, second])
    # 模型不匹配的那帧不进批量，另一帧照常推理
    assert adapter.batches == [1] or adapter.batches == []
    assert responses[0].status == 'ok'
    assert responses[1].status == 'capability_unavailable'
